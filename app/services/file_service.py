import re
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings

# ---------------------------------------------------------------
# STORAGE CHOICE: local disk, not S3.
# This is the simplest option for the current setup -- no extra
# cloud credentials/bucket to provision, and resumes/cover letters
# are small text-ish files. Files are written under settings.UPLOAD_DIR
# (default "storage/resumes"), namespaced by user_id, and referenced
# in profiles.documents by that relative path. If this ever needs to
# scale past a single backend instance (multiple app servers, no
# shared disk), swap this for S3 -- upload_resume/download_resume_from_link
# are the only two functions that would need to change; the DB shape
# (url_or_file_ref as a string) stays the same either way.
# ---------------------------------------------------------------

UPLOAD_ROOT = Path(settings.UPLOAD_DIR)


def _user_dir(user_id: str) -> Path:
    user_dir = UPLOAD_ROOT / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _safe_filename(original_name: str) -> str:
    original_name = original_name or "file"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
    return f"{uuid.uuid4().hex}_{name}"


async def _append_document(
    user_id: str,
    shared_db: AsyncIOMotorDatabase,
    title: str,
    url_or_file_ref: str,
    doc_type: str,
) -> dict:
    """Appends to profiles.documents (creating the profile doc if it
    doesn't exist yet). profiles lives in the shared db, same as
    user_settings -- it's account-level config, not per-storage-mode data."""
    document = {
        "title": title,
        "url_or_file_ref": url_or_file_ref,
        "doc_type": doc_type,
    }

    await shared_db["profiles"].update_one(
        {"user_id": user_id},
        {
            "$push": {"documents": document},
            "$setOnInsert": {"user_id": user_id},
        },
        upsert=True,
    )

    return document


async def upload_resume(
    user_id: str,
    file: UploadFile,
    title: str,
    shared_db: AsyncIOMotorDatabase,
    doc_type: str = "resume",
) -> dict:
    """Saves an uploaded file to local disk and records it in profiles.documents."""
    filename = _safe_filename(file.filename)
    dest_path = _user_dir(user_id) / filename

    contents = await file.read()
    dest_path.write_bytes(contents)

    return await _append_document(user_id, shared_db, title, str(dest_path), doc_type)


async def download_resume_from_link(
    user_id: str,
    url: str,
    title: str,
    shared_db: AsyncIOMotorDatabase,
    doc_type: str = "resume",
) -> dict:
    """Downloads a file from a user-provided URL server-side, saves it the
    same way as a direct upload, and records it in profiles.documents."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        contents = response.content

    original_name = url.split("/")[-1].split("?")[0] or "downloaded_file"
    filename = _safe_filename(original_name)
    dest_path = _user_dir(user_id) / filename
    dest_path.write_bytes(contents)

    return await _append_document(user_id, shared_db, title, str(dest_path), doc_type)


async def get_document_for_field(
    user_id: str,
    field_type_or_label: str,
    shared_db: AsyncIOMotorDatabase,
) -> Optional[dict]:
    """
    Matches the bot's requested field label (e.g. "Resume", "Upload your CV",
    "Cover Letter") against stored document titles using simple
    case-insensitive substring matching in both directions (handles the
    label being a superset OR subset of the stored title), returns the
    matched document dict (with url_or_file_ref) or None if nothing matches.
    """
    profile = await shared_db["profiles"].find_one({"user_id": user_id})
    if not profile:
        return None

    label_lower = field_type_or_label.lower()

    for document in profile.get("documents", []):
        title_lower = document.get("title", "").lower()
        if not title_lower:
            continue
        if title_lower in label_lower or label_lower in title_lower:
            return document

    return None
