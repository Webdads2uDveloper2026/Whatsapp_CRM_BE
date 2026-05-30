"""
app/api/v1/webhook.py  —  Production-ready centralized WhatsApp webhook

Architecture (Tech Provider / Solution Provider pattern)
═══════════════════════════════════════════════════════
One App-level webhook endpoint handles ALL tenants.

  GET  /api/v1/webhook/whatsapp   —  hub.challenge verification (Meta handshake)
  POST /api/v1/webhook/whatsapp   —  all inbound events for all tenants

  GET  /api/v1/webhook            —  legacy alias (same handler)
  POST /api/v1/webhook            —  legacy alias (same handler)

Tenant routing
──────────────
  1. Primary:  value.metadata.phone_number_id  →  Tenant.phone_number_id
  2. Fallback: entry[0].id (WABA ID)          →  Tenant.waba_id
  3. Dev only: any single active tenant

phone_number_id is the correct production key:
  • Every message/status event carries it in value.metadata
  • Unique per phone number (unlike waba_id which covers all numbers in a WABA)
  • Tech Providers use it to demultiplex events across customer accounts

Security
────────
  X-Hub-Signature-256 HMAC-SHA256 validated against META_APP_SECRET before any
  processing. Invalid signatures are silently accepted (return 200) so Meta
  never retries — but the payload is dropped.

Performance
───────────
  Returns HTTP 200 immediately. All DB writes, WebSocket pushes, and auto-reply
  logic run inside a FastAPI BackgroundTask — Meta's 20-second window is safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import structlog
from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.config import get_settings

router   = APIRouter(prefix="/webhook", tags=["webhook"])
settings = get_settings()
log      = structlog.get_logger("webhook")


# ══════════════════════════════════════════════════════════════════════════════
#  Security — HMAC-SHA256 signature validation
# ══════════════════════════════════════════════════════════════════════════════

def _verify_signature(raw_body: bytes, header: str | None) -> bool:
    """
    Validate X-Hub-Signature-256 against META_APP_SECRET.
    Skips validation when the secret is not configured (dev convenience).
    Uses hmac.compare_digest to prevent timing attacks.
    """
    secret = settings.meta_app_secret
    if not secret:
        return True                                 # dev mode — skip
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])  # strip "sha256="


# ══════════════════════════════════════════════════════════════════════════════
#  Tenant resolution  (phone_number_id → Tenant)
# ══════════════════════════════════════════════════════════════════════════════

async def _resolve_tenant(phone_number_id: str, waba_id: str):
    """
    Resolve the tenant for an inbound event.

    Priority:
      1. phone_number_id match  (most specific — unique per number)
      2. waba_id match          (less specific — one WABA may have many numbers)
      3. any active tenant      (dev/single-tenant fallback only)
    """
    from app.models.tenant import Tenant

    # 1. phone_number_id lookup — canonical for Tech Providers
    if phone_number_id:
        try:
            t = await Tenant.find_one(Tenant.phone_number_id == phone_number_id)
            if t:
                return t
        except Exception as e:
            log.warning("webhook.tenant_lookup_phone_error", error=str(e))

    # 2. waba_id lookup — fallback
    if waba_id:
        try:
            t = await Tenant.find_one(Tenant.waba_id == waba_id)
            if t:
                return t
        except Exception as e:
            log.warning("webhook.tenant_lookup_waba_error", error=str(e))

    # 3. single-tenant / dev fallback
    try:
        return await Tenant.find_one(Tenant.status == "active")
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  GET  /webhook/whatsapp   —  Meta verification handshake
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/whatsapp")
async def verify_whatsapp(
    hub_mode:         str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge:    str | None = Query(default=None, alias="hub.challenge"),
):
    """
    Meta sends GET ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    when you register or update the webhook URL.  Echo the challenge on success.

    IMPORTANT: Query params use dot-notation (hub.mode), not underscores.
    FastAPI requires Query(alias=...) to capture dot-notation names.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.webhook_verify_token:
        log.info("webhook.verified", url="whatsapp")
        return PlainTextResponse(hub_challenge or "")
    log.warning("webhook.verification_failed",
                received_token=hub_verify_token,
                expected_token=settings.webhook_verify_token[:4] + "****")
    return PlainTextResponse("Forbidden", status_code=403)


