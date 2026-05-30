"""
app/api/v1/flows.py — WhatsApp Flows CRUD + Meta Graph API integration

Flow lifecycle:
  1. POST   /flows           → create locally + register on Meta (gets meta_flow_id)
  2. PATCH  /flows/{id}      → save screens locally; upload JSON to Meta if DRAFT
  3. POST   /flows/{id}/publish → upload flow JSON to Meta + publish
  4. POST   /flows/{id}/send → send flow interactive message to contacts/tags
  5. DELETE /flows/{id}      → delete locally
"""
import json
import re
import uuid
import httpx
import logging

from datetime   import datetime
from fastapi    import APIRouter, HTTPException, Depends
from pydantic   import BaseModel, Field
from typing     import Optional, List

from app.models.tenant    import Tenant
from app.core.dependencies import get_current_tenant, get_active_tenant, get_tenant_from_token, get_active_tenant_from_token
from app.config import get_settings

router = APIRouter(prefix="/flows", tags=["flows"])
log    = logging.getLogger(__name__)

settings = get_settings()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_token(tenant: Tenant) -> str:
    """Resolve Meta access token — delegates to the centralized resolver."""
    from app.services.whatsapp import resolve_token
    return resolve_token(tenant)


def _api_version() -> str:
    return getattr(settings, "meta_api_version", None) or "v22.0"


def _safe_id(sid: str) -> str:
    """
    Convert any string to a Meta-safe UPPERCASE identifier.
    Meta requires: ONLY A-Z and underscores — NO numbers, NO hyphens.
    Digits are replaced with their word equivalents.
    """
    NUMBER_WORDS = {
        '0': 'ZERO', '1': 'ONE', '2': 'TWO', '3': 'THREE', '4': 'FOUR',
        '5': 'FIVE', '6': 'SIX', '7': 'SEVEN', '8': 'EIGHT', '9': 'NINE',
    }
    upper = sid.upper()
    for digit, word in NUMBER_WORDS.items():
        upper = upper.replace(digit, f'_{word}_')
    safe = re.sub(r'[^A-Z_]', '_', upper)
    safe = re.sub(r'_+', '_', safe).strip('_')
    return safe or 'SCREEN'


def _alpha_id(n: int) -> str:
    """Convert 0-based index to a letter-only screen ID: A→SCREEN_A, 26→SCREEN_AA, ..."""
    letters = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"SCREEN_{letters}"


def _opt_id(n: int) -> str:
    """Convert 0-based index to a letter-only dropdown option ID: OPT_A, OPT_B, ..."""
    letters = ""
    n += 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"OPT_{letters}"


