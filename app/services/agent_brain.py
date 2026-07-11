"""
agent_brain.py — the control-loop "agent" brain, sitting above ai_brain.py.

ai_brain.py stays exactly as-is: given a batch of form fields, it answers
them. It has no memory, makes no decisions, and doesn't know what a "job"
is — it's the hands, not the head.

This module is the head. It owns the per-job lifecycle:

    scan  -> decide (apply or skip this job?) -> fill (delegates to
    ai_brain) -> wait for submit result -> record outcome -> move on

One job runs at a time per user (matches the current single /ws/bot
connection model — see AgentSession.busy). Running several jobs in
parallel per user is a later, separate change (needs multiple bot/browser
sessions, not just this module) and is intentionally out of scope here.

Wiring into ws.py: ws.py's handle_scan_fields/handle_report_result stay in
place for the plain "just fill this form" path (backwards compatible with
older bot builds). This module adds a second, opt-in path: a "job_decision"
message type where the bot first tells the agent about a job (title,
company, description, url) *before* scanning any fields, and the agent
replies apply/skip. Only on "apply" does the bot proceed to scan_fields as
before. This keeps the existing message contract untouched and additive.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

import httpx

from app.core.db import get_db
from app.services import ai_brain

DECISION_TIMEOUT_SECONDS = 15


class JobDecision(str, Enum):
    APPLY = "apply"
    SKIP = "skip"


@dataclass
class AgentSession:
    """
    One per live /ws/bot connection. Tracks whether the agent is currently
    in the middle of a job (scan -> fill -> submit) so a second job_decision
    or scan_fields can't be processed concurrently and stomp on state —
    matches the "one job at a time" model.
    """

    user_id: str
    profile_cache: dict | None = None
    busy: bool = False
    current_job: dict | None = None
    decisions_made: int = 0
    jobs_applied: int = 0
    jobs_skipped: int = 0


def _build_decision_prompt(job: dict, profile: dict) -> str:
    doc_lines = "\n".join(f'- {d["title"]} ({d["type"]})' for d in profile.get("documents", [])) or "(none)"
    return f"""You are deciding whether a candidate should apply to a job listing.
Use ONLY the candidate information below — do not invent facts.

CANDIDATE ABOUT:
{profile.get("about_paragraph") or "(not provided)"}

CANDIDATE DOCUMENTS ON FILE:
{doc_lines}

JOB LISTING:
Title: {job.get("title") or "(unknown)"}
Company: {job.get("company") or "(unknown)"}
Description: {job.get("description") or "(no description provided)"}
URL: {job.get("url") or "(none)"}

Decide APPLY or SKIP based on whether this listing is a reasonable match for
the candidate's stated experience, skills, and goals. Prefer APPLY unless
there's a clear mismatch (e.g. wrong field entirely, seniority far beyond
the candidate's experience, or the listing is obviously spam/fake).

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"decision": "apply" or "skip", "reason": "one short sentence why"}}"""


async def decide_job(user_id: str, job: dict, profile_cache: dict | None = None) -> tuple[JobDecision, str]:
    """
    Single non-streaming decision call: should the candidate apply to this
    job? Returns (decision, reason). On any failure (timeout, bad JSON,
    provider error, no key configured) this defaults to APPLY rather than
    silently blocking the user's job search — a missed skip just costs one
    extra form-fill attempt, whereas a wrongly-blocked apply costs the user
    a job they might have wanted, which is worse and invisible to them.
    """
    if profile_cache is None:
        profile_cache = await ai_brain._load_profile_context(user_id)

    provider, api_key = await ai_brain._resolve_provider_and_key(user_id)
    if not api_key:
        return JobDecision.APPLY, "No AI key configured — defaulting to apply."

    prompt = _build_decision_prompt(job, profile_cache)

    try:
        raw = await _call_once(provider, api_key, prompt)
        parsed = json.loads(raw)
        decision_str = str(parsed.get("decision", "apply")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()
        decision = JobDecision.SKIP if decision_str == "skip" else JobDecision.APPLY
        return decision, reason or "(no reason given)"
    except Exception:
        return JobDecision.APPLY, "Decision call failed — defaulting to apply."


async def _call_once(provider: str, api_key: str, prompt: str) -> str:
    """
    Non-streaming single-shot completion, reusing the same provider
    endpoints ai_brain.py already talks to — kept separate from
    ai_brain._stream_provider because decisions don't need incremental
    parsing (the response is one small JSON object, not a growing list of
    field answers), so a plain request/response call is simpler and cheaper
    than wiring this through the streaming parser.
    """
    async with httpx.AsyncClient(timeout=DECISION_TIMEOUT_SECONDS) as client:
        if provider == "anthropic":
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

        elif provider in ("openai", "groq"):
            base_url = "https://api.openai.com/v1" if provider == "openai" else "https://api.groq.com/openai/v1"
            model = "gpt-4o-mini" if provider == "openai" else "llama-3.1-8b-instant"
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")

        return "{}"


async def record_decision(user_id: str, job: dict, decision: JobDecision, reason: str) -> None:
    """
    Persists every decision (apply and skip) to a new job_decisions
    collection so the agent has memory across sessions — the same job
    won't be re-evaluated from scratch next time, and the user/dashboard
    can later show "why did it skip this one" instead of the decision
    vanishing the moment it's made.
    """
    db = get_db()
    await db.job_decisions.insert_one(
        {
            "user_id": user_id,
            "job_title": job.get("title"),
            "company": job.get("company"),
            "url": job.get("url"),
            "decision": decision.value,
            "reason": reason,
            "decided_at": datetime.now(timezone.utc),
        }
    )


async def already_decided(user_id: str, job: dict) -> dict | None:
    """
    Checks memory before spending an AI call: if this exact job URL was
    already decided for this user, reuse that decision instead of asking
    the model again — cheaper, and consistent (the agent won't flip-flop
    on the same listing between sessions).
    """
    job_url = job.get("url")
    if not job_url:
        return None
    db = get_db()
    return await db.job_decisions.find_one({"user_id": user_id, "url": job_url})
