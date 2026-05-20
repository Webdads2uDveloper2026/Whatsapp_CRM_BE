from beanie import Document
from typing import Optional
from datetime import datetime


class Tenant(Document):
    # Account
    business_name:    str
    email:            str
    hashed_password:  str
    status:           str = "pending"   # pending | active | suspended

    # Google OAuth
    google_id:        Optional[str] = None
    avatar_url:       Optional[str] = None

    # WhatsApp / Meta
    waba_id:                 Optional[str] = None
    phone_number_id:         Optional[str] = None
    business_id:             Optional[str] = None
    display_phone_number:    Optional[str] = None
    waba_name:               Optional[str] = None
    encrypted_access_token:  Optional[str] = None
    waba_connected:          bool = False
    webhook_verify_token:    Optional[str] = None
    activated_at:            Optional[datetime] = None

    # Subscription
    plan_id:              Optional[str] = None
    plan_name:            str = "trial"
    subscription_status:  str = "trial"    # trial | active | expired | suspended
    subscription_start:   Optional[datetime] = None
    subscription_end:     Optional[datetime] = None
    agent_limit:          int = 1
    broadcast_limit:      int = 100
    template_limit:       int = 5
    contact_limit:        int = 500
    flow_builder:         bool = False
    analytics_access:     bool = False

    # Meta
    created_by_super_admin: bool = False
    notes:                Optional[str] = None
    tenant_id:            Optional[str] = None

    # Team
    custom_role_permissions: dict = {}
    agent_count_limit: int = 10

    # Timestamps
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name    = "tenants"
        indexes = ["email", "waba_id"]
