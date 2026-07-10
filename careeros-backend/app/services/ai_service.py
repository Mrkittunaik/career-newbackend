import json
from typing import List, Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.security import decrypt_value

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
ANTHROPIC_DEFAULT_MODEL = "claude-3-5-haiku-latest"


class AIClient:
    """
    Thin unified wrapper so callers don't care which provider is behind it --
    just `await client.complete(prompt)`. Uses raw httpx calls instead of
    each provider's SDK to keep this dependency-light (Groq's API is
    OpenAI-compatible, so those two share a code path).
    """

    def __init__(self, provider: str, api_key: str):
        self.provider = provider
        self.api_key = api_key

    async def complete(self, prompt: str, max_tokens: int = 500) -> str:
        if self.provider == "groq":
            return await self._call_openai_compatible(GROQ_URL, GROQ_DEFAULT_MODEL, prompt, max_tokens)
        elif self.provider == "openai":
            return await self._call_openai_compatible(OPENAI_URL, OPENAI_DEFAULT_MODEL, prompt, max_tokens)
        elif self.provider == "claude":
            return await self._call_anthropic(prompt, max_tokens)
        else:
            raise ValueError(f"Unsupported ai_provider: {self.provider!r}")

    async def _call_openai_compatible(self, url: str, model: str, prompt: str, max_tokens: int) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

    async def _call_anthropic(self, prompt: str, max_tokens: int) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": ANTHROPIC_DEFAULT_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
            data = response.json()
            return "".join(block.get("text", "") for block in data.get("content", [])).strip()


async def get_ai_client(user_id: str, shared_db: AsyncIOMotorDatabase) -> AIClient:
    """
    Resolves the right AIClient for a user:
      - if their user_settings has a non-"groq" ai_provider (or a groq
        provider with their own key set) AND an ai_key_encrypted present,
        decrypt it and build a client for THAT provider.
      - otherwise fall back to the default Groq client using the backend's
        own GROQ_API_KEY.
    Never logs/exposes the raw key beyond this function.
    """
    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})

    if settings_doc and settings_doc.get("ai_key_encrypted"):
        provider = settings_doc.get("ai_provider", "groq")
        raw_key = decrypt_value(settings_doc["ai_key_encrypted"])
        return AIClient(provider=provider, api_key=raw_key)

    return AIClient(provider="groq", api_key=settings.GROQ_API_KEY)


# =========================================================
# Prompt construction (kept separate so it's easy to tune
# without touching the call/parsing logic around it)
# =========================================================

def _build_answer_prompt(profile: Optional[dict], question_text: str, field_type: str) -> str:
    about_paragraph = (profile or {}).get("about_paragraph", "")
    doc_titles = [doc.get("title", "") for doc in (profile or {}).get("documents", [])]

    profile_context = about_paragraph or "No profile summary available."
    docs_context = (
        f"Relevant documents on file: {', '.join(doc_titles)}." if doc_titles else ""
    )

    return (
        "You are filling out a job application form on behalf of a candidate. "
        "Answer the following form field concisely and truthfully, based only on "
        "the candidate profile below. If the profile doesn't contain enough "
        "information to answer confidently, give a brief, reasonable, generic answer.\n\n"
        f"Candidate profile:\n{profile_context}\n{docs_context}\n\n"
        f"Form field type: {field_type}\n"
        f"Question: {question_text}\n\n"
        "Answer:"
    )


def _build_search_query_prompt(job_type: str, experience_level: str, site: str) -> str:
    return (
        f"Build an optimal job search query/filter string for the site '{site}', "
        f"for a candidate looking for '{job_type}' roles at '{experience_level}' "
        "experience level. Return ONLY the query string, no explanation."
    )


def _build_job_filter_prompt(job_type: str, experience_level: str, jobs_found_list: List[dict]) -> str:
    return (
        f"Target role: '{job_type}'. Target experience level: '{experience_level}'.\n"
        "Here is a list of scraped job listings (JSON):\n"
        f"{json.dumps(jobs_found_list)}\n\n"
        "Return ONLY a JSON array containing the listings from the input that are a "
        "genuine match for the target role and experience level -- exclude wrong "
        "seniority and wrong role. Preserve each listing's original fields exactly."
    )


# =========================================================
# Public entry points
# =========================================================

async def generate_answer(
    user_id: str,
    question_text: str,
    field_type: str,
    shared_db: AsyncIOMotorDatabase,
) -> str:
    """
    Pulls the user's profile (about_paragraph + doc titles), builds a prompt,
    and calls their resolved AI client to answer a single application
    question. Profiles live in the shared db (account-level config, like
    user_settings), not the per-storage-mode user db.
    """
    profile = await shared_db["profiles"].find_one({"user_id": user_id})
    prompt = _build_answer_prompt(profile, question_text, field_type)

    client = await get_ai_client(user_id, shared_db)
    return await client.complete(prompt)
