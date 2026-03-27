from beanie import Document
from typing import Optional
from datetime import datetime


class Tenant(Document):
    # ── Account ───────────────────────────────────────────────────────────
    business_name:    str
    email:            str
    hashed_password:  str
    status:           str = "pending"   # pending | active | suspended

    # ── Google OAuth ──────────────────────────────────────────────────────
    google_id:        Optional[str] = None
    avatar_url:       Optional[str] = None

    # ── WhatsApp / Meta ───────────────────────────────────────────────────
    waba_id:                 Optional[str] = None   # WhatsApp Business Account ID
    phone_number_id:         Optional[str] = None   # Phone Number ID
    display_phone_number:    Optional[str] = None   # e.g. +91 98765 43210
    waba_name:               Optional[str] = None   # Business name on WhatsApp
    encrypted_access_token:  Optional[str] = None   # Fernet-encrypted token
    waba_connected:          bool = False
    webhook_verify_token:    Optional[str] = None
    activated_at:            Optional[datetime] = None

    # ── Timestamps ────────────────────────────────────────────────────────
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name    = "tenants"
        indexes = ["email", "waba_id"]