# """
# app/api/v1/conversations.py  —  Complete conversations API
# """
# from datetime import datetime, timedelta
# from typing   import Optional, List
# from fastapi  import APIRouter, HTTPException, Depends, Query
# from pydantic import BaseModel, Field
# from app.models.tenant    import Tenant
# from app.core.dependencies import get_current_tenant, get_active_tenant

# router = APIRouter(prefix="/conversations", tags=["conversations"])


# # ─── Schemas ──────────────────────────────────────────────────────────────────
# class SendMessageRequest(BaseModel):
#     msg_type:  str = "text"   # text | template | image | video | audio | document | reaction

#     # TEXT
#     text:      str = ""

#     # MEDIA
#     media_url: str = ""
#     media_id:  str = ""
#     filename:  str = ""
#     caption:   str = ""

#     # REACTION
#     emoji:    str = ""
#     reply_to: str = ""        # wa_message_id to reply to

#     # TEMPLATE — runtime values (only when msg_type == "template")
#     template_name:   str  = ""
#     language:        str  = "en_US"
#     header_type:     str  = "none"   # text | image | video | document | none
#     header_text:     str  = ""
#     header_media_id: str  = ""       # Meta media_id from /media/upload
#     header_link:     str  = ""       # fallback public URL
#     header_filename: str  = ""       # for DOCUMENT display name
#     body_variables:  dict = Field(default_factory=dict)   # {"1":"John","2":"ORD-123"}
#     buttons:         list = Field(default_factory=list)   # dynamic button values

#     # Legacy support — old Inbox.jsx sent content: { body, template_name, ... }
#     content: dict = Field(default_factory=dict)


# # ─── List conversations ────────────────────────────────────────────────────────
# @router.get("")
# async def list_conversations(
#     tenant: Tenant = Depends(get_current_tenant),
#     page:   int    = Query(1, ge=1),
#     limit:  int    = Query(50, ge=1, le=100),
#     status: str    = Query(None),
#     search: str    = Query(None),
# ):
#     from app.database import db
#     tid = str(tenant.id)
#     q: dict = {"tenant_id": tid}
#     if status: q["status"] = status
#     if search:
#         q["$or"] = [
#             {"wa_id": {"$regex": search, "$options": "i"}},
#             {"last_message_preview": {"$regex": search, "$options": "i"}},
#         ]
#     total = await db.conversations.count_documents(q)
#     docs  = await (
#         db.conversations.find(q)
#         .sort("last_message_at", -1)
#         .skip((page - 1) * limit)
#         .limit(limit)
#         .to_list(limit)
#     )
#     # Enrich with contact names
#     results = []
#     for d in docs:
#         wa_id   = d.get("wa_id", "")
#         contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
#         results.append({
#             "id":                   str(d["_id"]),
#             "contact_id":           d.get("contact_id", ""),
#             "wa_id":                wa_id,
#             "profile_name":         contact.get("profile_name", wa_id) if contact else wa_id,
#             "status":               d.get("status", "open"),
#             "unread_count":         d.get("unread_count", 0),
#             "last_message_at":      d.get("last_message_at"),
#             "last_message_preview": d.get("last_message_preview", ""),
#             "window_expires_at":    d.get("window_expires_at"),
#             "created_at":           d.get("created_at"),
#         })
#     return {"total": total, "page": page, "conversations": results}


# # ─── Get single conversation ───────────────────────────────────────────────────
# @router.get("/{cid}")
# async def get_conversation(cid: str, tenant: Tenant = Depends(get_current_tenant)):
#     from app.database import db
#     from bson import ObjectId
#     tid = str(tenant.id)
#     doc = await db.conversations.find_one({"_id": ObjectId(cid), "tenant_id": tid})
#     if not doc:
#         raise HTTPException(404, "Conversation not found")
#     wa_id   = doc.get("wa_id", "")
#     contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
#     return {
#         "id":                   str(doc["_id"]),
#         "contact_id":           doc.get("contact_id", ""),
#         "wa_id":                wa_id,
#         "profile_name":         contact.get("profile_name", wa_id) if contact else wa_id,
#         "status":               doc.get("status", "open"),
#         "unread_count":         doc.get("unread_count", 0),
#         "last_message_at":      doc.get("last_message_at"),
#         "last_message_preview": doc.get("last_message_preview", ""),
#         "window_expires_at":    doc.get("window_expires_at"),
#         "created_at":           doc.get("created_at"),
#     }


# # ─── List messages ─────────────────────────────────────────────────────────────
# @router.get("/{cid}/messages")
# async def list_messages(
#     cid:    str,
#     tenant: Tenant = Depends(get_current_tenant),
#     page:   int    = Query(1, ge=1),
#     limit:  int    = Query(50, ge=1, le=100),
# ):
#     from app.database import db
#     from bson import ObjectId
#     tid   = str(tenant.id)
#     q     = {"conversation_id": cid, "tenant_id": tid}
#     total = await db.messages.count_documents(q)
#     docs  = await (
#         db.messages.find(q)
#         .sort("created_at", -1)
#         .skip((page - 1) * limit)
#         .limit(limit)
#         .to_list(limit)
#     )
#     docs.reverse()

