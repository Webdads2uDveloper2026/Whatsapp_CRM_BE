from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import List
from app.core.security import decode_token
from app.models.tenant import Tenant
from app.models.agent import Agent

bearer = HTTPBearer()


async def get_current_tenant(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> Tenant:
    try:
        payload = decode_token(creds.credentials)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Not an access token")

    tenant_id = payload.get("sub")
    tenant = await Tenant.get(tenant_id)
    if not tenant:
        raise HTTPException(status_code=401, detail="Tenant not found")
    return tenant


async def get_active_tenant(tenant: Tenant = Depends(get_current_tenant)) -> Tenant:
    if tenant.status != "active":
        raise HTTPException(
            status_code=403,
            detail="WhatsApp account not connected. Complete onboarding first.",
        )
    return tenant


async def get_current_agent(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> Agent:
    try:
        payload = decode_token(creds.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if payload.get("type_") != "agent":
        raise HTTPException(status_code=401, detail="Not an agent token")

    agent = await Agent.get(payload.get("sub"))
    if not agent or not agent.is_active:
        raise HTTPException(status_code=401, detail="Agent not found or inactive")
    return agent


def get_current_agent_role(required_roles: List[str]):
    async def _check(agent: Agent = Depends(get_current_agent)) -> Agent:
        if agent.role not in required_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Required role: {required_roles}. Your role: {agent.role}"
            )
        return agent
    return _check


async def require_superadmin(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
):
    try:
        payload = decode_token(creds.credentials)
    except ValueError:
        raise HTTPException(401, "Invalid token")

    token_type = payload.get("type") or payload.get("type_")

    # Tenant owner has full access
    if payload.get("type") == "access" and not payload.get("type_"):
        tenant = await Tenant.get(payload.get("sub"))
        if not tenant:
            raise HTTPException(401, "Tenant not found")
        return tenant

    # Agent token — must be superadmin
    if payload.get("type_") == "agent":
        agent = await Agent.get(payload.get("sub"))
        if not agent or not agent.is_active:
            raise HTTPException(401, "Agent not found or inactive")
        if agent.role != "superadmin":
            raise HTTPException(403, "Super Admin access required")
        return agent

    raise HTTPException(401, "Invalid token type")


async def get_tenant_from_token(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
) -> Tenant:
    """Accepts EITHER a tenant token OR an agent token. Always returns the Tenant."""
    try:
        payload = decode_token(creds.credentials)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if payload.get("type_") == "agent":
        agent = await Agent.get(payload.get("sub"))
        if not agent or not agent.is_active:
            raise HTTPException(status_code=401, detail="Agent not found or inactive")
        tenant = await Tenant.get(agent.tenant_id)
        if not tenant:
            raise HTTPException(status_code=401, detail="Tenant not found")
        return tenant

    if payload.get("type") == "access":
        tenant = await Tenant.get(payload.get("sub"))
        if not tenant:
            raise HTTPException(status_code=401, detail="Tenant not found")
        return tenant

    raise HTTPException(status_code=401, detail="Invalid token type")


async def get_active_tenant_from_token(
    tenant: Tenant = Depends(get_tenant_from_token),
) -> Tenant:
    """Same as get_tenant_from_token but also checks tenant WhatsApp is connected."""
    if tenant.status != "active":
        raise HTTPException(
            status_code=403,
            detail="WhatsApp account not connected. Complete onboarding first.",
        )
    return tenant
