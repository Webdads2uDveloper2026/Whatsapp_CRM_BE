from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from app.models.tenant import Tenant
from app.core.security import (
    verify_password, create_access_token, create_refresh_token, decode_token
)
from app.core.dependencies import get_current_tenant

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/login")
async def login(body: LoginRequest):
    tenant = await Tenant.find_one(Tenant.email == body.email.lower())
    if not tenant or not verify_password(body.password, tenant.hashed_password):
        raise HTTPException(401, "Invalid email or password")

    tid     = str(tenant.id)
    payload = {"sub": tid, "tenant_id": tid}
    return {
        "access_token":  create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type":    "bearer",
        "status":        tenant.status,
        "tenant_id":     tid,
    }


@router.post("/refresh")
async def refresh(body: RefreshRequest):
    try:
        data = decode_token(body.refresh_token)
    except ValueError:
        raise HTTPException(401, "Invalid or expired refresh token")

    if data.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token")

    tid     = data.get("tenant_id") or data.get("sub")
    tenant  = await Tenant.get(tid)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    payload = {"sub": tid, "tenant_id": tid}
    return {
        "access_token":  create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type":    "bearer",
    }


@router.get("/me")
async def me(tenant: Tenant = Depends(get_current_tenant)):
    """Return current tenant profile — uses getattr with defaults for missing fields."""
    return {
        "id":             str(tenant.id),
        "email":          tenant.email,
        "business_name":  tenant.business_name,
        "status":         tenant.status,
        "waba_connected": getattr(tenant, "waba_connected",        False),
        "waba_id":        getattr(tenant, "waba_id",               None),
        "phone_number":   getattr(tenant, "display_phone_number",  None),
        "phone_number_id":getattr(tenant, "phone_number_id",       None),
        "avatar_url":     getattr(tenant, "avatar_url",            None),
        # Optional fields — use getattr so missing fields don't crash
        "plan":           getattr(tenant, "plan",                  "free"),
        "timezone":       getattr(tenant, "timezone",              "UTC"),
        "industry":       getattr(tenant, "industry",              None),
        "website":        getattr(tenant, "website",               None),
        "created_at":     tenant.created_at,
    }


@router.post("/logout")
async def logout():
    # JWT is stateless — client just deletes the token
    return {"message": "Logged out successfully"}