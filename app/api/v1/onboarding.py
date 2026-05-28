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
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from app.config import get_settings
from app.models.tenant import Tenant
from app.core.security import hash_password, create_access_token, create_refresh_token, encrypt_token, decrypt_token
from app.core.dependencies import get_current_tenant

router   = APIRouter(prefix="/onboarding", tags=["onboarding"])
settings = get_settings()

GRAPH = "https://graph.facebook.com"
API_V = settings.meta_api_version or "v22.0"


# ── Webhook auto-registration ─────────────────────────────────────────────────
async def _register_app_webhook() -> dict:
    """
    Register the webhook URL at the Meta App level via the Graph API.

    This is equivalent to filling in the Callback URL + Verify Token in the
    Meta App Dashboard → WhatsApp → Configuration, but done programmatically.

    Meta will immediately send a GET verification request to the callback_url —
    the backend must be publicly reachable at that URL when this is called.

    Uses App Access Token ({app_id}|{app_secret}) — no user token needed.
    """
    webhook_base = (settings.webhook_base_url or settings.backend_url or "").strip().rstrip("/")
    if not webhook_base or "localhost" in webhook_base or "127.0.0.1" in webhook_base:
        return {"error": "WEBHOOK_BASE_URL must be a public HTTPS URL (not localhost). Set it in your .env and restart."}

    if not settings.meta_app_id or not settings.meta_app_secret:
        return {"error": "META_APP_ID and META_APP_SECRET must be set in .env"}

    callback_url  = f"{webhook_base}/api/v1/webhook/whatsapp"   # canonical endpoint
    app_token     = f"{settings.meta_app_id}|{settings.meta_app_secret}"

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(
            f"{GRAPH}/{API_V}/{settings.meta_app_id}/subscriptions",
            params={
                "object":       "whatsapp_business_account",
                "callback_url": callback_url,
                "fields":       "messages,message_template_status_update,account_update,phone_number_quality_update,phone_number_name_update,flows,security",
                "verify_token": settings.webhook_verify_token,
                "access_token": app_token,
            }
        )
    data = res.json()
    print(f"[WEBHOOK] App subscription result: {data}")

    if data.get("success"):
        return {"success": True, "callback_url": callback_url}
    if "error" in data:
        err = data["error"]
        return {"error": f"Meta error ({err.get('code')}): {err.get('message', str(err))}"}
    return {"error": f"Unexpected response: {data}"}


# ── Schemas ───────────────────────────────────────────────────────────────────
class RegisterBody(BaseModel):
    business_name: str
    email:         EmailStr
    password:      str

