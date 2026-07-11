"""
ai_brain.py — Part B "brain" module.

Builds the AI prompt from a user's profile + a batch of unanswered form
fields, dispatches to the resolved provider (shared platform key when
ai_provider="ours", else the user's own decrypted key), and streams
completed {index, value} answers out as soon as each one is parseable
from the partial response — without waiting for the full JSON to close.

Kept separate from ws.py so the WebSocket route stays focused on
connection lifecycle, not prompt/provider logic.

LIMITATION (real, not hypothetical): profile.py only stores a freeform
about_paragraph plus a list of uploaded documents (file path or link) —
nothing extracts text from an uploaded resume PDF. So the AI only ever
sees the about_paragraph and document titles/links, not actual resume
content. If a field needs specifics only present in a resume file (e.g.
"years at your last company"), the AI will do its best from
about_paragraph alone and may return an empty/low-confidence answer.
Document text extraction is a separate, later piece of work.
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import settings as app_settings
from app.core.db import get_core_db, get_user_db
from app.core.field_encryption import decrypt_secret

AI_TIMEOUT_SECONDS = 20

# Types the fill loop already treats specially — these should never be sent
# to the AI for a text answer (checkboxes/radios need true/false semantics
# select needs one of its real option values; automation.js's existing fill
# loop already skips 'select' entirely, so we don't answer those either).
SKIP_FIELD_TYPES = {"checkbox", "radio", "submit", "button", "file"}


def _filterable_fields(fields: list[dict]) -> list[dict]:
    """Drop fields that already have a value, or types the AI shouldn't
    answer (matches automation.js's own skip logic so we don't waste a
    token generating an answer that'll never be used)."""
    out = []
    for f in fields:
        if f.get("currentValue", "").strip():
            continue
        if f.get("type") in SKIP_FIELD_TYPES:
            continue
        out.append(f)
    return out


RESUME_TEXT_BUDGET_CHARS = 4000  # keeps the combined prompt bounded even with 2-3 uploaded docs


async def _load_profile_context(user_id: str) -> dict:
    """Loaded once per session by the caller (ws.py) and reused across
    re-scans within that same /ws/bot connection — this function itself
    is just the DB read; caching is the caller's job.

    Now includes each document's extracted_text (populated at upload time
    by resume_extract.py) so the AI actually reads resume content instead
    of only ever seeing the about_paragraph — this was the explicit
    LIMITATION called out in this module's old docstring; it's closed now."""
    db = await get_user_db(user_id)
    profile = await db.profiles.find_one({"user_id": user_id}) or {}
    documents = await db.documents.find({"user_id": user_id}).sort("created_at", -1).to_list(length=50)

    resume_text_parts = []
    budget_used = 0
    for d in documents:
        text = (d.get("extracted_text") or "").strip()
        if not text:
            continue
        remaining = RESUME_TEXT_BUDGET_CHARS - budget_used
        if remaining <= 0:
            break
        chunk = text[:remaining]
        resume_text_parts.append(f"--- {d.get('title') or 'document'} ---\n{chunk}")
        budget_used += len(chunk)

    return {
        "about_paragraph": profile.get("about_paragraph", ""),
        "documents": [{"title": d.get("title"), "type": d.get("type"), "url": d.get("url")} for d in documents],
        "resume_text": "\n\n".join(resume_text_parts),
    }


async def _resolve_provider_and_key(user_id: str) -> tuple[str, str]:
    # ai_provider/ai_api_key are account-level settings, always in the
    # hosted core DB regardless of the user's storage_mode for job/chat
    # content — see app/core/db.py's split rationale.
    db = get_core_db()
    doc = await db.settings.find_one({"user_id": user_id}) or {}
    provider = doc.get("ai_provider", app_settings.default_ai_provider)

    if provider == "ours" or not doc.get("ai_api_key"):
        # Shared platform key. Previously this hardcoded "anthropic" for the
        # "ours" case, which silently produced an empty key for every user
        # whenever only GROQ_API_KEY (or only OPENAI_API_KEY) was configured
        # on the server and ANTHROPIC_API_KEY was left blank — the resolver
        # would return provider="anthropic" with api_key="" and every AI call
        # would look like "no key configured" even though a usable platform
        # key existed. Instead, try each configured platform key in a fixed
        # preference order and use whichever one is actually set.
        key_map = {
            "anthropic": app_settings.anthropic_api_key,
            "groq": app_settings.groq_api_key,
            "openai": app_settings.openai_api_key,
        }
        # If the user explicitly picked a non-"ours" provider but just has no
        # personal key saved for it, respect that specific provider choice
        # first (e.g. they chose "groq" -> try the platform's groq key).
        if provider in key_map and key_map[provider]:
            return provider, key_map[provider]
        for candidate_provider, candidate_key in key_map.items():
            if candidate_key:
                return candidate_provider, candidate_key
        return "anthropic", ""

    decrypted = decrypt_secret(doc["ai_api_key"])
    if decrypted is None:
        # Corrupt/undecryptable key (e.g. FIELD_ENCRYPTION_KEY rotated) —
        # fall back to whichever platform key is configured rather than
        # hard-failing the whole scan.
        for candidate_provider, candidate_key in (
            ("anthropic", app_settings.anthropic_api_key),
            ("groq", app_settings.groq_api_key),
            ("openai", app_settings.openai_api_key),
        ):
            if candidate_key:
                return candidate_provider, candidate_key
        return "anthropic", ""
    return provider, decrypted


def _build_prompt(fields: list[dict], profile: dict) -> str:
    field_lines = []
    for f in fields:
        options = f.get("options")
        options_str = f" options={options}" if options else ""
        field_lines.append(
            f'- index={f["index"]}, type="{f.get("type") or f.get("tag")}", '
            f'question="{f.get("question") or "(no visible label)"}", '
            f"required={f.get('required', False)}{options_str}"
        )

    doc_lines = "\n".join(f'- {d["title"]} ({d["type"]}): {d["url"]}' for d in profile["documents"]) or "(none)"
    resume_text = profile.get("resume_text") or ""
    resume_section = resume_text if resume_text else "(no readable resume text — rely on the about paragraph only)"

    return f"""You are filling out a job application form on behalf of a candidate.
Use ONLY the candidate information below — do not invent facts, employers, or dates not present here.
When the resume text below and the about paragraph could both answer a field (e.g. exact job
titles, years of experience, company names, skills), prefer the resume text — it's the more
precise, factual source; the about paragraph is a looser summary.

CANDIDATE ABOUT:
{profile["about_paragraph"] or "(not provided)"}

CANDIDATE RESUME TEXT (extracted from uploaded document):
{resume_section}

CANDIDATE DOCUMENTS ON FILE:
{doc_lines}

FORM FIELDS TO ANSWER:
{chr(10).join(field_lines)}

OUTPUT RULES (strict - a wrong format breaks the auto-fill):
1. Return the RAW VALUE ONLY. Never wrap it in a sentence.
   - Field asking for a name -> "Kittu", NOT "My name is Kittu".
   - Field asking for a city -> "Hyderabad", NOT "I live in Hyderabad".
   - Field asking for years of experience -> "3", NOT "I have 3 years of experience".
2. If the question is yes/no in nature, answer with exactly "Yes" or "No" - nothing else.
3. If the field lists options, your answer MUST be copied EXACTLY (character for
   character) from one of those listed options. Never invent a value not in the list.
4. For email/phone/number fields, return only the plain value - no labels, no extra words.
5. Free-text questions (e.g. "Why do you want this role?") may be a short natural
   sentence or two, based only on the candidate info above - this is the one
   exception to rule 1.
6. If you don't have enough information to answer a field confidently, use an
   empty string "" rather than guessing - an empty answer is skipped safely,
   a wrong guess is not.

Respond with ONLY a JSON object mapping each field's index (as a string) to its answer value.
Return JSON only, no other text, no markdown code fences, no explanation.
Example shape: {{"0": "Kittu", "1": "kittu@example.com", "2": "Yes", "3": ""}}"""


async def stream_answers(user_id: str, fields: list[dict], profile: dict) -> AsyncIterator[tuple[int, str]]:
    """
    Yields (field_index, value) tuples as soon as each is parseable from the
    AI's streamed output — the caller (ws.py) pushes each one over the
    socket immediately rather than waiting for generation to finish.
    """
    filtered = _filterable_fields(fields)
    if not filtered:
        return

    provider, api_key = await _resolve_provider_and_key(user_id)
    if not api_key:
        # No usable key for this provider — nothing to do; caller will still
        # send answers_complete with zero chunks, which is a safe no-op.
        return

    prompt = _build_prompt(filtered, profile)
    already_yielded: set[int] = set()

    async for partial_text in _stream_provider(provider, api_key, prompt):
        # Attempt to parse whatever complete key/value pairs exist so far in
        # the accumulating text, even though the overall JSON object isn't
        # closed yet. This is a best-effort incremental parse, not a strict
        # streaming JSON parser — good enough for a flat {index: value} shape.
        for index, value in _extract_complete_pairs(partial_text, already_yielded):
            already_yielded.add(index)
            yield index, value

    # Catch anything left over once the stream fully closes (in case the
    # incremental parser missed the last pair before the closing brace).
    try:
        final_obj = json.loads(partial_text)
        for k, v in final_obj.items():
            idx = int(k)
            if idx not in already_yielded:
                yield idx, str(v)
    except (json.JSONDecodeError, NameError):
        pass


def _extract_complete_pairs(partial_text: str, already_yielded: set[int]) -> list[tuple[int, str]]:
    """
    Best-effort scan for complete "index": "value" pairs inside a still-
    growing JSON object string. Only returns pairs whose closing quote (or
    closing brace, for the last one) has definitely been seen — never
    guesses at a value that might still be mid-generation.
    """
    results = []
    depth = 0
    i = 0
    n = len(partial_text)
    while i < n:
        if partial_text[i] == "{":
            depth += 1
        elif partial_text[i] == "}":
            depth -= 1
        i += 1

    # Simple regex-free scan: find `"digits"` followed by `:` followed by a
    # quoted string that is fully closed (an even, non-escaped closing quote).
    import re

    for match in re.finditer(r'"(\d+)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]', partial_text):
        idx = int(match.group(1))
        if idx in already_yielded:
            continue
        value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
        results.append((idx, value))
    return results


async def _stream_provider(provider: str, api_key: str, prompt: str) -> AsyncIterator[str]:
    """
    Yields the accumulating text output as it streams in from the given
    provider. Each yield is the FULL text so far (not a delta) so the
    incremental parser above can just re-scan the whole thing each time —
    simpler and safe for a JSON object that's still growing.
    """
    if provider == "anthropic":
        async for chunk in _stream_anthropic(api_key, prompt):
            yield chunk
    elif provider == "openai":
        async for chunk in _stream_openai(api_key, prompt):
            yield chunk
    elif provider == "groq":
        async for chunk in _stream_groq(api_key, prompt):
            yield chunk
    else:
        return


async def _stream_anthropic(api_key: str, prompt: str) -> AsyncIterator[str]:
    accumulated = ""
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 2000,
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {}).get("text", "")
                    accumulated += delta
                    yield accumulated


async def _stream_openai(api_key: str, prompt: str) -> AsyncIterator[str]:
    accumulated = ""
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    accumulated += delta
                    yield accumulated


async def _stream_groq(api_key: str, prompt: str) -> AsyncIterator[str]:
    # Groq's API is OpenAI-compatible.
    # Using llama-3.1-8b-instant instead of llama-3.3-70b-versatile: same Groq
    # key, but the free-tier daily cap is 14,400 requests/day on the 8b model
    # vs only 1,000/day on the 70b model (per Groq's published rate limits,
    # https://console.groq.com/docs/rate-limits). Since Groq's limits are
    # per-organization (shared across every "ours" platform-key user, not
    # per-user), the 8b model gives ~14x more headroom before the shared pool
    # runs out for the day. Quality is somewhat lower than 70b, but adequate
    # for structured form-field answers extracted from profile data.
    accumulated = ""
    async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST",
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "stream": True,
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    accumulated += delta
                    yield accumulated
