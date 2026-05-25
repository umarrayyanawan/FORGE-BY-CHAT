from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import ARRAY, Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from system.api.auth.jwt import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
    verify_token,
)
from system.shared.database import Base, get_db


class UserDB(Base):
    __tablename__ = "forge_users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    roles = Column(ARRAY(String), default=["user"])
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 1800


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user account."""
    from sqlalchemy import select

    result = await db.execute(select(UserDB).where(UserDB.email == request.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = UserDB(email=request.email, hashed_password=hash_password(request.password))
    db.add(user)
    await db.commit()
    return {"message": "Account created successfully", "email": request.email}


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate and receive JWT tokens."""
    from sqlalchemy import select

    result = await db.execute(select(UserDB).where(UserDB.email == request.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token_data = {"sub": str(user.id), "email": user.email, "roles": user.roles}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh_token: str):
    """Exchange refresh token for new access token."""
    token_data = verify_token(refresh_token)
    data = {"sub": token_data.user_id, "email": token_data.email, "roles": token_data.roles}
    new_access = create_access_token(data)
    new_refresh = create_refresh_token(data)
    return TokenResponse(access_token=new_access, refresh_token=new_refresh)


@router.get("/me")
async def me(current_user=Depends(get_current_user)):
    """Get current authenticated user."""
    return {
        "user_id": current_user.user_id,
        "email": current_user.email,
        "roles": current_user.roles,
    }