def screens_to_flow_json(screens: list) -> dict:
    """
    Convert our internal screen/component format to WhatsApp Flows JSON v6.1.
    Docs: https://developers.facebook.com/docs/whatsapp/flows/reference/flowjson

    Rules enforced:
    - id fields must contain ONLY letters and underscores (no digits) — Meta requirement
    - Screen IDs are positional: SCREEN_A, SCREEN_B, SCREEN_C, ...
    - Dropdown option IDs are positional: OPT_A, OPT_B, ...
    - Every screen must have exactly one Footer action
    - At least one screen must be terminal=true
    - Terminal screen Footer must use complete action
    """

    # Map original stored ID → letter-only Meta ID
    id_map = {_safe_id(s["id"]): _alpha_id(i) for i, s in enumerate(screens)}

    def build_action(btn: dict) -> dict:
        orig = _safe_id(btn.get("next_screen", "")) if btn.get("next_screen") else ""
        target = id_map.get(orig, "")
        if btn.get("action") == "NAVIGATE" and target:
            return {
                "name":    "navigate",
                "next":    {"type": "screen", "name": target},
                "payload": {},
            }
        return {"name": "complete", "payload": {}}

    # ── build screens ──────────────────────────────────────────────────────
    meta_screens = []

    for screen in screens:
        safe_sid = id_map[_safe_id(screen["id"])]   # letter-only: SCREEN_A, SCREEN_B, ...
        children = []
        nav_btns = []      # collect action buttons; only ONE Footer allowed per screen

        for comp in screen.get("components", []):
            ctype = comp.get("type", "")

            if ctype == "text" and comp.get("text"):
                children.append({"type": "TextBody", "text": comp["text"]})

            elif ctype == "input":
                itype_map = {
                    "text": "text", "email": "email", "phone": "phone",
                    "number": "number", "date": "date", "password": "password",
                }
                item = {
                    "type":       "TextInput",
                    "name":       _safe_id(comp["id"]).lower(),
                    "label":      comp.get("label", "Input"),
                    "input-type": itype_map.get(comp.get("input_type", "text"), "text"),
                    "required":   comp.get("required", False),
                }
                if comp.get("placeholder"):
                    item["helper-text"] = comp["placeholder"]
                children.append(item)

            elif ctype == "dropdown":
                options = comp.get("options", [])
                if options:   # Meta rejects empty data-source
                    children.append({
                        "type":        "Dropdown",
                        "name":        _safe_id(comp["id"]).lower(),
                        "label":       comp.get("label", "Choose an option"),
                        "required":    comp.get("required", False),
                        "data-source": [
                            {"id": _safe_id(opt.get("id", f"opt_{i}")).lower(), "title": opt["title"]}
                            for i, opt in enumerate(options)
                        ],
                    })

            elif ctype == "media":
                if comp.get("media_type") == "image" and comp.get("url"):
                    img: dict = {
                        "type":       "Image",
                        "src":        comp["url"],
                        "width":      600,
                        "height":     300,
                        "scale-type": "cover",
                    }
                    if comp.get("alt_text"):
                        img["alt-text"] = comp["alt_text"]
                    children.append(img)

            elif ctype == "buttons":
                nav_btns.extend(comp.get("buttons", []))

            elif ctype == "footer":
                if comp.get("footer_text"):
                    children.append({"type": "TextCaption", "text": comp["footer_text"]})
                nav_btns.extend(comp.get("buttons", []))

        # ── Meta allows exactly ONE Footer per screen — use the primary (last) button ──
        if not nav_btns:
            nav_btns = [{"label": "Done", "action": "COMPLETE", "next_screen": ""}]

        is_terminal = screen.get("is_terminal", False)
        primary_btn = nav_btns[-1]   # last button is the primary CTA

        # Terminal screens MUST use complete action — navigate on terminal causes publish failure
        if is_terminal:
            footer_action = {"name": "complete", "payload": {}}
        else:
            footer_action = build_action(primary_btn)

        children.append({
            "type":            "Footer",
            "label":           primary_btn.get("label", "Done" if is_terminal else "Continue"),
            "on-click-action": footer_action,
        })

        meta_screens.append({
            "id":       safe_sid,
            "title":    screen.get("title", "Screen"),
            "terminal": is_terminal,
            "layout":   {"type": "SingleColumnLayout", "children": children},
        })

    # ── Ensure at least the last screen is terminal ────────────────────────
    if meta_screens and not any(s["terminal"] for s in meta_screens):
        last = meta_screens[-1]
        last["terminal"] = True
        # Fix its Footer action to complete — terminal screens can't navigate
        children = last["layout"]["children"]
        for comp in children:
            if comp.get("type") == "Footer":
                comp["on-click-action"] = {"name": "complete", "payload": {}}
                break

    return {
        "version": "6.1",
        "screens": meta_screens,
    }


def _validate_flow_json(flow_json: dict) -> list:
    """Pre-flight check before uploading to Meta. Returns list of error strings."""
    errors  = []
    id_pat  = re.compile(r'^[A-Za-z_]+$')
    screens = flow_json.get("screens", [])

    if not screens:
        return ["Flow must have at least one screen"]

    if not any(s.get("terminal") for s in screens):
        errors.append("At least one screen must be terminal")

    # Pass 1 — collect all valid screen IDs first
    screen_ids = {s.get("id", "") for s in screens}

    # Pass 2 — validate each screen's components against the full ID set
    for s in screens:
        sid = s.get("id", "")
        if not id_pat.match(sid):
            errors.append(f"Screen id '{sid}' has invalid chars (only A-Z and _ allowed)")

        has_footer = False
        for child in s.get("layout", {}).get("children", []):
            ctype = child.get("type", "")
            if ctype == "Footer":
                has_footer = True
                action = child.get("on-click-action", {})
                if action.get("name") == "navigate":
                    target = action.get("next", {}).get("name", "")
                    if target and target not in screen_ids:
                        errors.append(f"Navigate target '{target}' not found in screens")
            name = child.get("name", "")
            if name and not id_pat.match(name):
                errors.append(f"Component name '{name}' has invalid chars")
            for opt in child.get("data-source", []):
                oid = opt.get("id", "")
                if oid and not id_pat.match(oid):
                    errors.append(f"Dropdown option id '{oid}' has invalid chars")

        if not has_footer:
            errors.append(f"Screen '{sid}' has no Footer action")

    return errors


