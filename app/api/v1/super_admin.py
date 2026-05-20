"""
app/api/v1/super_admin.py — Super Admin portal backend
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr

from app.models.super_admin import SuperAdmin, SubscriptionPlan
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token
)

router = APIRouter(prefix="/super-admin", tags=["super-admin"])
bearer = HTTPBearer()


# ── Auth dependency ────────────────────────────────────────────────────────────

async def get_current_super_admin(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> SuperAdmin:
    try:
        payload = decode_token(creds.credentials)
    except ValueError:
        raise HTTPException(401, "Invalid token")
    if payload.get("type") != "access" or payload.get("role") != "super_admin":
        raise HTTPException(401, "Not a super admin token")
    sa = await SuperAdmin.get(payload.get("sub"))
    if not sa or not sa.is_active:
        raise HTTPException(401, "Super admin not found or inactive")
    return sa


# ── Login / Auth ───────────────────────────────────────────────────────────────

class SALoginRequest(BaseModel):
    email: EmailStr
    password: str

class SARefreshRequest(BaseModel):
    refresh_token: str


@router.post("/login")
async def super_admin_login(body: SALoginRequest):
    sa = await SuperAdmin.find_one(SuperAdmin.email == body.email.lower())
    if not sa or not verify_password(body.password, sa.hashed_password):
        raise HTTPException(401, "Invalid email or password")
    if not sa.is_active:
        raise HTTPException(403, "Account deactivated")
    sa.last_login_at = datetime.utcnow()
    await sa.save()
    payload = {"sub": str(sa.id), "role": "super_admin", "type": "access"}
    return {
        "access_token":  create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type":    "bearer",
        "name":          sa.name,
        "email":         sa.email,
    }


@router.post("/refresh")
async def super_admin_refresh(body: SARefreshRequest):
    try:
        data = decode_token(body.refresh_token)
    except ValueError:
        raise HTTPException(401, "Invalid or expired refresh token")
    if data.get("type") != "refresh" or data.get("role") != "super_admin":
        raise HTTPException(401, "Not a super admin refresh token")
    sa = await SuperAdmin.get(data.get("sub"))
    if not sa or not sa.is_active:
        raise HTTPException(401, "Super admin not found")
    payload = {"sub": str(sa.id), "role": "super_admin", "type": "access"}
    return {
        "access_token":  create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type":    "bearer",
    }


@router.get("/me")
async def super_admin_me(sa: SuperAdmin = Depends(get_current_super_admin)):
    return {"id": str(sa.id), "name": sa.name, "email": sa.email,
            "last_login_at": sa.last_login_at}


# ── Platform Stats ─────────────────────────────────────────────────────────────

@router.get("/stats")
async def platform_stats(sa: SuperAdmin = Depends(get_current_super_admin)):
    total_tenants  = await Tenant.count()
    active_tenants = await Tenant.find(Tenant.status == "active").count()
    trial_tenants  = await Tenant.find(Tenant.subscription_status == "trial").count()
    suspended      = await Tenant.find(Tenant.status == "suspended").count()
    waba_connected = await Tenant.find(Tenant.waba_connected == True).count()
    total_agents   = await Agent.count()
    total_plans    = await SubscriptionPlan.find(SubscriptionPlan.is_active == True).count()
    return {
        "tenants": {
            "total":          total_tenants,
            "active":         active_tenants,
            "trial":          trial_tenants,
            "suspended":      suspended,
            "waba_connected": waba_connected,
        },
        "agents": {"total": total_agents},
        "plans":  {"total": total_plans},
    }


# ── Tenant (Admin) Management ──────────────────────────────────────────────────

class CreateTenantRequest(BaseModel):
    business_name: str
    email: EmailStr
    password: str
    plan_id: Optional[str] = None
    notes: Optional[str] = None


class UpdateTenantRequest(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    plan_id: Optional[str] = None


@router.get("/tenants")
async def list_tenants(
    status: Optional[str] = None,
    subscription_status: Optional[str] = None,
    sa: SuperAdmin = Depends(get_current_super_admin),
):
    tenants = await Tenant.find_all().to_list()
    if status:
        tenants = [t for t in tenants if t.status == status]
    if subscription_status:
        tenants = [t for t in tenants if getattr(t, "subscription_status", "trial") == subscription_status]

    all_agents = await Agent.find_all().to_list()
    agent_map: dict[str, int] = {}
    for agent in all_agents:
        tid = agent.tenant_id
        if tid:
            agent_map[tid] = agent_map.get(tid, 0) + 1

    result = []
    for t in tenants:
        t_dict = _st(t)
        t_dict["agent_count"] = agent_map.get(str(t.id), 0)
        result.append(t_dict)

    return {"total": len(result), "tenants": result}


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, sa: SuperAdmin = Depends(get_current_super_admin)):
    t = await Tenant.get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    agents = await Agent.find(Agent.tenant_id == tenant_id).to_list()
    result = _st(t)
    result["agents"] = [_sa_agent(a) for a in agents]
    result["agent_count"] = len(agents)
    result["tenant_id"] = result["tenant_id"] or tenant_id
    return result


@router.post("/tenants", status_code=201)
async def create_tenant(body: CreateTenantRequest, sa: SuperAdmin = Depends(get_current_super_admin)):
    if await Tenant.find_one(Tenant.email == body.email.lower()):
        raise HTTPException(409, "Email already registered")
    plan_fields = {}
    if body.plan_id:
        plan = await SubscriptionPlan.get(body.plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found")
        plan_fields = {
            "plan_id":             body.plan_id,
            "plan_name":           plan.name,
            "subscription_status": "active",
            "subscription_start":  datetime.utcnow(),
            "agent_limit":         plan.agent_limit,
            "broadcast_limit":     plan.broadcast_limit,
            "template_limit":      plan.template_limit,
            "contact_limit":       plan.contact_limit,
            "flow_builder":        plan.flow_builder,
            "analytics_access":    plan.analytics,
        }
    t = Tenant(
        business_name=body.business_name,
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        status="active",
        notes=body.notes,
        created_by_super_admin=True,
        **plan_fields,
    )
    await t.insert()
    t.tenant_id = str(t.id)
    await t.save()
    return _st(t)


@router.patch("/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, body: UpdateTenantRequest,
                        sa: SuperAdmin = Depends(get_current_super_admin)):
    t = await Tenant.get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    if body.status:
        valid = {"active", "suspended", "trial", "pending_waba"}
        if body.status not in valid:
            raise HTTPException(400, f"Status must be one of {valid}")
        t.status = body.status
    if body.notes is not None:
        t.notes = body.notes
    if body.plan_id:
        plan = await SubscriptionPlan.get(body.plan_id)
        if not plan:
            raise HTTPException(404, "Plan not found")
        t.plan_id = body.plan_id
        t.plan_name = plan.name
        t.subscription_status = "active"
        t.subscription_start = datetime.utcnow()
        t.agent_limit = plan.agent_limit
        t.broadcast_limit = plan.broadcast_limit
        t.template_limit = plan.template_limit
        t.contact_limit = plan.contact_limit
        t.flow_builder = plan.flow_builder
        t.analytics_access = plan.analytics
    t.updated_at = datetime.utcnow()
    await t.save()
    return _st(t)


@router.post("/tenants/{tenant_id}/suspend")
async def suspend_tenant(tenant_id: str, sa: SuperAdmin = Depends(get_current_super_admin)):
    t = await Tenant.get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.status = "suspended"
    t.updated_at = datetime.utcnow()
    await t.save()
    return {"tenant_id": tenant_id, "status": "suspended"}


@router.post("/tenants/{tenant_id}/activate")
async def activate_tenant(tenant_id: str, sa: SuperAdmin = Depends(get_current_super_admin)):
    t = await Tenant.get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.status = "active"
    t.updated_at = datetime.utcnow()
    await t.save()
    return {"tenant_id": tenant_id, "status": "active"}


@router.post("/tenants/{tenant_id}/assign-plan")
async def assign_plan(tenant_id: str, body: dict,
                      sa: SuperAdmin = Depends(get_current_super_admin)):
    t = await Tenant.get(tenant_id)
    if not t:
        raise HTTPException(404, "Tenant not found")
    plan = await SubscriptionPlan.get(body.get("plan_id"))
    if not plan:
        raise HTTPException(404, "Plan not found")
    t.plan_id = str(plan.id)
    t.plan_name = plan.name
    t.subscription_status = "active"
    t.subscription_start = datetime.utcnow()
    t.agent_limit = plan.agent_limit
    t.broadcast_limit = plan.broadcast_limit
    t.template_limit = plan.template_limit
    t.contact_limit = plan.contact_limit
    t.flow_builder = plan.flow_builder
    t.analytics_access = plan.analytics
    t.updated_at = datetime.utcnow()
    await t.save()
    return _st(t)


# ── Subscription Plans CRUD ────────────────────────────────────────────────────

class CreatePlanRequest(BaseModel):
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
    sort_order: int = 0


@router.get("/plans")
async def list_plans(sa: SuperAdmin = Depends(get_current_super_admin)):
    plans = await SubscriptionPlan.find_all().to_list()
    plans.sort(key=lambda p: p.sort_order)
    return {"plans": [_sp(p) for p in plans]}


@router.post("/plans", status_code=201)
async def create_plan(body: CreatePlanRequest, sa: SuperAdmin = Depends(get_current_super_admin)):
    p = SubscriptionPlan(**body.model_dump())
    await p.insert()
    return _sp(p)


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: str, body: dict, sa: SuperAdmin = Depends(get_current_super_admin)):
    p = await SubscriptionPlan.get(plan_id)
    if not p:
        raise HTTPException(404, "Plan not found")
    allowed = {"name","description","price_monthly","price_yearly","agent_limit",
               "broadcast_limit","template_limit","contact_limit","flow_builder",
               "analytics","automations","api_access","whatsapp_accounts","is_active","sort_order"}
    for k, v in body.items():
        if k in allowed:
            setattr(p, k, v)
    p.updated_at = datetime.utcnow()
    await p.save()
    return _sp(p)


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, sa: SuperAdmin = Depends(get_current_super_admin)):
    p = await SubscriptionPlan.get(plan_id)
    if not p:
        raise HTTPException(404, "Plan not found")
    await p.delete()


# ── Migrations ────────────────────────────────────────────────────────────────

@router.post("/migrate/fix-tenant-ids")
async def fix_tenant_ids(sa: SuperAdmin = Depends(get_current_super_admin)):
    """One-time migration: set tenant_id = str(id) for tenants where tenant_id is null."""
    tenants = await Tenant.find_all().to_list()
    fixed = 0
    for t in tenants:
        if not getattr(t, "tenant_id", None):
            t.tenant_id = str(t.id)
            await t.save()
            fixed += 1
    return {"message": f"Fixed {fixed} tenants", "total": len(tenants)}


# ── Serializers ────────────────────────────────────────────────────────────────

def _st(t: Tenant) -> dict:
    return {
        "id":                    str(t.id),
        "tenant_id":             getattr(t, "tenant_id", None) or str(t.id),
        "business_name":         t.business_name,
        "email":                 t.email,
        "status":                t.status,
        "subscription_status":   getattr(t, "subscription_status", "trial"),
        "plan_id":               getattr(t, "plan_id", None),
        "plan_name":             getattr(t, "plan_name", "trial"),
        "subscription_start":    getattr(t, "subscription_start", None),
        "subscription_end":      getattr(t, "subscription_end", None),
        "agent_limit":           getattr(t, "agent_limit", 1),
        "broadcast_limit":       getattr(t, "broadcast_limit", 100),
        "template_limit":        getattr(t, "template_limit", 5),
        "contact_limit":         getattr(t, "contact_limit", 500),
        "flow_builder":          getattr(t, "flow_builder", False),
        "analytics_access":      getattr(t, "analytics_access", False),
        "waba_connected":        bool(t.waba_id),
        "waba_id":               t.waba_id,
        "phone_number":          t.display_phone_number,
        "notes":                 getattr(t, "notes", None),
        "created_by_super_admin":getattr(t, "created_by_super_admin", False),
        "created_at":            t.created_at,
        "updated_at":            t.updated_at,
    }


def _sa_agent(a: Agent) -> dict:
    return {
        "id":           str(a.id),
        "name":         a.name,
        "email":        a.email,
        "role":         a.role,
        "is_active":    a.is_active,
        "last_login_at":a.last_login_at,
    }


def _sp(p: SubscriptionPlan) -> dict:
    return {
        "id":               str(p.id),
        "name":             p.name,
        "description":      p.description,
        "price_monthly":    p.price_monthly,
        "price_yearly":     p.price_yearly,
        "agent_limit":      p.agent_limit,
        "broadcast_limit":  p.broadcast_limit,
        "template_limit":   p.template_limit,
        "contact_limit":    p.contact_limit,
        "flow_builder":     p.flow_builder,
        "analytics":        p.analytics,
        "automations":      p.automations,
        "api_access":       p.api_access,
        "whatsapp_accounts":p.whatsapp_accounts,
        "is_active":        p.is_active,
        "sort_order":       p.sort_order,
        "created_at":       p.created_at,
    }
