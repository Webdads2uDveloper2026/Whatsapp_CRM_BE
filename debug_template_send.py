"""
debug_template_send.py — Run this to diagnose template issues

Usage:
  cd ~/projects/wcrm/backend
  source wp_env/bin/activate
  python debug_template_send.py
"""
import asyncio, httpx, json, os, re
from dotenv import load_dotenv

load_dotenv()

TOKEN     = os.getenv('META_ACCESS_TOKEN', '')
PHONE_ID  = os.getenv('META_PHONE_NUMBER_ID', '578790305328460')
WABA_ID   = os.getenv('META_WABA_ID', '624519580627601')
VERSION   = os.getenv('META_API_VERSION', 'v22.0')
TEST_TO   = '918870395516'   # ← your test WhatsApp number

HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type':  'application/json',
}
MSG_URL = f'https://graph.facebook.com/{VERSION}/{PHONE_ID}/messages'

print(f'Phone ID : {PHONE_ID}')
print(f'WABA ID  : {WABA_ID}')
print(f'Token    : {TOKEN[:20]}...' if TOKEN else 'TOKEN MISSING!')
print()


async def main():
    async with httpx.AsyncClient(timeout=30) as client:

        # ── STEP 1: List all approved templates from Meta ─────────────────────
        print('=' * 70)
        print('STEP 1: Fetching your templates from Meta')
        print('=' * 70)
        r = await client.get(
            f'https://graph.facebook.com/{VERSION}/{WABA_ID}/message_templates',
            params={'fields': 'name,status,category,language,components', 'limit': 50},
            headers=HEADERS,
        )
        data = r.json()
        if 'error' in data:
            print(f'ERROR: {data["error"]["message"]}')
            return

        templates = data.get('data', [])
        print(f'Found {len(templates)} templates:\n')

        approved = []
        for t in templates:
            name   = t.get('name', '')
            status = t.get('status', '')
            comps  = t.get('components', [])
            body_c = next((c for c in comps if c.get('type') == 'BODY'), None)
            hdr_c  = next((c for c in comps if c.get('type') == 'HEADER'), None)
            btn_c  = next((c for c in comps if c.get('type') == 'BUTTONS'), None)

            body_text = body_c.get('text', '') if body_c else ''
            vars_list = re.findall(r'\{\{(\w+)\}\}', body_text)
            n_vars    = len(vars_list)
            hdr_fmt   = hdr_c.get('format', 'none') if hdr_c else 'none'
            n_btns    = len(btn_c.get('buttons', [])) if btn_c else 0

            flag = '✅' if status == 'APPROVED' else '❌'
            print(f'{flag} {name:35} | {status:10} | hdr:{hdr_fmt:8} | vars:{n_vars} {vars_list} | btns:{n_btns}')
            if body_text:
                print(f'   body: {body_text[:100]}')

            if status == 'APPROVED':
                approved.append({
                    'name': name, 'language': t.get('language','en_US'),
                    'n_vars': n_vars, 'vars': vars_list,
                    'hdr_fmt': hdr_fmt, 'n_btns': n_btns,
                    'body': body_text,
                })

        print()
        if not approved:
            print('⚠️  NO APPROVED TEMPLATES — you cannot send any templates until Meta approves them')
            return

        # ── STEP 2: Send the first approved template correctly ─────────────────
        print('=' * 70)
        print('STEP 2: Sending first approved template correctly')
        print('=' * 70)

        tpl = approved[0]
        print(f'\nTemplate: {tpl["name"]}')
        print(f'Variables needed: {tpl["n_vars"]} {tpl["vars"]}')
        print(f'Header: {tpl["hdr_fmt"]}')

        # Build correct components
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                TEST_TO,
            'type':              'template',
            'template': {
                'name':     tpl['name'],
                'language': {'code': tpl['language']},
            }
        }

        # Only add components if there are variables
        comps = []
        if tpl['n_vars'] > 0:
            # Use sample values for each variable
            params = [{'type': 'text', 'text': f'SampleValue{i+1}'} for i in range(tpl['n_vars'])]
            comps.append({'type': 'body', 'parameters': params})

        if comps:
            payload['template']['components'] = comps

        print(f'\nPayload:\n{json.dumps(payload, indent=2)}')

        r = await client.post(MSG_URL, json=payload, headers=HEADERS)
        resp = r.json()
        print(f'\nStatus: {r.status_code}')
        print(f'Response: {json.dumps(resp, indent=2)}')

        if r.status_code == 200:
            wamid = resp.get('messages', [{}])[0].get('id', '')
            print(f'\n✅ SUCCESS! Message ID: {wamid}')
            print('Check your WhatsApp for the message.')
        else:
            err = resp.get('error', {})
            print(f'\n❌ FAILED: {err.get("message", str(resp))}')
            print(f'   Code: {err.get("code")}')
            print(f'   Details: {err.get("error_data", {})}')

        # ── STEP 3: Summary ────────────────────────────────────────────────────
        print()
        print('=' * 70)
        print('STEP 3: What to fix in your CRM')
        print('=' * 70)
        for t in approved:
            print(f'\n  Template: {t["name"]}')
            if t['n_vars'] == 0:
                print(f'    → Send with NO components (no body variables)')
                print(f'    → In Inbox: leave body_variables empty {{}}')
            else:
                print(f'    → Send with {t["n_vars"]} body variable(s): {t["vars"]}')
                vars_str = ", ".join('"' + v + '": "value"' for v in t["vars"])
                print("    -> In Inbox: body_variables = {" + vars_str + "}")
            if t['hdr_fmt'] not in ('none', 'TEXT', ''):
                print(f'    → Needs {t["hdr_fmt"]} header media (id or link)')

asyncio.run(main())