# ══════════════════════════════════════════════════════════════════════════════
#  POST /webhook/whatsapp  —  receive all events for all tenants
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/whatsapp")
async def receive_whatsapp(request: Request, background_tasks: BackgroundTasks):
    """
    Single centralised webhook endpoint for all customer WhatsApp accounts.

    Flow:
      1. Validate X-Hub-Signature-256 — reject forged payloads silently
      2. Parse JSON
      3. Fan-out: each change is dispatched independently to a background task
      4. Return 200 immediately — never make Meta wait
    """
    raw_body = await request.body()

    # ── 1. Signature validation ───────────────────────────────────────────────
    sig = request.headers.get("X-Hub-Signature-256")
    if not _verify_signature(raw_body, sig):
        log.warning(
            "webhook.signature_invalid",
            ip=request.client.host if request.client else "unknown",
        )
        return Response(status_code=200)            # always 200 to Meta

    # ── 2. Parse ──────────────────────────────────────────────────────────────
    try:
        body = json.loads(raw_body)
    except Exception:
        log.warning("webhook.parse_error")
        return Response(status_code=200)

    if body.get("object") != "whatsapp_business_account":
        return Response(status_code=200)            # not a WhatsApp event

    # ── 3. Fan-out per change (one background task per phone number's events) ─
    for entry in body.get("entry", []):
        waba_id = entry.get("id", "")
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value           = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")

            log.info(
                "webhook.event",
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                msgs=len(value.get("messages", [])),
                statuses=len(value.get("statuses", [])),
            )

            background_tasks.add_task(
                _process_change, value, waba_id, phone_number_id
            )

    # ── 4. Respond immediately ────────────────────────────────────────────────
    return Response(status_code=200)


# ══════════════════════════════════════════════════════════════════════════════
#  Legacy aliases  GET/POST /webhook  (keep old Meta registration working)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("")
async def verify_legacy(
    hub_mode:         str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge:    str | None = Query(default=None, alias="hub.challenge"),
):
    return await verify_whatsapp(hub_mode, hub_verify_token, hub_challenge)


@router.post("")
async def receive_legacy(request: Request, background_tasks: BackgroundTasks):
    return await receive_whatsapp(request, background_tasks)


@router.get("/{tenant_id}")
async def verify_tenant_legacy(
    tenant_id:        str,
    hub_mode:         str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge:    str | None = Query(default=None, alias="hub.challenge"),
):
    return await verify_whatsapp(hub_mode, hub_verify_token, hub_challenge)


@router.post("/{tenant_id}")
async def receive_tenant_legacy(
    tenant_id: str,
    request:   Request,
    background_tasks: BackgroundTasks,
):
    return await receive_whatsapp(request, background_tasks)


# ══════════════════════════════════════════════════════════════════════════════
#  Background: resolve tenant + process one change block
# ══════════════════════════════════════════════════════════════════════════════