def _fmt_flow(d: dict) -> dict:
    return {
        "id":           str(d["_id"]),
        "meta_flow_id": d.get("meta_flow_id") or "",
        "name":         d.get("name", ""),
        "description":  d.get("description", ""),
        "category":     d.get("category", "OTHER"),
        "status":       d.get("status", "DRAFT"),
        "version":      d.get("version", 1),
        "screens":      d.get("screens", []),
        "created_at":   d.get("created_at"),
        "updated_at":   d.get("updated_at"),
    }


# ─── Schemas ──────────────────────────────────────────────────────────────────

class FlowCreateRequest(BaseModel):
    name:        str
    description: str  = ""
    category:    str  = "OTHER"
    screens:     list = Field(default_factory=list)


class FlowUpdateRequest(BaseModel):
    name:        Optional[str]  = None
    description: Optional[str]  = None
    category:    Optional[str]  = None
    screens:     Optional[list] = None


class FlowSendRequest(BaseModel):
    # Who to send to — provide at least one of these
    contact_ids: List[str] = Field(default_factory=list)   # specific conversation/contact IDs
    tags:        List[str] = Field(default_factory=list)   # all contacts with these tags
    send_all:    bool      = False                          # send to ALL contacts (use carefully)

    # Wrapper message fields
    flow_cta:    str = "Open"
    flow_header: str = ""
    flow_body:   str = "Tap the button below to get started."
    flow_footer: str = ""
    flow_screen: str = ""   # first screen override; defaults to first screen in flow


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
async def list_flows(tenant: Tenant = Depends(get_tenant_from_token)):
    from app.database import db
    tid  = str(tenant.id)
    docs = await db.flows.find({"tenant_id": tid}).sort("updated_at", -1).to_list(200)
    return {"flows": [_fmt_flow(d) for d in docs]}


@router.get("/{fid}")
async def get_flow(fid: str, tenant: Tenant = Depends(get_tenant_from_token)):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    return _fmt_flow(doc)


@router.post("")
async def create_flow(body: FlowCreateRequest, tenant: Tenant = Depends(get_active_tenant_from_token)):
    from app.database import db
    tid   = str(tenant.id)
    now   = datetime.utcnow()
    token = _get_token(tenant)
    waba_id    = getattr(tenant, "waba_id", None) or settings.meta_waba_id
    api_ver    = _api_version()
    meta_flow_id = None

    # 1. Register flow on Meta (best-effort)
    if token and waba_id:
        try:
            cat = body.category if body.category != "OTHER" else "LEAD_GENERATION"
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    f"https://graph.facebook.com/{api_ver}/{waba_id}/flows",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"name": body.name, "categories": [cat]},
                )
                resp = r.json()
                if "id" in resp:
                    meta_flow_id = resp["id"]
                    log.info(f"[FLOWS] Created on Meta: {meta_flow_id}")
                elif "error" in resp:
                    log.warning(f"[FLOWS] Meta create warning: {resp['error']}")
        except Exception as e:
            log.warning(f"[FLOWS] Meta create failed (saved locally): {e}")

    # 2. Persist locally
    doc = {
        "tenant_id":    tid,
        "meta_flow_id": meta_flow_id,
        "name":         body.name,
        "description":  body.description,
        "category":     body.category,
        "status":       "DRAFT",
        "version":      1,
        "screens":      body.screens,
        "created_at":   now,
        "updated_at":   now,
    }
    result = await db.flows.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _fmt_flow(doc)


