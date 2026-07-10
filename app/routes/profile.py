from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.db import get_db
from app.core.security import get_current_user
from app.schemas.profile import ProfileResponse, ProfileUpdate
from app.services.file_service import download_resume_from_link, upload_resume

router = APIRouter(prefix="/profile", tags=["profile"])


@router.post("", response_model=ProfileResponse)
async def save_profile(
    body: ProfileUpdate,
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])

    update_fields = {}
    if body.about_paragraph is not None:
        update_fields["about_paragraph"] = body.about_paragraph

    await shared_db["profiles"].update_one(
        {"user_id": user_id},
        {"$set": update_fields, "$setOnInsert": {"user_id": user_id}},
        upsert=True,
    )

    profile = await shared_db["profiles"].find_one({"user_id": user_id})
    return ProfileResponse(
        user_id=user_id,
        about_paragraph=profile.get("about_paragraph"),
        documents=profile.get("documents", []),
    )


@router.post("/documents", response_model=ProfileResponse)
async def upload_document(
    title: str = Form(...),
    doc_type: str = Form("resume"),
    link: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Accepts EITHER a multipart file upload OR a JSON-ish form field `link`
    (server downloads it), plus a `title` (and optional `doc_type`).
    Exactly one of file/link must be provided.
    """
    user_id = str(current_user["_id"])

    if file is not None and link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either a file upload or a link, not both",
        )

    if file is not None:
        await upload_resume(user_id, file, title, shared_db, doc_type=doc_type)
    elif link:
        await download_resume_from_link(user_id, link, title, shared_db, doc_type=doc_type)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either a file upload or a link",
        )

    profile = await shared_db["profiles"].find_one({"user_id": user_id})
    return ProfileResponse(
        user_id=user_id,
        about_paragraph=profile.get("about_paragraph"),
        documents=profile.get("documents", []),
    )


@router.get("", response_model=ProfileResponse)
async def get_profile(
    current_user: dict = Depends(get_current_user),
    shared_db: AsyncIOMotorDatabase = Depends(get_db),
):
    user_id = str(current_user["_id"])
    profile = await shared_db["profiles"].find_one({"user_id": user_id})

    if not profile:
        return ProfileResponse(user_id=user_id, about_paragraph=None, documents=[])

    return ProfileResponse(
        user_id=user_id,
        about_paragraph=profile.get("about_paragraph"),
        documents=profile.get("documents", []),
    )
