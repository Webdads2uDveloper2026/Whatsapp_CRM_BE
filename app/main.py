# """
# WhatsApp CRM — FastAPI entry point.
# """
# import structlog
# from contextlib import asynccontextmanager
# from fastapi import FastAPI
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse
# from slowapi import Limiter, _rate_limit_exceeded_handler
# from slowapi.util import get_remote_address
# from slowapi.errors import RateLimitExceeded

# from app.config import get_settings
# from app.database import connect_db, close_db

# from app.api.v1 import (
#     auth, onboarding, webhook, websocket,
#     contacts, conversations, templates, broadcasts,
#     analytics, agents,
# )
# from app.api.v1.other_routes import (
#     tenants_router, admin_router, automations_router,
#     media_router, integrations_router,
# )

# log = structlog.get_logger()
# settings = get_settings()

# limiter = Limiter(key_func=get_remote_address)


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     log.info("startup.begin", env=settings.app_env)
#     await connect_db()
#     log.info("startup.complete")
#     yield
#     await close_db()
#     log.info("shutdown.complete")


# app = FastAPI(
#     title=settings.app_name,
#     description="Multi-tenant WhatsApp CRM & Automation Platform",
#     version="1.0.0",
#     docs_url="/docs",
#     redoc_url="/redoc",
#     lifespan=lifespan,
# )

# app.state.limiter = limiter
# app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# cors_origins = ["*"] if settings.app_env == "development" else settings.cors_origins_list
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=cors_origins,
#     allow_credentials=settings.app_env != "development",
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# PREFIX = "/api/v1"
# for router in [
#     auth.router, onboarding.router, webhook.router,
#     contacts.router, conversations.router, templates.router,
#     broadcasts.router, analytics.router, agents.router,
#     tenants_router, admin_router, automations_router,
#     media_router, integrations_router,
# ]:
#     app.include_router(router, prefix=PREFIX)

# app.include_router(websocket.router)   # WS — no /api/v1 prefix


# @app.get("/health", tags=["system"])
# async def health():
#     return {"status": "ok", "env": settings.app_env, "version": "1.0.0"}


# @app.exception_handler(Exception)
# async def global_exception_handler(request, exc):
#     log.error("unhandled_exception", error=str(exc), path=str(request.url))
#     return JSONResponse(status_code=500, content={"detail": "Internal server error"})
"""
app/main.py — Clean version, no __init__.py dependency
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import connect_db, close_db

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await close_db()


app = FastAPI(
    title="WhatsApp CRM API",
    version="1.0.0",
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API = "/api/v1"


def _include(module_path: str, attr: str = "router"):
    import importlib
    try:
        mod    = importlib.import_module(module_path)
        router = getattr(mod, attr, None)
        if router:
            app.include_router(router, prefix=API)
            print(f"✅ Loaded router: {module_path}")
        else:
            print(f"⚠  No '{attr}' in {module_path}")
    except Exception as e:
        print(f"⚠  Skipped {module_path}: {e}")


# Core routers — must all exist
_include("app.api.v1.auth")
_include("app.api.v1.agents")
_include("app.api.v1.analytics")
_include("app.api.v1.autoreplies")
_include("app.api.v1.broadcasts")
_include("app.api.v1.contacts")
_include("app.api.v1.conversations")
_include("app.api.v1.media")
_include("app.api.v1.onboarding")
_include("app.api.v1.templates")
_include("app.api.v1.webhook")
_include("app.api.v1.other_routes")
_include("app.api.v1.google_auth")

# WebSocket — no prefix
try:
    from app.api.v1.websocket import router as ws_router
    app.include_router(ws_router)
    print("✅ Loaded router: websocket")
except Exception as e:
    print(f"⚠  Skipped websocket: {e}")


@app.get("/.well-known/health", tags=["health"])
async def health():
    return {"status": "ok"}


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse({"detail": "Not found"}, status_code=404)