#     # Mark conversation as read
#     await db.conversations.update_one(
#         {"_id": ObjectId(cid), "tenant_id": tid},
#         {"$set": {"unread_count": 0}}
#     )

#     # Enrich with reply_to message
#     result = []
#     for m in docs:
#         reply_doc = None
#         if m.get("reply_to"):
#             try:
#                 reply_doc = await db.messages.find_one({"_id": ObjectId(m["reply_to"])})
#                 if reply_doc:
#                     reply_doc = {
#                         "id":        str(reply_doc["_id"]),
#                         "direction": reply_doc.get("direction", ""),
#                         "type":      reply_doc.get("type", reply_doc.get("msg_type", "text")),
#                         "content":   reply_doc.get("content", {}),
#                     }
#             except Exception:
#                 pass
#         result.append({
#             "id":            str(m["_id"]),
#             "wa_message_id": m.get("wa_message_id", ""),
#             "direction":     m.get("direction", ""),
#             "type":          m.get("type", m.get("msg_type", "text")),
#             "msg_type":      m.get("msg_type", m.get("type", "text")),
#             "content":       m.get("content", {}),
#             "reply_to":      reply_doc,
#             "status":        m.get("status", ""),
#             "starred":       m.get("starred", False),
#             "created_at":    m.get("created_at"),
#         })
#     return {"total": total, "page": page, "messages": result}


# # ─── Send message ──────────────────────────────────────────────────────────────
# @router.post("/{cid}/messages")
# async def send_message(
#     cid:    str,
#     body:   SendMessageRequest,
#     tenant: Tenant = Depends(get_active_tenant),
# ):
#     from app.database import db
#     from app.services.whatsapp import get_wa_client, build_send_components
#     from bson import ObjectId

#     tid   = str(tenant.id)
#     convo = await db.conversations.find_one({"_id": ObjectId(cid), "tenant_id": tid})
#     if not convo:
#         raise HTTPException(404, "Conversation not found")

#     wa_id  = convo["wa_id"]
#     client = get_wa_client(tenant)
#     now    = datetime.utcnow()

#     resp        = {}
#     msg_content = {}

#     # ── Support legacy content-dict format AND new flat format ────────────────
#     # Old Inbox.jsx sent: { msg_type:"template", content:{ template_name:"x", components:[] } }
#     # New format sends:   { msg_type:"template", template_name:"x", body_variables:{} }
#     legacy = body.content or {}

#     # ── TEXT ──────────────────────────────────────────────────────────────────
#     if body.msg_type == "text":
#         text = body.text or legacy.get("body", "")
#         if not text.strip():
#             raise HTTPException(400, "text is required")

#         payload = {
#             "messaging_product": "whatsapp",
#             "recipient_type":    "individual",
#             "to":                wa_id,
#             "type":              "text",
#             "text":              {"body": text, "preview_url": False},
#         }
#         if body.reply_to:
#             payload["context"] = {"message_id": body.reply_to}

#         resp = await client._post(f"{client.base}/{client.phone_id}/messages", payload)
#         msg_content = {"body": text}

#     # ── TEMPLATE ──────────────────────────────────────────────────────────────
#     elif body.msg_type == "template":
#         # Resolve template_name from new or legacy format
#         tpl_name = body.template_name or legacy.get("template_name", "")
#         language = body.language or legacy.get("language", "en_US")

#         if not tpl_name.strip():
#             raise HTTPException(400, "template_name is required")

#         # If legacy components already provided (already built), use them directly
#         legacy_components = legacy.get("components", [])

#         if legacy_components:
#             # Legacy format — components already built, just normalize
#             from app.services.whatsapp import normalize_send_components
#             components = normalize_send_components(legacy_components)
#         else:
#             # New flat format — build from individual fields
#             components = build_send_components(
#                 header_type     = body.header_type or legacy.get("header_type", "none"),
#                 header_text     = body.header_text or legacy.get("header_text", ""),
#                 header_media_id = body.header_media_id or legacy.get("header_media_id", ""),
#                 header_link     = body.header_link or legacy.get("header_link", ""),
#                 header_filename = body.header_filename or legacy.get("header_filename", ""),
#                 body_variables  = body.body_variables or legacy.get("variables", {}),
#                 buttons         = body.buttons or legacy.get("buttons", []),
#             )

#         resp = await client.send_template(wa_id, tpl_name, language, components)
#         msg_content = {
#             "template_name":   tpl_name,
#             "language":        language,
#             "header_type":     body.header_type or legacy.get("header_type", ""),
#             "header_text":     body.header_text or legacy.get("header_text", ""),
#             "header_link":     body.header_link or body.header_media_id,
#             "variables":       body.body_variables or legacy.get("variables", {}),
#             "components":      components,
#         }

#     # ── REACTION ──────────────────────────────────────────────────────────────
#     elif body.msg_type == "reaction":
#         emoji     = body.emoji or legacy.get("emoji", "")
#         target_id = body.reply_to or legacy.get("message_id", "")
#         if not emoji or not target_id:
#             raise HTTPException(400, "emoji and reply_to (message_id) are required")

