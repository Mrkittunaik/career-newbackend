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

from app.core.db import get_user_db
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
    resume_text = profile.get("resume_text") or "(no readable resume text — rely on the about paragraph only)"
    return f"""You are deciding whether a candidate should apply to a job listing.
Use ONLY the candidate information below — do not invent facts. Weigh the resume text more
heavily than the about paragraph for concrete things like role titles, years of experience,
and specific skills/technologies — it's the more precise source.

CANDIDATE ABOUT:
{profile.get("about_paragraph") or "(not provided)"}

CANDIDATE RESUME TEXT:
{resume_text}

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
    db = await get_user_db(user_id)
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
    db = await get_user_db(user_id)
    return await db.job_decisions.find_one({"user_id": user_id, "url": job_url})


VALID_SITES = ["LinkedIn", "Indeed", "Glassdoor", "Naukri", "Monster", "ZipRecruiter", "Wellfound", "Dice", "SimplyHired"]


def _build_chat_prompt(message: str, history: list[dict]) -> str:
    history_lines = "\n".join(f'{h["role"]}: {h["content"]}' for h in history[-6:]) or "(no prior messages)"
    return f"""You are the assistant inside CareerOS, a job-search automation product.
The user is talking to you in a chat box. Your job is to read their message and decide:
(a) are they asking you to start a job search / apply to jobs, or
(b) are they just asking a question or chatting, with no job search to start.

RECENT CONVERSATION:
{history_lines}

LATEST USER MESSAGE:
{message}

VALID TARGET SITES (only use these, spelled exactly): {", ".join(VALID_SITES)}

If (a) — they want a job search started — extract:
- job_type: the role/title they want (e.g. "Backend Engineer"). If not stated, use "".
- experience_level: one of "fresher", "experienced", or "any". Infer from context if
  not stated explicitly (e.g. "I'm a student" -> fresher). Default to "any" if unclear.
- target_sites: array of site names from the VALID TARGET SITES list above that match
  what they asked for. If they didn't name any, default to ["LinkedIn", "Indeed"].
- reply: a short, friendly one-sentence confirmation of what you're about to do.

If (b) — no job search intent — just extract:
- reply: a short, helpful, friendly response as plain conversation. Do not invent
  facts about the user's applications or account; if they're asking about status
  info you don't have here, say you'll need to check the dashboard for that.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"intent": "job_search" or "chat", "job_type": "...", "experience_level": "...", "target_sites": [...], "reply": "..."}}
For intent "chat", you may omit job_type/experience_level/target_sites entirely."""


async def handle_chat_message(user_id: str, message: str, history: list[dict]) -> dict:
    """
    Parses a free-text chat message into either:
      - a job search request (intent="job_search"), which the caller (chat.py)
        turns into a real job_requests document — the same collection
        submit_job_request() in jobs.py already writes to, so the existing
        job-request pipeline (and eventually the bot/worker consuming it)
        needs no changes to understand a chat-originated request.
      - plain conversation (intent="chat"), just a reply with no side effect.

    On any AI failure, falls back to intent="chat" with an apologetic reply —
    a parsing failure should never silently queue a job search the user
    didn't clearly ask for.
    """
    provider, api_key = await ai_brain._resolve_provider_and_key(user_id)
    if not api_key:
        return {
            "intent": "chat",
            "reply": "I don't have an AI provider configured yet — add one in Settings and I'll be able to help.",
        }

    prompt = _build_chat_prompt(message, history)
    try:
        raw = await _call_once(provider, api_key, prompt)
        parsed = json.loads(raw)
        intent = parsed.get("intent") if parsed.get("intent") in ("job_search", "chat") else "chat"
        result = {"intent": intent, "reply": parsed.get("reply") or "Got it."}
        if intent == "job_search":
            sites = [s for s in parsed.get("target_sites", []) if s in VALID_SITES]
            result["job_type"] = str(parsed.get("job_type") or "").strip()
            result["experience_level"] = (
                parsed.get("experience_level") if parsed.get("experience_level") in ("fresher", "experienced", "any") else "any"
            )
            result["target_sites"] = sites or ["LinkedIn", "Indeed"]
            # A job search with no discernible role isn't actionable — fall back
            # to chat so the user gets asked to clarify instead of a silently
            # broken/empty job_requests entry being queued.
            if not result["job_type"]:
                return {
                    "intent": "chat",
                    "reply": "What kind of role are you looking for? (e.g. \"Backend Engineer\", \"Product Manager\")",
                }
        return result
    except Exception:
        return {"intent": "chat", "reply": "Sorry, I had trouble understanding that — could you rephrase?"}
