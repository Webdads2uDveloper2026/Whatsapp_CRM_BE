"""
seed_autoreply.py
Run: python seed_autoreply.py
"""
import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.getcwd())

async def main():
    from app.database import connect_db
    await connect_db()

    # Import db AFTER connect_db() so the module variable is populated
    import app.database as _db_module
    db = _db_module.db

    from app.models.tenant import Tenant
    tenant = await Tenant.find_one(Tenant.status == "active")
    if not tenant:
        print("❌ No active tenant")
        return

    tid = str(tenant.id)
    print(f"✅ Tenant: '{tenant.business_name}' id={tid}")

    # Show ALL docs in collection
    all_docs = await db.autoreplies.find({}).to_list(100)
    print(f"\nAll docs in autoreplies collection: {len(all_docs)}")
    for d in all_docs:
        match = "✅ MATCH" if d.get("tenant_id") == tid else f"❌ MISMATCH (stored={d.get('tenant_id')})"
        print(f"  '{d.get('name')}' active={d.get('is_active')} {match}")

    # Remove old test rules
    await db.autoreplies.delete_many({"name": "Test Welcome Reply", "tenant_id": tid})

    # Insert a working rule
    from datetime import datetime
    now = datetime.utcnow()
    ins = await db.autoreplies.insert_one({
        "tenant_id":  tid,
        "name":       "Test Welcome Reply",
        "is_active":  True,
        "priority":   1,
        "trigger":    {"type": "any", "keywords": [], "match": "contains"},
        "action":     {
            "type": "text",
            "text": "Hi! Thanks for your message. We will get back to you shortly.",
            "template_name": "", "language": "en_US", "variables": {}
        },
        "conditions": {"only_first_message": False, "cooldown_minutes": 0},
        "stats":      {"sent": 0, "last_triggered": None},
        "created_at": now, "updated_at": now,
    })
    print(f"\n✅ Inserted rule id={ins.inserted_id}")

    # Test the engine
    print("\n" + "="*60)
    print("Testing engine...")
    print("="*60)

    convo = await db.conversations.find_one({"tenant_id": tid, "status": "open"})
    if not convo:
        print("❌ No open conversation — send a WhatsApp message to your number first")
        return

    wa_id      = convo["wa_id"]
    convo_id   = str(convo["_id"])
    contact_id = str(convo.get("contact_id", ""))
    print(f"Using conversation wa_id=+{wa_id}")

    from app.api.v1.autoreplies import run_autoreplies
    await run_autoreplies(
        tenant       = tenant,
        wa_id        = wa_id,
        contact_id   = contact_id,
        convo_id     = convo_id,
        msg_type     = "text",
        content      = {"body": "hello test"},
        is_new_convo = False,
    )

    # Verify
    sent = await db.messages.find_one(
        {"conversation_id": convo_id, "auto_reply": True},
        sort=[("created_at", -1)]
    )
    if sent:
        print(f"\n✅ Auto-reply saved in DB!")
        print(f"   content : {sent.get('content')}")
        print(f"   wamid   : {sent.get('wa_message_id', '(empty)')}")
        print(f"   status  : {sent.get('status')}")
    else:
        print("\n❌ No auto_reply message in DB — check [AUTOREPLY] logs above")

asyncio.run(main())