class EmbeddedSignupBody(BaseModel):
    code:            str            # Short-lived code from FB.login()
    waba_id:         Optional[str] = ""   # From WA_EMBEDDED_SIGNUP message event
    phone_number_id: Optional[str] = ""   # From WA_EMBEDDED_SIGNUP message event
    redirect_uri:    Optional[str] = ""   # Reserved — not used in SDK popup flow

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
    print(f"[DEBUG] Meta token response: {token_data}")
    if "error" in token_data:
        err = token_data["error"]
        raise HTTPException(400, f"Meta token exchange failed (code {err.get('code')}): {err.get('message', str(err))}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(400, f"No access token in Meta response: {token_data}")

    # ── Step 1b: Exchange short-lived token for long-lived token (60 days) ──
    async with httpx.AsyncClient(timeout=30) as client:
        ll_res = await client.get(
            f"{GRAPH}/{API_V}/oauth/access_token",
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         settings.meta_app_id,
                "client_secret":     settings.meta_app_secret,
                "fb_exchange_token": access_token,
            }
        )
    ll_data = ll_res.json()
    print(f"[DEBUG] Long-lived token response: {ll_data}")
    if "access_token" in ll_data:
        access_token = ll_data["access_token"]  # use long-lived token
        print(f"[DEBUG] Using long-lived token (expires_in={ll_data.get('expires_in')}s)")

    # ── Step 2: Get WABA details ──────────────────────────────────────────
    # Use waba_id from the ES message event only — never fall back to .env
    waba_id         = body.waba_id or ""
    phone_number_id = body.phone_number_id or ""

    # Guard: frontend sometimes sends phone_number_id in the waba_id field
    if waba_id and phone_number_id and waba_id == phone_number_id:
        print(f"[WARN] waba_id == phone_number_id ({waba_id}) — clearing waba_id to force discovery")
        waba_id = ""

    if not waba_id:
        # Try to discover WABA from the token via debug_token granular_scopes
        async with httpx.AsyncClient(timeout=30) as client:
            waba_res = await client.get(
                f"{GRAPH}/{API_V}/debug_token",
                params={
                    "input_token":  access_token,
                    "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}",
                }
            )
        debug = waba_res.json().get("data", {})
        print(f"[DEBUG] debug_token granular_scopes: {debug.get('granular_scopes', [])}")
        for scope in debug.get("granular_scopes", []):
            if scope.get("scope") == "whatsapp_business_management":
                targets = scope.get("target_ids", [])
                if targets:
                    waba_id = targets[0]
                    break

    if not waba_id:
        raise HTTPException(400, "Could not determine WABA ID. Make sure the session info listener captured it.")

    # Final guard: waba_id must not equal phone_number_id
    if phone_number_id and waba_id == phone_number_id:
        raise HTTPException(400,
            f"waba_id ({waba_id}) matches phone_number_id — IDs are mixed up. "
            "Check that your Embedded Signup listener sends the correct IDs."
        )

    print(f"[INFO] Onboarding IDs — waba_id={waba_id}  phone_number_id={phone_number_id}")

    # ── Step 3: Get phone number details — always fetch from WABA API ────
    phone_number = ""
    display_name = ""

    async with httpx.AsyncClient(timeout=30) as client:
        phones_res = await client.get(
            f"{GRAPH}/{API_V}/{waba_id}/phone_numbers",
            params={
                "fields":       "id,display_phone_number,verified_name,status",
                "access_token": access_token,
            }
        )
    phones = phones_res.json().get("data", [])
    if phones:
        phone_number_id = phones[0].get("id", "")
        phone_number    = phones[0].get("display_phone_number", "")
        display_name    = phones[0].get("verified_name", "")
        print(f"[INFO] phone_number_id={phone_number_id} display={phone_number}")
    else:
        print(f"[WARN] No phone numbers found for WABA {waba_id}: {phones_res.json()}")

    # ── Step 4: Assign Tech Provider system user to customer WABA ────────
    partner_token = settings.meta_system_user_token or access_token
    if settings.meta_system_user_id and settings.meta_system_user_token:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                assign_res = await client.post(
                    f"{GRAPH}/{API_V}/{waba_id}/assigned_users",
                    # tasks must be repeated params, not a comma-separated string
                    params=[
                        ("user",         settings.meta_system_user_id),
                        ("tasks",        "MANAGE"),
                        ("tasks",        "DEVELOP"),
                        ("tasks",        "MESSAGING"),
                        ("access_token", access_token),
                    ]
                )
            assign_data = assign_res.json()
            if assign_data.get("success"):
                print(f"[INFO] System user assigned to WABA {waba_id} — using partner token")
                partner_token = settings.meta_system_user_token
            else:
                print(f"[WARN] System user assignment: {assign_data} — falling back to customer token")
        except Exception as e:
            print(f"[WARN] System user assignment failed ({e.__class__.__name__}: {e}) — falling back to customer token")
    else:
        print("[WARN] META_SYSTEM_USER_ID/TOKEN not set — storing customer token (expires in 60 days)")

    # ── Step 5: Subscribe app to WABA webhooks ────────────────────────────
    async with httpx.AsyncClient(timeout=30) as client:
        sub_res = await client.post(
            f"{GRAPH}/{API_V}/{waba_id}/subscribed_apps",
            params={"access_token": partner_token}
        )
    sub_data = sub_res.json()
    if not sub_data.get("success"):
        print(f"[WARN] WABA webhook subscription: {sub_data}")

    # ── Step 6: Save to tenant — store partner token (never expires) ──────
    tenant.waba_id                = waba_id
    tenant.phone_number_id        = phone_number_id
    tenant.display_phone_number   = phone_number
    tenant.waba_name              = display_name
    tenant.encrypted_access_token = encrypt_token(partner_token)
    tenant.webhook_verify_token   = settings.webhook_verify_token   # global platform token
    tenant.status                 = "active"
    tenant.waba_connected         = True
    tenant.activated_at           = datetime.now(timezone.utc)
    await tenant.save()

    # ── Step 7: Subscribe phone number to app (enables incoming messages) ─
    if phone_number_id:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                sub_res = await client.post(
                    f"{GRAPH}/{API_V}/{phone_number_id}/subscribed_apps",
                    params={"access_token": partner_token}
                )
            sub_data = sub_res.json()
            if sub_data.get("success"):
                print(f"[INFO] Phone {phone_number_id} subscribed to app — incoming messages enabled")
            else:
                print(f"[WARN] Phone subscription: {sub_data}")
        except Exception as e:
            print(f"[WARN] Phone subscription failed: {e}")

    # ── Step 8: Register App-level webhook URL with Meta automatically ────
    # This is what WATI/Interakt do — no manual Meta Dashboard config needed.
    # Non-fatal: if it fails the customer can retry from Settings → Channels.
    try:
        wb_result = await _register_app_webhook()
        if wb_result.get("success"):
            print(f"[INFO] App webhook registered: {wb_result.get('callback_url')}")
        else:
            print(f"[WARN] App webhook registration skipped: {wb_result.get('error')}")
    except Exception as e:
        print(f"[WARN] App webhook registration failed: {e}")

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
    tenant.webhook_verify_token   = settings.webhook_verify_token
    tenant.status                 = "active"
    tenant.waba_connected         = True
    tenant.activated_at           = datetime.now(timezone.utc)
    await tenant.save()

    try:
        wb_result = await _register_app_webhook()
        if not wb_result.get("success"):
            print(f"[WARN] App webhook registration skipped: {wb_result.get('error')}")
    except Exception as e:
        print(f"[WARN] App webhook registration failed: {e}")

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


