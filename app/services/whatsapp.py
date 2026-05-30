# """
# app/services/whatsapp.py — Complete WhatsApp Cloud API service

# SEND RULES (from Meta docs):
# - component type: lowercase  → header, body, button
# - parameter type: lowercase  → text, image, video, document, payload  
# - button sub_type: lowercase → quick_reply, url
# - button index: STRING       → "0", "1"
# - NO components key if template has no variables — omit entirely
# - Empty [] causes silent delivery failures
# """
# import httpx
# import logging
# from app.config import get_settings

# settings = get_settings()
# log      = logging.getLogger(__name__)


# def get_wa_client(tenant):
#     from app.core.security import decrypt_token
#     token = None
#     if getattr(tenant, 'encrypted_access_token', None):
#         try:
#             token = decrypt_token(tenant.encrypted_access_token)
#         except Exception:
#             pass
#     token    = token or settings.meta_access_token
#     phone_id = getattr(tenant, 'phone_number_id', None) or settings.meta_phone_number_id
#     version  = getattr(settings, 'meta_api_version', None) or 'v22.0'
#     if not token:   raise ValueError("No Meta access token configured")
#     if not phone_id: raise ValueError("No Meta phone number ID configured")
#     return WhatsAppClient(token, phone_id, version)


# class WhatsAppClient:
#     def __init__(self, access_token: str, phone_number_id: str, api_version: str = 'v22.0'):
#         self.token    = access_token
#         self.phone_id = phone_number_id
#         self.base     = f'https://graph.facebook.com/{api_version}'
#         self.base_url = self.base   # backwards-compat alias
#         self.headers  = {
#             'Authorization': f'Bearer {self.token}',
#             'Content-Type':  'application/json',
#         }

#     async def send_text(self, to: str, body: str, reply_to: str = None) -> dict:
#         payload = {
#             'messaging_product': 'whatsapp',
#             'recipient_type':    'individual',
#             'to':                to,
#             'type':              'text',
#             'text':              {'body': body, 'preview_url': False},
#         }
#         if reply_to:
#             payload['context'] = {'message_id': reply_to}
#         return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

#     async def send_template(
#         self,
#         to:         str,
#         name:       str,
#         language:   str  = 'en_US',
#         components: list = None,
#     ) -> dict:
#         """
#         Send a WhatsApp template.
#         components must be in correct Meta SEND format (lowercase types).
#         Pass None or [] for templates with no variables — components key omitted.
#         """
#         # Normalize types to lowercase and filter out components with no parameters
#         normalized = []
#         for comp in (components or []):
#             c = {**comp}
#             if 'type' in c:
#                 t = c['type'].lower()
#                 if t == 'buttons': t = 'button'
#                 c['type'] = t
#             if 'sub_type' in c:
#                 c['sub_type'] = c['sub_type'].lower()
#             if 'parameters' in c:
#                 c['parameters'] = [
#                     {**p, 'type': p['type'].lower()} if 'type' in p else {**p}
#                     for p in c['parameters']
#                 ]
#             normalized.append(c)

#         # Only keep components that actually have parameters
#         active = [c for c in normalized if c.get('parameters')]

#         payload: dict = {
#             'messaging_product': 'whatsapp',
#             'recipient_type':    'individual',
#             'to':                to,
#             'type':              'template',
#             'template': {
#                 'name':     name,
#                 'language': {'code': language},
#             },
#         }
#         # KEY: only add components key when there are actual values
#         # Sending "components": [] causes silent delivery failures on Meta
#         if active:
#             payload['template']['components'] = active

#         log.info(f'[WA SEND] template={name} to={to} components={active}')
#         return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

#     async def send_media(self, to, media_type, media_id=None, link=None,
#                          caption=None, filename=None, reply_to=None) -> dict:
#         obj: dict = {}
#         if media_id: obj['id']       = media_id
#         elif link:   obj['link']     = link
#         if caption:  obj['caption']  = caption
#         if filename: obj['filename'] = filename
#         payload = {
#             'messaging_product': 'whatsapp',
#             'recipient_type':    'individual',
#             'to': to, 'type': media_type, media_type: obj,
#         }
#         if reply_to: payload['context'] = {'message_id': reply_to}
#         return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

#     async def upload_media(self, file_bytes: bytes, mime_type: str, filename: str) -> dict:
#         async with httpx.AsyncClient(timeout=120) as client:
#             r = await client.post(
#                 f'{self.base}/{self.phone_id}/media',
#                 headers={'Authorization': f'Bearer {self.token}'},
#                 data={'messaging_product': 'whatsapp', 'type': mime_type},
#                 files={'file': (filename, file_bytes, mime_type)},
#             )
#             return r.json()

