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



def _build_problem_prompt(problem: dict, profile: dict) -> str:
    return f"""You are helping recover from a problem the automation bot just hit while
working a job application. Decide the best next step yourself — only ask the
human user if you genuinely cannot tell what to do safely.

PROBLEM REPORTED BY THE BOT:
Type: {problem.get("problem_type") or "(unspecified)"}
Details: {problem.get("details") or "(none given)"}
URL involved: {problem.get("url") or "(none)"}
Context (what it was doing): {problem.get("context") or "(none given)"}

CANDIDATE CONTEXT (for reference only, not usually relevant to this decision):
{profile.get("about_paragraph") or "(not provided)"}

Common problem types and the safe default action:
- broken_link / page_not_found / 404: action=skip_job (this one listing can't
  be reached — move on rather than stall the whole session over it).
- login_required / paywall: action=skip_job unless the details suggest the
  user is already supposed to be logged in, in which case action=ask_user.
- unrecognized_page_layout / cannot_find_fields: action=skip_job (bot can't
  safely fill a form it can't parse — don't guess at form fields).
- rate_limited / blocked_by_site: action=retry_later.
- anything ambiguous, risky (e.g. might submit wrong/incomplete data), or where
  guessing could cost the user a real opportunity they'd care about: action=ask_user.

Prefer resolving it yourself (skip_job or retry_later) over asking the user —
only use ask_user when the situation is genuinely unclear or consequential
enough that guessing wrong would be worse than a short delay for their answer.
Never invent a reason or pretend certainty you don't have.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"action": "skip_job" or "retry_later" or "ask_user", "reason": "one short sentence why", "question_for_user": "only filled in if action is ask_user, otherwise empty string"}}"""


async def resolve_problem(user_id: str, problem: dict, profile_cache: dict | None = None) -> dict:
    """
    Called when the bot hits something it can't push through on its own
    (broken link, page it doesn't recognize, rate limiting, etc.) — see
    ws.py's handle_report_problem. The AI decides the recovery step itself
    by default (skip this job / retry later); it only hands the decision
    back to the user (action="ask_user") when the situation is genuinely
    ambiguous or risky enough that guessing wrong would cost the user more
    than a short pause for their input would.

    On any failure (no key, bad JSON, provider error) this defaults to
    skip_job rather than stalling the whole automation session waiting on
    something that may never resolve — same fail-open philosophy as
    decide_job.
    """
    if profile_cache is None:
        profile_cache = await ai_brain._load_profile_context(user_id)

    provider, api_key = await ai_brain._resolve_provider_and_key(user_id)
    if not api_key:
        return {"action": "skip_job", "reason": "No AI key configured — skipping this one.", "question_for_user": ""}

    prompt = _build_problem_prompt(problem, profile_cache)

    try:
        raw = await _call_once(provider, api_key, prompt)
        parsed = json.loads(raw)
        action = str(parsed.get("action", "skip_job")).strip().lower()
        if action not in ("skip_job", "retry_later", "ask_user"):
            action = "skip_job"
        return {
            "action": action,
            "reason": str(parsed.get("reason", "")).strip() or "(no reason given)",
            "question_for_user": str(parsed.get("question_for_user", "")).strip() if action == "ask_user" else "",
        }
    except Exception:
        return {"action": "skip_job", "reason": "Recovery decision failed — skipping this one.", "question_for_user": ""}


VALID_SITES = ["LinkedIn", "Indeed", "Glassdoor", "Naukri", "Monster", "ZipRecruiter", "Wellfound", "Dice", "SimplyHired"]

# The four things the intake flow needs before it's allowed to ask "should I
# start?". company_pref is intentionally not required — after a couple turns
# with it still unknown, the AI is told to default it to "any" rather than
# stall the whole flow over a preference the user may not have an opinion on.
REQUIRED_INTAKE_FIELDS = ("role", "location", "experience_type", "target_sites")

EXPERIENCE_TYPE_TO_LEVEL = {
    "internship": "fresher",
    "fresher": "fresher",
    "experienced": "experienced",
}


def _merge_intake(current: dict, extracted: dict) -> dict:
    """New extracted values only overwrite current ones when they're
    actually non-empty — a turn where the user only answered "Bangalore"
    should never wipe out a role the user already gave two turns ago just
    because this turn's extraction returned an empty string for it."""
    merged = dict(current or {})
    for key in ("role", "location", "experience_type", "company_pref"):
        value = (extracted.get(key) or "").strip()
        if value:
            merged[key] = value
    sites = extracted.get("target_sites")
    if sites:
        valid = [s for s in sites if s in VALID_SITES]
        if valid:
            merged["target_sites"] = valid
    return merged


