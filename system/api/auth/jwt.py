"""JWT authentication utilities for the FORGE API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field

from system.config.settings import settings
from system.observability.logging.logger import get_logger
from system.shared.exceptions import AuthenticationError, AuthorizationError

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
REFRESH_TOKEN_EXPIRE_DAYS: int = 7

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TokenData(BaseModel):
    """Decoded JWT payload for an authenticated FORGE user."""

    user_id: str
    email: str
    roles: List[str] = Field(default_factory=list)
    token_type: str = "access"


class Token(BaseModel):
    """Response body returned on successful authentication."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the bcrypt *hashed* value."""
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# Token creation
# ---------------------------------------------------------------------------


def _build_claims(data: dict, expire_delta: timedelta) -> dict:
    """Merge *data* with standard JWT claims and return the payload dict."""
    now = datetime.now(tz=timezone.utc)
    return {
        **data,
        "iat": now,
        "exp": now + expire_delta,
    }


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Mint a signed JWT access token.

    Args:
        data: Claims to embed (must include ``sub``, ``email``, ``roles``).
        expires_delta: Override the default expiry window.

    Returns:
        Encoded JWT string.
    """
    delta = expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = _build_claims({**data, "type": "access"}, delta)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """Mint a signed JWT refresh token with a longer TTL.

    Args:
        data: Claims to embed (typically ``sub`` and ``email``).

    Returns:
        Encoded JWT string.
    """
    delta = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = _build_claims({**data, "type": "refresh"}, delta)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def create_token_pair(user_id: str, email: str, roles: List[str]) -> Token:
    """Create a matching access + refresh token pair for a user.

    Args:
        user_id: Database PK / UUID for the user.
        email: User's email address.
        roles: List of role strings (e.g. ``["admin", "user"]``).

    Returns:
        :class:`Token` containing both tokens and metadata.
    """
    claims = {"sub": user_id, "email": email, "roles": roles}
    access = create_access_token(claims)
    refresh = create_refresh_token({"sub": user_id, "email": email})
    return Token(
        access_token=access,
        refresh_token=refresh,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def verify_token(token: str, expected_type: str = "access") -> TokenData:
    """Decode and validate a JWT.

    Args:
        token: Raw JWT string.
        expected_type: ``"access"`` or ``"refresh"``.

    Returns:
        Decoded :class:`TokenData`.

    Raises:
        :class:`AuthenticationError`: If the token is invalid, expired, or
            the wrong type.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        logger.warning("jwt.decode_failed", error=str(exc))
        raise AuthenticationError(
            message="Invalid or expired token",
            details={"reason": str(exc)},
        ) from exc

    token_type = payload.get("type")
    if token_type != expected_type:
        raise AuthenticationError(
            message=f"Expected {expected_type} token, got {token_type}",
            details={"expected": expected_type, "actual": token_type},
        )

    user_id = payload.get("sub")
    email = payload.get("email")
    if not user_id or not email:
        raise AuthenticationError(
            message="Token is missing required claims (sub, email)",
        )

    return TokenData(
        user_id=user_id,
        email=email,
        roles=payload.get("roles", []),
        token_type=token_type,
    )


# ---------------------------------------------------------------------------
# FastAPI security scheme
# ---------------------------------------------------------------------------

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> TokenData:
    """FastAPI dependency — validate the Bearer token and return :class:`TokenData`.

    Args:
        credentials: Injected by FastAPI from the ``Authorization`` header.

    Returns:
        Authenticated :class:`TokenData`.

    Raises:
        :class:`HTTPException` 401: If no credentials are provided or the
            token is invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        token_data = verify_token(credentials.credentials, expected_type="access")
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return token_data


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[TokenData]:
    """Like :func:`get_current_user` but returns ``None`` for unauthenticated requests."""
    if credentials is None:
        return None
    try:
        return verify_token(credentials.credentials, expected_type="access")
    except AuthenticationError:
        return None


async def require_admin(
    current_user: TokenData = Depends(get_current_user),
) -> TokenData:
    """FastAPI dependency — require the ``admin`` role.

    Args:
        current_user: Injected authenticated user.

    Returns:
        The same :class:`TokenData` if the user is an admin.

    Raises:
        :class:`HTTPException` 403: If the user lacks the ``admin`` role.
    """
    if "admin" not in current_user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation",
        )
    return current_user


def require_role(role: str):
    """Return a FastAPI dependency factory that requires a specific role.

    Usage::

        @router.get("/secret", dependencies=[Depends(require_role("ops"))])
    """

    async def _check(current_user: TokenData = Depends(get_current_user)) -> TokenData:
        if role not in current_user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is required for this operation",
            )
        return current_user

    return _check