#     async def send_reaction(self, to: str, message_id: str, emoji: str) -> dict:
#         return await self._post(f'{self.base}/{self.phone_id}/messages', {
#             'messaging_product': 'whatsapp',
#             'recipient_type':    'individual',
#             'to': to, 'type': 'reaction',
#             'reaction': {'message_id': message_id, 'emoji': emoji},
#         })

#     async def mark_read(self, message_id: str) -> dict:
#         return await self._post(f'{self.base}/{self.phone_id}/messages', {
#             'messaging_product': 'whatsapp',
#             'status':            'read',
#             'message_id':        message_id,
#         })

#     async def _post(self, url: str, payload: dict) -> dict:
#         async with httpx.AsyncClient(timeout=30) as client:
#             r    = await client.post(url, json=payload, headers=self.headers)
#             data = r.json()
#             if r.status_code >= 400:
#                 log.error(f'[WA API] {r.status_code}: {data}')
#             return data


# # ─── Component builder ────────────────────────────────────────────────────────

# def build_send_components(
#     *,
#     header_type:     str  = 'none',
#     header_text:     str  = '',
#     header_media_id: str  = '',
#     header_link:     str  = '',
#     header_filename: str  = '',
#     body_variables:  dict = None,   # {"first_name":"John"} or {"1":"John","2":"ORD-1"}
#     buttons:         list = None,   # [{"type":"QUICK_REPLY","payload":"YES","index":0}]
# ) -> list:
#     """
#     Build Meta Cloud API SEND components.
#     Returns only components with actual values — never empty component objects.
#     """
#     comps = []
#     ht = (header_type or 'none').strip().lower()

#     # Header
#     if ht == 'text' and header_text.strip():
#         comps.append({'type': 'header', 'parameters': [{'type': 'text', 'text': header_text}]})
#     elif ht in ('image', 'video', 'document'):
#         obj: dict = {}
#         if header_media_id.strip(): obj['id']   = header_media_id.strip()
#         elif header_link.strip():   obj['link'] = header_link.strip()
#         if obj:
#             if ht == 'document' and header_filename.strip():
#                 obj['filename'] = header_filename.strip()
#             comps.append({'type': 'header', 'parameters': [{'type': ht, ht: obj}]})

#     # Body — sort numeric keys first ({{1}}, {{2}}...) then named
#     if body_variables:
#         def sort_key(k): return (0, int(k)) if k.isdigit() else (1, k)
#         params = []
#         for k in sorted(body_variables.keys(), key=sort_key):
#             v = body_variables[k]
#             if v is not None and str(v).strip():
#                 params.append({'type': 'text', 'text': str(v)})
#         if params:
#             comps.append({'type': 'body', 'parameters': params})

#     # Buttons (only dynamic ones need parameters)
#     for i, btn in enumerate(buttons or []):
#         bt  = (btn.get('type') or '').upper()
#         idx = str(btn.get('index', i))
#         if bt == 'QUICK_REPLY':
#             pval = btn.get('payload') or btn.get('text') or ''
#             if pval:
#                 comps.append({'type': 'button', 'sub_type': 'quick_reply',
#                               'index': idx, 'parameters': [{'type': 'payload', 'payload': pval}]})
#         elif bt == 'URL':
#             suffix = btn.get('url_suffix') or btn.get('text') or ''
#             if suffix.strip():
#                 comps.append({'type': 'button', 'sub_type': 'url',
#                               'index': idx, 'parameters': [{'type': 'text', 'text': suffix}]})
#         elif bt == 'COPY_CODE':
#             code = btn.get('code') or btn.get('text') or ''
#             if code.strip():
#                 comps.append({'type': 'button', 'sub_type': 'copy_code',
#                               'index': idx, 'parameters': [{'type': 'coupon_code', 'coupon_code': code}]})

#     return comps


# def normalize_send_components(components: list) -> list:
#     """Normalize component types to lowercase for Meta SEND endpoint."""
#     out = []
#     for comp in components:
#         c = {**comp}
#         if 'type' in c:
#             t = c['type'].lower()
#             if t == 'buttons': t = 'button'
#             c['type'] = t
#         if 'sub_type' in c: c['sub_type'] = c['sub_type'].lower()
#         if 'parameters' in c:
#             c['parameters'] = [
#                 {**p, 'type': p['type'].lower()} if 'type' in p else {**p}
#                 for p in c['parameters']
#             ]
#         out.append(c)
#     return out


