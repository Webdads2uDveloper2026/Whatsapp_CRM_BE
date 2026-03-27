"""
app/api/v1/templates.py  —  Complete template management + sending API

ENDPOINTS:
  POST   /templates                  → Create template on Meta (UPPERCASE components)
  POST   /templates/sync             → Pull all templates from Meta → save locally
  GET    /templates/local            → List templates from local DB
  DELETE /templates/{name}           → Delete from Meta + local DB
  POST   /templates/send/{wa_id}     → Send approved template, save to conversation
  POST   /media/upload               → Upload media → get Meta media_id + preview URL
"""
import httpx, logging
from datetime import datetime, timedelta
from typing   import Optional
from fastapi  import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel, Field
from app.models.tenant    import Tenant
from app.core.dependencies import get_current_tenant, get_active_tenant
from app.config           import get_settings

router   = APIRouter(prefix='/templates', tags=['templates'])
settings = get_settings()
log      = logging.getLogger(__name__)

# ─── MIME types allowed per media type ───────────────────────────────────────
ALLOWED_MIME = {
    'image':    {'image/jpeg', 'image/png', 'image/webp', 'image/gif'},
    'video':    {'video/mp4', 'video/3gpp'},
    'document': {
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    },
}


# ─── Pydantic schemas ─────────────────────────────────────────────────────────
class CreateTemplateReq(BaseModel):
    name:       str
    category:   str  = 'MARKETING'
    language:   str  = 'en_US'
    components: list = []


class SendTemplateReq(BaseModel):
    """
    Runtime values for sending a template.
    Fixed text in the template body/footer is NOT needed here —
    only the variable values and media for the header.
    """
    template_name:    str
    language:         str  = 'en_US'

    # Header runtime value — provide the one matching the template's header type
    header_type:      str  = 'none'   # text | image | video | document | none
    header_text:      str  = ''       # TEXT header variable
    header_media_id:  str  = ''       # media_id from /media/upload
    header_link:      str  = ''       # public URL (fallback if no media_id)
    header_filename:  str  = ''       # DOCUMENT filename shown to recipient

    # Body variables: positional or named
    # Positional: { "1": "John", "2": "ORD-123" }   for {{1}}, {{2}} in body
    # Named:      { "name": "John", "order": "ORD-123" }
    body_variables:   dict = Field(default_factory=dict)

    # Button runtime values (only dynamic ones need entries)
    # [
    #   { "type": "QUICK_REPLY",  "payload": "YES",       "index": 0 },
    #   { "type": "URL",          "url_suffix": "ORD-123","index": 1 },
    #   { "type": "COPY_CODE",    "code": "SALE20",       "index": 2 },
    # ]
    buttons:          list = Field(default_factory=list)


# ─── CREATE ───────────────────────────────────────────────────────────────────
@router.post('')
async def create_template(
    body:   CreateTemplateReq,
    tenant: Tenant = Depends(get_active_tenant),
):
    """Submit new template to Meta. Uses UPPERCASE component types."""
    import re
    if not body.name or not re.match(r'^[a-z0-9_]+$', body.name):
        raise HTTPException(400, 'name must be lowercase letters, digits, underscores only')

    token, waba_id = _creds(tenant)

    from app.services.whatsapp import normalize_create_components
    comps = normalize_create_components(body.components)

    # ── Auto-upload image/video/document headers to get header_handle ────────
    # Uses Meta Resumable Upload API (/{APP_ID}/uploads) which returns 'h' handle.
    # This is different from /media endpoint — only resumable upload works here.
    for comp in comps:
        if comp.get('type') == 'HEADER' and comp.get('format') in ('IMAGE','VIDEO','DOCUMENT'):
            example = comp.get('example', {})
            handles = example.get('header_handle', [])
            if handles and handles[0] and handles[0].startswith('http'):
                url_to_upload = handles[0]
                try:
                    handle = await _upload_media_for_template(
                        token, tenant, url_to_upload, comp['format']
                    )
                    if handle:
                        comp['example'] = {'header_handle': [handle]}
                    else:
                        # Upload failed — must remove example, Meta rejects invalid handles
                        comp.pop('example', None)
                        print('[TEMPLATE] ⚠ No handle — header example removed')
                except Exception as ue:
                    print(f'[TEMPLATE] Upload exception: {ue}')
                    comp.pop('example', None)
            elif not handles:
                # No example provided at all — remove key
                comp.pop('example', None)

    r_data = await _meta_post(
        f'https://graph.facebook.com/{settings.meta_api_version}/{waba_id}/message_templates',
        token,
        {
            'name':       body.name,
            'category':   body.category.upper(),
            'language':   body.language,
            'components': comps,
        }
    )

    await _upsert_local(tenant, {
        'meta_id':    r_data.get('id', ''),
        'name':       body.name,
        'category':   body.category.upper(),
        'language':   body.language,
        'status':     'PENDING',
        'components': body.components,
        'created_at': datetime.utcnow(),
    })

    return {
        'id':      r_data.get('id'),
        'name':    body.name,
        'status':  'PENDING',
        'message': 'Submitted to Meta for review (approved within 24h)',
    }


