from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from app.models.tenant import Tenant
from app.core.dependencies import get_current_tenant

router = APIRouter(prefix="/analytics", tags=["analytics"])


def get_db():
    """Get db lazily at request time — avoids importing None at module load."""
    from app.database import db
    if db is None:
        raise RuntimeError("Database not connected")
    return db


@router.get("/overview")
async def overview(
    tenant: Tenant = Depends(get_current_tenant),
    days: int = Query(7, ge=1, le=90),
):
    db    = get_db()
    since = datetime.utcnow() - timedelta(days=days)
    tid   = str(tenant.id)

    total_contacts = await db.contacts.count_documents({"tenant_id": tid})
    opted_in       = await db.contacts.count_documents({"tenant_id": tid, "opted_in": True})
    open_convos    = await db.conversations.count_documents({"tenant_id": tid, "status": "open"})
    inbound        = await db.messages.count_documents({"tenant_id": tid, "direction": "inbound",  "created_at": {"$gte": since}})
    outbound       = await db.messages.count_documents({"tenant_id": tid, "direction": "outbound", "created_at": {"$gte": since}})
    delivered      = await db.messages.count_documents({"tenant_id": tid, "direction": "outbound", "status": "delivered", "created_at": {"$gte": since}})
    read_msgs      = await db.messages.count_documents({"tenant_id": tid, "direction": "outbound", "status": "read",      "created_at": {"$gte": since}})
    failed         = await db.messages.count_documents({"tenant_id": tid, "direction": "outbound", "status": "failed",    "created_at": {"$gte": since}})

    return {
        "period_days": days,
        "contacts":    {"total": total_contacts, "opted_in": opted_in},
        "conversations": {"open": open_convos},
        "messages": {
            "inbound":       inbound,
            "outbound":      outbound,
            "failed":        failed,
            "delivery_rate": round(delivered / max(outbound, 1) * 100, 1),
            "read_rate":     round(read_msgs  / max(outbound, 1) * 100, 1),
        },
    }


@router.get("/daily")
async def daily(
    tenant: Tenant = Depends(get_current_tenant),
    days: int = Query(30, ge=7, le=90),
):
    db    = get_db()
    since = datetime.utcnow() - timedelta(days=days)
    tid   = str(tenant.id)

    pipeline = [
        {"$match": {"tenant_id": tid, "created_at": {"$gte": since}}},
        {"$group": {
            "_id": {
                "date":      {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                "direction": "$direction",
            },
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.date": 1}},
    ]
    rows = await db.messages.aggregate(pipeline).to_list(None)

    by_date: dict = {}
    for r in rows:
        d = r["_id"]["date"]
        if d not in by_date:
            by_date[d] = {"date": d, "inbound": 0, "outbound": 0}
        by_date[d][r["_id"]["direction"]] = r["count"]

    return {"days": days, "data": list(by_date.values())}