# def normalize_create_components(components: list) -> list:
#     """Ensure UPPERCASE types for Meta CREATE endpoint."""
#     out = []
#     for comp in components:
#         c = {**comp}
#         if 'type' in c:   c['type']   = c['type'].upper()
#         if 'format' in c: c['format'] = c['format'].upper()
#         if 'buttons' in c and isinstance(c['buttons'], list):
#             c['buttons'] = [{**b, 'type': b['type'].upper()} if 'type' in b else {**b}
#                             for b in c['buttons']]
#         out.append(c)
#     return out

"""
app/services/whatsapp.py — Complete WhatsApp Cloud API service

SEND RULES (from Meta docs):
- component type: lowercase  → header, body, button
- parameter type: lowercase  → text, image, video, document, payload  
- button sub_type: lowercase → quick_reply, url
- button index: STRING       → "0", "1"
- NO components key if template has no variables — omit entirely
- Empty [] causes silent delivery failures
"""
import httpx
import logging
from app.config import get_settings

settings = get_settings()
log      = logging.getLogger(__name__)


def resolve_token(tenant=None) -> str:
    """
    Single source of truth for Meta access token resolution.

    Priority (WATI/Interakt-style):
      1. META_SYSTEM_USER_TOKEN — platform-owned, never expires, works across all WABAs
      2. Tenant encrypted_access_token — per-customer, expires in 60 days (fallback only)
      3. META_ACCESS_TOKEN .env fallback — legacy last resort

    Call this anywhere you need a token. Never read tokens directly from tenant or settings.
    """
    # 1. Platform system user — permanent
    token = (settings.meta_system_user_token or "").strip() or None

    # 2. Tenant stored token
    if not token and tenant and getattr(tenant, 'encrypted_access_token', None):
        try:
            from app.core.security import decrypt_token
            token = decrypt_token(tenant.encrypted_access_token)
        except Exception:
            pass

    # 3. .env fallback
    if not token:
        token = (settings.meta_access_token or "").strip() or None

    return token or ""


def resolve_waba_creds(tenant) -> tuple:
    """
    Returns (token, waba_id) for WABA-level Graph API calls:
    templates, phone_numbers, subscriptions, webhook registration.

    Validates that waba_id and phone_number_id are not swapped
    (a common onboarding bug).
    """
    from fastapi import HTTPException
    token   = resolve_token(tenant)
    waba_id = (getattr(tenant, 'waba_id', None) or settings.meta_waba_id or "").strip()

    if not token:
        raise HTTPException(400,
            "No Meta access token configured. "
            "Set META_SYSTEM_USER_TOKEN in your backend .env and restart."
        )
    if not waba_id:
        raise HTTPException(400,
            "No WABA ID configured for this account. "
            "Complete the WhatsApp onboarding first."
        )

    phone_number_id = (getattr(tenant, 'phone_number_id', None) or "").strip()
    if phone_number_id and waba_id == phone_number_id:
        raise HTTPException(400,
            f"waba_id ({waba_id}) equals phone_number_id — IDs are swapped. "
            "Disconnect and reconnect your WhatsApp account."
        )
    return token, waba_id


def get_wa_client(tenant):
    """
    Returns a WhatsAppClient scoped to the tenant's phone number.
    Uses resolve_token() so the system user token is always preferred.
    """
    token    = resolve_token(tenant)
    phone_id = (getattr(tenant, 'phone_number_id', None) or settings.meta_phone_number_id or "").strip()
    version  = (getattr(settings, 'meta_api_version', None) or 'v22.0').strip()

    if not token:
        raise ValueError(
            "No Meta access token configured. "
            "Set META_SYSTEM_USER_TOKEN in .env and restart the server."
        )
    if not phone_id:
        raise ValueError(
            "No phone_number_id found for this tenant. "
            "Complete WhatsApp onboarding first."
        )
    return WhatsAppClient(token, phone_id, version)


