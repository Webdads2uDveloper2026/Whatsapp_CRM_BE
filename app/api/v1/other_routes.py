from datetime import datetime
from typing import Optional, Any
from fastapi import APIRouter, HTTPException, Depends, Header, UploadFile, File
from pydantic import BaseModel, EmailStr
import httpx

from app.models.tenant import Tenant
from app.models.automation import Automation
from app.models.contact import Contact
from app.core.dependencies import get_current_tenant
from app.core.security import hash_password, encrypt_token
from app.config import get_settings

settings = get_settings()

# ══════════════════════════════════════════════════════════════
# TENANTS
# ══════════════════════════════════════════════════════════════
tenants_router = APIRouter(prefix="/tenants", tags=["tenants"])


@tenants_router.get("/me")
async def get_me(tenant: Tenant = Depends(get_current_tenant)):
    return _st(tenant)


@tenants_router.patch("/me")
async def update_me(body: dict, tenant: Tenant = Depends(get_current_tenant)):
    allowed = {"business_name", "website", "industry", "timezone", "logo_url"}
    for k, v in body.items():
        if k in allowed:
            setattr(tenant, k, v)
    tenant.updated_at = datetime.utcnow()
    await tenant.save()
    return _st(tenant)


def _st(t: Tenant) -> dict:
    return {"id": str(t.id), "tenant_id": t.tenant_id, "business_name": t.business_name,
            "email": t.email, "plan": t.plan, "status": t.status,
            "waba_id": t.waba_id, "phone_number_id": t.phone_number_id,
            "phone_number": t.display_phone_number, "waba_connected": bool(t.waba_id),
            "created_at": t.created_at}


# ══════════════════════════════════════════════════════════════
# ADMIN (super-admin only)
# ══════════════════════════════════════════════════════════════
admin_router = APIRouter(prefix="/admin", tags=["admin"])


async def _require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != settings.admin_secret_key:
        raise HTTPException(403, "Invalid admin key")


@admin_router.get("/tenants")
async def list_all_tenants(admin=Depends(_require_admin)):
    tenants = await Tenant.find_all().sort(-Tenant.created_at).to_list()
    return {"total": len(tenants), "tenants": [_st(t) for t in tenants]}


@admin_router.patch("/tenants/{tenant_id}/status")
async def set_tenant_status(tenant_id: str, body: dict, admin=Depends(_require_admin)):
    new_status = body.get("status")
    if new_status not in ("active", "suspended", "trial", "pending_waba"):
        raise HTTPException(400, "Invalid status")
    t = await Tenant.find_one(Tenant.tenant_id == tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.status = new_status
    t.updated_at = datetime.utcnow()
    await t.save()
    return {"tenant_id": tenant_id, "status": new_status}


@admin_router.get("/stats")
async def platform_stats(admin=Depends(_require_admin)):
    total   = await Tenant.count()
    active  = await Tenant.find(Tenant.status == "active").count()
    pending = await Tenant.find(Tenant.status == "pending_waba").count()
    return {"tenants": {"total": total, "active": active, "pending_waba": pending}}


# ══════════════════════════════════════════════════════════════
# AUTOMATIONS
# ══════════════════════════════════════════════════════════════
automations_router = APIRouter(prefix="/automations", tags=["automations"])

VALID_TRIGGERS = {"keyword","first_message","no_reply","button_click","any_message","opt_in"}
VALID_ACTIONS  = {"send_text","send_template","add_tag","remove_tag","assign_agent","close_conversation"}


class CreateAutomationRequest(BaseModel):
    name: str
    trigger_type: str
    trigger_config: dict[str, Any] = {}
    action_type: str
    action_config: dict[str, Any] = {}
    conditions: list[dict[str, Any]] = []
    priority: int = 0


@automations_router.get("")
async def list_automations(tenant: Tenant = Depends(get_current_tenant)):
    items = await Automation.find(Automation.tenant_id == str(tenant.id)).sort(-Automation.priority).to_list()
    return [_sa(a) for a in items]


@automations_router.post("", status_code=201)
async def create_automation(body: CreateAutomationRequest,
                            tenant: Tenant = Depends(get_current_tenant)):
    if body.trigger_type not in VALID_TRIGGERS:
        raise HTTPException(400, f"Invalid trigger_type")
    if body.action_type not in VALID_ACTIONS:
        raise HTTPException(400, f"Invalid action_type")
    a = Automation(tenant_id=str(tenant.id), **body.model_dump())
    await a.insert()
    return _sa(a)


@automations_router.post("/{aid}/toggle")
async def toggle_automation(aid: str, tenant: Tenant = Depends(get_current_tenant)):
    a = await Automation.get(aid)
    if not a or a.tenant_id != str(tenant.id):
        raise HTTPException(404, "Not found")
    a.status = "paused" if a.status == "active" else "active"
    a.updated_at = datetime.utcnow()
    await a.save()
    return {"id": aid, "status": a.status}


@automations_router.delete("/{aid}", status_code=204)
async def delete_automation(aid: str, tenant: Tenant = Depends(get_current_tenant)):
    a = await Automation.get(aid)
    if not a or a.tenant_id != str(tenant.id):
        raise HTTPException(404, "Not found")
    await a.delete()


def _sa(a: Automation) -> dict:
    return {"id": str(a.id), "name": a.name, "status": a.status,
            "trigger_type": a.trigger_type, "action_type": a.action_type,
            "priority": a.priority, "run_count": a.run_count, "created_at": a.created_at}


# ══════════════════════════════════════════════════════════════
# MEDIA
# ══════════════════════════════════════════════════════════════
media_router = APIRouter(prefix="/media", tags=["media"])
GRAPH = f"https://graph.facebook.com/{settings.meta_api_version}"


@media_router.post("/upload")
async def upload_media(file: UploadFile = File(...),
                       tenant: Tenant = Depends(get_current_tenant)):
    from app.services.whatsapp import get_wa_client
    client = get_wa_client(tenant)
    content = await file.read()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{GRAPH}/{client.phone_number_id}/media",
            headers={"Authorization": f"Bearer {client.access_token}"},
            files={"file": (file.filename, content, file.content_type)},
            data={"messaging_product": "whatsapp"},
        )
    data = r.json()
    if "error" in data:
        raise HTTPException(400, data["error"].get("message"))
    return {"media_id": data["id"]}


