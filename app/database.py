from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from beanie import init_beanie
import structlog
from app.config import get_settings

log = structlog.get_logger()
settings = get_settings()

_client: AsyncIOMotorClient | None = None
db: AsyncIOMotorDatabase | None = None


async def connect_db() -> None:
    global _client, db
    _client = AsyncIOMotorClient(settings.mongodb_url)
    db = _client[settings.mongodb_db_name]

    from app.models.tenant import Tenant
    from app.models.agent import Agent, Contact, Conversation, Message, Template, Broadcast, Automation

    await init_beanie(
        database=db,
        document_models=[
            Tenant, Agent, Contact, Conversation,
            Message, Template, Broadcast, Automation,
        ],
    )
    log.info("mongodb.connected", db=settings.mongodb_db_name)


async def close_db() -> None:
    global _client
    if _client:
        _client.close()
    log.info("mongodb.disconnected")