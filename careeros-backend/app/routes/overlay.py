from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.db import get_db
from app.core.security import get_current_user
from app.schemas.overlay import (
    RegenerateTokenResponse,
    ValidateTokenRequest,
    ValidateTokenResponse,
)
from app.services.token_service import generate_bot_token, validate_bot_token

router = APIRouter(prefix="/overlay", tags=["overlay"])


@router.post("/validate-token", response_model=ValidateTokenResponse)
async def validate_token(
    body: ValidateTokenRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Public endpoint -- this IS the auth check for the bot's initial pairing
    handshake, so no auth dependency here. The bot calls this with the token
    a user pasted in from the website; matches config.js's expected
    {valid, user_id} contract exactly.
    """
    user_id = await validate_bot_token(body.token, db)
    if user_id is None:
        return ValidateTokenResponse(valid=False, user_id=None)

    return ValidateTokenResponse(valid=True, user_id=user_id)


@router.post("/regenerate-token", response_model=RegenerateTokenResponse)
async def regenerate_token(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Website-JWT-protected. Works for both first-time pairing (no existing
    bot_tokens row) and regeneration (kills the old row, issues a new one) --
    generate_bot_token handles both cases identically. Raw token is returned
    once here and never retrievable again.
    """
    user_id = str(current_user["_id"])
    raw_token = await generate_bot_token(user_id, db)

    return RegenerateTokenResponse(
        token=raw_token,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
