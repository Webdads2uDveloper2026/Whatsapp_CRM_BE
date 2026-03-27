"""
Onboarding API — Meta WhatsApp Embedded Signup
Based on official Meta docs: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup

Full flow:
1. Frontend: FB.login() → gets code + waba_id + phone_number_id
2. POST /onboarding/embedded-signup → backend exchanges code for access token
3. Backend subscribes app to WABA webhooks
4. Backend fetches phone number details
5. Tenant marked as active

Your account values (from PDF + previous session):
  App ID:         671128085546989
  Business ID:    1049205566947316
  WABA ID:        4078908755658039
  Phone ID:       162465382
  Config ID:      1198909162287179
"""
import httpx
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, EmailStr
from app.config import get_settings
from app.models.tenant import Tenant
from app.core.security import hash_password, create_access_token, create_refresh_token, encrypt_token
from app.core.dependencies import get_current_tenant

router   = APIRouter(prefix="/onboarding", tags=["onboarding"])
settings = get_settings()

GRAPH = "https://graph.facebook.com"
API_V = settings.meta_api_version or "v22.0"


# ── Schemas ───────────────────────────────────────────────────────────────────
class RegisterBody(BaseModel):
    business_name: str
    email:         EmailStr
    password:      str

class EmbeddedSignupBody(BaseModel):
    code:            str            # Short-lived code from FB.login()
    waba_id:         Optional[str] = ""   # From WA_EMBEDDED_SIGNUP message event
    phone_number_id: Optional[str] = ""   # From WA_EMBEDDED_SIGNUP message event

class ManualConnectBody(BaseModel):
    waba_id:         str
    phone_number_id: str
    access_token:    str

class RefreshTokenBody(BaseModel):
    refresh_token: str


# ── Register (email/password) ─────────────────────────────────────────────────
@router.post("/register", status_code=201)
async def register(body: RegisterBody):
    if await Tenant.find_one(Tenant.email == body.email.lower()):
        raise HTTPException(409, "An account with this email already exists")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    tenant = Tenant(
        business_name   = body.business_name,
        email           = body.email.lower(),
        hashed_password = hash_password(body.password),
        status          = "pending",
    )
    await tenant.insert()

    tid     = str(tenant.id)
    payload = {"sub": tid, "tenant_id": tid}
    return {
        "access_token":  create_access_token(payload),
        "refresh_token": create_refresh_token(payload),
        "token_type":    "bearer",
        "tenant_id":     tid,
        "status":        tenant.status,
    }