@router.patch("/{fid}")
async def update_flow(fid: str, body: FlowUpdateRequest, tenant: Tenant = Depends(get_active_tenant_from_token)):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)

    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")

    updates: dict = {"updated_at": datetime.utcnow()}
    if body.name        is not None: updates["name"]        = body.name
    if body.description is not None: updates["description"] = body.description
    if body.category    is not None: updates["category"]    = body.category
    if body.screens     is not None: updates["screens"]     = body.screens

    await db.flows.update_one({"_id": ObjectId(fid)}, {"$set": updates})

    # Upload updated flow JSON to Meta when screens change (only for DRAFT flows)
    if body.screens and doc.get("meta_flow_id") and doc.get("status") == "DRAFT":
        token   = _get_token(tenant)
        api_ver = _api_version()
        if token:
            try:
                flow_json_str = json.dumps(screens_to_flow_json(body.screens))
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(
                        f"https://graph.facebook.com/{api_ver}/{doc['meta_flow_id']}/assets",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"asset_type": "FLOW_JSON", "name": "flow.json"},
                        files={"file": ("flow.json", flow_json_str.encode(), "application/json")},
                    )
                    log.info(f"[FLOWS] JSON uploaded: {r.json()}")
            except Exception as e:
                log.warning(f"[FLOWS] JSON upload failed (local save OK): {e}")

    updated = await db.flows.find_one({"_id": ObjectId(fid)})
    return _fmt_flow(updated)


