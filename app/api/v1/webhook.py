"""
app/api/v1/webhook.py — Fixed duplicate message bug
"""
from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from app.config import get_settings

router   = APIRouter(prefix="/webhook", tags=["webhook"])
settings = get_settings()


@router.get("/{tenant_id}")
async def verify(
    tenant_id:        str,
    hub_mode:         str = None,
    hub_verify_token: str = None,
    hub_challenge:    str = None,
):
    if hub_mode == "subscribe" and hub_verify_token == settings.webhook_verify_token:
        return PlainTextResponse(hub_challenge or "")
    return PlainTextResponse("Verification failed", status_code=403)


@router.post("/{tenant_id}")
async def receive(tenant_id: str, request: Request):
    from app.database import db

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    # Find tenant
    tenant = None
    if len(tenant_id) == 24 and all(c in '0123456789abcdef' for c in tenant_id.lower()):
        try:
            from bson import ObjectId
            from app.models.tenant import Tenant
            doc = await db.tenants.find_one({"_id": ObjectId(tenant_id)})
            if doc:
                tenant = await Tenant.get(tenant_id)
        except Exception:
            pass

    if not tenant:
        try:
            from app.models.tenant import Tenant
            tenant = await Tenant.find_one(Tenant.status == "active")
        except Exception:
            pass

    if not tenant:
        return Response(status_code=200)

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") == "messages":
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        await _handle_message(tenant, msg, value.get("contacts", []))
                    for status in value.get("statuses", []):
                        await _handle_status(tenant, status)
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        import traceback; traceback.print_exc()

    return Response(status_code=200)