# ─── SYNC from Meta ───────────────────────────────────────────────────────────
@router.post('/sync')
async def sync_templates(tenant: Tenant = Depends(get_active_tenant)):
    """Fetch all templates from Meta and save/update locally."""
    token, waba_id = _creds(tenant)

    synced  = 0
    url     = f'https://graph.facebook.com/{settings.meta_api_version}/{waba_id}/message_templates'
    params  = {
        'limit':  100,
        'fields': 'id,name,status,category,language,components,quality_score,rejected_reason',
    }

    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            r    = await client.get(url, params=params, headers=_auth(token))
            data = r.json()
            _check_error(data, 'sync')

            for t in data.get('data', []):
                await _upsert_local(tenant, {
                    'meta_id':         t.get('id', ''),
                    'name':            t.get('name', ''),
                    'category':        (t.get('category') or 'MARKETING').upper(),
                    'language':        t.get('language', 'en_US'),
                    'status':          (t.get('status') or 'PENDING').upper(),
                    'components':      t.get('components', []),
                    'quality_score':   (t.get('quality_score') or {}).get('score'),
                    'rejected_reason': t.get('rejected_reason'),
                    'synced_at':       datetime.utcnow(),
                })
                synced += 1

            nxt    = data.get('paging', {}).get('next')
            url    = nxt if nxt and nxt != url else None
            params = {}

    return {'synced': synced}


# ─── LIST local ───────────────────────────────────────────────────────────────
@router.get('/local')
async def list_local(
    tenant: Tenant = Depends(get_current_tenant),
    status: Optional[str] = None,
):
    from app.database import db
    q: dict = {'tenant_id': str(tenant.id)}
    if status:
        q['status'] = status.upper()
    docs = await db.templates.find(q).sort('created_at', -1).to_list(500)
    return {'total': len(docs), 'templates': [_serialize(d) for d in docs]}


# ─── DELETE ───────────────────────────────────────────────────────────────────
@router.delete('/{name}')
async def delete_template(name: str, tenant: Tenant = Depends(get_active_tenant)):
    from app.database import db
    token, waba_id = _creds(tenant)

    async with httpx.AsyncClient(timeout=30) as client:
        r    = await client.delete(
            f'https://graph.facebook.com/{settings.meta_api_version}/{waba_id}/message_templates',
            params  = {'name': name},
            headers = _auth(token),
        )
        data = r.json()

    # code 100 = "Template does not exist" — treat as success
    if 'error' in data and data['error'].get('code') not in (100, '100'):
        err = data['error']
        raise HTTPException(400, f"Meta error: {err.get('message', str(err))}")

    await db.templates.delete_many({'tenant_id': str(tenant.id), 'name': name})
    return {'deleted': name}


