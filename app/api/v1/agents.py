from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr
from beanie import PydanticObjectId
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token, decode_token
from app.core.dependencies import get_current_tenant, get_current_agent, require_superadmin

router = APIRouter(prefix="/agents", tags=["agents"])

ROLES = ["superadmin", "manager", "agent"]
PERMS = {
    "superadmin": ["inbox","contacts","broadcasts","templates","automations","analytics","settings","agents"],
    "manager":    ["inbox","contacts","broadcasts","templates","automations","analytics"],
    "agent":      ["inbox","contacts"],
}


class CreateAgentRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "agent"


class AgentLoginRequest(BaseModel):
    email: EmailStr
    password: str
    tenant_id: Optional[str] = None


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    permissions: Optional[List[str]] = None
    password: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class AgentRefreshRequest(BaseModel):
    refresh_token: str


class UpdateAgentProfileRequest(BaseModel):
    name: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _s(a: Agent) -> dict:
    effective_permissions = a.custom_permissions if a.custom_permissions else PERMS.get(a.role, [])
    return {
        "id": str(a.id),
        "name": a.name,
        "email": a.email,
        "role": a.role,
        "is_active": a.is_active,
        "avatar_initials": a.avatar_initials,
        "permissions": effective_permissions,
        "custom_permissions": a.custom_permissions,
        "invited_by": getattr(a, 'invited_by', None),
        "last_login_at": a.last_login_at,
        "last_active_at": getattr(a, 'last_active_at', None),
        "created_at": a.created_at,
        "tenant_id": a.tenant_id,
    }


async def _owned(aid: str, tenant: Tenant) -> Agent:
    a = await Agent.get(aid)
    if not a or a.tenant_id != str(tenant.id):
        raise HTTPException(404, "Agent not found")
    return a


