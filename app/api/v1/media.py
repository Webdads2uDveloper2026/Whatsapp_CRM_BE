"""
app/api/v1/media.py — Upload media to Meta for use in template headers
"""
import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from app.models.tenant import Tenant
from app.core.dependencies import get_active_tenant
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
    tenant: Tenant     = Depends(get_active_tenant),
):
    """
    Upload media to Meta's servers.
    Returns: { "id": "meta_media_id", "url": "optional_url" }
    Used for template header images/videos/documents.
    """
    from app.core.security import decrypt_token

    # Get token
    token = None
    if tenant.encrypted_access_token:
        try:
            token = decrypt_token(tenant.encrypted_access_token)
        except Exception:
            pass
    token = token or settings.meta_access_token

    phone_id = tenant.phone_number_id or settings.meta_phone_number_id

    if not token or not phone_id:
        raise HTTPException(400, "WhatsApp not configured")

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