@router.post("/{fid}/publish")
async def publish_flow(fid: str, tenant: Tenant = Depends(get_active_tenant_from_token)):
    from app.database import db
    from bson import ObjectId
    tid     = str(tenant.id)
    token   = _get_token(tenant)
    api_ver = _api_version()
    waba_id = getattr(tenant, "waba_id", None) or settings.meta_waba_id

    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    if doc.get("status") == "PUBLISHED":
        raise HTTPException(400, "Flow is already published")
    if not token:
        raise HTTPException(503, "No Meta access token configured")

    meta_flow_id = doc.get("meta_flow_id")

    # ── Check actual Meta status (sync only — never clear meta_flow_id here) ──
    if meta_flow_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                chk = await client.get(
                    f"https://graph.facebook.com/{api_ver}/{meta_flow_id}",
                    params={"fields": "id,status", "access_token": token},
                )
                chk_data = chk.json()
                meta_status = chk_data.get("status", "")
                log.info(f"[FLOWS] Meta status check: {chk_data}")
                if meta_status == "PUBLISHED":
                    await db.flows.update_one({"_id": ObjectId(fid)}, {"$set": {"status": "PUBLISHED"}})
                    return {"success": True, "meta_flow_id": meta_flow_id, "status": "PUBLISHED",
                            "note": "Flow was already published on Meta — local status updated"}
                if meta_status == "DEPRECATED":
                    raise HTTPException(400,
                        f"This flow (Meta ID: {meta_flow_id}) has been DEPRECATED on Meta and cannot be published. "
                        "Delete this flow and create a new one.")
                if meta_status == "BLOCKED":
                    raise HTTPException(400,
                        f"This flow (Meta ID: {meta_flow_id}) is BLOCKED on Meta. "
                        "Check your Meta Business Manager for details.")
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"[FLOWS] Meta status check failed (continuing): {e}")

    # ── Step 1: Register on Meta if not done yet ──────────────────────────────
    if not meta_flow_id:
        if not waba_id:
            raise HTTPException(503, "WABA ID not configured. Connect WhatsApp in Settings first.")
        try:
            VALID_CATEGORIES = {
                "SIGN_UP", "SIGN_IN", "APPOINTMENT_BOOKING", "LEAD_GENERATION",
                "CONTACT_US", "CUSTOMER_SUPPORT", "SURVEY", "OTHER",
            }
            cat = doc.get("category", "LEAD_GENERATION")
            if cat not in VALID_CATEGORIES:
                cat = "LEAD_GENERATION"

            # ── Try to find an existing Meta flow with the same name first ────
            # (avoids error 100/4016019 "duplicate name" when re-registering)
            async with httpx.AsyncClient(timeout=20) as hc:
                list_r = await hc.get(
                    f"https://graph.facebook.com/{api_ver}/{waba_id}/flows",
                    params={"fields": "id,name,status", "access_token": token},
                )
                existing_flows = list_r.json().get("data", [])
                log.info(f"[FLOWS] Existing Meta flows: {[f.get('name') for f in existing_flows]}")

            matched = next(
                (f for f in existing_flows if f.get("name", "").strip().lower() == doc["name"].strip().lower()),
                None,
            )
            if matched:
                meta_flow_id = matched["id"]
                log.info(f"[FLOWS] Reusing existing Meta flow '{matched['name']}' → id={meta_flow_id} status={matched.get('status')}")
                await db.flows.update_one({"_id": ObjectId(fid)}, {"$set": {"meta_flow_id": meta_flow_id}})
            else:
                # No existing flow — register fresh
                reg_payload = {"name": doc["name"], "categories": [cat]}
                log.info(f"[FLOWS] Registering new flow — waba_id={waba_id} payload={reg_payload}")
                async with httpx.AsyncClient(timeout=20) as hc:
                    r    = await hc.post(
                        f"https://graph.facebook.com/{api_ver}/{waba_id}/flows",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json=reg_payload,
                    )
                    resp = r.json()
                    log.info(f"[FLOWS] Registration response (HTTP {r.status_code}): {resp}")
                if "id" not in resp:
                    err      = resp.get("error", {})
                    err_msg  = err.get("message", str(resp))
                    err_code = err.get("code", "")
                    err_sub  = err.get("error_subcode", "")
                    log.error(f"[FLOWS] Registration failed — code={err_code} sub={err_sub}: {err_msg}")
                    raise HTTPException(502, f"Meta flow registration failed (code {err_code}): {err_msg}")
                meta_flow_id = resp["id"]
                await db.flows.update_one({"_id": ObjectId(fid)}, {"$set": {"meta_flow_id": meta_flow_id}})
                log.info(f"[FLOWS] Registered new Meta flow: {meta_flow_id}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Meta API error during registration: {e}")

    # ── Step 2: Upload flow JSON ───────────────────────────────────────────────
    screens = doc.get("screens", [])
    if not screens:
        raise HTTPException(422, "Flow has no screens. Add at least one screen before publishing.")

    try:
        flow_json_obj = screens_to_flow_json(screens)
        pre_errors    = _validate_flow_json(flow_json_obj)
        if pre_errors:
            raise HTTPException(422, f"Flow JSON pre-flight failed: {'; '.join(pre_errors)}")
        flow_json_str = json.dumps(flow_json_obj)
        log.info(f"[FLOWS] Uploading JSON: {flow_json_str}")
        async with httpx.AsyncClient(timeout=30) as client:
            upload_r = await client.post(
                f"https://graph.facebook.com/{api_ver}/{meta_flow_id}/assets",
                headers={"Authorization": f"Bearer {token}"},
                data={"asset_type": "FLOW_JSON", "name": "flow.json"},
                files={"file": ("flow.json", flow_json_str.encode(), "application/json")},
            )
            upload_data = upload_r.json()
            log.info(f"[FLOWS] JSON upload response: {upload_data}")

            # Capture validation warnings even when success=True (they block publish)
            v_errs = upload_data.get("validation_errors", [])
            if v_errs:
                msgs = "; ".join(
                    e.get("message", str(e)) for e in v_errs if isinstance(e, dict)
                ) or str(v_errs)
                log.warning(f"[FLOWS] Validation warnings: {msgs}")
                raise HTTPException(422, f"Meta flow JSON has errors: {msgs}")

            if upload_data.get("success") is False or "error" in upload_data:
                err_msg = upload_data.get("error", {}).get("message", str(upload_data))
                raise HTTPException(422, f"Flow JSON upload failed: {err_msg}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Meta API error during JSON upload: {e}")

    # ── Step 3: Publish ───────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            pub_r = await client.post(
                f"https://graph.facebook.com/{api_ver}/{meta_flow_id}/publish",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={},
            )
            pub_data = pub_r.json()
            log.info(f"[FLOWS] Publish response (status={pub_r.status_code}): {pub_data}")

        if "error" in pub_data:
            err     = pub_data["error"]
            code    = err.get("code", "")
            subcode = err.get("error_subcode", "")
            msg     = err.get("message", str(err))
            fbtrace = err.get("fbtrace_id", "")
            log.error(f"[FLOWS] Publish error code={code} subcode={subcode} fbtrace={fbtrace}: {msg}")
            raise HTTPException(422, f"Meta publish failed (code {code}): {msg}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Meta API error during publish: {e}")

    # ── Update local status ───────────────────────────────────────────────────
    await db.flows.update_one(
        {"_id": ObjectId(fid)},
        {"$set": {"status": "PUBLISHED", "meta_flow_id": meta_flow_id, "updated_at": datetime.utcnow()}},
    )
    return {"success": True, "meta_flow_id": meta_flow_id, "status": "PUBLISHED"}


