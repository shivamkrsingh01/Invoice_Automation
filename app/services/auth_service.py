"""Simple session auth for the admin panel (Part 6).

One admin user, credentials from .env - deliberately simple, matching
this project's scale (a single admin, not a multi-user system). A
session is a signed JWT stored in an httpOnly cookie, so the browser
sends it automatically on every request and JavaScript can never read
or steal it (protects against XSS token theft, unlike storing a token
in localStorage).
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Cookie, HTTPException

from app.config.settings import settings
from app.utils.logging_config import logger

COOKIE_NAME = "session_token"
TOKEN_TTL_HOURS = 12
ALGORITHM = "HS256"


def authenticate(username: str, password: str) -> bool:
    """Constant-time comparison - avoids leaking how many characters of
    the guess were correct via response-timing differences."""
    if not settings.admin_username or not settings.admin_password:
        raise ValueError(
            "Admin login is not configured - set ADMIN_USERNAME and "
            "ADMIN_PASSWORD in .env"
        )
    username_ok = secrets.compare_digest(username, settings.admin_username)
    password_ok = secrets.compare_digest(password, settings.admin_password)
    return username_ok and password_ok


def create_session_token(username: str) -> str:
    if not settings.session_secret_key:
        raise ValueError(
            "SESSION_SECRET_KEY is not set in .env - required to sign "
            'login sessions. Generate one with: python -c "import secrets; '
            'print(secrets.token_hex(32))"'
        )
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, settings.session_secret_key, algorithm=ALGORITHM)


def verify_session_token(token: str) -> str:
    """Returns the username if the token is valid; raises HTTPException
    (401) otherwise - expired, tampered, or malformed all land here."""
    try:
        payload = jwt.decode(token, settings.session_secret_key, algorithms=[ALGORITHM])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired - please log in again")
    except jwt.InvalidTokenError as e:
        logger.warning(f"Rejected invalid session token: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid session")


def require_auth(session_token: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)) -> str:
    """FastAPI dependency - add `user: str = Depends(require_auth)` to any
    endpoint that should only work for a logged-in admin. Returns the
    username on success; raises 401 otherwise, which FastAPI turns into a
    proper error response before the endpoint body ever runs."""
    if not session_token:
        raise HTTPException(status_code=401, detail="Not logged in")
    return verify_session_token(session_token)