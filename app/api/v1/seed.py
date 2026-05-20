"""
app/api/v1/seed.py — Idempotent startup seed: super admin + default plans
"""
from app.models.super_admin import SuperAdmin, SubscriptionPlan
from app.core.security import hash_password


async def seed_super_admin():
    exists = await SuperAdmin.find_one(SuperAdmin.email == "admin@example.com")
    if not exists:
        sa = SuperAdmin(
            email="admin@example.com",
            hashed_password=hash_password("Admin@123"),
            name="Super Admin",
        )
        await sa.insert()
        print("✅ Super Admin seeded: admin@example.com / Admin@123")
    else:
        print("ℹ️  Super Admin already exists")


async def seed_plans():
    count = await SubscriptionPlan.count()
    if count > 0:
        print("ℹ️  Plans already seeded")
        return

    plans = [
        SubscriptionPlan(
            name="Trial", description="Free trial with limited features",
            price_monthly=0, price_yearly=0,
            agent_limit=1, broadcast_limit=100, template_limit=5,
            contact_limit=500, flow_builder=False, analytics=False,
            automations=False, api_access=False, sort_order=0,
        ),
        SubscriptionPlan(
            name="Starter", description="Perfect for small businesses",
            price_monthly=999, price_yearly=9990,
            agent_limit=3, broadcast_limit=1000, template_limit=10,
            contact_limit=1000, flow_builder=False, analytics=False,
            automations=True, api_access=False, sort_order=1,
        ),
        SubscriptionPlan(
            name="Growth", description="For growing teams",
            price_monthly=2499, price_yearly=24990,
            agent_limit=10, broadcast_limit=10000, template_limit=50,
            contact_limit=10000, flow_builder=True, analytics=True,
            automations=True, api_access=False, sort_order=2,
        ),
        SubscriptionPlan(
            name="Pro", description="Full-featured for power users",
            price_monthly=4999, price_yearly=49990,
            agent_limit=25, broadcast_limit=50000, template_limit=200,
            contact_limit=50000, flow_builder=True, analytics=True,
            automations=True, api_access=True, sort_order=3,
        ),
        SubscriptionPlan(
            name="Enterprise", description="Unlimited — contact us",
            price_monthly=0, price_yearly=0,
            agent_limit=999, broadcast_limit=999999, template_limit=999,
            contact_limit=999999, flow_builder=True, analytics=True,
            automations=True, api_access=True, sort_order=4,
        ),
    ]
    for p in plans:
        await p.insert()
    print(f"✅ {len(plans)} subscription plans seeded")


async def run_all_seeds():
    await seed_super_admin()
    await seed_plans()