async def _process_change(value: dict, waba_id: str, phone_number_id: str) -> None:
    """
    Runs in background after the HTTP 200 is already sent.

    Resolves the tenant for this phone_number_id, then processes every
    message and status in the change value block.
    """
    tenant = await _resolve_tenant(phone_number_id, waba_id)
    if not tenant:
        log.error(
            "webhook.tenant_not_found",
            phone_number_id=phone_number_id,
            waba_id=waba_id,
        )
        return

    contacts = value.get("contacts", [])

    for msg in value.get("messages", []):
        try:
            await _handle_message(
                tenant, msg, contacts,
                phone_number_id=phone_number_id,
                waba_id=waba_id,
            )
        except Exception as e:
            log.error("webhook.message_error", error=str(e), exc_info=True)

    for status in value.get("statuses", []):
        try:
            await _handle_status(tenant, status)
        except Exception as e:
            log.error("webhook.status_error", error=str(e), exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Message handler
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_message(
    tenant,
    msg:            dict,
    contacts:       list,
    phone_number_id: str = "",
    waba_id:        str  = "",
) -> None:
    from app.database import db
    from datetime import datetime, timedelta

    tid      = str(tenant.id)
    wa_id    = msg.get("from", "")
    msg_id   = msg.get("id",   "")
    msg_type = msg.get("type", "text")
    now      = datetime.utcnow()

    if not wa_id or not msg_id:
        return

    # Deduplication — Meta may deliver the same event more than once
    if await db.messages.find_one({"wa_message_id": msg_id, "tenant_id": tid}):
        log.debug("webhook.duplicate_skipped", msg_id=msg_id)
        return

    # Contact name from the contacts array
    profile_name = next(
        (c.get("profile", {}).get("name", "") for c in contacts if c.get("wa_id") == wa_id),
        "",
    )

    # ── Find or create contact ────────────────────────────────────────────────
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
                {"$set": {"profile_name": profile_name, "updated_at": now}},
            )

    content, preview = _extract_content(msg, msg_type)

    # ── Find or create conversation ───────────────────────────────────────────
    convo = await db.conversations.find_one({
        "tenant_id": tid, "wa_id": wa_id, "status": "open"
    })
    if not convo:
        res = await db.conversations.insert_one({
            "tenant_id":            tid,
            "contact_id":           contact_id,
            "wa_id":                wa_id,
            "phone_number_id":      phone_number_id,
            "waba_id":              waba_id,
            "status":               "open",
            "unread_count":         1,
            "last_message_at":      now,
            "last_message_preview": preview,
            "window_expires_at":    now + timedelta(hours=24),
            "created_at":           now,
            "updated_at":           now,
        })
        convo_id     = str(res.inserted_id)
        is_new_convo = True
    else:
        convo_id     = str(convo["_id"])
        is_new_convo = False
        await db.conversations.update_one(
            {"_id": convo["_id"]},
            {"$set": {
                "last_message_at":      now,
                "last_message_preview": preview,
                "window_expires_at":    now + timedelta(hours=24),
                "updated_at":           now,
            }, "$inc": {"unread_count": 1}},
        )

    # ── Save message ──────────────────────────────────────────────────────────
    res = await db.messages.insert_one({
        "tenant_id":        tid,
        "conversation_id":  convo_id,
        "contact_id":       contact_id,
        "wa_id":            wa_id,
        "wa_message_id":    msg_id,
        "phone_number_id":  phone_number_id,
        "waba_id":          waba_id,
        "direction":        "inbound",
        "msg_type":         msg_type,
        "type":             msg_type,
        "content":          content,
        "status":           "received",
        "created_at":       now,
    })
    message_id = str(res.inserted_id)

    log.info(
        "webhook.message_saved",
        tenant_id=tid,
        wa_id=wa_id,
        msg_type=msg_type,
        phone_number_id=phone_number_id,
        preview=preview[:60],
    )

    # ── Mark as read ──────────────────────────────────────────────────────────
    try:
        from app.services.whatsapp import get_wa_client
        await get_wa_client(tenant).mark_read(msg_id)
    except Exception:
        pass

    # ── WebSocket push ────────────────────────────────────────────────────────
    try:
        from app.api.v1.websocket import broadcast_to_tenant
        await broadcast_to_tenant(tid, {
            "type":            "new_message",
            "conversation_id": convo_id,
            "message": {
                "id":            message_id,
                "direction":     "inbound",
                "type":          msg_type,
                "msg_type":      msg_type,
                "content":       content,
                "wa_message_id": msg_id,
                "status":        "received",
                "created_at":    now.isoformat(),
            },
        })
    except Exception as e:
        log.warning("webhook.ws_push_failed", error=str(e))

    # ── Auto-reply engine ─────────────────────────────────────────────────────
    try:
        import asyncio
        from app.api.v1.autoreplies import run_autoreplies
        await asyncio.sleep(0.3)
        await run_autoreplies(
            tenant       = tenant,
            wa_id        = wa_id,
            contact_id   = contact_id,
            convo_id     = convo_id,
            msg_type     = msg_type,
            content      = content,
            is_new_convo = is_new_convo,
        )
    except Exception as e:
        log.error("webhook.autoreply_error", error=str(e), exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Status handler  (delivered / read / failed / sent)
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_status(tenant, status: dict) -> None:
    from app.database import db
    from datetime import datetime

    tid        = str(tenant.id)
    wa_msg_id  = status.get("id",     "")
    new_status = status.get("status", "")

    if not wa_msg_id or not new_status:
        return

    result = await db.messages.find_one_and_update(
        {"wa_message_id": wa_msg_id, "tenant_id": tid},
        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}},
        return_document=True,
    )

    log.info("webhook.status_update",
             tenant_id=tid, wa_msg_id=wa_msg_id, status=new_status)

    if result:
        try:
            from app.api.v1.websocket import broadcast_to_tenant
            await broadcast_to_tenant(tid, {
                "type":            "status_update",
                "wa_message_id":   wa_msg_id,
                "status":          new_status,
                "conversation_id": str(result.get("conversation_id", "")),
            })
        except Exception:
            pass

        if result.get("broadcast_id") and new_status in ("delivered", "read", "failed"):
            await db.broadcasts.update_one(
                {"_id": result["broadcast_id"]},
                {"$inc": {f"{new_status}_count": 1}},
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Content extractor — all WhatsApp message types
# ══════════════════════════════════════════════════════════════════════════════

def _extract_content(msg: dict, msg_type: str) -> tuple[dict, str]:
    """Return (content_dict, preview_string) for any WhatsApp message type."""

    if msg_type == "text":
        body = msg.get("text", {}).get("body", "")
        return {"body": body}, body[:80]

    if msg_type == "image":
        img = msg.get("image", {})
        cap = img.get("caption", "")
        return ({"image": img, "caption": cap, "mime_type": img.get("mime_type"), "id": img.get("id")},
                f"📷 {cap}" if cap else "📷 Image")

    if msg_type == "video":
        vid = msg.get("video", {})
        cap = vid.get("caption", "")
        return ({"video": vid, "caption": cap, "mime_type": vid.get("mime_type"), "id": vid.get("id")},
                f"🎥 {cap}" if cap else "🎥 Video")

    if msg_type == "audio":
        audio = msg.get("audio", {})
        return ({"audio": audio, "id": audio.get("id"), "mime_type": audio.get("mime_type"),
                 "voice": audio.get("voice", False)},
                "🎤 Voice message" if audio.get("voice") else "🎵 Audio")

    if msg_type == "document":
        doc   = msg.get("document", {})
        fname = doc.get("filename", "Document")
        return ({"document": doc, "filename": fname, "caption": doc.get("caption"),
                 "mime_type": doc.get("mime_type"), "id": doc.get("id")},
                f"📄 {fname}")

    if msg_type == "sticker":
        s = msg.get("sticker", {})
        return ({"sticker": s, "id": s.get("id"), "animated": s.get("animated", False)}, "😊 Sticker")

    if msg_type == "location":
        loc = msg.get("location", {})
        return ({"location": loc, "latitude": loc.get("latitude"), "longitude": loc.get("longitude"),
                 "name": loc.get("name"), "address": loc.get("address")},
                f"📍 {loc.get('name') or loc.get('address') or 'Location'}")

    if msg_type == "contacts":
        cts   = msg.get("contacts", [])
        names = ", ".join(c.get("name", {}).get("formatted_name", "") for c in cts)
        return {"contacts": cts, "names": names}, f"👤 {names or 'Contact'}"

    if msg_type == "reaction":
        r     = msg.get("reaction", {})
        emoji = r.get("emoji", "👍")
        return ({"reaction": r, "emoji": emoji, "message_id": r.get("message_id")},
                f"Reacted {emoji}")

    if msg_type == "button":
        btn = msg.get("button", {})
        return {"body": btn.get("text"), "payload": btn.get("payload")}, f"🔘 {btn.get('text', '')}"

    if msg_type == "interactive":
        r     = msg.get("interactive", {})
        itype = r.get("type", "")

        # WhatsApp Flow response (nfm_reply)
        if itype == "nfm_reply":
            nfm  = r.get("nfm_reply", {})
            name = nfm.get("name", "")
            body = nfm.get("body", "")
            resp_json = nfm.get("response_json", "{}")
            try:
                flow_data = json.loads(resp_json) if isinstance(resp_json, str) else resp_json
            except Exception:
                flow_data = {}
            return (
                {
                    "body":      body or "Flow response received",
                    "type":      "flow_response",
                    "flow_name": name,
                    "flow_data": flow_data,
                    "nfm_reply": nfm,
                },
                f"📋 {name or 'Flow'} response",
            )

        btn_r  = r.get("button_reply", {})
        list_r = r.get("list_reply", {})
        title  = btn_r.get("title") or list_r.get("title") or ""
        return (
            {"body": title, "type": itype, "button_reply": btn_r, "list_reply": list_r},
            f"🔘 {title}",
        )

    if msg_type == "order":
        return {"order": msg.get("order", {})}, "🛒 Order received"

    if msg_type == "system":
        sys_msg = msg.get("system", {})
        return {"body": sys_msg.get("body", ""), "system": sys_msg}, sys_msg.get("body", "System message")

    if msg_type == "unsupported":
        return {"body": "⚠ Unsupported message type"}, "⚠ Unsupported"

    return {"body": f"[{msg_type}]", "raw": msg}, f"[{msg_type}]"
