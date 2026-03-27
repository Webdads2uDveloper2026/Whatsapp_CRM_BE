from datetime import datetime
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Query, UploadFile, File
from pydantic import BaseModel
from app.models.contact import Contact
from app.models.tenant import Tenant
from app.core.dependencies import get_current_tenant

router = APIRouter(prefix="/contacts", tags=["contacts"])


class CreateContactRequest(BaseModel):
    wa_id: str
    profile_name: Optional[str] = None
    email: Optional[str] = None
    tags: list[str] = []
    custom_fields: dict[str, Any] = {}
    opted_in: bool = False


class UpdateContactRequest(BaseModel):
    profile_name: Optional[str] = None
    email: Optional[str] = None
    tags: Optional[list[str]] = None
    custom_fields: Optional[dict[str, Any]] = None
    opted_in: Optional[bool] = None
    is_blocked: Optional[bool] = None


class BulkContactItem(BaseModel):
    wa_id: str
    profile_name: Optional[str] = None
    email: Optional[str] = None
    tags: list[str] = []
    opted_in: bool = False


class BulkUploadRequest(BaseModel):
    contacts: list[BulkContactItem]


@router.get("")
async def list_contacts(
    tenant: Tenant = Depends(get_current_tenant),
    tag: Optional[str] = Query(None),
    opted_in: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    q = Contact.find(Contact.tenant_id == str(tenant.id))
    if tag:
        q = q.find({"tags": tag})
    if opted_in is not None:
        q = q.find(Contact.opted_in == opted_in)
    if search:
        q = q.find({"$or": [
            {"profile_name": {"$regex": search, "$options": "i"}},
            {"wa_id": {"$regex": search}},
            {"email": {"$regex": search, "$options": "i"}},
        ]})
    total = await q.count()
    contacts = await q.skip((page - 1) * limit).limit(limit).to_list()
    return {"total": total, "page": page, "limit": limit,
            "contacts": [_s(c) for c in contacts]}


@router.post("", status_code=201)
async def create_contact(body: CreateContactRequest,
                         tenant: Tenant = Depends(get_current_tenant)):
    if await Contact.find_one(Contact.tenant_id == str(tenant.id), Contact.wa_id == body.wa_id):
        raise HTTPException(409, "Contact already exists")
    c = Contact(tenant_id=str(tenant.id), **body.model_dump())
    await c.insert()
    return _s(c)


@router.get("/{cid}")
async def get_contact(cid: str, tenant: Tenant = Depends(get_current_tenant)):
    return _s(await _owned(cid, tenant))


@router.patch("/{cid}")
async def update_contact(cid: str, body: UpdateContactRequest,
                         tenant: Tenant = Depends(get_current_tenant)):
    c = await _owned(cid, tenant)
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(c, f, v)
    c.updated_at = datetime.utcnow()
    await c.save()
    return _s(c)


@router.delete("/{cid}", status_code=204)
async def delete_contact(cid: str, tenant: Tenant = Depends(get_current_tenant)):
    await (await _owned(cid, tenant)).delete()


@router.post("/{cid}/tags/{tag}")
async def add_tag(cid: str, tag: str, tenant: Tenant = Depends(get_current_tenant)):
    c = await _owned(cid, tenant)
    if tag not in c.tags:
        c.tags.append(tag)
        await c.save()
    return {"tags": c.tags}


@router.delete("/{cid}/tags/{tag}")
async def remove_tag(cid: str, tag: str, tenant: Tenant = Depends(get_current_tenant)):
    c = await _owned(cid, tenant)
    c.tags = [t for t in c.tags if t != tag]
    await c.save()
    return {"tags": c.tags}


@router.post("/bulk", status_code=201)
async def bulk_upload(body: BulkUploadRequest,
                      tenant: Tenant = Depends(get_current_tenant)):
    created = skipped = 0
    for item in body.contacts:
        if await Contact.find_one(Contact.tenant_id == str(tenant.id),
                                  Contact.wa_id == item.wa_id):
            skipped += 1
            continue
        await Contact(tenant_id=str(tenant.id), **item.model_dump()).insert()
        created += 1
    return {"created": created, "skipped": skipped, "total": len(body.contacts)}


async def _owned(cid: str, tenant: Tenant) -> Contact:
    c = await Contact.get(cid)
    if not c or c.tenant_id != str(tenant.id):
        raise HTTPException(404, "Contact not found")
    return c


def _s(c: Contact) -> dict:
    return {"id": str(c.id), "wa_id": c.wa_id, "profile_name": c.profile_name,
            "email": c.email, "tags": c.tags, "custom_fields": c.custom_fields,
            "opted_in": c.opted_in, "is_blocked": c.is_blocked,
            "last_seen_at": c.last_seen_at, "created_at": c.created_at}
