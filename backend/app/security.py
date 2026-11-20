from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User


password_hasher = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
# Verify a real Argon2 hash for unknown accounts, too.
dummy_password_hash = password_hasher.hash("non-account-timing-placeholder")


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hasher.verify(password, encoded)


def create_access_token(user_id: int) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": now,
            "exp": now + timedelta(hours=settings.token_hours),
            "type": "access",
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None:
        raise unauthorized()
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_settings().jwt_secret_key,
            algorithms=["HS256"],
            options={"require": ["sub", "iat", "exp"]},
        )
        subject = payload["sub"]
        if (
            not isinstance(subject, str) or not subject.isascii()
            or not subject.isdecimal() or len(subject) > 10
            or payload.get("type") != "access"
        ):
            raise unauthorized()
        user_id = int(subject)
        if not 0 < user_id <= 2147483647:
            raise unauthorized()
    except jwt.InvalidTokenError:
        raise unauthorized() from None
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise unauthorized()
    return user
