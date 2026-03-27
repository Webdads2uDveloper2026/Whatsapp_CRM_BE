"""
app/api/v1/broadcasts.py

Fixes:
1. Template send payload uses correct Meta Cloud API component format
2. Each sent message is saved to a conversation so it appears in Inbox
3. View single broadcast details
4. Edit broadcast (draft only)
5. Delete broadcast
6. Get contacts list for a broadcast
7. Reset stuck broadcast
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from pydantic import BaseModel
from app.models.tenant import Tenant
from app.core.dependencies import get_current_tenant, get_active_tenant

router = APIRouter(prefix="/broadcasts", tags=["broadcasts"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class CreateBroadcastRequest(BaseModel):
    name:                 str
    template_name:        str
    template_language:    str       = "en_US"
    audience_type:        str       = "all"       # all | tag | contact_ids
    audience_tags:        List[str] = []
    audience_contact_ids: List[str] = []
    components:           list      = []           # Legacy — kept for compat
    # New: variables for body params e.g. {"1": "John", "name": "John"}
    variables:            dict      = {}
    header_text:          str       = ""
    header_media:         str       = ""
    header_type:          str       = ""
    button_payloads:      list      = []
    schedule_type:        str       = "draft"      # draft | now | later
    scheduled_at:         Optional[str] = None


class EditBroadcastRequest(BaseModel):
    name:                 Optional[str]       = None
    template_name:        Optional[str]       = None
    template_language:    Optional[str]       = None
    audience_type:        Optional[str]       = None
    audience_tags:        Optional[List[str]] = None
    audience_contact_ids: Optional[List[str]] = None
    components:           Optional[list]      = None
    scheduled_at:         Optional[str]       = None


# ── Create ────────────────────────────────────────────────────────────────────
@router.post("", status_code=201)
async def create_broadcast(
    body: CreateBroadcastRequest,
    bg:   BackgroundTasks,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    tid = str(tenant.id)

    doc = {
        "tenant_id":            tid,
        "name":                 body.name,
        "template_name":        body.template_name,
        "template_language":    body.template_language,
        "audience_type":        body.audience_type,
        "audience_tags":        body.audience_tags,
        "audience_contact_ids": body.audience_contact_ids,
        "components":           body.components,
        "variables":            body.variables,        # ← body variable values
        "header_type":          body.header_type,
        "header_text":          body.header_text,
        "header_media":         body.header_media,
        "button_payloads":      body.button_payloads,
        "status":               "draft",
        "total_recipients":     0,
        "sent_count":           0,
        "delivered_count":      0,
        "read_count":           0,
        "failed_count":         0,
        "scheduled_at":         body.scheduled_at,
        "created_at":           datetime.utcnow(),
        "updated_at":           datetime.utcnow(),
    }
    result = await db.broadcasts.insert_one(doc)
    bid    = str(result.inserted_id)
    doc["id"] = bid

    # Do NOT auto-send here — frontend calls /send explicitly after create
    # This prevents double-send when schedule_type="now"
    return _s(doc)


# ── List ──────────────────────────────────────────────────────────────────────
@router.get("")
async def list_broadcasts(
    tenant: Tenant = Depends(get_current_tenant),
    page:   int    = Query(1, ge=1),
    limit:  int    = Query(50, ge=1, le=100),
    status: Optional[str] = Query(None),
):
    from app.database import db
    tid = str(tenant.id)
    q   = {"tenant_id": tid}
    if status:
        q["status"] = status

    total = await db.broadcasts.count_documents(q)
    docs  = await (
        db.broadcasts.find(q)
        .sort("created_at", -1)
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list(limit)
    )
    return {"total": total, "page": page, "broadcasts": [_s(d) for d in docs]}


# ── Get one ───────────────────────────────────────────────────────────────────
@router.get("/{bid}")
async def get_broadcast(bid: str, tenant: Tenant = Depends(get_current_tenant)):
    from app.database import db
    from bson import ObjectId
    doc = await db.broadcasts.find_one({"_id": ObjectId(bid), "tenant_id": str(tenant.id)})
    if not doc:
        raise HTTPException(404, "Broadcast not found")
    return _s(doc)


# ── Edit (draft only) ─────────────────────────────────────────────────────────
@router.patch("/{bid}")
async def edit_broadcast(
    bid:  str,
    body: EditBroadcastRequest,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)

    doc = await db.broadcasts.find_one({"_id": ObjectId(bid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Broadcast not found")
    if doc["status"] != "draft":
        raise HTTPException(400, f"Can only edit draft broadcasts (current: {doc['status']})")

    updates: dict = {"updated_at": datetime.utcnow()}
    for field in ["name","template_name","template_language","audience_type","audience_tags","audience_contact_ids","components","scheduled_at"]:
        val = getattr(body, field)
        if val is not None:
            updates[field] = val

    await db.broadcasts.update_one({"_id": ObjectId(bid)}, {"$set": updates})
    updated = await db.broadcasts.find_one({"_id": ObjectId(bid)})
    return _s(updated)


# ── Delete ────────────────────────────────────────────────────────────────────
@router.delete("/{bid}")
async def delete_broadcast(bid: str, tenant: Tenant = Depends(get_current_tenant)):
    from app.database import db
    from bson import ObjectId
    r = await db.broadcasts.delete_one({"_id": ObjectId(bid), "tenant_id": str(tenant.id)})
    if r.deleted_count == 0:
        raise HTTPException(404, "Broadcast not found")
    return {"message": "Deleted", "id": bid}


# ── Get contacts for a broadcast ──────────────────────────────────────────────
@router.get("/{bid}/contacts")
async def broadcast_contacts(
    bid:    str,
    page:   int = Query(1, ge=1),
    limit:  int = Query(50, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
):
    """
    Return the contact list that was / will be targeted by this broadcast.
    """
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)

    doc = await db.broadcasts.find_one({"_id": ObjectId(bid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Broadcast not found")

    # Build same query used during send
    q: dict = {"tenant_id": tid, "opted_in": True}
    if doc["audience_type"] == "tag" and doc.get("audience_tags"):
        q["tags"] = {"$in": doc["audience_tags"]}
    elif doc["audience_type"] == "contact_ids" and doc.get("audience_contact_ids"):
        q["_id"] = {"$in": [ObjectId(cid) for cid in doc["audience_contact_ids"]]}

    total    = await db.contacts.count_documents(q)
    contacts = await db.contacts.find(q).skip((page-1)*limit).limit(limit).to_list(limit)

    return {
        "total":    total,
        "page":     page,
        "contacts": [
            {
                "id":           str(c["_id"]),
                "profile_name": c.get("profile_name",""),
                "wa_id":        c.get("wa_id",""),
                "tags":         c.get("tags",[]),
                "opted_in":     c.get("opted_in", False),
                "status":       c.get("status",""),
            }
            for c in contacts
        ]
    }


# ── Send ──────────────────────────────────────────────────────────────────────
@router.post("/{bid}/send")
async def send_broadcast(
    bid: str,
    bg:  BackgroundTasks,
    tenant: Tenant = Depends(get_active_tenant),
):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)

    doc = await db.broadcasts.find_one({"_id": ObjectId(bid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Broadcast not found")

    # Only block re-sending if already completed
    if doc["status"] == "completed":
        raise HTTPException(400, "Already completed. Duplicate it to send again.")

    await db.broadcasts.update_one(
        {"_id": ObjectId(bid)},
        {"$set": {"status": "queued", "updated_at": datetime.utcnow()}}
    )
    bg.add_task(_run_broadcast, bid, tid)
    return {"message": "Broadcast queued", "id": bid, "status": "queued"}


# ── Reset (unstick running/failed) ────────────────────────────────────────────
@router.post("/{bid}/reset")
async def reset_broadcast(bid: str, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)

    doc = await db.broadcasts.find_one({"_id": ObjectId(bid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Broadcast not found")
    if doc["status"] == "completed":
        raise HTTPException(400, "Completed broadcasts cannot be reset. Duplicate instead.")

    await db.broadcasts.update_one(
        {"_id": ObjectId(bid)},
        {"$set": {"status": "draft", "updated_at": datetime.utcnow()}}
    )
    return {"message": "Reset to draft", "id": bid}


# ── Background sender ─────────────────────────────────────────────────────────
async def _run_broadcast(bid: str, tenant_id: str):
    """
    Send broadcast. For each contact:
    1. Send template via Meta API (correct component format)
    2. Find or create a Conversation for that wa_id
    3. Save Message with conversation_id → shows in Inbox
    4. Update conversation last_message_preview
    """
    from app.database import db
    from app.models.tenant import Tenant as TModel
    from app.services.whatsapp import get_wa_client
    from bson import ObjectId

    try:
        doc    = await db.broadcasts.find_one({"_id": ObjectId(bid)})
        tenant = await TModel.get(tenant_id)
        if not doc or not tenant:
            return

        await db.broadcasts.update_one(
            {"_id": ObjectId(bid)},
            {"$set": {"status": "running", "updated_at": datetime.utcnow()}}
        )

        # Build audience
        q: dict = {"tenant_id": tenant_id, "opted_in": True}
        if doc["audience_type"] == "tag" and doc.get("audience_tags"):
            q["tags"] = {"$in": doc["audience_tags"]}
        elif doc["audience_type"] == "contact_ids" and doc.get("audience_contact_ids"):
            q["_id"] = {"$in": [ObjectId(c) for c in doc["audience_contact_ids"]]}

        contacts = await db.contacts.find(q).to_list(10000)
        await db.broadcasts.update_one(
            {"_id": ObjectId(bid)},
            {"$set": {"total_recipients": len(contacts)}}
        )

        client    = get_wa_client(tenant)
        sent = failed = 0
        now  = datetime.utcnow()
        tpl_name = doc["template_name"]
        preview  = f"📋 {tpl_name}"

        for contact in contacts:
            wa_id      = contact.get("wa_id", "")
            contact_id = str(contact["_id"])
            if not wa_id:
                continue

            try:
                # Build components using whatsapp.py builder
                from app.services.whatsapp import build_send_components
                import re as _re

                raw_vars = doc.get("variables") or {}

                # If broadcast has no variables but template needs them,
                # look up template from DB to get variable count & order
                tpl_doc = await db.templates.find_one({"name": tpl_name})
                if tpl_doc:
                    body_comp = next(
                        (c for c in tpl_doc.get("components", []) if c.get("type") == "BODY"),
                        None
                    )
                    if body_comp:
                        tpl_body_text = body_comp.get("text", "")
                        tpl_var_keys  = _re.findall(r"\{\{(\w+)\}\}", tpl_body_text)
                        if tpl_var_keys and not raw_vars:
                            print(f"[BROADCAST] ⚠ Template '{tpl_name}' needs vars {tpl_var_keys} but broadcast has none — skipping contact")
                            failed += 1
                            continue
                        # Reorder named vars to match template order
                        if raw_vars and not all(k.isdigit() for k in raw_vars.keys()):
                            raw_vars = {k: raw_vars[k] for k in tpl_var_keys if k in raw_vars}

                send_components = build_send_components(
                    header_type     = doc.get("header_type", "none") or "none",
                    header_text     = doc.get("header_text", ""),
                    header_media_id = "",
                    header_link     = doc.get("header_media", ""),
                    body_variables  = raw_vars,
                    buttons         = doc.get("button_payloads") or [],
                )
                print(f"[BROADCAST] Sending to +{wa_id} | tpl={tpl_name} | vars={raw_vars} | components={send_components}")
                resp = await client.send_template(
                    wa_id,
                    tpl_name,
                    doc.get("template_language", "en_US"),
                    send_components,
                )

                if "error" in resp:
                    failed += 1
                    err = resp["error"]
                    print(f"[BROADCAST] Meta error {err.get('code')} for {wa_id}: {err.get('message')}")
                    continue

                wa_msg_id = resp.get("messages", [{}])[0].get("id", "")
                sent += 1

                # ── Find or create conversation ────────────────────────────
                convo = await db.conversations.find_one({
                    "tenant_id": tenant_id,
                    "wa_id":     wa_id,
                    "status":    "open",
                })

                if not convo:
                    ins = await db.conversations.insert_one({
                        "tenant_id":            tenant_id,
                        "contact_id":           contact_id,
                        "wa_id":                wa_id,
                        "status":               "open",
                        "unread_count":         0,
                        "last_message_at":      now,
                        "last_message_preview": preview,
                        "window_expires_at":    now + timedelta(hours=24),
                        "created_at":           now,
                        "updated_at":           now,
                    })
                    convo_id = str(ins.inserted_id)
                else:
                    convo_id = str(convo["_id"])
                    await db.conversations.update_one(
                        {"_id": convo["_id"]},
                        {"$set": {
                            "last_message_at":      now,
                            "last_message_preview": preview,
                            "window_expires_at":    now + timedelta(hours=24),
                            "updated_at":           now,
                        }}
                    )

                # ── Save message so it shows in Inbox ──────────────────────
                await db.messages.insert_one({
                    "tenant_id":       tenant_id,
                    "conversation_id": convo_id,
                    "contact_id":      contact_id,
                    "broadcast_id":    bid,
                    "wa_id":           wa_id,
                    "wa_message_id":   wa_msg_id,
                    "direction":       "outbound",
                    "msg_type":        "template",
                    "type":            "template",
                    "content": {
                        "template_name": tpl_name,
                        "language":      doc.get("template_language", "en_US"),
                        "body":          preview,
                        "components":    doc.get("components", []),
                    },
                    "status":     "sent",
                    "created_at": now,
                })

            except Exception as e:
                print(f"[BROADCAST] Exception for {wa_id}: {e}")
                failed += 1

            await asyncio.sleep(0.05)   # ~20 msg/sec

        # Mark done
        await db.broadcasts.update_one(
            {"_id": ObjectId(bid)},
            {"$set": {
                "status":       "completed",
                "sent_count":   sent,
                "failed_count": failed,
                "updated_at":   datetime.utcnow(),
            }}
        )
        print(f"[BROADCAST] {bid} done — sent:{sent} failed:{failed}")

    except Exception as e:
        print(f"[BROADCAST] Fatal: {e}")
        import traceback; traceback.print_exc()
        try:
            from bson import ObjectId as OID
            await db.broadcasts.update_one(
                {"_id": OID(bid)},
                {"$set": {"status": "failed", "updated_at": datetime.utcnow()}}
            )
        except Exception:
            pass


# ── Serializer ────────────────────────────────────────────────────────────────
def _s(d: dict) -> dict:
    return {
        "id":                   str(d.get("_id", d.get("id", ""))),
        "name":                 d.get("name", ""),
        "template_name":        d.get("template_name", ""),
        "template_language":    d.get("template_language", "en_US"),
        "audience_type":        d.get("audience_type", "all"),
        "audience_tags":        d.get("audience_tags", []),
        "audience_contact_ids": d.get("audience_contact_ids", []),
        "components":           d.get("components", []),
        "status":               d.get("status", "draft"),
        "total_recipients":     d.get("total_recipients", 0),
        "sent_count":           d.get("sent_count", 0),
        "delivered_count":      d.get("delivered_count", 0),
        "read_count":           d.get("read_count", 0),
        "failed_count":         d.get("failed_count", 0),
        "scheduled_at":         d.get("scheduled_at"),
        "created_at":           d.get("created_at"),
        "updated_at":           d.get("updated_at"),
    }


# ── Force reset any stuck "running" broadcasts on startup ─────────────────────
async def reset_stuck_broadcasts():
    """Call this from app startup to unstick any broadcasts left in 'running' state."""
    import app.database as _db_module
    db = _db_module.db
    if not db:
        return
    r = await db.broadcasts.update_many(
        {"status": "running"},
        {"$set": {"status": "draft", "updated_at": datetime.utcnow()}}
    )
    if r.modified_count:
        print(f"[BROADCAST] Reset {r.modified_count} stuck broadcast(s) to draft")