@router.get("/{fid}/debug-json")
async def debug_flow_json(fid: str, tenant: Tenant = Depends(get_tenant_from_token)):
    """Return the generated WhatsApp Flows JSON that would be uploaded to Meta."""
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    screens = doc.get("screens", [])
    if not screens:
        raise HTTPException(422, "Flow has no screens")
    return screens_to_flow_json(screens)


@router.get("/{fid}/preview")
async def get_flow_preview(fid: str, tenant: Tenant = Depends(get_tenant_from_token)):
    """Fetch the Meta-hosted preview URL for a published flow."""
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")

    meta_flow_id = doc.get("meta_flow_id")
    if not meta_flow_id:
        return {"preview_url": None, "expires_at": None}

    token   = _get_token(tenant)
    api_ver = _api_version()
    if not token:
        return {"preview_url": None, "expires_at": None}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://graph.facebook.com/{api_ver}/{meta_flow_id}",
                params={"fields": "preview", "access_token": token},
            )
            resp = r.json()
        preview = resp.get("preview", {})
        return {
            "preview_url": preview.get("preview_url"),
            "expires_at":  preview.get("expires_at"),
            "meta_flow_id": meta_flow_id,
        }
    except Exception as e:
        log.warning(f"[FLOWS] Preview fetch failed: {e}")
        return {"preview_url": None, "expires_at": None}


@router.post("/{fid}/send")
async def send_flow_to_contacts(
    fid:  str,
    body: FlowSendRequest,
    tenant: Tenant = Depends(get_active_tenant_from_token),
):
    """
    Send a published flow as an interactive message to selected contacts.
    Resolves contacts by ID, tag, or all — then dispatches via WhatsApp API.
    """
    from app.database import db
    from bson import ObjectId
    from app.services.whatsapp import get_wa_client
    from datetime import datetime as dt

    tid = str(tenant.id)

    # Load the flow
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    if doc.get("status") != "PUBLISHED":
        raise HTTPException(400, "Flow must be published before sending")
    if not doc.get("meta_flow_id"):
        raise HTTPException(400, "Flow has no Meta flow ID — publish it first")

    # ── Resolve target contacts ───────────────────────────────────────────────
    contact_query: dict = {"tenant_id": tid, "opted_in": {"$ne": False}, "is_blocked": {"$ne": True}}

    if body.send_all:
        pass  # no extra filter
    elif body.tags:
        contact_query["tags"] = {"$in": body.tags}
    elif body.contact_ids:
        contact_query["_id"] = {"$in": [ObjectId(c) for c in body.contact_ids]}
    else:
        raise HTTPException(400, "Provide contact_ids, tags, or set send_all=true")

    contacts = await db.contacts.find(contact_query).to_list(1000)
    if not contacts:
        raise HTTPException(404, "No contacts found matching the criteria")

    # ── Send to each contact ──────────────────────────────────────────────────
    client  = get_wa_client(tenant)
    # Map stored screen ID → positional Meta ID (SCREEN_A, SCREEN_B, ...)
    stored_screens = doc.get("screens", [])
    target_stored  = (body.flow_screen or "").strip()
    if target_stored and stored_screens:
        _idx = next((i for i, s in enumerate(stored_screens) if s.get("id") == target_stored), 0)
    else:
        _idx = 0
    first_screen = _alpha_id(_idx) if stored_screens else ""

    sent = failed = 0
    errors = []

    for contact in contacts:
        wa_id = contact.get("wa_id", "")
        if not wa_id:
            failed += 1
            continue
        try:
            token = str(uuid.uuid4())
            resp  = await client.send_flow(
                to           = wa_id,
                flow_id      = doc["meta_flow_id"],
                flow_token   = token,
                cta_text     = body.flow_cta    or "Open",
                header_text  = body.flow_header,
                body_text    = body.flow_body   or "Tap the button below to get started.",
                footer_text  = body.flow_footer,
                first_screen = first_screen,
            )
            if "error" in resp:
                err_msg = resp["error"].get("message", str(resp["error"]))
                err_code = resp["error"].get("code", "")
                log.error(f"[FLOWS SEND] WA error code={err_code} to={wa_id}: {err_msg}")
                failed += 1
                errors.append({"wa_id": wa_id, "error": f"({err_code}) {err_msg}"})
                continue

            # Record outbound message
            now = dt.utcnow()
            convo = await db.conversations.find_one({"tenant_id": tid, "wa_id": wa_id})
            if convo:
                await db.messages.insert_one({
                    "tenant_id":       tid,
                    "conversation_id": str(convo["_id"]),
                    "wa_message_id":   resp.get("messages", [{}])[0].get("id", ""),
                    "direction":       "outbound",
                    "msg_type":        "flow",
                    "type":            "flow",
                    "content": {
                        "flow_id":    doc["meta_flow_id"],
                        "flow_token": token,
                        "cta":        body.flow_cta or "Open",
                        "body":       body.flow_body,
                    },
                    "status":     "sent",
                    "created_at": now,
                })
            sent += 1
        except Exception as e:
            failed += 1
            errors.append({"wa_id": wa_id, "error": str(e)})

    return {
        "sent":    sent,
        "failed":  failed,
        "total":   len(contacts),
        "errors":  errors[:10],  # cap to first 10 for brevity
    }


