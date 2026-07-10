import json
from typing import List

from motor.motor_asyncio import AsyncIOMotorDatabase


async def build_search_query(
    job_type: str,
    experience_level: str,
    site: str,
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
) -> str:
    """
    Turns (job_type, experience_level) into a site-appropriate search
    query/filter string, via the user's resolved AI client (their own key if
    set, otherwise the default Groq client).
    """
    from app.services.ai_service import _build_search_query_prompt, get_ai_client

    prompt = _build_search_query_prompt(job_type, experience_level, site)
    client = await get_ai_client(user_id, shared_db)

    try:
        query = await client.complete(prompt, max_tokens=100)
        return query.strip()
    except Exception:
        # AI call failed (bad key, rate limit, network) -- fall back to a
        # basic deterministic query rather than blocking queue creation
        return f"{job_type} {experience_level}".strip()


async def filter_matching_jobs(
    job_type: str,
    experience_level: str,
    jobs_found_list: List[dict],
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
) -> List[dict]:
    """
    Takes raw scraped job listings (shape: [{title, company, link, description}, ...])
    and uses the user's resolved AI client to filter out mismatches (wrong
    seniority, wrong role), returning only approved jobs.
    """
    if not jobs_found_list:
        return []

    from app.services.ai_service import _build_job_filter_prompt, get_ai_client

    prompt = _build_job_filter_prompt(job_type, experience_level, jobs_found_list)
    client = await get_ai_client(user_id, shared_db)

    try:
        raw_response = await client.complete(prompt, max_tokens=2000)
        approved = json.loads(raw_response)
        if not isinstance(approved, list):
            raise ValueError("AI filter response was not a JSON array")
        return approved
    except Exception:
        # AI call failed or returned unparseable JSON -- fall back to naive
        # keyword pass-through rather than silently dropping every job
        job_type_lower = job_type.lower()
        return [
            job for job in jobs_found_list
            if job_type_lower in job.get("title", "").lower()
        ]
