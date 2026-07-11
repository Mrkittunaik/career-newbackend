from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from pydantic import BaseModel

from app.core.db import get_core_db, get_user_db
from app.core.security import get_current_user_id

router = APIRouter(prefix="/profile", tags=["profile"])

# Resumes are stored in MongoDB via GridFS instead of local disk — Render's
# filesystem is ephemeral (wiped on every restart/redeploy), so anything
# written to disk would eventually vanish. GridFS keeps the file bytes in
# the same Mongo cluster as everything else (hosted, or the user's own DB
# if storage_mode == "own" — same routing as every other collection here).
MAX_RESUME_BYTES = 200 * 1024  # 200KB cap on resume uploads


class UpdateProfileBody(BaseModel):
    about_paragraph: str


class LinkDocumentBody(BaseModel):
    link: str
    title: str


def _serialize_document(doc: dict) -> dict:
    doc_type = doc.get("type")
    if doc_type == "file":
        # Bytes live in GridFS now, not on disk — url points at our own
        # download route, which streams the file back out on request.
        url = f"/api/v1/profile/documents/{doc['_id']}/download"
    else:
        url = doc.get("url")

    return {
        "id": str(doc["_id"]),
        "title": doc.get("title"),
        "type": doc_type,  # "file" | "link"
        "url": url,
        "created_at": doc.get("created_at"),
    }


@router.get("")
async def get_profile(user_id: str = Depends(get_current_user_id)):
    user_db = await get_user_db(user_id)
    profile = await user_db.profiles.find_one({"user_id": user_id})
    if profile is None:
        # Shouldn't normally happen (created at signup) but don't 500 if it does.
        await user_db.profiles.insert_one({"user_id": user_id, "about_paragraph": "", "created_at": datetime.now(timezone.utc)})
        profile = {"about_paragraph": ""}

    # email lives on the users collection, which is account-essential and
    # always in the hosted core DB regardless of the user's storage_mode.
    core_db = get_core_db()
    user = await core_db.users.find_one({"_id": ObjectId(user_id)})
    documents = await user_db.documents.find({"user_id": user_id}).sort("created_at", -1).to_list(length=200)

    return {
        "email": user["email"] if user else None,
        "about_paragraph": profile.get("about_paragraph", ""),
        "documents": [_serialize_document(d) for d in documents],
    }


@router.post("")
async def update_profile(body: UpdateProfileBody, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    await db.profiles.update_one(
        {"user_id": user_id},
        {"$set": {"about_paragraph": body.about_paragraph, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return {"about_paragraph": body.about_paragraph}


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def add_document(request: Request, user_id: str = Depends(get_current_user_id)):
    """
    Handles both call shapes the frontend uses against this same path:
    - multipart/form-data with a `file` + `title` field (api/profile.js:uploadDocument)
    - application/json body { link, title } (api/profile.js:linkDocument)
    We branch on Content-Type since FastAPI can't mix File/Form and a JSON
    body in a single route signature.
    """
    db = await get_user_db(user_id)
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        title = form.get("title")
        if upload is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing file in form data")

        contents = await upload.read()
        if len(contents) > MAX_RESUME_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large — max {MAX_RESUME_BYTES // 1024}KB per resume.",
            )

        bucket = AsyncIOMotorGridFSBucket(db)
        gridfs_id = await bucket.upload_from_stream(
            upload.filename or "resume",
            contents,
            metadata={"user_id": user_id, "content_type": upload.content_type},
        )

        doc = {
            "user_id": user_id,
            "title": title or upload.filename or "Untitled document",
            "type": "file",
            "gridfs_id": gridfs_id,
            "filename": upload.filename or "resume",
            "content_type": upload.content_type,
            "created_at": datetime.now(timezone.utc),
        }
    else:
        body = LinkDocumentBody.model_validate(await request.json())
        doc = {
            "user_id": user_id,
            "title": body.title or body.link,
            "type": "link",
            "url": body.link,
            "created_at": datetime.now(timezone.utc),
        }

    result = await db.documents.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_document(doc)


@router.get("/documents/{doc_id}/download")
async def download_document(doc_id: str, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    try:
        oid = ObjectId(doc_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    doc = await db.documents.find_one({"_id": oid, "user_id": user_id, "type": "file"})
    if doc is None or "gridfs_id" not in doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    bucket = AsyncIOMotorGridFSBucket(db)
    try:
        stream = await bucket.open_download_stream(doc["gridfs_id"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found in storage")

    async def iterator():
        while True:
            chunk = await stream.readchunk()
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        iterator(),
        media_type=doc.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.get("filename", "resume")}"'},
    )


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    try:
        oid = ObjectId(doc_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    doc = await db.documents.find_one({"_id": oid, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    # Clean up the GridFS blob too, or it's an orphaned chunk sitting in
    # Mongo forever with nothing pointing at it.
    if doc.get("type") == "file" and doc.get("gridfs_id"):
        bucket = AsyncIOMotorGridFSBucket(db)
        try:
            await bucket.delete(doc["gridfs_id"])
        except Exception:
            pass  # already gone or never existed — deleting the record still proceeds

    await db.documents.delete_one({"_id": oid, "user_id": user_id})
    return {"deleted": True}
