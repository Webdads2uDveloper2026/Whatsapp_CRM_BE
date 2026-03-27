from datetime import datetime, timedelta
import structlog
from app.models.tenant import Tenant
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message

log = structlog.get_logger()


def _get_db():
    """Lazy import — db is None at module load, only valid after connect_db() runs."""
    from app.database import db
    return db


async def handle_webhook_payload(tenant: Tenant, payload: dict) -> None:
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    await _inbound(tenant, value, msg)
                for st in value.get("statuses", []):
                    await _status(tenant, st)
    except Exception as e:
        log.error("webhook.error", tenant=tenant.tenant_id, error=str(e))


async def _inbound(tenant: Tenant, value: dict, msg: dict) -> None:
    from app.api.v1.websocket import broadcast_to_tenant
    db    = _get_db()
    tid   = str(tenant.id)
    wa_id = msg.get("from", "")
    mid   = msg.get("id", "")

    # Deduplicate
    if await db.messages.find_one({"wa_message_id": mid}):
        return

    # Upsert contact
    contact = await Contact.find_one(Contact.tenant_id == tid, Contact.wa_id == wa_id)
    if not contact:
        pname   = value.get("contacts", [{}])[0].get("profile", {}).get("name")
        contact = Contact(tenant_id=tid, wa_id=wa_id, profile_name=pname)
        await contact.insert()

    # Upsert conversation
    now     = datetime.utcnow()
    preview = _preview(msg)
    convo   = await Conversation.find_one(
        Conversation.tenant_id == tid,
        Conversation.wa_id == wa_id,
        Conversation.status == "open",
    )
    if not convo:
        convo = Conversation(
            tenant_id=tid, contact_id=str(contact.id), wa_id=wa_id,
            status="open", last_message_at=now, last_message_preview=preview,
            window_expires_at=now + timedelta(hours=24), unread_count=1,
        )
        await convo.insert()
    else:
        convo.last_message_at      = now
        convo.last_message_preview = preview
        convo.window_expires_at    = now + timedelta(hours=24)
        convo.unread_count         = (convo.unread_count or 0) + 1
        await convo.save()

    # Save message
    msg_type = msg.get("type", "text")
    message  = Message(
        tenant_id=tid, conversation_id=str(convo.id), contact_id=str(contact.id),
        wa_message_id=mid, direction="inbound", msg_type=msg_type,
        content={msg_type: msg.get(msg_type, {})}, status="received",
    )
    await message.insert()

    # Push real-time event
    await broadcast_to_tenant(tid, {
        "type":            "new_message",
        "conversation_id": str(convo.id),
        "message": {
            "id":           str(message.id),
            "direction":    "inbound",
            "type":         msg_type,
            "content":      message.content,
            "from":         wa_id,
            "created_at":   now.isoformat(),
        },
        "contact":     {"id": str(contact.id), "wa_id": wa_id, "name": contact.profile_name},
        "unread_count": convo.unread_count,
    })


async def _status(tenant: Tenant, status: dict) -> None:
    from app.api.v1.websocket import broadcast_to_tenant
    db  = _get_db()
    mid = status.get("id", "")
    st  = status.get("status", "")
    tid = str(tenant.id)

    result = await db.messages.find_one_and_update(
        {"wa_message_id": mid, "tenant_id": tid},
        {"$set": {"status": st, "updated_at": datetime.utcnow()}},
        return_document=True,
    )
    if result:
        await broadcast_to_tenant(tid, {
            "type":            "status_update",
            "wa_message_id":   mid,
            "status":          st,
            "conversation_id": str(result.get("conversation_id", "")),
        })
        if result.get("broadcast_id") and st in ("delivered", "read", "failed"):
            await db.broadcasts.update_one(
                {"_id": result["broadcast_id"]},
                {"$inc": {f"{st}_count": 1}},
            )


def _preview(msg: dict) -> str:
    t = msg.get("type", "")
    if t == "text":
        return msg.get("text", {}).get("body", "")[:80]
    return {"image": "[Image]", "video": "[Video]", "document": "[Document]",
            "audio": "[Audio]", "location": "[Location]"}.get(t, f"[{t}]")