"""
Full broadcast test — creates, sends, verifies.
Run: python test_broadcast_send.py
"""
import asyncio, os, sys, json
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.getcwd())

async def main():
    from app.database import connect_db
    await connect_db()
    import app.database as _db
    db = _db.db

    from app.models.tenant import Tenant
    tenant = await Tenant.find_one(Tenant.status == "active")
    tid = str(tenant.id)
    print(f"Tenant: {tenant.business_name} | {tid}")

    # ── Pick a good template (0 vars preferred) ───────────────────────────────
    import re
    templates = await db.templates.find({"status": "APPROVED"}).to_list(50)
    print(f"\nApproved templates: {len(templates)}")

    # Find one with 0 variables first (easiest to test)
    zero_var_tpl = None
    two_var_tpl  = None
    for t in templates:
        body = next((c.get("text","") for c in t.get("components",[]) if c.get("type")=="BODY"), "")
        vars_found = re.findall(r'\{\{(\w+)\}\}', body)
        if not vars_found and not zero_var_tpl:
            zero_var_tpl = t
        if len(vars_found) == 2 and not two_var_tpl:
            two_var_tpl = (t, vars_found)
        print(f"  {t['name']:35} vars={vars_found}")

    # ── Get opted-in contacts ─────────────────────────────────────────────────
    contacts = await db.contacts.find({"tenant_id": tid, "opted_in": True}).to_list(5)
    print(f"\nOpted-in contacts: {len(contacts)}")
    for c in contacts:
        print(f"  +{c['wa_id']} {c.get('profile_name','')}")
    if not contacts:
        print("❌ No opted-in contacts — add contacts with opted_in=True first"); return

    # ── Test 1: Send a 0-variable template directly ───────────────────────────
    from app.services.whatsapp import get_wa_client, build_send_components
    client = get_wa_client(tenant)
    wa_id  = contacts[0]["wa_id"]

    if zero_var_tpl:
        tpl = zero_var_tpl
        print(f"\n{'='*60}")
        print(f"TEST 1: Sending 0-var template '{tpl['name']}' → +{wa_id}")
        resp = await client.send_template(wa_id, tpl["name"], tpl.get("language","en_US"), [])
        print(f"Meta: {json.dumps(resp, indent=2)}")
        if "error" not in resp:
            print("✅ 0-var template works!")
    else:
        print("No 0-var template found")

    # ── Test 2: Send 2-variable template with sample values ───────────────────
    if two_var_tpl:
        tpl, var_names = two_var_tpl
        variables = {v: f"TestValue{i+1}" for i,v in enumerate(var_names)}
        components = build_send_components(body_variables=variables)
        print(f"\n{'='*60}")
        print(f"TEST 2: Sending 2-var template '{tpl['name']}' → +{wa_id}")
        print(f"Variables: {variables}")
        print(f"Components: {json.dumps(components, indent=2)}")
        resp = await client.send_template(wa_id, tpl["name"], tpl.get("language","en_US"), components)
        print(f"Meta: {json.dumps(resp, indent=2)}")
        if "error" in resp:
            err = resp["error"]
            print(f"❌ ERROR {err.get('code')}: {err.get('message')}")
            print(f"   Details: {err.get('error_data',{})}")
        else:
            print("✅ 2-var template works!")

    # ── Test 3: Create + send broadcast via API ───────────────────────────────
    print(f"\n{'='*60}")
    print("TEST 3: Create broadcast via API and send")
    import httpx
    # Get auth token
    from app.core.security import create_access_token
    token = create_access_token({"sub": tid, "tenant_id": tid})

    tpl_to_use = zero_var_tpl or (two_var_tpl[0] if two_var_tpl else None)
    if not tpl_to_use:
        print("No template available"); return

    vars_to_use = {}
    if two_var_tpl and tpl_to_use == two_var_tpl[0]:
        vars_to_use = {v: f"Test{i+1}" for i,v in enumerate(two_var_tpl[1])}

    async with httpx.AsyncClient(timeout=30) as http:
        # Create broadcast
        r = await http.post("http://localhost:8002/api/v1/broadcasts",
            json={
                "name":            "API Test Broadcast",
                "template_name":   tpl_to_use["name"],
                "template_language": tpl_to_use.get("language","en_US"),
                "audience_type":   "all",
                "schedule_type":   "draft",
                "variables":       vars_to_use,
            },
            headers={"Authorization": f"Bearer {token}"}
        )
        print(f"Create status: {r.status_code}")
        data = r.json()
        print(f"Create response: {json.dumps(data, indent=2)}")

        if r.status_code not in (200, 201):
            print("❌ Create failed"); return

        bid = data.get("id")
        print(f"Broadcast id: {bid}")

        # Send it
        r2 = await http.post(f"http://localhost:8002/api/v1/broadcasts/{bid}/send",
            headers={"Authorization": f"Bearer {token}"}
        )
        print(f"\nSend status: {r2.status_code}")
        print(f"Send response: {json.dumps(r2.json(), indent=2)}")

    # Wait for background task
    await asyncio.sleep(4)

    # Check result
    bc = await db.broadcasts.find_one({"_id": __import__("bson").ObjectId(bid)})
    if bc:
        print(f"\nBroadcast result:")
        print(f"  status  : {bc['status']}")
        print(f"  total   : {bc['total_recipients']}")
        print(f"  sent    : {bc['sent_count']}")
        print(f"  failed  : {bc['failed_count']}")
        if bc['sent_count'] > 0:
            print("✅ BROADCAST WORKING!")
        else:
            print("❌ Still failing — check backend logs for [BROADCAST] lines")

asyncio.run(main())