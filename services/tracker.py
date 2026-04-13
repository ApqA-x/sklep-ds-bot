from __future__ import annotations

import asyncio
import logging

from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker.bus import Bus
from voice_tracker import domain
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret
from voice_tracker.tracker import Defaults, Service, decode_voice_event

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging("tracker")
    cfg = load_config()
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("tracker service starting")

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)

    nats = NATS()
    await nats.connect(cfg.nats_url)
    bus = Bus(nats, cfg.event_signing_secret, "tracker")
    service = Service(
        repo,
        bus,
        Defaults(tracking_mode=cfg.tracking_mode, tracked_channel_ids=cfg.tracked_channel_ids),
    )

    async def handle(payload: bytes) -> None:
        try:
            await service.HandleVoiceEvent(decode_voice_event(payload))
        except Exception:
            logger.exception("voice event handling failed payload_size=%s", len(payload))
            raise

    await bus.subscribe(None, domain.SUBJECT_VOICE_EVENT, repo, handle)
    await service.Start()

    try:
        await asyncio.Event().wait()
    finally:
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