@media_router.get("/{media_id}/url")
async def get_media_url(media_id: str, tenant: Tenant = Depends(get_current_tenant)):
    from app.services.whatsapp import get_wa_client
    client = get_wa_client(tenant)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{GRAPH}/{media_id}",
                        headers={"Authorization": f"Bearer {client.access_token}"})
    meta = r.json()
    if "error" in meta:
        raise HTTPException(400, meta["error"].get("message"))
    return {"url": meta.get("url"), "mime_type": meta.get("mime_type"),
            "access_token": client.access_token}


# ══════════════════════════════════════════════════════════════
# INTEGRATIONS (Google Sheets import)
# ══════════════════════════════════════════════════════════════
integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])


class SheetsImportRequest(BaseModel):
    sheet_url: str
    wa_id_column: str = "phone"
    name_column: Optional[str] = "name"
    tags_column: Optional[str] = "tags"
    opted_in_column: Optional[str] = "opted_in"


def _sheet_to_csv(url: str) -> str:
    if "output=csv" in url:
        return url
    if "/spreadsheets/d/" in url:
        sid = url.split("/spreadsheets/d/")[1].split("/")[0]
        gid = url.split("gid=")[1].split("&")[0] if "gid=" in url else "0"
        return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    raise ValueError("Invalid Google Sheets URL")


@integrations_router.post("/google-sheets/import")
async def import_sheets(body: SheetsImportRequest,
                        tenant: Tenant = Depends(get_current_tenant)):
    try:
        csv_url = _sheet_to_csv(body.sheet_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        resp = await c.get(csv_url)
        resp.raise_for_status()

    lines = [l for l in resp.text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        raise HTTPException(400, "Sheet is empty")

    headers = [h.strip().strip('"').lower() for h in lines[0].split(",")]

    def idx(name):
        try: return headers.index(name.lower())
        except ValueError: return None

    wa_idx  = idx(body.wa_id_column) or idx("phone") or idx("mobile")
    nm_idx  = idx(body.name_column) or idx("name")
    tg_idx  = idx(body.tags_column) or idx("tags")
    op_idx  = idx(body.opted_in_column) or idx("opted_in")

    if wa_idx is None:
        raise HTTPException(400, f"Phone column not found. Headers: {headers}")

    created = skipped = errors = 0
    for line in lines[1:]:
        vals = [v.strip().strip('"') for v in line.split(",")]
        def g(i): return vals[i] if i is not None and i < len(vals) else ""
        wa_id = g(wa_idx).replace("+","").replace(" ","").replace("-","")
        if not wa_id or not wa_id.isdigit():
            errors += 1; continue
        if await Contact.find_one(Contact.tenant_id == str(tenant.id), Contact.wa_id == wa_id):
            skipped += 1; continue
        tags_raw = g(tg_idx)
        tags = [t.strip() for t in tags_raw.replace(";",",").split(",") if t.strip()] if tags_raw else []
        await Contact(tenant_id=str(tenant.id), wa_id=wa_id,
                      profile_name=g(nm_idx) or None, tags=tags,
                      opted_in=g(op_idx).lower() in ("true","1","yes")).insert()
        created += 1

    return {"created": created, "skipped": skipped, "errors": errors}
