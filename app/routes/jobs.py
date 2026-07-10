from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.db import get_db, get_user_db
from app.core.security import get_current_user
from app.schemas.dashboard import JobApplicationResponse
from app.schemas.job_request import JobRequestCreate, JobRequestResponse
from app.services.queue_service import create_job_request

router = APIRouter(prefix="/jobs", tags=["jobs"])


async def _resolve_user_db(user_id: str, shared_db: AsyncIOMotorDatabase) -> AsyncIOMotorDatabase:
    """Loads this user's settings and resolves the correct DB handle
    (shared vs their own mongo) per storage_mode."""
    settings_doc = await shared_db["user_settings"].find_one({"user_id": user_id})
    settings_dict = settings_doc or {}
    settings_dict["user_id"] = user_id  # ensure present even if no settings row yet
    return await get_user_db(settings_dict)


@router.post("/request", response_model=JobRequestResponse)
async def request_job(
    body: JobRequestCreate,
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])
    user_db = await _resolve_user_db(user_id, shared_db)

    job_request_doc = await create_job_request(
        user_id=user_id,
        job_type=body.job_type,
        experience_level=body.experience_level,
        target_sites=body.target_sites,
        shared_db=shared_db,
        user_db=user_db,
    )

    return JobRequestResponse(
        id=str(job_request_doc["_id"]),
        user_id=job_request_doc["user_id"],
        job_type=job_request_doc["job_type"],
        experience_level=job_request_doc["experience_level"],
        target_sites=job_request_doc.get("target_sites", []),
        status=job_request_doc["status"],
        created_at=job_request_doc["created_at"],
    )


@router.get("", response_model=List[JobApplicationResponse])
async def list_job_applications(
    limit: int = Query(20, ge=1, le=100),
    skip: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])
    user_db = await _resolve_user_db(user_id, shared_db)

    cursor = (
        user_db["job_applications"]
        .find({"user_id": user_id})
        .sort("applied_at", -1)
        .skip(skip)
        .limit(limit)
    )
    applications = await cursor.to_list(length=None)

    return [
        JobApplicationResponse(
            id=str(app["_id"]),
            user_id=app["user_id"],
            task_id=app["task_id"],
            role=app["role"],
            company=app["company"],
            link=app["link"],
            status=app["status"],
            reason=app.get("reason"),
            applied_at=app["applied_at"],
        )
        for app in applications
    ]
