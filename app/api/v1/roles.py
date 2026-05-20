from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
from app.core.dependencies import require_superadmin
from app.models.tenant import Tenant

router = APIRouter(prefix="/roles", tags=["roles"])

ALL_PERMISSIONS = [
    "inbox", "contacts", "broadcasts", "templates",
    "automations", "analytics", "settings", "agents"
]

DEFAULT_PERMS = {
    "superadmin": ALL_PERMISSIONS,
    "manager":    ["inbox","contacts","broadcasts","templates","automations","analytics"],
    "agent":      ["inbox","contacts"],
}

ROLE_META = [
    {"role": "superadmin", "label": "Super Admin",
     "description": "Full access to all features including user management and settings."},
    {"role": "manager",    "label": "Manager",
     "description": "Access to inbox, contacts, broadcasts, templates, automations, analytics."},
    {"role": "agent",      "label": "Agent",
     "description": "Access to inbox and contacts only."},
]


@router.get("")
async def list_roles():
    return {
        "roles": [
            {**m, "permissions": DEFAULT_PERMS[m["role"]]}
            for m in ROLE_META
        ],
        "available_permissions": ALL_PERMISSIONS,
    }


@router.get("/permissions-matrix")
async def permissions_matrix():
    return {
        "permissions": ALL_PERMISSIONS,
        "matrix": {
            role: {p: (p in perms) for p in ALL_PERMISSIONS}
            for role, perms in DEFAULT_PERMS.items()
        },
    }


class UpdateRolePermissionsRequest(BaseModel):
    permissions: List[str]


@router.patch("/{role}/permissions")
async def update_role_permissions(
    role: str,
    body: UpdateRolePermissionsRequest,
    actor=Depends(require_superadmin),
):
    if role not in DEFAULT_PERMS:
        raise HTTPException(400, f"Role must be one of: {list(DEFAULT_PERMS.keys())}")
    if role == "superadmin":
        raise HTTPException(400, "Super Admin permissions cannot be modified.")

    invalid = [p for p in body.permissions if p not in ALL_PERMISSIONS]
    if invalid:
        raise HTTPException(400, f"Invalid permissions: {invalid}")

    if hasattr(actor, 'business_name'):
        tenant = actor
    else:
        tenant = await Tenant.get(actor.tenant_id)

    if not tenant.custom_role_permissions:
        tenant.custom_role_permissions = {}
    tenant.custom_role_permissions[role] = body.permissions
    await tenant.save()
    return {"role": role, "permissions": body.permissions}