# ── Live phone number status from Meta ───────────────────────────────────────
@router.get("/phone-status")
async def phone_status(tenant: Tenant = Depends(get_current_tenant)):
    """
    Fetch real-time phone number status and quality rating directly from the
    Meta Graph API — the same data shown in Meta Business Manager.

    Returns per-number fields:
      status         — CONNECTED | PENDING | FLAGGED | RESTRICTED | UNKNOWN
      quality_rating — GREEN (High) | YELLOW (Medium) | RED (Low) | UNKNOWN
      verified_name  — the approved display name
      name_status    — APPROVED | PENDING_REVIEW | DECLINED | etc.
    """
    if not getattr(tenant, "waba_id", None):
        raise HTTPException(400, "No WABA connected. Complete onboarding first.")

    # Resolve token through the same priority chain as get_wa_client():
    #   system user token → tenant stored token → env fallback
    from app.services.whatsapp import get_wa_client
    try:
        client_obj = get_wa_client(tenant)
        token      = client_obj.token
    except ValueError as e:
        raise HTTPException(400, str(e))

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(
            f"{GRAPH}/{API_V}/{tenant.waba_id}/phone_numbers",
            params={
                "fields":       "id,display_phone_number,verified_name,status,quality_rating,name_status,code_verification_status",
                "access_token": token,
            }
        )
    data = res.json()

    if "error" in data:
        err = data["error"]
        raise HTTPException(400, f"Meta error ({err.get('code')}): {err.get('message', str(err))}")

    phones = data.get("data", [])

    # Normalise quality_rating to a label + colour
    QUALITY_MAP = {
        "GREEN":   {"label": "High",    "color": "#3fb950"},
        "YELLOW":  {"label": "Medium",  "color": "#d29922"},
        "RED":     {"label": "Low",     "color": "#f85149"},
        "UNKNOWN": {"label": "Unknown", "color": "#8b949e"},
    }

    STATUS_MAP = {
        "CONNECTED":   {"label": "Connected",   "color": "#3fb950"},
        "PENDING":     {"label": "Pending",      "color": "#d29922"},
        "FLAGGED":     {"label": "Flagged",      "color": "#f85149"},
        "RESTRICTED":  {"label": "Restricted",   "color": "#f85149"},
        "UNKNOWN":     {"label": "Unknown",      "color": "#8b949e"},
        "UNVERIFIED":  {"label": "Unverified",   "color": "#8b949e"},
        "DELETED":     {"label": "Deleted",      "color": "#f85149"},
        "MIGRATED":    {"label": "Migrated",     "color": "#8b949e"},
    }

    result = []
    for p in phones:
        raw_quality = (p.get("quality_rating") or "UNKNOWN").upper()
        raw_status  = (p.get("status")         or "UNKNOWN").upper()
        q = QUALITY_MAP.get(raw_quality, QUALITY_MAP["UNKNOWN"])
        s = STATUS_MAP.get(raw_status,   STATUS_MAP["UNKNOWN"])

        result.append({
            "id":             p.get("id", ""),
            "phone_number":   p.get("display_phone_number", ""),
            "verified_name":  p.get("verified_name", ""),
            "name_status":    p.get("name_status", ""),
            "status":         raw_status,
            "status_label":   s["label"],
            "status_color":   s["color"],
            "quality_rating": raw_quality,
            "quality_label":  q["label"],
            "quality_color":  q["color"],
        })

    return {
        "phones":                result,
        "waba_id":               tenant.waba_id,
        "active_phone_number_id": tenant.phone_number_id or "",
    }