def _intake_is_ready(intake: dict) -> bool:
    if not intake.get("role") or not intake.get("location"):
        return False
    if not intake.get("experience_type"):
        return False
    if not intake.get("target_sites"):
        return False
    return True


def _build_intake_prompt(message: str, history: list[dict], intake: dict, awaiting_confirmation: bool, paused: bool) -> str:
    history_lines = "\n".join(f'{h["role"]}: {h["content"]}' for h in history[-8:]) or "(no prior messages)"

    known_lines = "\n".join(
        [
            f"- role/title: {intake.get('role') or '(not yet known)'}",
            f"- location: {intake.get('location') or '(not yet known)'}",
            f"- experience_type: {intake.get('experience_type') or '(not yet known)'} (must be one of: internship, fresher, experienced)",
            f"- company_pref: {intake.get('company_pref') or '(not yet known, optional)'} (one of: startup, top_company, any)",
            f"- target_sites: {', '.join(intake.get('target_sites') or []) or '(not yet known)'}",
        ]
    )

    if awaiting_confirmation:
        confirmation_instructions = """
The assistant already asked the user to confirm starting automation with the
info above. The LATEST USER MESSAGE below is their answer to that question.
- If it's an affirmative ("yes", "start", "go ahead", "do it", "sure", etc.) set
  confirmed_start=true and reply with a short "Starting now..." style message.
- If it's a decline or a request to change something ("no", "wait", "change the
  role to X", etc.): set confirmed_start=false, update any field they asked to
  change in "intake", and reply asking what they'd like to adjust (or just
  acknowledge the cancellation if they said no with no changes).
- If it's clearly unrelated to confirming (a tangent question, small talk) treat
  it the same way as the PAUSED/off-topic handling below: answer it, set
  pause_intake=true, confirmed_start=false, and don't touch intake.
Never set confirmed_start=true unless the user's LATEST message is clearly a yes."""
    elif paused:
        confirmation_instructions = """
Intake is currently PAUSED — the user previously asked to stop/step away from the
job-search questions (e.g. said "leave it", "later", "not now", or asked something
unrelated), and the assistant respected that by not asking further intake
questions since. The LATEST USER MESSAGE is what they're saying now.
- If it's a real answer to resuming the job search (a role, location, an
  affirmative like "ok let's continue", "back to it", or new job-search details),
  set resume_intake=true, extract anything usable into "intake", and continue the
  flow naturally: if that answer combined with what's already known completes
  everything, ask to confirm; otherwise ask for the next missing piece.
- If it's still unrelated (another tangent, more small talk, another question),
  answer that question directly and helpfully, keep resume_intake=false, and
  don't touch intake or ask any job-search question this turn — let the user
  bring it back up themselves, like a person would.
Never resume the job-search questions on your own initiative while paused —
only resume once the user's message is clearly about the job search again."""
    else:
        confirmation_instructions = """
Not awaiting confirmation right now — this is a normal intake turn.
FIRST, decide what kind of message this is:
1. A direct answer to what was just asked (or new job-search info volunteered
   early) -> extract it into "intake" (merge with what's already known, don't
   guess), then continue: if role, location, experience_type, and at least one
   target_site are ALL known, set ready=true and "reply" is a short summary
   ending in a clear yes/no question ("Should I start applying now?"). Otherwise
   set ready=false and "reply" asks ONE short, friendly question for the single
   most important missing piece, in this order: role -> location ->
   experience_type -> target_sites -> company_pref. Never re-ask about a field
   already known, and never ask about more than one missing field per turn.
2. An explicit request to stop/pause/come back later ("leave it", "we'll do this
   later", "not now", "hold on") -> set pause_intake=true, ready=false, and
   "reply" is a short, warm acknowledgement that doesn't ask anything further
   (e.g. "No problem — just say the word whenever you want to pick this back
   up."). Don't guess at or discard the intake already collected.
3. An unrelated tangent (a different question, small talk, a product question)
   that ISN'T a stop request -> treat this like a helpful human would when
   interrupted mid-conversation: actually answer their question well in "reply",
   set pause_intake=true so the next turn doesn't rigidly re-ask the pending
   intake question, and don't touch intake this turn. Never invent facts to
   answer it — if you genuinely don't know, say so plainly instead of guessing.
Never fabricate an answer just to seem helpful — if you don't know something
factual, say you're not sure rather than making it up."""

    return f"""You are the job-search intake assistant inside CareerOS. Your job is to
collect enough information before any automation starts, one question at a time,
while still holding a normal, human conversation — never dump multiple questions
in one message, never start automation without an explicit yes, never re-ask
something already known, and never invent information you don't actually have.

RECENT CONVERSATION:
{history_lines}

LATEST USER MESSAGE:
{message}

INFO COLLECTED SO FAR:
{known_lines}

VALID TARGET SITES (only use these, spelled exactly): {", ".join(VALID_SITES)}

{confirmation_instructions}

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"intake": {{"role": "...", "location": "...", "experience_type": "...", "company_pref": "...", "target_sites": [...]}}, "ready": true or false, "confirmed_start": true or false, "pause_intake": true or false, "resume_intake": true or false, "reply": "..."}}
Omit or leave blank any intake field you don't have new info for."""