@router.post("/{fid}/reset-meta-id")
async def reset_meta_id(fid: str, tenant: Tenant = Depends(get_active_tenant_from_token)):
    """
    Clear the stored meta_flow_id so the next publish registers a fresh flow on Meta.
    Use this when the existing Meta flow is stuck in DEPRECATED/BLOCKED state.
    """
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    old_id = doc.get("meta_flow_id")
    await db.flows.update_one(
        {"_id": ObjectId(fid)},
        {"$set": {"meta_flow_id": None, "status": "DRAFT", "updated_at": datetime.utcnow()}},
    )
    log.info(f"[FLOWS] Reset meta_flow_id (was {old_id}) for flow {fid}")
    return {"success": True, "old_meta_flow_id": old_id, "message": "Meta ID cleared. Next publish will register a fresh flow."}


@router.delete("/{fid}")
async def delete_flow(fid: str, tenant: Tenant = Depends(get_active_tenant_from_token)):
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")
    await db.flows.delete_one({"_id": ObjectId(fid)})
    return {"success": True}


@router.get("/{fid}/meta-status")
async def get_meta_status(fid: str, tenant: Tenant = Depends(get_tenant_from_token)):
    """
    Fetch the current status of this flow directly from Meta Graph API.
    Returns the live Meta status even if the local DB is out of sync.
    """
    from app.database import db
    from bson import ObjectId
    tid = str(tenant.id)
    doc = await db.flows.find_one({"_id": ObjectId(fid), "tenant_id": tid})
    if not doc:
        raise HTTPException(404, "Flow not found")

    meta_flow_id = doc.get("meta_flow_id")
    if not meta_flow_id:
        return {"meta_status": None, "meta_flow_id": None, "synced": False,
                "message": "Flow has no Meta ID — publish it first"}

    token   = _get_token(tenant)
    api_ver = _api_version()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://graph.facebook.com/{api_ver}/{meta_flow_id}",
            params={"fields": "id,name,status,validation_errors,health_status", "access_token": token},
        )
    data = r.json()

    if "error" in data:
        raise HTTPException(502, f"Meta API error: {data['error'].get('message', str(data['error']))}")

    meta_status = data.get("status", "UNKNOWN")

    # Sync local status if it differs
    if meta_status != doc.get("status"):
        await db.flows.update_one(
            {"_id": ObjectId(fid)},
            {"$set": {"status": meta_status, "updated_at": datetime.utcnow()}},
        )

    return {
        "meta_flow_id":       meta_flow_id,
        "meta_status":        meta_status,
        "validation_errors":  data.get("validation_errors", []),
        "health_status":      data.get("health_status"),
        "synced":             True,
    }