#         resp = await client._post(f"{client.base}/{client.phone_id}/messages", {
#             "messaging_product": "whatsapp",
#             "recipient_type":    "individual",
#             "to":                wa_id,
#             "type":              "reaction",
#             "reaction":          {"message_id": target_id, "emoji": emoji},
#         })
#         msg_content = {"emoji": emoji, "message_id": target_id}

#     # ── MEDIA ─────────────────────────────────────────────────────────────────
#     elif body.msg_type in ("image", "video", "audio", "document", "sticker"):
#         obj: dict = {}
#         mid  = body.media_id  or legacy.get("media_id", "")
#         link = body.media_url or legacy.get("link", "") or legacy.get("url", "")
#         cap  = body.caption   or legacy.get("caption", "")
#         fn   = body.filename  or legacy.get("filename", "")
#         if mid:  obj["id"]       = mid
#         elif link: obj["link"]   = link
#         if cap:  obj["caption"]  = cap
#         if fn:   obj["filename"] = fn

#         payload = {
#             "messaging_product": "whatsapp",
#             "recipient_type":    "individual",
#             "to":                wa_id,
#             "type":              body.msg_type,
#             body.msg_type:       obj,
#         }
#         if body.reply_to:
#             payload["context"] = {"message_id": body.reply_to}

#         resp = await client._post(f"{client.base}/{client.phone_id}/messages", payload)
#         msg_content = {"url": link or mid, "caption": cap, "filename": fn}

#     else:
#         raise HTTPException(400, f"Unsupported msg_type: {body.msg_type}")

#     # ── Check Meta error ──────────────────────────────────────────────────────
#     if "error" in resp:
#         err  = resp["error"]
#         code = err.get("code", 0)
#         msg  = err.get("message", str(err))
#         ERRORS = {
#             131047: "24-hour window closed. Use a template to re-open the conversation.",
#             131026: "Recipient is not a valid WhatsApp number.",
#             132000: f"Template variable mismatch — check that variables match the approved template: {msg}",
#             132001: "Template not found or not approved on Meta.",
#             132005: f"Template body format error: {msg}",
#         }
#         raise HTTPException(400, ERRORS.get(code, f"WhatsApp error ({code}): {msg}"))

#     wa_msg_id = (resp.get("messages") or [{}])[0].get("id", "")

#     # ── Find reply_to message ─────────────────────────────────────────────────
#     reply_msg = None
#     if body.reply_to and body.msg_type not in ("reaction",):
#         reply_msg = await db.messages.find_one({"wa_message_id": body.reply_to, "tenant_id": tid})

#     # ── Save message ──────────────────────────────────────────────────────────
#     msg_doc = {
#         "tenant_id":       tid,
#         "conversation_id": cid,
#         "contact_id":      str(convo.get("contact_id", "")),
#         "wa_id":           wa_id,
#         "wa_message_id":   wa_msg_id,
#         "direction":       "outbound",
#         "type":            body.msg_type,
#         "msg_type":        body.msg_type,
#         "content":         msg_content,
#         "reply_to":        str(reply_msg["_id"]) if reply_msg else None,
#         "status":          "sent",
#         "starred":         False,
#         "created_at":      now,
#     }
#     result = await db.messages.insert_one(msg_doc)

#     # ── Update conversation preview ───────────────────────────────────────────
#     preview = (
#         (body.text or legacy.get("body", ""))[:60]  if body.msg_type == "text"
#         else f"📋 {body.template_name or legacy.get('template_name','')}" if body.msg_type == "template"
#         else f"📎 {body.msg_type}"
#     )
#     await db.conversations.update_one(
#         {"_id": ObjectId(cid)},
#         {"$set": {
#             "last_message_at":      now,
#             "last_message_preview": preview,
#             "window_expires_at":    now + timedelta(hours=24),
#             "updated_at":           now,
#         }}
#     )

#     # ── WebSocket push ────────────────────────────────────────────────────────
#     try:
#         from app.api.v1.websocket import broadcast_to_tenant
#         await broadcast_to_tenant(tid, {
#             "type":            "new_message",
#             "conversation_id": cid,
#             "message": {
#                 "id":          str(result.inserted_id),
#                 "direction":   "outbound",
#                 "type":        body.msg_type,
#                 "msg_type":    body.msg_type,
#                 "content":     msg_content,
#                 "reply_to":    None,
#                 "status":      "sent",
#                 "created_at":  now.isoformat(),
#                 "wa_message_id": wa_msg_id,
#             },
#         })
#     except Exception:
#         pass

#     return {
#         "id":            str(result.inserted_id),
#         "wa_message_id": wa_msg_id,
#         "direction":     "outbound",
#         "type":          body.msg_type,
#         "content":       msg_content,
#         "reply_to":      None,
#         "status":        "sent",
#         "created_at":    now.isoformat(),
#     }