async def _handle_message(tenant, msg: dict, contacts: list):
    from app.database import db
    from datetime import datetime, timedelta

    try:
        wa_id     = msg.get("from", "")
        msg_id    = msg.get("id",   "")
        msg_type  = msg.get("type", "text")
        now       = datetime.utcnow()
        tid       = str(tenant.id)

        if not wa_id or not msg_id:
            return

        # ── Deduplication — skip if already saved ──────────────────────────
        existing = await db.messages.find_one({"wa_message_id": msg_id, "tenant_id": tid})
        if existing:
            print(f"[WEBHOOK] Duplicate skipped: {msg_id}")
            return

        # ── Contact name ───────────────────────────────────────────────────
        profile_name = next(
            (c.get("profile", {}).get("name", "") for c in contacts if c.get("wa_id") == wa_id),
            ""
        )

        # ── Find or create contact ─────────────────────────────────────────
        contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
        if not contact:
            res        = await db.contacts.insert_one({
                "tenant_id":    tid,
                "wa_id":        wa_id,
                "profile_name": profile_name or wa_id,
                "opted_in":     True,
                "status":       "New",
                "created_at":   now,
                "updated_at":   now,
            })
            contact_id = str(res.inserted_id)
        else:
            contact_id = str(contact["_id"])
            if profile_name and not contact.get("profile_name"):
                await db.contacts.update_one(
                    {"_id": contact["_id"]},
                    {"$set": {"profile_name": profile_name, "updated_at": now}}
                )

        # ── Build content — all WhatsApp message types ────────────────────
        if msg_type == "text":
            text_body = msg.get("text", {}).get("body", "")
            content   = {"body": text_body}
            preview   = text_body[:80]

        elif msg_type == "image":
            img     = msg.get("image", {})
            caption = img.get("caption", "")
            content = {
                "image":    img,
                "caption":  caption,
                "mime_type": img.get("mime_type", ""),
                "id":       img.get("id", ""),
            }
            preview = f"📷 {caption}" if caption else "📷 Image"

        elif msg_type == "video":
            vid     = msg.get("video", {})
            caption = vid.get("caption", "")
            content = {
                "video":    vid,
                "caption":  caption,
                "mime_type": vid.get("mime_type", ""),
                "id":       vid.get("id", ""),
            }
            preview = f"🎥 {caption}" if caption else "🎥 Video"

        elif msg_type == "audio":
            audio   = msg.get("audio", {})
            content = {
                "audio":    audio,
                "id":       audio.get("id", ""),
                "mime_type": audio.get("mime_type", ""),
                "voice":    audio.get("voice", False),
            }
            preview = "🎤 Voice message" if audio.get("voice") else "🎵 Audio"

        elif msg_type == "document":
            doc     = msg.get("document", {})
            fname   = doc.get("filename", "Document")
            caption = doc.get("caption", "")
            content = {
                "document": doc,
                "filename": fname,
                "caption":  caption,
                "mime_type": doc.get("mime_type", ""),
                "id":       doc.get("id", ""),
            }
            preview = f"📄 {fname}"

        elif msg_type == "sticker":
            sticker = msg.get("sticker", {})
            content = {
                "sticker":  sticker,
                "id":       sticker.get("id", ""),
                "animated": sticker.get("animated", False),
            }
            preview = "😊 Sticker"

        elif msg_type == "location":
            loc     = msg.get("location", {})
            name    = loc.get("name", "")
            address = loc.get("address", "")
            content = {
                "location": loc,
                "latitude":  loc.get("latitude"),
                "longitude": loc.get("longitude"),
                "name":      name,
                "address":   address,
            }
            preview = f"📍 {name or address or 'Location'}"

        elif msg_type == "contacts":
            cts     = msg.get("contacts", [])
            names   = ", ".join(c.get("name", {}).get("formatted_name", "") for c in cts)
            content = {"contacts": cts, "names": names}
            preview = f"👤 {names or 'Contact'}"

        elif msg_type == "reaction":
            reaction = msg.get("reaction", {})
            emoji    = reaction.get("emoji", "👍")
            msg_reacted = reaction.get("message_id", "")
            content  = {
                "reaction":   reaction,
                "emoji":      emoji,
                "message_id": msg_reacted,
            }
            preview = f"Reacted {emoji}"

        elif msg_type == "button":
            btn_text = msg.get("button", {}).get("text", "")
            payload  = msg.get("button", {}).get("payload", "")
            content  = {"body": btn_text, "payload": payload}
            preview  = f"🔘 {btn_text}"

        elif msg_type == "interactive":
            r        = msg.get("interactive", {})
            btn_r    = r.get("button_reply", {})
            list_r   = r.get("list_reply", {})
            title    = btn_r.get("title") or list_r.get("title") or ""
            content  = {
                "body":     title,
                "type":     r.get("type", ""),
                "button_reply": btn_r,
                "list_reply":   list_r,
            }
            preview  = f"🔘 {title}"

        elif msg_type == "order":
            order   = msg.get("order", {})
            content = {"order": order}
            preview = "🛒 Order received"

        elif msg_type == "system":
            sys_msg = msg.get("system", {})
            content = {"body": sys_msg.get("body", ""), "system": sys_msg}
            preview = sys_msg.get("body", "System message")

        elif msg_type == "unsupported":
            content = {"body": "⚠ Unsupported message type"}
            preview = "⚠ Unsupported"

        else:
            content = {"body": f"[{msg_type}]", "raw": msg}
            preview = f"[{msg_type}]"


        # ── Find or create conversation ────────────────────────────────────
        convo = await db.conversations.find_one({"tenant_id": tid, "wa_id": wa_id, "status": "open"})
        if not convo:
            res      = await db.conversations.insert_one({
                "tenant_id":            tid,
                "contact_id":           contact_id,
                "wa_id":                wa_id,
                "status":               "open",
                "unread_count":         1,
                "last_message_at":      now,
                "last_message_preview": preview,
                "window_expires_at":    now + timedelta(hours=24),
                "created_at":           now,
                "updated_at":           now,
            })
            convo_id = str(res.inserted_id)
        else:
            convo_id = str(convo["_id"])
            await db.conversations.update_one(
                {"_id": convo["_id"]},
                {"$set": {
                    "last_message_at":      now,
                    "last_message_preview": preview,
                    "window_expires_at":    now + timedelta(hours=24),
                    "updated_at":           now,
                }, "$inc": {"unread_count": 1}}
            )

        # ── Save message ───────────────────────────────────────────────────
        res = await db.messages.insert_one({
            "tenant_id":       tid,
            "conversation_id": convo_id,
            "contact_id":      contact_id,
            "wa_id":           wa_id,
            "wa_message_id":   msg_id,
            "direction":       "inbound",
            "msg_type":        msg_type,
            "type":            msg_type,
            "content":         content,
            "status":          "received",
            "created_at":      now,
        })
        message_id = str(res.inserted_id)

        # ── Mark read ──────────────────────────────────────────────────────
        try:
            from app.services.whatsapp import get_wa_client
            await get_wa_client(tenant).mark_read(msg_id)
        except Exception:
            pass

        # ── WebSocket push — single broadcast, no re-insert ───────────────
        try:
            from app.api.v1.websocket import broadcast_to_tenant
            await broadcast_to_tenant(tid, {
                "type":            "new_message",
                "conversation_id": convo_id,
                "message": {
                    "id":         message_id,
                    "direction":  "inbound",
                    "type":       msg_type,
                    "content":    content,
                    "status":     "received",
                    "created_at": now.isoformat(),
                }
            })
        except Exception:
            pass

        print(f"[WEBHOOK] ✅ Saved: +{wa_id} → {preview}")

        # ── Auto-reply engine ──────────────────────────────────────────────
        try:
            import asyncio
            from app.api.v1.autoreplies import run_autoreplies
            # Small delay so the inbound message saves/broadcasts first
            await asyncio.sleep(0.5)
            await run_autoreplies(
                tenant       = tenant,
                wa_id        = wa_id,
                contact_id   = contact_id,
                convo_id     = convo_id,
                msg_type     = msg_type,
                content      = content,
                is_new_convo = (convo is None),  # True when brand-new conversation
            )
        except Exception as ar_err:
            import traceback
            print(f"[AUTOREPLY] Engine error: {ar_err}")
            traceback.print_exc()

    except Exception as e:
        print(f"[WEBHOOK] _handle_message error: {e}")
        import traceback; traceback.print_exc()


async def _handle_status(tenant, status: dict):
    from app.database import db
    try:
        wa_msg_id  = status.get("id",     "")
        new_status = status.get("status", "")
        tid        = str(tenant.id)
        if not wa_msg_id or not new_status:
            return

        await db.messages.update_one(
            {"wa_message_id": wa_msg_id, "tenant_id": tid},
            {"$set": {"status": new_status}}
        )

        try:
            from app.api.v1.websocket import broadcast_to_tenant
            await broadcast_to_tenant(tid, {
                "type":          "status_update",
                "wa_message_id": wa_msg_id,
                "status":        new_status,
            })
        except Exception:
            pass

        print(f"[WEBHOOK] Status: {wa_msg_id} → {new_status}")
    except Exception as e:
        print(f"[WEBHOOK] _handle_status error: {e}")