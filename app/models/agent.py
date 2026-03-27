from beanie import Document
from pydantic import EmailStr
from typing import Optional, Any
from datetime import datetime


class Agent(Document):
    tenant_id: str
    name: str
    email: EmailStr
    hashed_password: str
    role: str = "agent"                     # superadmin | manager | agent
    is_active: bool = True
    avatar_initials: str = ""
    last_login_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "agents"
        indexes = ["tenant_id", "email"]


class Contact(Document):
    tenant_id: str
    wa_id: str                              # phone number without +
    profile_name: Optional[str] = None
    email: Optional[str] = None
    tags: list[str] = []
    custom_fields: dict[str, Any] = {}
    opted_in: bool = False
    is_blocked: bool = False
    last_seen_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "contacts"
        indexes = ["tenant_id", "wa_id"]


class Conversation(Document):
    tenant_id: str
    contact_id: str
    wa_id: str
    status: str = "open"                    # open | resolved | bot_handling | spam
    assigned_agent: Optional[str] = None
    unread_count: int = 0
    last_message_at: Optional[datetime] = None
    last_message_preview: Optional[str] = None
    window_expires_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "conversations"
        indexes = ["tenant_id", "wa_id", "status"]


class Message(Document):
    tenant_id: str
    conversation_id: str
    contact_id: str
    wa_message_id: Optional[str] = None
    direction: str                          # inbound | outbound
    msg_type: str = "text"
    content: dict = {}
    status: str = "sent"                    # sent | delivered | read | failed | received
    error_code: Optional[str] = None
    broadcast_id: Optional[str] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "messages"
        indexes = ["tenant_id", "conversation_id", "wa_message_id"]


class Template(Document):
    tenant_id: str
    wa_template_id: Optional[str] = None
    name: str
    category: str = "MARKETING"
    language: str = "en_US"
    components: list[dict[str, Any]] = []
    status: str = "PENDING"                 # PENDING | APPROVED | REJECTED | PAUSED
    template_type: str = "text"
    quality_score: Optional[str] = None
    rejected_reason: Optional[str] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "templates"
        indexes = ["tenant_id", "name", "status"]


class Broadcast(Document):
    tenant_id: str
    name: str
    template_name: str
    template_language: str = "en_US"
    template_components: list[dict[str, Any]] = []
    audience_type: str = "all"
    audience_tags: list[str] = []
    audience_contact_ids: list[str] = []
    status: str = "draft"                   # draft | queued | running | completed | failed | cancelled
    total_recipients: int = 0
    sent_count: int = 0
    delivered_count: int = 0
    read_count: int = 0
    failed_count: int = 0
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "broadcasts"
        indexes = ["tenant_id", "status"]


class Automation(Document):
    tenant_id: str
    name: str
    status: str = "active"                  # active | paused
    trigger_type: str                       # keyword | first_message | no_reply | any_message
    trigger_config: dict[str, Any] = {}
    action_type: str                        # send_text | send_template | add_tag | assign_agent
    action_config: dict[str, Any] = {}
    conditions: list[dict[str, Any]] = []
    priority: int = 0
    run_count: int = 0
    created_at: datetime = datetime.utcnow()
    updated_at: datetime = datetime.utcnow()

    class Settings:
        name = "automations"
        indexes = ["tenant_id", "status"]