# # ─── Update conversation status ────────────────────────────────────────────────
# @router.patch("/{cid}")
# async def update_conversation(
#     cid:    str,
#     body:   dict,
#     tenant: Tenant = Depends(get_active_tenant),
# ):
#     from app.database import db
#     from bson import ObjectId
#     tid  = str(tenant.id)
#     upd  = {}
#     if "status" in body:   upd["status"]      = body["status"]
#     if "assigned_to" in body: upd["assigned_to"] = body["assigned_to"]
#     if not upd:
#         raise HTTPException(400, "Nothing to update")
#     upd["updated_at"] = datetime.utcnow()
#     r = await db.conversations.update_one(
#         {"_id": ObjectId(cid), "tenant_id": tid}, {"$set": upd}
#     )
#     if r.matched_count == 0:
#         raise HTTPException(404, "Conversation not found")
#     return {"updated": True}


# # ─── Delete message ────────────────────────────────────────────────────────────
# @router.delete("/{cid}/messages/{mid}")
# async def delete_message(
#     cid:    str,
#     mid:    str,
#     tenant: Tenant = Depends(get_active_tenant),
# ):
#     from app.database import db
#     from bson import ObjectId
#     tid = str(tenant.id)
#     r   = await db.messages.delete_one({"_id": ObjectId(mid), "tenant_id": tid})
#     if r.deleted_count == 0:
#         raise HTTPException(404, "Message not found")
#     return {"deleted": True}


# # ─── Start conversation ────────────────────────────────────────────────────────
# class StartConvoRequest(BaseModel):
#     wa_id:             str
#     contact_id:        str  = ""
#     template_name:     str  = ""
#     template_language: str  = "en_US"

# @router.post("/start")
# async def start_conversation(
#     body:   StartConvoRequest,
#     tenant: Tenant = Depends(get_active_tenant),
# ):
#     """
#     Find or create an open conversation for wa_id.
#     Optionally send an opening template message.
#     Returns { conversation, contact }.
#     """
#     from app.database import db
#     from bson import ObjectId

#     tid = str(tenant.id)
#     now = datetime.utcnow()
#     wa_id = body.wa_id.strip().lstrip("+").replace(" ", "")

#     if not wa_id:
#         raise HTTPException(400, "wa_id is required")

#     # ── Find or create contact ────────────────────────────────────────────────
#     contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
#     if not contact:
#         if body.contact_id:
#             try:
#                 contact = await db.contacts.find_one({"_id": ObjectId(body.contact_id), "tenant_id": tid})
#             except Exception:
#                 pass
#     if not contact:
#         res = await db.contacts.insert_one({
#             "tenant_id":    tid,
#             "wa_id":        wa_id,
#             "profile_name": wa_id,
#             "opted_in":     True,
#             "status":       "New",
#             "created_at":   now,
#             "updated_at":   now,
#         })
#         contact_id = str(res.inserted_id)
#     else:
#         contact_id = str(contact["_id"])

#     # ── Find existing open conversation or create new ─────────────────────────
#     convo = await db.conversations.find_one({
#         "tenant_id": tid, "wa_id": wa_id, "status": "open"
#     })

#     if not convo:
#         res = await db.conversations.insert_one({
#             "tenant_id":            tid,
#             "contact_id":           contact_id,
#             "wa_id":                wa_id,
#             "status":               "open",
#             "unread_count":         0,
#             "last_message_at":      now,
#             "last_message_preview": "",
#             "window_expires_at":    now + timedelta(hours=24),
#             "created_at":           now,
#             "updated_at":           now,
#         })
#         convo_id  = str(res.inserted_id)
#         is_new    = True
#     else:
#         convo_id = str(convo["_id"])
#         is_new   = False

#     # ── Send opening template if requested ────────────────────────────────────
#     if body.template_name.strip():
#         try:
#             from app.services.whatsapp import get_wa_client
#             client = get_wa_client(tenant)
#             resp   = await client.send_template(
#                 wa_id,
#                 body.template_name.strip(),
#                 body.template_language or "en_US",
#                 [],   # no variables for opening template
#             )
#             if "error" not in resp:
#                 wa_msg_id = (resp.get("messages") or [{}])[0].get("id", "")
#                 preview   = f"📋 {body.template_name}"
#                 await db.messages.insert_one({
#                     "tenant_id":       tid,
#                     "conversation_id": convo_id,
#                     "contact_id":      contact_id,
#                     "wa_id":           wa_id,
#                     "wa_message_id":   wa_msg_id,
#                     "direction":       "outbound",
#                     "type":            "template",
#                     "msg_type":        "template",
#                     "content":         {"template_name": body.template_name, "language": body.template_language},
#                     "status":          "sent",
#                     "created_at":      now,
#                 })
#                 await db.conversations.update_one(
#                     {"_id": ObjectId(convo_id)},
#                     {"$set": {"last_message_preview": preview, "updated_at": now}}
#                 )
#         except Exception as e:
#             print(f"[START_CONVO] Template send failed: {e}")

#     # ── Return ────────────────────────────────────────────────────────────────
#     convo_doc = await db.conversations.find_one({"_id": ObjectId(convo_id)})
#     contact_doc = await db.contacts.find_one({"_id": ObjectId(contact_id)})

