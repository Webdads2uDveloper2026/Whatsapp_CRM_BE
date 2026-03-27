"""
Reset stuck broadcasts and re-test.
Run: python reset_broadcast.py
"""
import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.getcwd())

async def main():
    from app.database import connect_db
    await connect_db()
    import app.database as _db
    db = _db.db
    from datetime import datetime

    # Reset all stuck running broadcasts to draft
    r = await db.broadcasts.update_many(
        {"status": "running"},
        {"$set": {"status": "draft", "failed_count": 0, "sent_count": 0, "updated_at": datetime.utcnow()}}
    )
    print(f"✅ Reset {r.modified_count} stuck broadcast(s) to draft")

    # Show all broadcasts
    docs = await db.broadcasts.find({}).to_list(20)
    print(f"\nAll broadcasts ({len(docs)}):")
    for d in docs:
        print(f"  '{d['name']}' | status={d['status']} | tpl={d['template_name']} | total={d.get('total_recipients',0)} | sent={d.get('sent_count',0)} | failed={d.get('failed_count',0)}")

    # Check if template is approved
    tpl_names = list(set(d['template_name'] for d in docs))
    if tpl_names:
        print(f"\nChecking templates: {tpl_names}")
        for name in tpl_names:
            tpl = await db.templates.find_one({"name": name})
            if tpl:
                print(f"  '{name}' → status={tpl.get('status')} | components={len(tpl.get('components',[]))}")
            else:
                print(f"  '{name}' → NOT FOUND in local DB (need to sync from Meta)")

asyncio.run(main())