@router.post("/meta-sync-all")
async def sync_all_from_meta(tenant: Tenant = Depends(get_active_tenant_from_token)):
    """
    Fetch all flows from Meta's WABA and:
    - Update status of locally-known flows
    - Return list of Meta flows not yet imported into the CRM
    """
    from app.database import db

    token   = _get_token(tenant)
    api_ver = _api_version()
    waba_id = getattr(tenant, "waba_id", None) or settings.meta_waba_id
    tid     = str(tenant.id)

    if not waba_id:
        raise HTTPException(503, "WABA not connected. Complete WhatsApp setup first.")
    if not token:
        raise HTTPException(503, "No Meta access token available.")

    # Fetch all flows from Meta
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"https://graph.facebook.com/{api_ver}/{waba_id}/flows",
            params={"fields": "id,name,status,categories,validation_errors", "access_token": token},
        )
    data = r.json()

    if "error" in data:
        raise HTTPException(502, f"Meta API error: {data['error'].get('message', str(data['error']))}")

    meta_flows  = data.get("data", [])
    local_docs  = await db.flows.find({"tenant_id": tid}).to_list(500)
    local_by_meta_id = {d.get("meta_flow_id"): d for d in local_docs if d.get("meta_flow_id")}

    updated       = 0
    not_imported  = []

    for mf in meta_flows:
        mfid   = mf["id"]
        mstatus = mf.get("status", "DRAFT")

        if mfid in local_by_meta_id:
            local = local_by_meta_id[mfid]
            if local.get("status") != mstatus:
                await db.flows.update_one(
                    {"_id": local["_id"]},
                    {"$set": {"status": mstatus, "updated_at": datetime.utcnow()}},
                )
                updated += 1
        else:
            not_imported.append({
                "meta_flow_id": mfid,
                "name":         mf.get("name", ""),
                "status":       mstatus,
                "categories":   mf.get("categories", []),
            })

    return {
        "total_on_meta":  len(meta_flows),
        "updated_locally": updated,
        "not_imported":   not_imported,
        "message": f"Synced {updated} flows. {len(not_imported)} Meta flow(s) not yet in CRM.",
    }


@router.post("/import-meta/{meta_flow_id}")
async def import_meta_flow(meta_flow_id: str, tenant: Tenant = Depends(get_active_tenant_from_token)):
    """
    Import an existing Meta flow (created in Meta Business Manager) into the CRM.
    Creates a local record linked to the existing Meta flow ID.
    """
    from app.database import db

    token   = _get_token(tenant)
    api_ver = _api_version()
    tid     = str(tenant.id)
    now     = datetime.utcnow()

    # Check not already imported
    existing = await db.flows.find_one({"tenant_id": tid, "meta_flow_id": meta_flow_id})
    if existing:
        raise HTTPException(409, f"Flow with Meta ID {meta_flow_id} is already imported")

    # Fetch flow details from Meta
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://graph.facebook.com/{api_ver}/{meta_flow_id}",
            params={"fields": "id,name,status,categories", "access_token": token},
        )
    mf = r.json()

    if "error" in mf:
        raise HTTPException(502, f"Meta API error: {mf['error'].get('message', str(mf['error']))}")

    category = (mf.get("categories") or ["OTHER"])[0]
    doc = {
        "tenant_id":    tid,
        "meta_flow_id": meta_flow_id,
        "name":         mf.get("name", "Imported Flow"),
        "description":  f"Imported from Meta (ID: {meta_flow_id})",
        "category":     category,
        "status":       mf.get("status", "DRAFT"),
        "version":      1,
        "screens":      [],   # no local screens yet — flow was built in Meta
        "created_at":   now,
        "updated_at":   now,
    }
    result = await db.flows.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _fmt_flow(doc)