# ── Webhook info ──────────────────────────────────────────────────────────────
@router.get("/webhook-info")
async def webhook_info(tenant: Tenant = Depends(get_current_tenant)):
    """
    Returns everything the user needs to configure the webhook in the
    Meta App Dashboard, plus whether the system user token is set.
    """
    tid          = str(tenant.id)
    backend_url  = (settings.webhook_base_url or settings.backend_url or "").strip().rstrip("/")
    is_localhost = not backend_url or "localhost" in backend_url or "127.0.0.1" in backend_url

    canonical_url = f"{backend_url}/api/v1/webhook/whatsapp"   # what register-webhook registers
    return {
        "tenant_id":              tid,
        "webhook_callback_url":   canonical_url,        # canonical App-level URL (no tenant_id)
        "webhook_callback_url_legacy": f"{backend_url}/api/v1/webhook/{tid}",
        "verify_token":           settings.webhook_verify_token,
        "waba_connected":         bool(getattr(tenant, "waba_id", None)),
        "waba_id":                getattr(tenant, "waba_id", None),
        "system_user_configured": bool(settings.meta_system_user_token),
        "is_localhost":           is_localhost,
        "backend_url":            backend_url,
    }


# ── Webhook delivery probe  ───────────────────────────────────────────────────
@router.get("/webhook-probe")
async def webhook_probe(_: Tenant = Depends(get_current_tenant)):
    """
    Self-test: hit our own webhook GET endpoint to confirm the verify_token
    handshake works end-to-end, before registering with Meta.

    Uses the current WEBHOOK_BASE_URL so you know exactly what Meta would receive.
    Returns {"verified": true} on success.
    """
    import secrets
    backend_url = (settings.webhook_base_url or settings.backend_url or "").strip().rstrip("/")
    if not backend_url or "localhost" in backend_url or "127.0.0.1" in backend_url:
        return {"verified": False, "error": "WEBHOOK_BASE_URL must be a public HTTPS URL (e.g. ngrok). Update .env and restart server."}

    challenge    = secrets.token_hex(16)
    callback_url = f"{backend_url}/api/v1/webhook/whatsapp"

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(callback_url, params={
                "hub.mode":         "subscribe",
                "hub.verify_token": settings.webhook_verify_token,
                "hub.challenge":    challenge,
            })
        except Exception as e:
            return {"verified": False, "error": f"Cannot reach {callback_url}: {e}"}

    if res.status_code == 200 and res.text.strip() == challenge:
        return {"verified": True, "callback_url": callback_url, "status_code": res.status_code}

    return {
        "verified":    False,
        "callback_url": callback_url,
        "status_code": res.status_code,
        "body":         res.text[:200],
        "hint": (
            "403 → verify_token mismatch (check WEBHOOK_VERIFY_TOKEN in .env). "
            "404 → route not found (restart server after code changes). "
            "Connection error → ngrok not running or WEBHOOK_BASE_URL is stale."
        ),
    }