class WhatsAppClient:
    def __init__(self, access_token: str, phone_number_id: str, api_version: str = 'v22.0'):
        self.token    = access_token
        self.phone_id = phone_number_id
        self.base     = f'https://graph.facebook.com/{api_version}'
        self.base_url = self.base   # backwards-compat alias
        self.headers  = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type':  'application/json',
        }

    async def send_text(self, to: str, body: str, reply_to: str = None) -> dict:
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                to,
            'type':              'text',
            'text':              {'body': body, 'preview_url': False},
        }
        if reply_to:
            payload['context'] = {'message_id': reply_to}
        return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

    async def send_template(
        self,
        to:         str,
        name:       str,
        language:   str  = 'en_US',
        components: list = None,
    ) -> dict:
        """
        Send a WhatsApp template.
        components must be in correct Meta SEND format (lowercase types).
        Pass None or [] for templates with no variables — components key omitted.
        """
        # Normalize types to lowercase and filter out components with no parameters
        normalized = []
        for comp in (components or []):
            c = {**comp}
            if 'type' in c:
                t = c['type'].lower()
                if t == 'buttons': t = 'button'
                c['type'] = t
            if 'sub_type' in c:
                c['sub_type'] = c['sub_type'].lower()
            if 'parameters' in c:
                c['parameters'] = [
                    {**p, 'type': p['type'].lower()} if 'type' in p else {**p}
                    for p in c['parameters']
                ]
            normalized.append(c)

        # Only keep components that actually have parameters
        active = [c for c in normalized if c.get('parameters')]

        payload: dict = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                to,
            'type':              'template',
            'template': {
                'name':     name,
                'language': {'code': language},
            },
        }
        # KEY: only add components key when there are actual values
        # Sending "components": [] causes silent delivery failures on Meta
        if active:
            payload['template']['components'] = active

        log.info(f'[WA SEND] template={name} to={to} components={active}')
        return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

    async def send_media(self, to, media_type, media_id=None, link=None,
                         caption=None, filename=None, reply_to=None) -> dict:
        obj: dict = {}
        if media_id: obj['id']       = media_id
        elif link:   obj['link']     = link
        if caption:  obj['caption']  = caption
        if filename: obj['filename'] = filename
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to': to, 'type': media_type, media_type: obj,
        }
        if reply_to: payload['context'] = {'message_id': reply_to}
        return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

    async def upload_media(self, file_bytes: bytes, mime_type: str, filename: str) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f'{self.base}/{self.phone_id}/media',
                headers={'Authorization': f'Bearer {self.token}'},
                data={'messaging_product': 'whatsapp', 'type': mime_type},
                files={'file': (filename, file_bytes, mime_type)},
            )
            return r.json()

    async def send_reaction(self, to: str, message_id: str, emoji: str) -> dict:
        return await self._post(f'{self.base}/{self.phone_id}/messages', {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to': to, 'type': 'reaction',
            'reaction': {'message_id': message_id, 'emoji': emoji},
        })

    async def send_flow(
        self,
        to:           str,
        flow_id:      str,
        flow_token:   str,
        cta_text:     str  = 'Open',
        header_text:  str  = '',
        body_text:    str  = 'Tap the button below to get started.',
        footer_text:  str  = '',
        first_screen: str  = '',
        flow_data:    dict = None,
    ) -> dict:
        """
        Send a published WhatsApp Flow as an interactive message.
        Docs: https://developers.facebook.com/docs/whatsapp/flows/sending-flows
        """
        import re
        # Meta requires flow_id to be numeric — catch wrong IDs early
        if not str(flow_id).strip().isdigit():
            raise ValueError(
                f"flow_id '{flow_id}' is not a valid Meta numeric flow ID. "
                "Make sure the flow is published on Meta and has a numeric meta_flow_id."
            )

        # Meta enforces 20-char max on flow_cta
        safe_cta = (cta_text or 'Open').strip()[:20] or 'Open'
        # Keep only letters+underscore (Meta requirement). Default to SCREEN_A
        # if empty — navigate action ALWAYS requires flow_action_payload.screen
        import re as _re
        safe_screen = _re.sub(r'[^A-Z_]', '', (first_screen or '').upper()) or 'SCREEN_A'

        parameters: dict = {
            'flow_message_version': '3',
            'flow_token':           flow_token,
            'flow_id':              str(flow_id).strip(),
            'flow_cta':             safe_cta,
            'flow_action':          'navigate',
            'flow_action_payload':  {
                'screen': safe_screen,
                **(flow_data or {}),
            },
        }

        interactive: dict = {
            'type':   'flow',
            'body':   {'text': body_text or 'Tap to open the form.'},
            'action': {'name': 'flow', 'parameters': parameters},
        }
        if header_text.strip():
            interactive['header'] = {'type': 'text', 'text': header_text}
        if footer_text.strip():
            interactive['footer'] = {'text': footer_text}

        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                to,
            'type':              'interactive',
            'interactive':       interactive,
        }
        log.info(f'[WA SEND] flow={flow_id} screen={safe_screen} cta="{safe_cta}" to={to}')
        log.debug(f'[WA SEND] full payload: {payload}')
        return await self._post(f'{self.base}/{self.phone_id}/messages', payload)

    async def mark_read(self, message_id: str) -> dict:
        return await self._post(f'{self.base}/{self.phone_id}/messages', {
            'messaging_product': 'whatsapp',
            'status':            'read',
            'message_id':        message_id,
        })

    async def _post(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r    = await client.post(url, json=payload, headers=self.headers)
            data = r.json()
            if r.status_code >= 400:
                log.error(f'[WA API] {r.status_code}: {data}')
            return data


# ─── Component builder ────────────────────────────────────────────────────────

def build_send_components(
    *,
    header_type:     str  = 'none',
    header_text:     str  = '',
    header_media_id: str  = '',
    header_link:     str  = '',
    header_filename: str  = '',
    body_variables:  dict = None,   # {"first_name":"John"} or {"1":"John","2":"ORD-1"}
    buttons:         list = None,   # [{"type":"QUICK_REPLY","payload":"YES","index":0}]
) -> list:
    """
    Build Meta Cloud API SEND components.
    Returns only components with actual values — never empty component objects.
    """
    comps = []
    ht = (header_type or 'none').strip().lower()

    # Header
    if ht == 'text' and header_text.strip():
        comps.append({'type': 'header', 'parameters': [{'type': 'text', 'text': header_text}]})
    elif ht in ('image', 'video', 'document'):
        obj: dict = {}
        if header_media_id.strip(): obj['id']   = header_media_id.strip()
        elif header_link.strip():   obj['link'] = header_link.strip()
        if obj:
            if ht == 'document' and header_filename.strip():
                obj['filename'] = header_filename.strip()
            comps.append({'type': 'header', 'parameters': [{'type': ht, ht: obj}]})

    # Body — preserve insertion order for named vars, numeric sort for {{1}},{{2}}
    if body_variables:
        import re as _re
        all_keys = list(body_variables.keys())

        # If ALL keys are numeric strings → sort numerically ({{1}},{{2}},{{3}})
        if all(k.isdigit() for k in all_keys):
            ordered_keys = sorted(all_keys, key=lambda k: int(k))
        else:
            # Named vars (first_name, order_number) — keep dict insertion order.
            # The caller must pass them in the correct template order.
            ordered_keys = all_keys

        params = []
        for k in ordered_keys:
            v = body_variables[k]
            if v is not None and str(v).strip():
                params.append({'type': 'text', 'text': str(v)})
        if params:
            comps.append({'type': 'body', 'parameters': params})

    # Buttons (only dynamic ones need parameters)
    for i, btn in enumerate(buttons or []):
        bt  = (btn.get('type') or '').upper()
        idx = str(btn.get('index', i))
        if bt == 'QUICK_REPLY':
            pval = btn.get('payload') or btn.get('text') or ''
            if pval:
                comps.append({'type': 'button', 'sub_type': 'quick_reply',
                              'index': idx, 'parameters': [{'type': 'payload', 'payload': pval}]})
        elif bt == 'URL':
            suffix = btn.get('url_suffix') or btn.get('text') or ''
            if suffix.strip():
                comps.append({'type': 'button', 'sub_type': 'url',
                              'index': idx, 'parameters': [{'type': 'text', 'text': suffix}]})
        elif bt == 'COPY_CODE':
            code = btn.get('code') or btn.get('text') or ''
            if code.strip():
                comps.append({'type': 'button', 'sub_type': 'copy_code',
                              'index': idx, 'parameters': [{'type': 'coupon_code', 'coupon_code': code}]})

    return comps


def normalize_send_components(components: list) -> list:
    """Normalize component types to lowercase for Meta SEND endpoint."""
    out = []
    for comp in components:
        c = {**comp}
        if 'type' in c:
            t = c['type'].lower()
            if t == 'buttons': t = 'button'
            c['type'] = t
        if 'sub_type' in c: c['sub_type'] = c['sub_type'].lower()
        if 'parameters' in c:
            c['parameters'] = [
                {**p, 'type': p['type'].lower()} if 'type' in p else {**p}
                for p in c['parameters']
            ]
        out.append(c)
    return out


def normalize_create_components(components: list) -> list:
    """Ensure UPPERCASE types for Meta CREATE endpoint."""
    out = []
    for comp in components:
        c = {**comp}
        if 'type' in c:   c['type']   = c['type'].upper()
        if 'format' in c: c['format'] = c['format'].upper()
        if 'buttons' in c and isinstance(c['buttons'], list):
            c['buttons'] = [{**b, 'type': b['type'].upper()} if 'type' in b else {**b}
                            for b in c['buttons']]
        out.append(c)
    return out