async def handle_chat_message(user_id: str, message: str, history: list[dict], intake_state: dict | None = None) -> dict:
    """
    Multi-turn intake state machine, replacing the old one-shot "did they
    ask for a job search" classifier. That old version could queue an
    automation run off a single ambiguous sentence with no location,
    experience level, or site chosen — this version always collects role,
    location, experience_type (internship/fresher/experienced), and at
    least one target site first, asking one short question per turn, and
    then requires an explicit yes before intent becomes "job_search".

    intake_state is the conversation's stored progress so far (see chat.py,
    which persists this on the conversations document): {role, location,
    experience_type, company_pref, target_sites, status}. status is one of
    "collecting" | "awaiting_confirmation" | "confirmed".

    Returns a dict always containing "intake" (the updated state to persist)
    and "awaiting_confirmation" (bool), plus "intent" — only "job_search"
    when the user just explicitly confirmed on this exact turn; "chat"
    otherwise, even mid-intake (nothing gets queued to the bot until the
    user says yes).
    """
    provider, api_key = await ai_brain._resolve_provider_and_key(user_id)
    if not api_key:
        return {
            "intent": "chat",
            "reply": "I don't have an AI provider configured yet — add one in Settings and I'll be able to help.",
            "intake": intake_state or {},
            "awaiting_confirmation": False,
        }

    intake_state = intake_state or {}
    awaiting_confirmation = intake_state.get("status") == "awaiting_confirmation"
    paused = intake_state.get("status") == "paused"

    prompt = _build_intake_prompt(message, history, intake_state, awaiting_confirmation, paused)
    try:
        raw = await _call_once(provider, api_key, prompt)
        parsed = json.loads(raw)
    except Exception:
        return {
            "intent": "chat",
            "reply": "Sorry, I had trouble understanding that — could you rephrase?",
            "intake": intake_state,
            "awaiting_confirmation": awaiting_confirmation,
        }

    merged_intake = _merge_intake(intake_state, parsed.get("intake") or {})
    reply = parsed.get("reply") or "Got it."
    confirmed = bool(parsed.get("confirmed_start")) and awaiting_confirmation

    if confirmed:
        merged_intake["status"] = "confirmed"
        experience_level = EXPERIENCE_TYPE_TO_LEVEL.get(merged_intake.get("experience_type"), "any")
        return {
            "intent": "job_search",
            "reply": reply,
            "intake": merged_intake,
            "awaiting_confirmation": False,
            "job_type": merged_intake.get("role") or "",
            "experience_level": experience_level,
            "target_sites": merged_intake.get("target_sites") or ["LinkedIn", "Indeed"],
            "location": merged_intake.get("location"),
            "company_pref": merged_intake.get("company_pref") or "any",
        }

    # Paused: the user asked to stop, or went off on a tangent — the reply
    # already answers/acknowledges that, and we deliberately don't push
    # ready/awaiting_confirmation forward this turn so the next message
    # isn't rigidly forced back into the intake question. Only a message
    # the AI recognizes as "back to the job search" (resume_intake=true)
    # clears this and lets normal intake logic resume.
    if bool(parsed.get("pause_intake")) and not bool(parsed.get("resume_intake")):
        merged_intake["status"] = "paused"
        return {
            "intent": "chat",
            "reply": reply,
            "intake": merged_intake,
            "awaiting_confirmation": False,
        }

    ready = bool(parsed.get("ready")) or _intake_is_ready(merged_intake)
    merged_intake["status"] = "awaiting_confirmation" if ready else "collecting"

    return {
        "intent": "chat",
        "reply": reply,
        "intake": merged_intake,
        "awaiting_confirmation": ready,
    }