# ── Register App webhook (manual trigger from Settings UI) ───────────────────
@router.post("/register-webhook")
async def register_webhook(_: Tenant = Depends(get_current_tenant)):
    """
    Manually trigger App-level webhook registration with Meta.
    Called from Settings → Channels when the user clicks 'Register Webhook'.
    Also called automatically at the end of embedded_signup and connect_manual.
    """
    result = await _register_app_webhook()
    if result.get("success"):
        return result
    raise HTTPException(400, result.get("error", "Webhook registration failed"))


# ── WABA subscription check + fix ────────────────────────────────────────────
@router.post("/subscribe-waba")
async def subscribe_waba(tenant: Tenant = Depends(get_current_tenant)):
    """
    Check whether the WABA is subscribed to this app and (re-)subscribe if not.

    This is SEPARATE from registering the app-level webhook URL.
    App-level webhook = where Meta sends events (callback URL).
    WABA subscription = tells Meta to actually send events for this WABA there.

    Both are required. The WABA subscription is done once per WABA and persists.
    If the embedded signup subscription failed silently, call this endpoint.
    """
    if not getattr(tenant, "waba_id", None):
        raise HTTPException(400, "No WABA connected. Complete onboarding first.")

    from app.services.whatsapp import get_wa_client
    try:
        client = get_wa_client(tenant)
        token  = client.token
    except ValueError as e:
        raise HTTPException(400, str(e))

    waba_id = tenant.waba_id

    # ── 1. Check current subscription status ──────────────────────────────────
    async with httpx.AsyncClient(timeout=15) as client:
        check_res = await client.get(
            f"{GRAPH}/{API_V}/{waba_id}/subscribed_apps",
            params={"access_token": token},
        )
    check_data = check_res.json()

    already_subscribed = False
    if "data" in check_data:
        for app in check_data["data"]:
            if str(app.get("id")) == str(settings.meta_app_id) or app.get("whatsapp_business_api_data"):
                already_subscribed = True
                break

    if already_subscribed:
        return {
            "subscribed":  True,
            "action":      "already_active",
            "waba_id":     waba_id,
            "detail":      "WABA was already subscribed. Meta should be delivering events.",
        }

    # ── 2. Subscribe ──────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=15) as client:
        sub_res = await client.post(
            f"{GRAPH}/{API_V}/{waba_id}/subscribed_apps",
            params={"access_token": token},
        )
    sub_data = sub_res.json()

    if sub_data.get("success"):
        return {
            "subscribed": True,
            "action":     "subscribed_now",
            "waba_id":    waba_id,
            "detail":     "WABA is now subscribed. Meta will deliver events to your webhook.",
        }

    err = sub_data.get("error", {})
    raise HTTPException(400, f"Subscription failed ({err.get('code', '?')}): {err.get('message', str(sub_data))}")


