from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class EmailPasswordBody(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginBody(BaseModel):
    google_id_token: str


async def _create_default_docs_for_user(db, user_id: str):
    """Seeds an empty profile + settings row so downstream GETs don't 404."""
    await db.profiles.insert_one(
        {"user_id": user_id, "about_paragraph": "", "created_at": datetime.now(timezone.utc)}
    )
    await db.settings.insert_one(
        {
            "user_id": user_id,
            "bot_online": False,
            "bot_token_hash": None,
            "storage_mode": "hosted",
            "mongo_url": None,
            "ai_provider": settings.default_ai_provider,
            "ai_api_key": None,
            "gmail_connected": False,
            "gmail_email": None,
            "gmail_last_checked": None,
        }
    )


@router.post("/signup")
async def signup(body: EmailPasswordBody):
    db = get_db()
    existing = await db.users.find_one({"email": body.email.lower()})
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists")

    result = await db.users.insert_one(
        {
            "email": body.email.lower(),
            "password_hash": hash_password(body.password),
            "auth_provider": "password",
            "created_at": datetime.now(timezone.utc),
        }
    )
    user_id = str(result.inserted_id)
    await _create_default_docs_for_user(db, user_id)

    token = create_access_token(user_id)
    return {"token": token}


@router.post("/login")
async def login(body: EmailPasswordBody):
    db = get_db()
    user = await db.users.find_one({"email": body.email.lower()})
    if not user or not user.get("password_hash") or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    token = create_access_token(str(user["_id"]))
    return {"token": token}


@router.post("/google")
async def login_with_google(body: GoogleLoginBody):
    if not settings.google_signin_configured:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Google Sign-In isn't configured yet. Set GOOGLE_CLIENT_ID on the server.",
        )

    # Deferred import: google-auth is only needed once GOOGLE_CLIENT_ID is set.
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        idinfo = google_id_token.verify_oauth2_token(
            body.google_id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")

    email = idinfo.get("email")
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token had no email")

    db = get_db()
    user = await db.users.find_one({"email": email.lower()})
    if user is None:
        result = await db.users.insert_one(
            {
                "email": email.lower(),
                "password_hash": None,
                "auth_provider": "google",
                "created_at": datetime.now(timezone.utc),
            }
        )
        user_id = str(result.inserted_id)
        await _create_default_docs_for_user(db, user_id)
    else:
        user_id = str(user["_id"])

    token = create_access_token(user_id)
    return {"token": token}