#     return {
#         "conversation": {
#             "id":                   convo_id,
#             "contact_id":           contact_id,
#             "wa_id":                wa_id,
#             "profile_name":         contact_doc.get("profile_name", wa_id) if contact_doc else wa_id,
#             "status":               "open",
#             "unread_count":         0,
#             "last_message_at":      convo_doc.get("last_message_at") if convo_doc else now,
#             "last_message_preview": convo_doc.get("last_message_preview", "") if convo_doc else "",
#             "window_expires_at":    convo_doc.get("window_expires_at") if convo_doc else None,
#             "created_at":           convo_doc.get("created_at") if convo_doc else now,
#         },
#         "contact": {
#             "id":           contact_id,
#             "wa_id":        wa_id,
#             "profile_name": contact_doc.get("profile_name", wa_id) if contact_doc else wa_id,
#         },
#         "is_new": is_new,
#     }

"""
app/api/v1/conversations.py  —  Complete conversations API
"""
from datetime import datetime, timedelta
from typing   import Optional, List
from fastapi  import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from app.models.tenant    import Tenant
from app.core.dependencies import get_current_tenant, get_active_tenant

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ─── Schemas ──────────────────────────────────────────────────────────────────
class SendMessageRequest(BaseModel):
    msg_type:  str = "text"   # text | template | image | video | audio | document | reaction

    # TEXT
    text:      str = ""

    # MEDIA
    media_url: str = ""
    media_id:  str = ""
    filename:  str = ""
    caption:   str = ""

    # REACTION
    emoji:    str = ""
    reply_to: str = ""        # wa_message_id to reply to

    # TEMPLATE — runtime values (only when msg_type == "template")
    template_name:   str  = ""
    language:        str  = "en_US"
    header_type:     str  = "none"   # text | image | video | document | none
    header_text:     str  = ""
    header_media_id: str  = ""       # Meta media_id from /media/upload
    header_link:     str  = ""       # fallback public URL
    header_filename: str  = ""       # for DOCUMENT display name
    body_variables:  dict = Field(default_factory=dict)   # {"1":"John","2":"ORD-123"}
    buttons:         list = Field(default_factory=list)   # dynamic button values

    # Legacy support — old Inbox.jsx sent content: { body, template_name, ... }
    content: dict = Field(default_factory=dict)


