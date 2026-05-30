"""
app/api/v1/media.py — Upload media to Meta for use in template headers
"""
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Query, Request
from app.models.tenant import Tenant
from app.core.dependencies import get_active_tenant, get_active_tenant_from_token, get_tenant_from_token
from app.config import get_settings

router   = APIRouter(prefix="/media", tags=["media"])
settings = get_settings()

ALLOWED = {
    "image":    {"image/jpeg","image/png","image/webp"},
    "video":    {"video/mp4","video/3gpp"},
    "document": {"application/pdf","application/msword",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
}

@router.post("/upload")
async def upload_media(
    file:   UploadFile = File(...),
    type:   str        = Form("image"),
    tenant: Tenant     = Depends(get_active_tenant_from_token),
):
    """
    Upload media to Meta's servers.
    Returns: { "id": "meta_media_id", "url": "optional_url" }
    Used for template header images/videos/documents.
    """
    from app.services.whatsapp import resolve_token
    token    = resolve_token(tenant)
    phone_id = (tenant.phone_number_id or settings.meta_phone_number_id or "").strip()

    if not token or not phone_id:
        raise HTTPException(400, "WhatsApp not configured — set META_SYSTEM_USER_TOKEN and complete onboarding")

    # Validate file type
    content_type = file.content_type or "application/octet-stream"
    allowed_types = ALLOWED.get(type, set())
    if allowed_types and content_type not in allowed_types:
        raise HTTPException(400, f"Invalid file type {content_type} for {type}. Allowed: {allowed_types}")

    # Read file
    data = await file.read()
    if len(data) > 100 * 1024 * 1024:  # 100MB max
        raise HTTPException(400, "File too large (max 100MB)")

    # Upload to Meta
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://graph.facebook.com/{settings.meta_api_version}/{phone_id}/media",
            headers = {"Authorization": f"Bearer {token}"},
            data    = {"messaging_product": "whatsapp", "type": content_type},
            files   = {"file": (file.filename, data, content_type)},
        )
        resp = r.json()

    if "error" in resp:
        err = resp["error"]
        raise HTTPException(400, f"Meta upload error: {err.get('message', str(err))}")

    media_id = resp.get("id", "")
    return {
        "id":       media_id,
        "filename": file.filename,
        "type":     type,
        "message":  "Media uploaded successfully",
    }


@router.get("/proxy")
async def proxy_media(
    media_id: str,
    request:  Request = None,
    token:    str     = Query(default=""),   # <img> tags can't send headers — accept token here
):
    """
    Proxy Meta media to browser.
    Meta media URLs require Bearer auth — browsers cannot fetch them directly.
    Accepts JWT as ?token= (for <img>/<video>/<audio> tags) or Authorization header.
    Usage: GET /media/proxy?media_id=<id>&token=<jwt>
    """
    from app.services.whatsapp import resolve_token
    from app.core.security import decode_token
    from app.models.tenant import Tenant as TenantModel
    from fastapi.responses import StreamingResponse

    # Resolve JWT from query param or Authorization header
    auth_header = request.headers.get("Authorization", "") if request else ""
    jwt = token or (auth_header[7:] if auth_header.startswith("Bearer ") else "")
    if not jwt:
        raise HTTPException(401, "Authentication required — pass ?token=<jwt>")

    try:
        payload = decode_token(jwt)
        tenant  = await TenantModel.get(payload.get("sub"))
        if not tenant:
            raise HTTPException(401, "Tenant not found")
    except Exception:
        raise HTTPException(401, "Invalid token")

    meta_token = resolve_token(tenant)
    api_ver    = getattr(settings, "meta_api_version", "v22.0")

    if not meta_token:
        raise HTTPException(400, "WhatsApp not configured")

    # Step 1: resolve the actual download URL from Meta
    async with httpx.AsyncClient(timeout=15) as client:
        r         = await client.get(
            f"https://graph.facebook.com/{api_ver}/{media_id}",
            headers={"Authorization": f"Bearer {meta_token}"},
        )
        meta_data = r.json()

    if "error" in meta_data:
        raise HTTPException(404, f"Media not found: {meta_data['error'].get('message','')}")

    media_url = meta_data.get("url")
    mime_type = meta_data.get("mime_type", "application/octet-stream")
    file_size = meta_data.get("file_size")

    if not media_url:
        raise HTTPException(404, "Media URL not available from Meta")

    # Step 2: stream bytes back to the browser
    async def _stream():
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream("GET", media_url, headers={"Authorization": f"Bearer {meta_token}"}) as r:
                async for chunk in r.aiter_bytes(8192):
                    yield chunk

    headers = {
        "Content-Type":  mime_type,
        "Cache-Control": "private, max-age=3600",
    }
    if file_size:
        headers["Content-Length"] = str(file_size)

    return StreamingResponse(_stream(), media_type=mime_type, headers=headers)