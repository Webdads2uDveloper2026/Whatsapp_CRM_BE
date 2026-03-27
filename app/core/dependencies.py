from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
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