# ── Webhook pipeline simulator ────────────────────────────────────────────────
@router.post("/simulate-webhook")
async def simulate_webhook(
    tenant:    Tenant = Depends(get_current_tenant),
    from_number: str  = "919999999999",
    message_text: str = "Test message from simulator",
):
    """
    Inject a fake WhatsApp message directly into the processing pipeline,
    bypassing Meta entirely.  Use this to verify the full chain:
      DB write → WebSocket push → Inbox UI update

    If this works but real WhatsApp messages don't arrive, the issue is in
    the Meta → ngrok → server path (WABA subscription, webhook URL, ngrok).

    If this also fails, the issue is in your backend processing code or DB.
    """
    import time, uuid
    from app.api.v1.webhook import _process_change

    fake_value = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": tenant.display_phone_number or "+91 0000000000",
            "phone_number_id":      tenant.phone_number_id or "",
        },
        "contacts": [{
            "profile": {"name": "Webhook Simulator"},
            "wa_id":   from_number,
        }],
        "messages": [{
            "from":      from_number,
            "id":        f"sim_{uuid.uuid4().hex[:16]}",
            "timestamp": str(int(time.time())),
            "type":      "text",
            "text":      {"body": message_text},
        }],
    }

    try:
        await _process_change(
            value           = fake_value,
            waba_id         = tenant.waba_id or "",
            phone_number_id = tenant.phone_number_id or "",
        )
        return {
            "ok":      True,
            "detail":  "Message injected. Check your Inbox for the conversation.",
            "from":    from_number,
            "message": message_text,
        }
    except Exception as e:
        import traceback
        return {
            "ok":       False,
            "error":    str(e),
            "traceback": traceback.format_exc(),
        }


# ── Disconnect WABA ───────────────────────────────────────────────────────────
@router.post("/disconnect")
async def disconnect(tenant: Tenant = Depends(get_current_tenant)):
    """Disconnect WABA — clears WhatsApp credentials and resets status to pending."""
    tenant.waba_id                = None
    tenant.phone_number_id        = None
    tenant.display_phone_number   = None
    tenant.waba_name              = None
    tenant.encrypted_access_token = None
    tenant.status                 = "pending"
    tenant.waba_connected         = False
    tenant.activated_at           = None
    await tenant.save()
    return {"message": "WhatsApp Business Account disconnected successfully"}


# ── Refresh Meta access token ─────────────────────────────────────────────────
@router.post("/refresh-meta-token")
async def refresh_meta_token(tenant: Tenant = Depends(get_current_tenant)):
    """
    Exchange the stored user access token for a fresh long-lived token.
    Long-lived tokens expire in ~60 days — call this periodically.

    Per Meta docs:
    GET https://graph.facebook.com/oauth/access_token
        ?grant_type=fb_exchange_token
        &client_id=APP_ID
        &client_secret=APP_SECRET
        &fb_exchange_token=CURRENT_TOKEN
    """
    if not tenant.encrypted_access_token:
        raise HTTPException(400, "No access token stored. Please reconnect WhatsApp.")

    current_token = decrypt_token(tenant.encrypted_access_token)

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{GRAPH}/{API_V}/oauth/access_token",
            params={
                "grant_type":       "fb_exchange_token",
                "client_id":        settings.meta_app_id,
                "client_secret":    settings.meta_app_secret,
                "fb_exchange_token": current_token,
            }
        )

    data = res.json()
    if "error" in data:
        raise HTTPException(400, f"Token refresh failed: {data['error'].get('message', str(data['error']))}")

    new_token = data.get("access_token")
    if not new_token:
        raise HTTPException(400, "No token in Meta response")

    tenant.encrypted_access_token = encrypt_token(new_token)
    await tenant.save()

    expires_in = data.get("expires_in", 5183944)
    return {
        "message":    "Access token refreshed successfully",
        "expires_in": expires_in,
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