# ─── SEND ────────────────────────────────────────────────────────────────────
@router.post('/send/{wa_id}')
async def send_template(
    wa_id:  str,
    body:   SendTemplateReq,
    tenant: Tenant = Depends(get_active_tenant),
):
    """
    Send an approved template to a WhatsApp number.
    Saves message to conversation → visible live in Inbox.
    """
    from app.services.whatsapp import get_wa_client, build_send_components
    from app.database import db

    client   = get_wa_client(tenant)
    tid      = str(tenant.id)
    clean_wa = wa_id.replace('+', '').replace(' ', '').replace('-', '')

    # ── Build correct Meta SEND components ───────────────────────────────────
    components = build_send_components(
        header_type     = body.header_type,
        header_text     = body.header_text,
        header_media_id = body.header_media_id,
        header_link     = body.header_link,
        header_filename = body.header_filename,
        body_variables  = body.body_variables or {},
        buttons         = body.buttons or [],
    )

    log.info(f'[SEND TPL] {body.template_name} → {clean_wa} | components={components}')

    # ── Call Meta API ─────────────────────────────────────────────────────────
    resp = await client.send_template(clean_wa, body.template_name, body.language, components)

    if 'error' in resp:
        err  = resp['error']
        code = err.get('code', 0)
        msg  = err.get('message', str(err))
        log.error(f'[SEND TPL] Meta error {code}: {msg}')

        # Human-readable error messages
        ERRORS = {
            131047: '24-hour window closed. Templates can only be sent as the first message.',
            131026: 'Recipient is not a valid WhatsApp number.',
            132000: f'Template variable count mismatch: {msg}',
            132001: 'Template not found or not approved.',
            132005: f'Template message body format error: {msg}',
            130472: 'Too many template messages sent to this number recently.',
        }
        raise HTTPException(400, ERRORS.get(code, f'WhatsApp error ({code}): {msg}'))

    wa_msg_id = (resp.get('messages') or [{}])[0].get('id', '')
    now       = datetime.utcnow()

    # ── Find or create contact ────────────────────────────────────────────────
    contact = await db.contacts.find_one({'tenant_id': tid, 'wa_id': clean_wa})
    if not contact:
        ins        = await db.contacts.insert_one({
            'tenant_id': tid, 'wa_id': clean_wa,
            'profile_name': clean_wa, 'opted_in': True,
            'tags': [], 'status': 'New',
            'created_at': now, 'updated_at': now,
        })
        contact_id = str(ins.inserted_id)
    else:
        contact_id = str(contact['_id'])

    # ── Find or create open conversation ─────────────────────────────────────
    convo = await db.conversations.find_one({
        'tenant_id': tid, 'wa_id': clean_wa, 'status': 'open'
    })
    preview = f'📋 {body.template_name}'
    win_exp = now + timedelta(hours=24)

    if not convo:
        ins      = await db.conversations.insert_one({
            'tenant_id': tid, 'contact_id': contact_id, 'wa_id': clean_wa,
            'status': 'open', 'unread_count': 0,
            'last_message_at': now, 'last_message_preview': preview,
            'window_expires_at': win_exp, 'created_at': now, 'updated_at': now,
        })
        convo_id = str(ins.inserted_id)
    else:
        convo_id = str(convo['_id'])
        await db.conversations.update_one({'_id': convo['_id']}, {'$set': {
            'last_message_at': now, 'last_message_preview': preview,
            'window_expires_at': win_exp, 'updated_at': now,
        }})

    # ── Save message → Inbox ──────────────────────────────────────────────────
    msg_content = {
        'template_name':  body.template_name,
        'language':       body.language,
        'header_type':    body.header_type,
        'header_text':    body.header_text,
        'header_link':    body.header_link or body.header_media_id,
        'header_filename':body.header_filename,
        'variables':      body.body_variables,
    }
    ins = await db.messages.insert_one({
        'tenant_id':       tid,
        'conversation_id': convo_id,
        'contact_id':      contact_id,
        'wa_id':           clean_wa,
        'wa_message_id':   wa_msg_id,
        'direction':       'outbound',
        'type':            'template',
        'msg_type':        'template',
        'content':         msg_content,
        'status':          'sent',
        'created_at':      now,
    })

    # ── WebSocket live push ───────────────────────────────────────────────────
    try:
        from app.api.v1.websocket import broadcast_to_tenant
        await broadcast_to_tenant(tid, {
            'type':            'new_message',
            'conversation_id': convo_id,
            'message': {
                'id':          str(ins.inserted_id),
                'direction':   'outbound',
                'type':        'template',
                'content':     msg_content,
                'status':      'sent',
                'created_at':  now.isoformat(),
            },
        })
    except Exception as e:
        log.warning(f'[WS push] {e}')

    return {
        'success':         True,
        'wa_message_id':   wa_msg_id,
        'conversation_id': convo_id,
        'message':         'Template sent and saved to conversation',
    }