# ─── List conversations ────────────────────────────────────────────────────────
@router.get("")
async def list_conversations(
    tenant: Tenant = Depends(get_current_tenant),
    page:   int    = Query(1, ge=1),
    limit:  int    = Query(50, ge=1, le=100),
    status: str    = Query(None),
    search: str    = Query(None),
):
    from app.database import db
    tid = str(tenant.id)
    q: dict = {"tenant_id": tid}
    if status: q["status"] = status
    if search:
        q["$or"] = [
            {"wa_id": {"$regex": search, "$options": "i"}},
            {"last_message_preview": {"$regex": search, "$options": "i"}},
        ]
    total = await db.conversations.count_documents(q)
    docs  = await (
        db.conversations.find(q)
        .sort("last_message_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list(limit)
    )
    # Enrich with contact names
    results = []
    for d in docs:
        wa_id   = d.get("wa_id", "")
        contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
        results.append({
            "id":                   str(d["_id"]),
            "contact_id":           d.get("contact_id", ""),
            "wa_id":                wa_id,
            "profile_name":         contact.get("profile_name", wa_id) if contact else wa_id,
            "status":               d.get("status", "open"),
            "unread_count":         d.get("unread_count", 0),
            "last_message_at":      d.get("last_message_at"),
            "last_message_preview": d.get("last_message_preview", ""),
            "window_expires_at":    d.get("window_expires_at"),
            "created_at":           d.get("created_at"),
        })
    return {"total": total, "page": page, "conversations": results}


# ─── Get single conversation ───────────────────────────────────────────────────
@router.get("/{cid}")
async def get_conversation(cid: str, tenant: Tenant = Depends(get_current_tenant)):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.conversations.find_one({"_id": ObjectId(cid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Conversation not found")
    wa_id   = doc.get("wa_id", "")
    contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
    return {
        "id":                   str(doc["_id"]),
        "contact_id":           doc.get("contact_id", ""),
        "wa_id":                wa_id,
        "profile_name":         contact.get("profile_name", wa_id) if contact else wa_id,
        "status":               doc.get("status", "open"),
        "unread_count":         doc.get("unread_count", 0),
        "last_message_at":      doc.get("last_message_at"),
        "last_message_preview": doc.get("last_message_preview", ""),
        "window_expires_at":    doc.get("window_expires_at"),
        "created_at":           doc.get("created_at"),
    }


# ─── List messages ─────────────────────────────────────────────────────────────
@router.get("/{cid}/messages")
async def list_messages(
    cid:    str,
    tenant: Tenant = Depends(get_current_tenant),
    page:   int    = Query(1, ge=1),
    limit:  int    = Query(50, ge=1, le=100),
):
    from app.database import db
    from bson import ObjectId
    tid   = str(tenant.id)
    q     = {"conversation_id": cid, "tenant_id": tid}
    total = await db.messages.count_documents(q)
    docs  = await (
        db.messages.find(q)
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list(limit)
    )
    docs.reverse()

    # Mark conversation as read
    await db.conversations.update_one(
        {"_id": ObjectId(cid), "tenant_id": tid},
        {"$set": {"unread_count": 0}}
    )

    # Enrich with reply_to message
    result = []
    for m in docs:
        reply_doc = None
        if m.get("reply_to"):
            try:
                reply_doc = await db.messages.find_one({"_id": ObjectId(m["reply_to"])})
                if reply_doc:
                    reply_doc = {
                        "id":        str(reply_doc["_id"]),
                        "direction": reply_doc.get("direction", ""),
                        "type":      reply_doc.get("type", reply_doc.get("msg_type", "text")),
                        "content":   reply_doc.get("content", {}),
                    }
            except Exception:
                pass
        result.append({
            "id":            str(m["_id"]),
            "wa_message_id": m.get("wa_message_id", ""),
            "direction":     m.get("direction", ""),
            "type":          m.get("type", m.get("msg_type", "text")),
            "msg_type":      m.get("msg_type", m.get("type", "text")),
            "content":       m.get("content", {}),
            "reply_to":      reply_doc,
            "status":        m.get("status", ""),
            "starred":       m.get("starred", False),
            "created_at":    m.get("created_at"),
        })
    return {"total": total, "page": page, "messages": result}


# ─── Send message ──────────────────────────────────────────────────────────────
@router.post("/{cid}/messages")
async def send_message(
    cid:    str,
    body:   SendMessageRequest,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    from app.services.whatsapp import get_wa_client, build_send_components
    from bson import ObjectId

    tid   = str(tenant.id)
    convo = await db.conversations.find_one({"_id": ObjectId(cid), "tenant_id": tid})
    if not convo:
        raise HTTPException(404, "Conversation not found")

    wa_id  = convo["wa_id"]
    client = get_wa_client(tenant)
    now    = datetime.utcnow()

    resp        = {}
    msg_content = {}

    # ── Support legacy content-dict format AND new flat format ────────────────
    # Old Inbox.jsx sent: { msg_type:"template", content:{ template_name:"x", components:[] } }
    # New format sends:   { msg_type:"template", template_name:"x", body_variables:{} }
    legacy = body.content or {}

    # ── TEXT ──────────────────────────────────────────────────────────────────
    if body.msg_type == "text":
        text = body.text or legacy.get("body", "")
        if not text.strip():
            raise HTTPException(400, "text is required")

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type":    "individual",
            "to":                wa_id,
            "type":              "text",
            "text":              {"body": text, "preview_url": False},
        }
        if body.reply_to:
            payload["context"] = {"message_id": body.reply_to}

        resp = await client._post(f"{client.base}/{client.phone_id}/messages", payload)
        msg_content = {"body": text}

    # ── TEMPLATE ──────────────────────────────────────────────────────────────
    elif body.msg_type == "template":
        # Resolve template_name from new or legacy format
        tpl_name = body.template_name or legacy.get("template_name", "")
        language = body.language or legacy.get("language", "en_US")

        if not tpl_name.strip():
            raise HTTPException(400, "template_name is required")

        # If legacy components already provided (already built), use them directly
        legacy_components = legacy.get("components", [])

        if legacy_components:
            # Legacy format — components already built, just normalize
            from app.services.whatsapp import normalize_send_components
            components = normalize_send_components(legacy_components)
        else:
            # New flat format — build from individual fields
            raw_vars = body.body_variables or legacy.get("variables", {})

            # For named variables (first_name, order_number etc.) we must send
            # them in the exact order they appear in the template body.
            # Look up the template from DB to get the correct order.
            if raw_vars and not all(k.isdigit() for k in raw_vars.keys()):
                import re as _re
                tpl_doc = await db.templates.find_one({"name": tpl_name})
                if tpl_doc:
                    body_comp = next(
                        (c for c in tpl_doc.get("components", []) if c.get("type") == "BODY"),
                        None
                    )
                    if body_comp:
                        tpl_body_text = body_comp.get("text", "")
                        ordered_keys  = _re.findall(r"\{\{(\w+)\}\}", tpl_body_text)
                        # Rebuild dict in template order
                        raw_vars = {k: raw_vars[k] for k in ordered_keys if k in raw_vars}

            components = build_send_components(
                header_type     = body.header_type or legacy.get("header_type", "none"),
                header_text     = body.header_text or legacy.get("header_text", ""),
                header_media_id = body.header_media_id or legacy.get("header_media_id", ""),
                header_link     = body.header_link or legacy.get("header_link", ""),
                header_filename = body.header_filename or legacy.get("header_filename", ""),
                body_variables  = raw_vars,
                buttons         = body.buttons or legacy.get("buttons", []),
            )

        resp = await client.send_template(wa_id, tpl_name, language, components)
        msg_content = {
            "template_name":   tpl_name,
            "language":        language,
            "header_type":     body.header_type or legacy.get("header_type", ""),
            "header_text":     body.header_text or legacy.get("header_text", ""),
            "header_link":     body.header_link or body.header_media_id,
            "variables":       body.body_variables or legacy.get("variables", {}),
            "components":      components,
        }

    # ── REACTION ──────────────────────────────────────────────────────────────
    elif body.msg_type == "reaction":
        emoji     = body.emoji or legacy.get("emoji", "")
        target_id = body.reply_to or legacy.get("message_id", "")
        if not emoji or not target_id:
            raise HTTPException(400, "emoji and reply_to (message_id) are required")

        resp = await client._post(f"{client.base}/{client.phone_id}/messages", {
            "messaging_product": "whatsapp",
            "recipient_type":    "individual",
            "to":                wa_id,
            "type":              "reaction",
            "reaction":          {"message_id": target_id, "emoji": emoji},
        })
        msg_content = {"emoji": emoji, "message_id": target_id}

    # ── MEDIA ─────────────────────────────────────────────────────────────────
    elif body.msg_type in ("image", "video", "audio", "document", "sticker"):
        obj: dict = {}
        mid  = body.media_id  or legacy.get("media_id", "")
        link = body.media_url or legacy.get("link", "") or legacy.get("url", "")
        cap  = body.caption   or legacy.get("caption", "")
        fn   = body.filename  or legacy.get("filename", "")
        if mid:  obj["id"]       = mid
        elif link: obj["link"]   = link
        if cap:  obj["caption"]  = cap
        if fn:   obj["filename"] = fn

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type":    "individual",
            "to":                wa_id,
            "type":              body.msg_type,
            body.msg_type:       obj,
        }
        if body.reply_to:
            payload["context"] = {"message_id": body.reply_to}

        resp = await client._post(f"{client.base}/{client.phone_id}/messages", payload)
        msg_content = {"url": link or mid, "caption": cap, "filename": fn}

    else:
        raise HTTPException(400, f"Unsupported msg_type: {body.msg_type}")

    # ── Check Meta error ──────────────────────────────────────────────────────
    if "error" in resp:
        err  = resp["error"]
        code = err.get("code", 0)
        msg  = err.get("message", str(err))
        ERRORS = {
            131047: "24-hour window closed. Use a template to re-open the conversation.",
            131026: "Recipient is not a valid WhatsApp number.",
            132000: f"Template variable mismatch — check that variables match the approved template: {msg}",
            132001: "Template not found or not approved on Meta.",
            132005: f"Template body format error: {msg}",
        }
        raise HTTPException(400, ERRORS.get(code, f"WhatsApp error ({code}): {msg}"))

    wa_msg_id = (resp.get("messages") or [{}])[0].get("id", "")

    # ── Find reply_to message ─────────────────────────────────────────────────
    reply_msg = None
    if body.reply_to and body.msg_type not in ("reaction",):
        reply_msg = await db.messages.find_one({"wa_message_id": body.reply_to, "tenant_id": tid})

    # ── Save message ──────────────────────────────────────────────────────────
    msg_doc = {
        "tenant_id":       tid,
        "conversation_id": cid,
        "contact_id":      str(convo.get("contact_id", "")),
        "wa_id":           wa_id,
        "wa_message_id":   wa_msg_id,
        "direction":       "outbound",
        "type":            body.msg_type,
        "msg_type":        body.msg_type,
        "content":         msg_content,
        "reply_to":        str(reply_msg["_id"]) if reply_msg else None,
        "status":          "sent",
        "starred":         False,
        "created_at":      now,
    }
    result = await db.messages.insert_one(msg_doc)

    # ── Update conversation preview ───────────────────────────────────────────
    preview = (
        (body.text or legacy.get("body", ""))[:60]  if body.msg_type == "text"
        else f"📋 {body.template_name or legacy.get('template_name','')}" if body.msg_type == "template"
        else f"📎 {body.msg_type}"
    )
    await db.conversations.update_one(
        {"_id": ObjectId(cid)},
        {"$set": {
            "last_message_at":      now,
            "last_message_preview": preview,
            "window_expires_at":    now + timedelta(hours=24),
            "updated_at":           now,
        }}
    )

    # ── WebSocket push ────────────────────────────────────────────────────────
    try:
        from app.api.v1.websocket import broadcast_to_tenant
        await broadcast_to_tenant(tid, {
            "type":            "new_message",
            "conversation_id": cid,
            "message": {
                "id":          str(result.inserted_id),
                "direction":   "outbound",
                "type":        body.msg_type,
                "msg_type":    body.msg_type,
                "content":     msg_content,
                "reply_to":    None,
                "status":      "sent",
                "created_at":  now.isoformat(),
                "wa_message_id": wa_msg_id,
            },
        })
    except Exception:
        pass

    return {
        "id":            str(result.inserted_id),
        "wa_message_id": wa_msg_id,
        "direction":     "outbound",
        "type":          body.msg_type,
        "content":       msg_content,
        "reply_to":      None,
        "status":        "sent",
        "created_at":    now.isoformat(),
    }


# ─── Update conversation status ────────────────────────────────────────────────
@router.patch("/{cid}")
async def update_conversation(
    cid:    str,
    body:   dict,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    from bson import ObjectId
    tid  = str(tenant.id)
    upd  = {}
    if "status" in body:   upd["status"]      = body["status"]
    if "assigned_to" in body: upd["assigned_to"] = body["assigned_to"]
    if not upd:
        raise HTTPException(400, "Nothing to update")
    upd["updated_at"] = datetime.utcnow()
    r = await db.conversations.update_one(
        {"_id": ObjectId(cid), "tenant_id": tid}, {"$set": upd}
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Conversation not found")
    return {"updated": True}


# ─── Delete message ────────────────────────────────────────────────────────────
@router.delete("/{cid}/messages/{mid}")
async def delete_message(
    cid:    str,
    mid:    str,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    r   = await db.messages.delete_one({"_id": ObjectId(mid), "tenant_id": tid})
    if r.deleted_count == 0:
        raise HTTPException(404, "Message not found")
    return {"deleted": True}


# ─── Start conversation ────────────────────────────────────────────────────────
class StartConvoRequest(BaseModel):
    wa_id:             str
    contact_id:        str  = ""
    template_name:     str  = ""
    template_language: str  = "en_US"

@router.post("/start")
async def start_conversation(
    body:   StartConvoRequest,
    tenant: Tenant = Depends(get_active_tenant),
):
    """
    Find or create an open conversation for wa_id.
    Optionally send an opening template message.
    Returns { conversation, contact }.
    """
    from app.database import db
    from bson import ObjectId

    tid = str(tenant.id)
    now = datetime.utcnow()
    wa_id = body.wa_id.strip().lstrip("+").replace(" ", "")

    if not wa_id:
        raise HTTPException(400, "wa_id is required")

    # ── Find or create contact ────────────────────────────────────────────────
    contact = await db.contacts.find_one({"tenant_id": tid, "wa_id": wa_id})
    if not contact:
        if body.contact_id:
            try:
                contact = await db.contacts.find_one({"_id": ObjectId(body.contact_id), "tenant_id": tid})
            except Exception:
                pass
    if not contact:
        res = await db.contacts.insert_one({
            "tenant_id":    tid,
            "wa_id":        wa_id,
            "profile_name": wa_id,
            "opted_in":     True,
            "status":       "New",
            "created_at":   now,
            "updated_at":   now,
        })
        contact_id = str(res.inserted_id)
    else:
        contact_id = str(contact["_id"])

    # ── Find existing open conversation or create new ─────────────────────────
    convo = await db.conversations.find_one({
        "tenant_id": tid, "wa_id": wa_id, "status": "open"
    })

    if not convo:
        res = await db.conversations.insert_one({
            "tenant_id":            tid,
            "contact_id":           contact_id,
            "wa_id":                wa_id,
            "status":               "open",
            "unread_count":         0,
            "last_message_at":      now,
            "last_message_preview": "",
            "window_expires_at":    now + timedelta(hours=24),
            "created_at":           now,
            "updated_at":           now,
        })
        convo_id  = str(res.inserted_id)
        is_new    = True
    else:
        convo_id = str(convo["_id"])
        is_new   = False

    # ── Send opening template if requested ────────────────────────────────────
    if body.template_name.strip():
        try:
            from app.services.whatsapp import get_wa_client
            client = get_wa_client(tenant)
            resp   = await client.send_template(
                wa_id,
                body.template_name.strip(),
                body.template_language or "en_US",
                [],   # no variables for opening template
            )
            if "error" not in resp:
                wa_msg_id = (resp.get("messages") or [{}])[0].get("id", "")
                preview   = f"📋 {body.template_name}"
                await db.messages.insert_one({
                    "tenant_id":       tid,
                    "conversation_id": convo_id,
                    "contact_id":      contact_id,
                    "wa_id":           wa_id,
                    "wa_message_id":   wa_msg_id,
                    "direction":       "outbound",
                    "type":            "template",
                    "msg_type":        "template",
                    "content":         {"template_name": body.template_name, "language": body.template_language},
                    "status":          "sent",
                    "created_at":      now,
                })
                await db.conversations.update_one(
                    {"_id": ObjectId(convo_id)},
                    {"$set": {"last_message_preview": preview, "updated_at": now}}
                )
        except Exception as e:
            print(f"[START_CONVO] Template send failed: {e}")

    # ── Return ────────────────────────────────────────────────────────────────
    convo_doc = await db.conversations.find_one({"_id": ObjectId(convo_id)})
    contact_doc = await db.contacts.find_one({"_id": ObjectId(contact_id)})

    return {
        "conversation": {
            "id":                   convo_id,
            "contact_id":           contact_id,
            "wa_id":                wa_id,
            "profile_name":         contact_doc.get("profile_name", wa_id) if contact_doc else wa_id,
            "status":               "open",
            "unread_count":         0,
            "last_message_at":      convo_doc.get("last_message_at") if convo_doc else now,
            "last_message_preview": convo_doc.get("last_message_preview", "") if convo_doc else "",
            "window_expires_at":    convo_doc.get("window_expires_at") if convo_doc else None,
            "created_at":           convo_doc.get("created_at") if convo_doc else now,
        },
        "contact": {
            "id":           contact_id,
            "wa_id":        wa_id,
            "profile_name": contact_doc.get("profile_name", wa_id) if contact_doc else wa_id,
        },
        "is_new": is_new,
    }