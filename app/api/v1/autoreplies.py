"""
app/api/v1/autoreplies.py  —  Auto-reply rules engine
"""
import traceback
from datetime import datetime
from typing   import List, Optional

from bson     import ObjectId
from fastapi  import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.models.tenant     import Tenant
from app.core.dependencies import get_current_tenant, get_active_tenant

router = APIRouter(prefix="/autoreplies", tags=["autoreplies"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class Trigger(BaseModel):
    type:     str       = "any"       # any | keyword | first_message | outside_hours
    keywords: List[str] = []
    match:    str       = "contains"  # contains | exact | starts_with

class Action(BaseModel):
    type:          str  = "text"      # text | template
    text:          str  = ""
    template_name: str  = ""
    language:      str  = "en_US"
    variables:     dict = Field(default_factory=dict)

class Conditions(BaseModel):
    only_first_message: bool = False
    cooldown_minutes:   int  = 0

class AutoReplyCreate(BaseModel):
    name:       str
    is_active:  bool       = True
    priority:   int        = 10
    trigger:    Trigger    = Field(default_factory=Trigger)
    action:     Action     = Field(default_factory=Action)
    conditions: Conditions = Field(default_factory=Conditions)

class AutoReplyUpdate(BaseModel):
    name:       Optional[str]        = None
    is_active:  Optional[bool]       = None
    priority:   Optional[int]        = None
    trigger:    Optional[Trigger]    = None
    action:     Optional[Action]     = None
    conditions: Optional[Conditions] = None


# ── CRUD endpoints ────────────────────────────────────────────────────────────
@router.get("")
async def list_autoreplies(tenant: Tenant = Depends(get_current_tenant)):
    from app.database import db
    docs = await db.autoreplies.find(
        {"tenant_id": str(tenant.id)}
    ).sort("priority", 1).to_list(100)
    return {"autoreplies": [_fmt(d) for d in docs]}


@router.post("")
async def create_autoreply(body: AutoReplyCreate, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    now = datetime.utcnow()
    doc = {
        "tenant_id":  str(tenant.id),
        "name":       body.name,
        "is_active":  body.is_active,
        "priority":   body.priority,
        "trigger":    body.trigger.model_dump(),
        "action":     body.action.model_dump(),
        "conditions": body.conditions.model_dump(),
        "stats":      {"sent": 0, "last_triggered": None},
        "created_at": now,
        "updated_at": now,
    }
    res = await db.autoreplies.insert_one(doc)
    doc["_id"] = res.inserted_id
    return _fmt(doc)


@router.get("/{rule_id}")
async def get_autoreply(rule_id: str, tenant: Tenant = Depends(get_current_tenant)):
    from app.database import db
    doc = await db.autoreplies.find_one({"_id": ObjectId(rule_id), "tenant_id": str(tenant.id)})
    if not doc:
        raise HTTPException(404, "Not found")
    return _fmt(doc)


@router.patch("/{rule_id}/toggle")
async def toggle_autoreply(rule_id: str, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    doc = await db.autoreplies.find_one({"_id": ObjectId(rule_id), "tenant_id": str(tenant.id)})
    if not doc:
        raise HTTPException(404, "Not found")
    new_state = not doc.get("is_active", True)
    await db.autoreplies.update_one(
        {"_id": ObjectId(rule_id)},
        {"$set": {"is_active": new_state, "updated_at": datetime.utcnow()}}
    )
    return {"is_active": new_state}


@router.patch("/{rule_id}")
async def update_autoreply(rule_id: str, body: AutoReplyUpdate, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    upd = {"updated_at": datetime.utcnow()}
    if body.name       is not None: upd["name"]       = body.name
    if body.is_active  is not None: upd["is_active"]  = body.is_active
    if body.priority   is not None: upd["priority"]   = body.priority
    if body.trigger    is not None: upd["trigger"]    = body.trigger.model_dump()
    if body.action     is not None: upd["action"]     = body.action.model_dump()
    if body.conditions is not None: upd["conditions"] = body.conditions.model_dump()
    r = await db.autoreplies.update_one(
        {"_id": ObjectId(rule_id), "tenant_id": str(tenant.id)},
        {"$set": upd}
    )
    if r.matched_count == 0:
        raise HTTPException(404, "Not found")
    return {"updated": True}


@router.delete("/{rule_id}")
async def delete_autoreply(rule_id: str, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    r = await db.autoreplies.delete_one({"_id": ObjectId(rule_id), "tenant_id": str(tenant.id)})
    if r.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"deleted": True}


# ── Engine ────────────────────────────────────────────────────────────────────
async def run_autoreplies(
    *,
    tenant,
    wa_id:        str,
    contact_id:   str,
    convo_id:     str,
    msg_type:     str,
    content:      dict,
    is_new_convo: bool,
):
    """
    Called from webhook._handle_message() after every inbound message is saved.
    Evaluates active rules in priority order. Fires the FIRST match.
    """
    from app.database import db

    tid = str(tenant.id)
    now = datetime.utcnow()

    # ── Step 1: load rules ────────────────────────────────────────────────────
    rules = await db.autoreplies.find(
        {"tenant_id": tid, "is_active": True}
    ).sort("priority", 1).to_list(50)

    print(f"[AUTOREPLY] tenant={tid} | rules={len(rules)} | wa_id={wa_id} | new={is_new_convo}")
    if not rules:
        return

    msg_text = (content.get("body") or content.get("caption") or "").lower().strip()

    for rule in rules:
        trigger    = rule.get("trigger",    {})
        action     = rule.get("action",     {})
        conditions = rule.get("conditions", {})
        rname      = rule.get("name", str(rule["_id"]))

        # ── Guard: only_first_message ─────────────────────────────────────
        if conditions.get("only_first_message") and not is_new_convo:
            print(f"[AUTOREPLY] '{rname}' skip — not first message")
            continue

        # ── Guard: cooldown ───────────────────────────────────────────────
        cooldown = int(conditions.get("cooldown_minutes") or 0)
        if cooldown > 0:
            last = rule.get("stats", {}).get("last_triggered")
            if last:
                if isinstance(last, str):
                    try: last = datetime.fromisoformat(last)
                    except: last = None
                if last and (now - last).total_seconds() < cooldown * 60:
                    print(f"[AUTOREPLY] '{rname}' skip — cooldown active")
                    continue

        # ── Trigger check ─────────────────────────────────────────────────
        t_type  = trigger.get("type", "any")
        matched = False

        if t_type == "any":
            matched = True
        elif t_type == "first_message":
            matched = is_new_convo
        elif t_type == "keyword":
            keywords   = [k.lower().strip() for k in trigger.get("keywords", []) if k.strip()]
            match_mode = trigger.get("match", "contains")
            for kw in keywords:
                if match_mode == "exact"       and msg_text == kw:           matched = True; break
                if match_mode == "starts_with" and msg_text.startswith(kw):  matched = True; break
                if match_mode == "contains"    and kw in msg_text:           matched = True; break
        elif t_type == "outside_hours":
            matched = not (now.weekday() < 5 and 9 <= now.hour < 18)

        print(f"[AUTOREPLY] '{rname}' trigger={t_type} matched={matched} text='{msg_text}'")
        if not matched:
            continue

        # ── Send ──────────────────────────────────────────────────────────
        a_type = action.get("type", "text")
        print(f"[AUTOREPLY] '{rname}' FIRING — action={a_type}")

        try:
            resp      = await _send_action(tenant, wa_id, action, a_type)
            wa_msg_id = (resp.get("messages") or [{}])[0].get("id", "")

            if "error" in resp:
                err = resp["error"]
                print(f"[AUTOREPLY] '{rname}' Meta ERROR ({err.get('code')}): {err.get('message')}")
                continue

            print(f"[AUTOREPLY] '{rname}' ✅ sent | wamid={wa_msg_id}")

            # ── Save message ──────────────────────────────────────────────
            if a_type == "text":
                reply_content = {"body": action.get("text", "")}
                preview       = (action.get("text") or "")[:60]
            else:
                reply_content = {
                    "template_name": action.get("template_name", ""),
                    "language":      action.get("language", "en_US"),
                }
                preview = f"📋 {action.get('template_name','')}"

            msg_res = await db.messages.insert_one({
                "tenant_id":       tid,
                "conversation_id": convo_id,
                "contact_id":      contact_id,
                "wa_id":           wa_id,
                "wa_message_id":   wa_msg_id,
                "direction":       "outbound",
                "type":            a_type,
                "msg_type":        a_type,
                "content":         reply_content,
                "status":          "sent",
                "auto_reply":      True,
                "auto_reply_rule": str(rule["_id"]),
                "created_at":      now,
            })

            await db.conversations.update_one(
                {"_id": ObjectId(convo_id)},
                {"$set": {
                    "last_message_at":      now,
                    "last_message_preview": f"🤖 {preview}",
                    "updated_at":           now,
                }}
            )

            await db.autoreplies.update_one(
                {"_id": rule["_id"]},
                {"$inc": {"stats.sent": 1}, "$set": {"stats.last_triggered": now}}
            )

            # WebSocket push
            try:
                from app.api.v1.websocket import broadcast_to_tenant
                await broadcast_to_tenant(tid, {
                    "type":            "new_message",
                    "conversation_id": convo_id,
                    "message": {
                        "id":           str(msg_res.inserted_id),
                        "direction":    "outbound",
                        "type":         a_type,
                        "msg_type":     a_type,
                        "content":      reply_content,
                        "status":       "sent",
                        "auto_reply":   True,
                        "wa_message_id": wa_msg_id,
                        "created_at":   now.isoformat(),
                    }
                })
            except Exception as ws_err:
                print(f"[AUTOREPLY] WS push failed: {ws_err}")

            return  # Stop after first successful rule

        except Exception as e:
            print(f"[AUTOREPLY] '{rname}' EXCEPTION: {e}")
            traceback.print_exc()
            continue


async def _send_action(tenant, wa_id: str, action: dict, a_type: str) -> dict:
    """Send the auto-reply message via WhatsApp API."""
    from app.services.whatsapp import get_wa_client, build_send_components

    client = get_wa_client(tenant)

    if a_type == "text":
        text = (action.get("text") or "").strip()
        if not text:
            raise ValueError("Auto-reply text is empty")
        print(f"[AUTOREPLY] send_text to={wa_id} text='{text[:50]}'")
        return await client.send_text(wa_id, text)

    elif a_type == "template":
        tpl_name  = (action.get("template_name") or "").strip()
        language  = action.get("language") or "en_US"
        variables = action.get("variables") or {}
        if not tpl_name:
            raise ValueError("Auto-reply template_name is empty")
        components = build_send_components(body_variables=variables) if variables else []
        print(f"[AUTOREPLY] send_template to={wa_id} tpl={tpl_name} vars={variables}")
        return await client.send_template(wa_id, tpl_name, language, components)

    raise ValueError(f"Unknown action type: {a_type}")


def _fmt(doc: dict) -> dict:
    return {
        "id":         str(doc["_id"]),
        "tenant_id":  doc.get("tenant_id", ""),
        "name":       doc.get("name", ""),
        "is_active":  doc.get("is_active", True),
        "priority":   doc.get("priority", 10),
        "trigger":    doc.get("trigger",    {}),
        "action":     doc.get("action",     {}),
        "conditions": doc.get("conditions", {}),
        "stats":      doc.get("stats", {"sent": 0, "last_triggered": None}),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }