"""
Gmail OAuth + reply-scanning integration.

Wire-up checklist (once you have Google Cloud credentials):
1. Create an OAuth 2.0 Client ID (type: Web application) in Google Cloud Console.
2. Add scope: https://www.googleapis.com/auth/gmail.readonly
3. Set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REDIRECT_URI in .env
4. GMAIL_REDIRECT_URI must exactly match an "Authorized redirect URI" on the client.

Until those env vars are set, `build_oauth_url()` raises and the settings
router surfaces a clear "not configured" error to the frontend instead of a
silent failure.
"""

from urllib.parse import urlencode

from app.core.config import settings

GOOGLE_OAUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def build_oauth_url(state: str) -> str:
    if not settings.gmail_configured:
        raise RuntimeError("Gmail OAuth is not configured (missing GMAIL_CLIENT_ID/SECRET).")

    params = {
        "client_id": settings.gmail_client_id,
        "redirect_uri": settings.gmail_redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_OAUTH_BASE}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    """Exchanges an OAuth `code` for access/refresh tokens. Requires httpx + real credentials."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "redirect_uri": settings.gmail_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def scan_inbox_for_replies(access_token: str) -> list[dict]:
    """
    Placeholder for the real Gmail API scan (users.messages.list + get,
    filtered by known job-application senders/threads). Implement once
    Gmail OAuth is live; return shape should be a list of
    {message_id, from, snippet, received_at} dicts for the caller to match
    against job_applications by company/domain.
    """
    raise NotImplementedError("Gmail scanning isn't implemented yet — wire this up once OAuth is live.")