# ─── UPLOAD MEDIA ────────────────────────────────────────────────────────────
# Registered separately at /media/upload via media.py
# But also available here as /templates/media/upload for convenience
@router.post('/media/upload')
async def upload_media_for_template(
    file:   UploadFile = File(...),
    type:   str        = Form('image'),
    tenant: Tenant     = Depends(get_active_tenant),
):
    """
    Upload image/video/document to Meta for use in template headers.
    Returns { "id": "META_MEDIA_ID", "preview_url": "..." }
    Use the id as header_media_id when sending the template.
    """
    from app.services.whatsapp import get_wa_client

    mime    = file.content_type or 'application/octet-stream'
    allowed = ALLOWED_MIME.get(type, set())
    if allowed and mime not in allowed:
        raise HTTPException(400, f'Invalid MIME type {mime} for {type}. Allowed: {allowed}')

    data = await file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(400, 'File too large (max 100MB)')

    client = get_wa_client(tenant)
    resp   = await client.upload_media(data, mime, file.filename or 'upload')

    if 'error' in resp:
        err = resp['error']
        raise HTTPException(400, f"Meta upload error: {err.get('message', str(err))}")

    media_id = resp.get('id', '')

    # Fetch the URL so frontend can show a preview
    preview_url = ''
    if media_id:
        try:
            token = _creds(tenant)[0]
            async with httpx.AsyncClient(timeout=15) as client_http:
                r    = await client_http.get(
                    f'https://graph.facebook.com/{settings.meta_api_version}/{media_id}',
                    headers=_auth(token),
                )
                preview_url = r.json().get('url', '')
        except Exception:
            pass

    return {
        'id':          media_id,
        'preview_url': preview_url,
        'filename':    file.filename,
        'type':        type,
        'mime':        mime,
        'size':        len(data),
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _creds(tenant: Tenant):
    from app.core.security import decrypt_token
    token = None
    if getattr(tenant, 'encrypted_access_token', None):
        try:
            token = decrypt_token(tenant.encrypted_access_token)
        except Exception:
            pass
    token   = token or settings.meta_access_token
    waba_id = getattr(tenant, 'waba_id', None) or settings.meta_waba_id
    if not token:   raise HTTPException(400, 'No access token configured')
    if not waba_id: raise HTTPException(400, 'No WABA ID configured')
    return token, waba_id


def _auth(token: str) -> dict:
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


async def _upload_media_for_template(token: str, tenant, url: str, fmt: str) -> str:
    """
    Upload media using Meta Resumable Upload API to get header_handle for templates.
    
    Flow:
    1. POST /{APP_ID}/uploads  → get upload session id
    2. POST /{session_id}      → upload bytes → get 'h' handle
    3. Use 'h' as header_handle in template component
    """
    import httpx as _httpx
    from app.config import get_settings as _gs
    _settings = _gs()

    app_id  = _settings.meta_app_id
    version = _settings.meta_api_version or 'v22.0'

    if not app_id:
        print('[TEMPLATE] META_APP_ID not set — cannot upload header media')
        return ''

    mime_map = {'IMAGE': 'image/png', 'VIDEO': 'video/mp4', 'DOCUMENT': 'application/pdf'}
    fname_map = {'IMAGE': 'header.png', 'VIDEO': 'header.mp4', 'DOCUMENT': 'header.pdf'}
    mime  = mime_map.get(fmt.upper(), 'image/png')
    fname = fname_map.get(fmt.upper(), 'header.png')

    async with _httpx.AsyncClient(timeout=60) as client:
        # Step 1: Download file
        dl = await client.get(url, follow_redirects=True, timeout=30)
        if dl.status_code != 200:
            print(f'[TEMPLATE] Download failed {dl.status_code}: {url}')
            return ''
        file_bytes = dl.content
        file_size  = len(file_bytes)
        print(f'[TEMPLATE] Downloaded {file_size} bytes from {url}')

        # Step 2: Create upload session
        r1 = await client.post(
            f'https://graph.facebook.com/{version}/{app_id}/uploads',
            params={
                'file_name':    fname,
                'file_length':  file_size,
                'file_type':    mime,
                'access_token': token,
            }
        )
        s1 = r1.json()
        print(f'[TEMPLATE] Upload session: {r1.status_code} → {s1}')
        if 'error' in s1:
            print(f'[TEMPLATE] Session error: {s1["error"].get("message")}')
            return ''
        session_id = s1.get('id', '')
        if not session_id:
            print('[TEMPLATE] No session ID returned')
            return ''

        # Step 3: Upload file bytes (use OAuth not Bearer, file_offset header)
        r2 = await client.post(
            f'https://graph.facebook.com/{version}/{session_id}',
            headers={
                'Authorization': f'OAuth {token}',
                'file_offset':   '0',
                'Content-Type':  mime,
            },
            content=file_bytes,
        )
        s2 = r2.json()
        print(f'[TEMPLATE] Upload result: {r2.status_code} → {s2}')
        if 'error' in s2:
            print(f'[TEMPLATE] Upload error: {s2["error"].get("message")}')
            return ''

        handle = s2.get('h', '')
        if handle:
            print(f'[TEMPLATE] ✅ Got header_handle: {handle[:40]}...')
        return handle


# ─── Upload header media → get header_handle ─────────────────────────────────
@router.post('/upload-header')
async def upload_header_media(
    file:   UploadFile = File(...),
    tenant: Tenant     = Depends(get_active_tenant),
):
    """
    Upload an image/video/document to Meta via Resumable Upload API.
    Returns { header_handle } which can be used when creating a template.
    
    Flow:
    1. Frontend picks a file and POSTs it here as multipart
    2. We download file bytes and run Meta Resumable Upload (2-step)
    3. Return the 'h' handle to frontend
    4. Frontend includes it in template create payload
    """
    token, waba_id = _creds(tenant)
    app_id  = settings.meta_app_id
    version = settings.meta_api_version or 'v22.0'

    if not app_id:
        raise HTTPException(400, 'META_APP_ID not configured in .env — add META_APP_ID=671128085546989')

    file_bytes  = await file.read()
    file_size   = len(file_bytes)
    mime_type   = file.content_type or 'image/png'
    file_name   = file.filename or 'header.png'

    print(f'[TEMPLATE] Uploading header media: {file_name} ({file_size} bytes) type={mime_type}')

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Create upload session
        r1 = await client.post(
            f'https://graph.facebook.com/{version}/{app_id}/uploads',
            params={
                'file_name':    file_name,
                'file_length':  file_size,
                'file_type':    mime_type,
                'access_token': token,
            }
        )
        s1 = r1.json()
        print(f'[TEMPLATE] Upload session: {r1.status_code} {s1}')
        if 'error' in s1:
            raise HTTPException(400, f'Upload session failed: {s1["error"].get("message")}')
        session_id = s1.get('id', '')
        if not session_id:
            raise HTTPException(400, 'No upload session ID returned from Meta')

        # Step 2: Upload file bytes
        r2 = await client.post(
            f'https://graph.facebook.com/{version}/{session_id}',
            headers={
                'Authorization': f'OAuth {token}',
                'file_offset':   '0',
                'Content-Type':  mime_type,
            },
            content=file_bytes,
        )
        s2 = r2.json()
        print(f'[TEMPLATE] Upload result: {r2.status_code} {s2}')
        if 'error' in s2:
            raise HTTPException(400, f'Upload failed: {s2["error"].get("message")}')

        handle = s2.get('h', '')
        if not handle:
            raise HTTPException(400, f'No handle returned: {s2}')

        print(f'[TEMPLATE] ✅ header_handle={handle[:50]}...')
        return {
            'header_handle': handle,
            'file_name':     file_name,
            'file_size':     file_size,
            'mime_type':     mime_type,
        }


def _check_error(data: dict, ctx: str):
    if 'error' in data:
        err     = data['error']
        msg     = err.get('message', str(err))
        details = err.get('error_data', {}).get('details', '')
        full    = f'{msg} | {details}' if details else msg
        print(f'[TEMPLATE] Meta error: code={err.get("code")} msg={msg} details={details}')
        raise HTTPException(400, f'Meta {ctx} error: {full}')


async def _meta_post(url: str, token: str, payload: dict) -> dict:
    import json as _json
    async with httpx.AsyncClient(timeout=30) as client:
        r    = await client.post(url, json=payload, headers=_auth(token))
        data = r.json()
    print(f'[TEMPLATE] Meta response status={r.status_code}: {_json.dumps(data)[:300]}')
    _check_error(data, 'request')
    return data


async def _upsert_local(tenant: Tenant, data: dict):
    from app.database import db
    data['tenant_id'] = str(tenant.id)
    await db.templates.update_one(
        {'tenant_id': str(tenant.id), 'name': data['name']},
        {'$set': data},
        upsert=True,
    )


def _serialize(doc: dict) -> dict:
    return {
        'id':              str(doc.get('_id', '')),
        'meta_id':         doc.get('meta_id', ''),
        'name':            doc.get('name', ''),
        'category':        doc.get('category', ''),
        'language':        doc.get('language', ''),
        'status':          doc.get('status', 'PENDING'),
        'components':      doc.get('components', []),
        'quality_score':   doc.get('quality_score'),
        'rejected_reason': doc.get('rejected_reason'),
        'usage_count':     doc.get('usage_count', 0),
        'created_at':      doc.get('created_at'),
        'synced_at':       doc.get('synced_at'),
    }