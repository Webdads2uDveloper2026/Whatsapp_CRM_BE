"""
app/api/v1/websocket.py

Fixed:
- Ping loop now checks connection state before sending
- All sends wrapped in try/except WebSocketDisconnect
- broadcast_to_tenant silently skips dead connections
- No more "Cannot call send once a close message has been sent" errors
"""
import asyncio
import json
import logging
from collections import defaultdict
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt

from app.config import get_settings

router   = APIRouter()
settings = get_settings()
log      = logging.getLogger(__name__)

# ── Connection registry ────────────────────────────────────────────────────────
_connections: Dict[str, Set[WebSocket]] = defaultdict(set)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}")


async def _safe_send(ws: WebSocket, data: dict) -> bool:
    """Send JSON to a single WebSocket. Returns False if connection is dead."""
    try:
        await ws.send_json(data)
        return True
    except (WebSocketDisconnect, RuntimeError, Exception):
        return False


async def broadcast_to_tenant(tenant_id: str, message: dict):
    """Broadcast a message to all connected clients for a tenant."""
    dead = []
    for ws in list(_connections.get(tenant_id, [])):
        ok = await _safe_send(ws, message)
        if not ok:
            dead.append(ws)
    for ws in dead:
        _connections[tenant_id].discard(ws)
    if tenant_id in _connections and not _connections[tenant_id]:
        del _connections[tenant_id]


# ── WebSocket endpoint ─────────────────────────────────────────────────────────
@router.websocket("/ws/inbox")
async def inbox_ws(
    websocket: WebSocket,
    token: str = Query(default=""),
):
    """
    WebSocket endpoint for Inbox real-time updates.
    Connect with: ws://host/api/v1/ws/inbox?token=<JWT>

    Sends:
      {"type": "connected"}
      {"type": "ping"}  — every 25 seconds keepalive
      {"type": "new_message", "conversation_id": "...", "message": {...}}
      {"type": "status_update", "wa_message_id": "...", "status": "delivered"}

    Receives:
      {"type": "pong"}  — client keepalive response (ignored, any message keeps alive)
    """
    # ── Auth ─────────────────────────────────────────────────────────────────
    tenant_id = None
    try:
        if not token:
            await websocket.close(code=1008)
            return
        payload   = _decode_token(token)
        tenant_id = payload.get("tenant_id")
        if not tenant_id:
            await websocket.close(code=1008)
            return
    except ValueError:
        await websocket.close(code=1008)
        return

    # ── Accept and register ───────────────────────────────────────────────────
    await websocket.accept()
    _connections[tenant_id].add(websocket)
    log.info(f"[WS] Connected: tenant={tenant_id} total={len(_connections[tenant_id])}")

    # Send connected confirmation
    await _safe_send(websocket, {"type": "connected", "tenant_id": tenant_id})

    # ── Main loop ─────────────────────────────────────────────────────────────
    ping_task = None
    try:
        async def ping_loop():
            """Send keepalive ping every 25s. Stops gracefully on disconnect."""
            while True:
                await asyncio.sleep(25)
                # Check if this socket is still registered before pinging
                if websocket not in _connections.get(tenant_id, set()):
                    break
                ok = await _safe_send(websocket, {"type": "ping"})
                if not ok:
                    break

        ping_task = asyncio.create_task(ping_loop())

        # Wait for incoming messages (pong, etc.)
        while True:
            try:
                raw = await websocket.receive_text()
                # pong / any keepalive — just ignore, connection is alive
            except WebSocketDisconnect:
                break
            except RuntimeError:
                # "Cannot call receive once a close message has been sent."
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(f"[WS] Error for tenant={tenant_id}: {e}")
    finally:
        # Cancel ping task cleanly
        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except (asyncio.CancelledError, Exception):
                pass

        # Unregister
        _connections[tenant_id].discard(websocket)
        if not _connections.get(tenant_id):
            _connections.pop(tenant_id, None)
        log.info(f"[WS] Disconnected: tenant={tenant_id}")