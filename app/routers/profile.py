import uuid
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.db import get_core_db, get_user_db
from app.core.security import get_current_user_id

router = APIRouter(prefix="/profile", tags=["profile"])

UPLOAD_DIR = Path("uploads/documents")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class UpdateProfileBody(BaseModel):
    about_paragraph: str


class LinkDocumentBody(BaseModel):
    link: str
    title: str


def _serialize_document(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "title": doc.get("title"),
        "type": doc.get("type"),  # "file" | "link"
        "url": doc.get("url"),
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

        ext = Path(upload.filename or "").suffix
        stored_name = f"{uuid.uuid4().hex}{ext}"
        dest = UPLOAD_DIR / stored_name
        contents = await upload.read()
        dest.write_bytes(contents)

        doc = {
            "user_id": user_id,
            "title": title or upload.filename or "Untitled document",
            "type": "file",
            "url": f"/uploads/documents/{stored_name}",
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


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, user_id: str = Depends(get_current_user_id)):
    db = await get_user_db(user_id)
    try:
        oid = ObjectId(doc_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    result = await db.documents.delete_one({"_id": oid, "user_id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return {"deleted": True}
