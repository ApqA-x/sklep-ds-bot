from __future__ import annotations

import asyncio
import logging

from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker.bus import Bus
from voice_tracker import domain
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret
from voice_tracker.summary import Service

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging("writer")
    cfg = load_config()
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("writer service starting")

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "writer")
    service = Service(repo, bus)

    async def _handle_session_closed(payload: bytes) -> None:
        try:
            await service.HandleSessionClosed(payload)
        except Exception:
            logger.exception("summary generation failed payload_size=%s", len(payload))
            raise

    await bus.subscribe(None, domain.SUBJECT_SESSION_CLOSED, repo, _handle_session_closed)
    await service.Start()

    async def sweep_pending() -> None:
        while True:
            await asyncio.sleep(60)
            await service.Start()

    sweep = asyncio.create_task(sweep_pending())
    try:
        await asyncio.Event().wait()
    finally:
        sweep.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