def _tenant_id_from_actor(actor) -> str:
    if hasattr(actor, 'business_name'):
        return str(actor.id)
    return actor.tenant_id


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.post("/login")
async def agent_login(body: AgentLoginRequest):
    agent = await Agent.find_one(Agent.email == body.email.lower())
    if not agent:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if body.tenant_id and agent.tenant_id != body.tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not verify_password(body.password, agent.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    if not agent.is_active:
        raise HTTPException(403, "Your account has been deactivated. Contact your admin.")

    agent.last_login_at = datetime.utcnow()
    await agent.save()

    effective_perms = getattr(agent, 'custom_permissions', None) or PERMS.get(agent.role, [])
    payload = {"sub": str(agent.id), "type_": "agent", "tenant_id": agent.tenant_id, "role": agent.role}
    return {
        "access_token":    create_access_token(payload),
        "refresh_token":   create_refresh_token(payload),
        "token_type":      "bearer",
        "agent_id":        str(agent.id),
        "name":            agent.name,
        "email":           agent.email,
        "role":            agent.role,
        "permissions":     effective_perms,
        "avatar_initials": agent.avatar_initials or (agent.name[0].upper() if agent.name else 'A'),
        "tenant_id":       agent.tenant_id,
    }


@router.post("/refresh")
async def agent_refresh(body: AgentRefreshRequest):
    try:
        data = decode_token(body.refresh_token)
    except ValueError:
        raise HTTPException(401, "Invalid or expired refresh token")
    if data.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token")
    agent = await Agent.get(data.get("sub"))
    if not agent or not agent.is_active:
        raise HTTPException(401, "Agent not found or inactive")
    effective_perms = getattr(agent, 'custom_permissions', None) or PERMS.get(agent.role, [])
    payload = {"sub": str(agent.id), "type_": "agent",
               "tenant_id": agent.tenant_id, "role": agent.role}
    return {
        "access_token":    create_access_token(payload),
        "refresh_token":   create_refresh_token(payload),
        "token_type":      "bearer",
        "agent_id":        str(agent.id),
        "name":            agent.name,
        "email":           agent.email,
        "role":            agent.role,
        "permissions":     effective_perms,
        "avatar_initials": agent.avatar_initials or (agent.name[0].upper() if agent.name else 'A'),
        "tenant_id":       agent.tenant_id,
    }


# ── Agent self-service endpoints ──────────────────────────────────────────────

@router.get("/me")
async def get_agent_me(agent: Agent = Depends(get_current_agent)):
    agent.last_active_at = datetime.utcnow()
    await agent.save()
    return _s(agent)


@router.patch("/me")
async def update_agent_profile(body: UpdateAgentProfileRequest, agent: Agent = Depends(get_current_agent)):
    if body.name:
        agent.name = body.name
        agent.avatar_initials = "".join(w[0].upper() for w in body.name.split()[:2])
    agent.updated_at = datetime.utcnow()
    await agent.save()
    return _s(agent)


@router.post("/me/change-password")
async def change_agent_password(body: ChangePasswordRequest, agent: Agent = Depends(get_current_agent)):
    if not verify_password(body.current_password, agent.hashed_password):
        raise HTTPException(400, "Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    agent.hashed_password = hash_password(body.new_password)
    agent.updated_at = datetime.utcnow()
    await agent.save()
    return {"message": "Password changed successfully"}


# ── Tenant-managed agent endpoints ────────────────────────────────────────────

@router.get("")
async def list_agents(tenant: Tenant = Depends(get_current_tenant)):
    agents = await Agent.find(Agent.tenant_id == str(tenant.id)).to_list()
    return [_s(a) for a in agents]


@router.post("", status_code=201)
async def create_agent(body: CreateAgentRequest, tenant: Tenant = Depends(get_current_tenant)):
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}")
    if await Agent.find_one(Agent.tenant_id == str(tenant.id), Agent.email == body.email.lower()):
        raise HTTPException(409, "Agent email already exists")
    initials = "".join(w[0].upper() for w in body.name.split()[:2])
    a = Agent(tenant_id=str(tenant.id), name=body.name, email=body.email.lower(),
              hashed_password=hash_password(body.password), role=body.role, avatar_initials=initials)
    await a.insert()
    return _s(a)


@router.patch("/{aid}")
async def update_agent(aid: str, body: UpdateAgentRequest,
                       tenant: Tenant = Depends(get_current_tenant)):
    a = await _owned(aid, tenant)
    if body.name:
        a.name = body.name
        a.avatar_initials = "".join(w[0].upper() for w in body.name.split()[:2])
    if body.role:
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of {ROLES}")
        a.role = body.role
    if body.is_active is not None:
        a.is_active = body.is_active
    if body.permissions is not None:
        a.custom_permissions = body.permissions
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        a.hashed_password = hash_password(body.password)
    a.updated_at = datetime.utcnow()
    await a.save()
    return _s(a)


@router.delete("/{aid}", status_code=204)
async def delete_agent(aid: str, tenant: Tenant = Depends(get_current_tenant)):
    await (await _owned(aid, tenant)).delete()


# ── Superadmin / actor endpoints (accept both tenant owner and superadmin agent) ──

@router.get("/{aid}")
async def get_agent(aid: str, actor=Depends(require_superadmin)):
    tenant_id = _tenant_id_from_actor(actor)
    a = await Agent.find_one(Agent.id == PydanticObjectId(aid), Agent.tenant_id == tenant_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return _s(a)


@router.post("/{aid}/activate")
async def activate_agent(aid: str, actor=Depends(require_superadmin)):
    tenant_id = _tenant_id_from_actor(actor)
    a = await Agent.find_one(Agent.id == PydanticObjectId(aid), Agent.tenant_id == tenant_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    a.is_active = True
    a.updated_at = datetime.utcnow()
    await a.save()
    return _s(a)


@router.post("/{aid}/deactivate")
async def deactivate_agent(aid: str, actor=Depends(require_superadmin)):
    tenant_id = _tenant_id_from_actor(actor)
    a = await Agent.find_one(Agent.id == PydanticObjectId(aid), Agent.tenant_id == tenant_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    a.is_active = False
    a.updated_at = datetime.utcnow()
    await a.save()
    return _s(a)


@router.post("/{aid}/reset-password")
async def reset_agent_password(aid: str, body: ResetPasswordRequest, actor=Depends(require_superadmin)):
    tenant_id = _tenant_id_from_actor(actor)
    a = await Agent.find_one(Agent.id == PydanticObjectId(aid), Agent.tenant_id == tenant_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    a.hashed_password = hash_password(body.new_password)
    a.updated_at = datetime.utcnow()
    await a.save()
    return {"message": "Password reset successfully"}
