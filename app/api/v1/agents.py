from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token
from app.core.dependencies import get_current_tenant

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
    tenant_id: str


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


@router.post("/login")
async def agent_login(body: AgentLoginRequest):
    agent = await Agent.find_one(Agent.tenant_id == body.tenant_id,
                                  Agent.email == body.email.lower())
    if not agent or not verify_password(body.password, agent.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not agent.is_active:
        raise HTTPException(403, "Account deactivated")
    agent.last_login_at = datetime.utcnow()
    await agent.save()
    data = {"sub": str(agent.id), "type_": "agent", "tenant_id": body.tenant_id, "role": agent.role}
    return {
        "access_token":  create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type":    "bearer",
        "agent_id":      str(agent.id),
        "name":          agent.name,
        "role":          agent.role,
        "permissions":   PERMS.get(agent.role, []),
    }


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
    if body.role:
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of {ROLES}")
        a.role = body.role
    if body.is_active is not None:
        a.is_active = body.is_active
    a.updated_at = datetime.utcnow()
    await a.save()
    return _s(a)


@router.delete("/{aid}", status_code=204)
async def delete_agent(aid: str, tenant: Tenant = Depends(get_current_tenant)):
    await (await _owned(aid, tenant)).delete()


async def _owned(aid: str, tenant: Tenant) -> Agent:
    a = await Agent.get(aid)
    if not a or a.tenant_id != str(tenant.id):
        raise HTTPException(404, "Agent not found")
    return a


def _s(a: Agent) -> dict:
    return {"id": str(a.id), "name": a.name, "email": a.email, "role": a.role,
            "is_active": a.is_active, "avatar_initials": a.avatar_initials,
            "permissions": PERMS.get(a.role, []), "last_login_at": a.last_login_at,
            "created_at": a.created_at}