# ── Embedded Signup — exchange code for access token ─────────────────────────
@router.post("/embedded-signup")
async def embedded_signup(
    body:   EmbeddedSignupBody,
    tenant: Tenant = Depends(get_current_tenant),
):
    """
    Exchange the short-lived code from FB.login() for a long-lived
    Business Integration System User access token.

    Per Meta docs:
    GET https://graph.facebook.com/v22.0/oauth/access_token
        ?client_id=APP_ID
        &client_secret=APP_SECRET
        &code=CODE
    """
    if not body.code:
        raise HTTPException(400, "code is required")
    if not settings.meta_app_secret:
        raise HTTPException(500, "META_APP_SECRET not configured in .env")

    # ── Step 1: Exchange code for access token ────────────────────────────
    async with httpx.AsyncClient(timeout=30) as client:
        token_res = await client.get(
            f"{GRAPH}/{API_V}/oauth/access_token",
            params={
                "client_id":     settings.meta_app_id,
                "client_secret": settings.meta_app_secret,
                "code":          body.code,
            }
        )

    token_data = token_res.json()
    if "error" in token_data:
        err = token_data["error"]
        raise HTTPException(400, f"Meta token exchange failed: {err.get('message', str(err))}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(400, f"No access token in Meta response: {token_data}")

    # ── Step 2: Get WABA details ──────────────────────────────────────────
    # Use waba_id from the ES message event, or fall back to the one in .env
    waba_id = body.waba_id or settings.meta_waba_id
    phone_number_id = body.phone_number_id or settings.meta_phone_number_id

    if not waba_id:
        # Try to discover WABA from the token
        async with httpx.AsyncClient(timeout=30) as client:
            waba_res = await client.get(
                f"{GRAPH}/{API_V}/debug_token",
                params={
                    "input_token":  access_token,
                    "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}",
                }
            )
        debug = waba_res.json().get("data", {})
        # Try to get WABA from granular_scopes
        for scope in debug.get("granular_scopes", []):
            if scope.get("scope") == "whatsapp_business_management":
                targets = scope.get("target_ids", [])
                if targets:
                    waba_id = targets[0]
                    break

    if not waba_id:
        raise HTTPException(400, "Could not determine WABA ID. Make sure the session info listener captured it.")

    # ── Step 3: Get phone number details ──────────────────────────────────
    phone_number    = ""
    display_name    = ""

    if phone_number_id:
        async with httpx.AsyncClient(timeout=30) as client:
            ph_res = await client.get(
                f"{GRAPH}/{API_V}/{phone_number_id}",
                params={
                    "fields":       "display_phone_number,verified_name,status",
                    "access_token": access_token,
                }
            )
        ph_data = ph_res.json()
        if "error" not in ph_data:
            phone_number = ph_data.get("display_phone_number", "")
            display_name = ph_data.get("verified_name", "")
    else:
        # Fetch first phone number from WABA
        async with httpx.AsyncClient(timeout=30) as client:
            phones_res = await client.get(
                f"{GRAPH}/{API_V}/{waba_id}/phone_numbers",
                params={"access_token": access_token}
            )
        phones = phones_res.json().get("data", [])
        if phones:
            phone_number_id = phones[0].get("id", "")
            phone_number    = phones[0].get("display_phone_number", "")
            display_name    = phones[0].get("verified_name", "")

    # ── Step 4: Subscribe app to WABA webhooks ────────────────────────────
    async with httpx.AsyncClient(timeout=30) as client:
        sub_res = await client.post(
            f"{GRAPH}/{API_V}/{waba_id}/subscribed_apps",
            params={"access_token": access_token}
        )
    sub_data = sub_res.json()
    if not sub_data.get("success"):
        # Non-fatal — log but continue
        print(f"[WARN] WABA webhook subscription: {sub_data}")

    # ── Step 5: Register webhook endpoint (set callback URL) ─────────────
    if settings.webhook_base_url:
        webhook_url   = f"{settings.webhook_base_url}/api/v1/webhook/{str(tenant.id)}"
        verify_token  = settings.webhook_verify_token or "default_verify_token"
        print(verify_token)
        async with httpx.AsyncClient(timeout=30) as client:
            wh_res = await client.post(
                f"{GRAPH}/{API_V}/{settings.meta_app_id}/subscriptions",
                params={
                    "object":           "whatsapp_business_account",
                    "callback_url":     webhook_url,
                    "verify_token":     verify_token,
                    "fields":           "messages,message_deliveries,message_reads,message_echoes",
                    "access_token":     f"{settings.meta_app_id}|{settings.meta_app_secret}",
                }
            )
        wh_data = wh_res.json()
        if not wh_data.get("success"):
            print(f"[WARN] Webhook registration: {wh_data}")

    # ── Step 6: Save to tenant ────────────────────────────────────────────
    tenant.waba_id               = waba_id
    tenant.phone_number_id       = phone_number_id
    tenant.display_phone_number  = phone_number
    tenant.waba_name             = display_name
    tenant.encrypted_access_token = encrypt_token(access_token)
    tenant.status                = "active"
    tenant.waba_connected        = True
    tenant.activated_at          = datetime.utcnow()
    await tenant.save()

    return {
        "status":       "active",
        "waba_id":      waba_id,
        "phone_number": phone_number,
        "display_name": display_name,
        "message":      "WhatsApp Business Account connected successfully",
    }


# ── Manual Connect ────────────────────────────────────────────────────────────
@router.post("/connect-manual")
async def connect_manual(
    body:   ManualConnectBody,
    tenant: Tenant = Depends(get_current_tenant),
):
    """Connect using manually entered credentials (WABA ID + Phone ID + token)."""
    # Validate token by fetching WABA info
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GRAPH}/{API_V}/{body.waba_id}",
            params={
                "fields":       "id,name,currency,message_template_namespace",
                "access_token": body.access_token,
            }
        )
    data = res.json()
    if "error" in data:
        err = data["error"]
        raise HTTPException(400, f"Meta error ({err.get('code','?')}): {err.get('message', str(err))}")

    # Get phone number
    phone_number = ""
    async with httpx.AsyncClient(timeout=30) as client:
        ph_res = await client.get(
            f"{GRAPH}/{API_V}/{body.phone_number_id}",
            params={
                "fields":       "display_phone_number,verified_name,status",
                "access_token": body.access_token,
            }
        )
    ph = ph_res.json()
    if "error" not in ph:
        phone_number = ph.get("display_phone_number", "")

    # Subscribe webhooks
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{GRAPH}/{API_V}/{body.waba_id}/subscribed_apps",
            params={"access_token": body.access_token}
        )

    tenant.waba_id                = body.waba_id
    tenant.phone_number_id        = body.phone_number_id
    tenant.display_phone_number   = phone_number
    tenant.waba_name              = data.get("name", "")
    tenant.encrypted_access_token = encrypt_token(body.access_token)
    tenant.status                 = "active"
    tenant.waba_connected         = True
    tenant.activated_at           = datetime.utcnow()
    await tenant.save()

    return {
        "status":       "active",
        "waba_id":      body.waba_id,
        "phone_number": phone_number,
        "message":      "Connected successfully via manual setup",
    }


# ── Status ────────────────────────────────────────────────────────────────────
@router.get("/status")
async def status(tenant: Tenant = Depends(get_current_tenant)):
    return {
        "status":         tenant.status,
        "waba_connected": tenant.waba_connected,
        "waba_id":        tenant.waba_id,
        "phone_number":   tenant.display_phone_number,
        "phone_number_id": tenant.phone_number_id,
    }


# ── Webhook verify (GET) ──────────────────────────────────────────────────────
@router.get("/webhook-verify")
async def webhook_verify(
    hub_mode:          Optional[str] = None,
    hub_verify_token:  Optional[str] = None,
    hub_challenge:     Optional[str] = None,
):
    """Meta calls this to verify your webhook endpoint during setup."""
    from fastapi.responses import PlainTextResponse
    if hub_mode == "subscribe" and hub_verify_token == settings.webhook_verify_token:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(403, "Verification failed")