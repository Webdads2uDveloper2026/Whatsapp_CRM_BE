"""
Google OAuth for tenant registration/login.

Setup:
1. Go to console.cloud.google.com → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Web application)
3. Add Authorized redirect URI: http://localhost:8000/api/v1/auth/google/callback
4. Add to .env:
   GOOGLE_CLIENT_ID=your_client_id
   GOOGLE_CLIENT_SECRET=your_client_secret
   FRONTEND_URL=http://localhost:5173
"""
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from app.config import get_settings
from app.models.tenant import Tenant
from app.core.security import hash_password, create_access_token, create_refresh_token
import secrets

router   = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_URL  = "https://www.googleapis.com/oauth2/v3/userinfo"


def get_redirect_uri():
    return f"{settings.backend_url}/api/v1/auth/google/callback"


@router.get("/google")
async def google_login():
    """Redirect user to Google OAuth consent screen."""
    if not settings.google_client_id:
        raise HTTPException(501, "Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env")

    params = {
        "client_id":     settings.google_client_id,
        "redirect_uri":  get_redirect_uri(),
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "prompt":        "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{query}")


@router.get("/google/callback")
async def google_callback(code: str = None, error: str = None):
    """Handle Google OAuth callback — exchange code for user info."""
    frontend = settings.frontend_url or "http://localhost:5173"

    if error or not code:
        return RedirectResponse(f"{frontend}/login?error=google_cancelled")

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_res = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri":  get_redirect_uri(),
            "grant_type":    "authorization_code",
        })

    if token_res.status_code != 200:
        return RedirectResponse(f"{frontend}/login?error=google_token_failed")

    tokens      = token_res.json()
    id_token    = tokens.get("id_token")
    access_tok  = tokens.get("access_token")

    # Get user info
    async with httpx.AsyncClient() as client:
        user_res = await client.get(GOOGLE_USER_URL, headers={"Authorization": f"Bearer {access_tok}"})

    if user_res.status_code != 200:
        return RedirectResponse(f"{frontend}/login?error=google_userinfo_failed")

    guser = user_res.json()
    email = guser.get("email", "").lower()
    name  = guser.get("name", "")

    if not email:
        return RedirectResponse(f"{frontend}/login?error=google_no_email")

    # Find or create tenant
    tenant = await Tenant.find_one(Tenant.email == email)

    if not tenant:
        # New user — create account
        tenant = Tenant(
            business_name    = name or email.split("@")[0],
            email            = email,
            hashed_password  = hash_password(secrets.token_hex(32)),  # random pw, login via Google
            status           = "pending",
            google_id        = guser.get("sub"),
            avatar_url       = guser.get("picture"),
        )
        await tenant.insert()
        # Send to onboarding
        next_path = "/onboarding"
    else:
        # Existing user
        if not tenant.google_id:
            tenant.google_id  = guser.get("sub")
            tenant.avatar_url = guser.get("picture")
            await tenant.save()
        next_path = "/dashboard" if tenant.status == "active" else "/onboarding"

    # Issue JWT tokens
    payload       = {"sub": str(tenant.id), "tenant_id": str(tenant.id)}
    access_token  = create_access_token(payload)
    refresh_token = create_refresh_token(payload)

    # Redirect to frontend with tokens in query string
    # (Frontend stores them in localStorage on landing)
    return RedirectResponse(
        f"{frontend}/auth/google/success?access_token={access_token}&refresh_token={refresh_token}&next={next_path}"
    )