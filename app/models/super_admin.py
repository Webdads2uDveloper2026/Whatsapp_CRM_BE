from beanie import Document
from typing import Optional
from datetime import datetime


class SuperAdmin(Document):
    email: str
    hashed_password: str
    name: str = "Super Admin"
    is_active: bool = True
    last_login_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "super_admins"
        indexes = ["email"]


class SubscriptionPlan(Document):
    name: str
    description: str = ""
    price_monthly: float = 0.0
    price_yearly: float = 0.0
    agent_limit: int = 3
    broadcast_limit: int = 1000
    template_limit: int = 10
    contact_limit: int = 1000
    flow_builder: bool = False
    analytics: bool = False
    automations: bool = True
    api_access: bool = False
    whatsapp_accounts: int = 1
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "subscription_plans"
        indexes = ["name", "is_active"]
