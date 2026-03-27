"""
Test auto-reply end-to-end without needing a webhook.
Run from backend root:
  cd ~/projects/wcrm/backend
  source wp_env/bin/activate
  python test_autoreply.py
"""
import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv()

# ── Minimal bootstrap so we can import app modules ────────────────────────────
sys.path.insert(0, os.getcwd())

async def main():
    from app.database import connect_db
    await connect_db()

    from app.database import db

    print("\n" + "="*60)
    print("STEP 1: Check active auto-reply rules in DB")
    print("="*60)
    rules = await db.autoreplies.find({}).to_list(20)
    if not rules:
        print("❌ NO RULES FOUND in 'autoreplies' collection!")
        print("   → Go to Automations page and create a rule first.")
        return
    for r in rules:
        print(f"  Rule: '{r['name']}' | active={r.get('is_active')} | "
              f"priority={r.get('priority')} | trigger={r.get('trigger',{}).get('type')} | "
              f"action={r.get('action',{}).get('type')}")

    print("\n" + "="*60)
    print("STEP 2: Find a tenant")
    print("="*60)
    from app.models.tenant import Tenant
    tenant = await Tenant.find_one(Tenant.status == "active")
    if not tenant:
        print("❌ No active tenant found")
        return
    print(f"  Tenant: {tenant.name} | id={tenant.id}")
    print(f"  Phone ID: {getattr(tenant,'phone_number_id',None) or os.getenv('META_PHONE_NUMBER_ID','NOT SET')}")
    print(f"  Has token: {bool(getattr(tenant,'encrypted_access_token',None) or os.getenv('META_ACCESS_TOKEN',''))}")

    print("\n" + "="*60)
    print("STEP 3: Test WhatsApp client")
    print("="*60)
    from app.services.whatsapp import get_wa_client
    try:
        client = get_wa_client(tenant)
        print(f"  ✅ Client created | phone_id={client.phone_id}")
        print(f"  Base URL: {client.base}")
    except Exception as e:
        print(f"  ❌ get_wa_client failed: {e}")
        return

    print("\n" + "="*60)
    print("STEP 4: Simulate run_autoreplies()")
    print("="*60)

    # Find a real conversation to use
    convo = await db.conversations.find_one({"tenant_id": str(tenant.id), "status": "open"})
    if not convo:
        print("❌ No open conversation found — send a message from WhatsApp first")
        return

    wa_id      = convo["wa_id"]
    convo_id   = str(convo["_id"])
    contact_id = str(convo.get("contact_id", ""))
    print(f"  Using conversation: {convo_id} | wa_id=+{wa_id}")

    from app.api.v1.autoreplies import run_autoreplies
    print("  Calling run_autoreplies()...")
    try:
        await run_autoreplies(
            tenant       = tenant,
            wa_id        = wa_id,
            contact_id   = contact_id,
            convo_id     = convo_id,
            msg_type     = "text",
            content      = {"body": "hello"},
            is_new_convo = False,
        )
        print("  ✅ run_autoreplies() completed without exception")
    except Exception as e:
        import traceback
        print(f"  ❌ run_autoreplies() raised: {e}")
        traceback.print_exc()

    print("\n" + "="*60)
    print("STEP 5: Check if auto-reply message was saved")
    print("="*60)
    msgs = await db.messages.find(
        {"conversation_id": convo_id, "auto_reply": True}
    ).sort("created_at", -1).limit(3).to_list(3)
    if msgs:
        for m in msgs:
            print(f"  ✅ Auto-reply saved: type={m.get('type')} | status={m.get('status')} | wamid={m.get('wa_message_id','')[:30]}")
    else:
        print("  ❌ No auto_reply messages found in DB for this conversation")

    print("\n" + "="*60)
    print("DONE")
    print("="*60)

asyncio.run(main())