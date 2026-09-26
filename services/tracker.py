from __future__ import annotations

import asyncio
import logging

from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker.bus import Bus
from voice_tracker import domain, eventlog
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
    bus = Bus(nats, cfg.event_signing_secret, "tracker", max_age_seconds=cfg.event_max_age_seconds)
    service = Service(
        repo,
        eventlog.DurablePublisher(bus, repo.db, issuer="tracker"),
        Defaults(tracking_mode=cfg.tracking_mode, tracked_channel_ids=cfg.tracked_channel_ids),
    )
    startup_ready = asyncio.Event()

    async def handle(payload: bytes) -> None:
        await startup_ready.wait()
        try:
            event = decode_voice_event(payload)
            logger.info(
                "voice event received guild=%s user=%s prev_channel=%s channel=%s is_bot=%s",
                event.guild_id or "-",
                event.user_id or "-",
                event.previous_channel_id or "-",
                event.channel_id or "-",
                event.is_bot,
            )
            await service.HandleVoiceEvent(event)
            logger.info(
                "voice event processed guild=%s user=%s prev_channel=%s channel=%s",
                event.guild_id or "-",
                event.user_id or "-",
                event.previous_channel_id or "-",
                event.channel_id or "-",
            )
        except Exception:
            logger.exception("voice event handling failed payload_size=%s", len(payload))
            raise

    await bus.subscribe(
        None, domain.SUBJECT_VOICE_EVENT, None, handle, consumer="tracker", db=repo.db
    )

    async def sweep() -> None:
        # T09/E01: догрузка пропущенных/незавершённых событий из журнала; republish
        # записанных, но не доставленных на транспорт session.closed (E02).
        while True:
            await asyncio.sleep(cfg.event_sweep_interval_seconds)
            try:
                await eventlog.republish_pending(bus, repo.db, domain.SUBJECT_SESSION_CLOSED)
                n = await eventlog.sweep_pending(
                    repo.db,
                    "tracker",
                    [domain.SUBJECT_VOICE_EVENT],
                    handle,
                    max_deliver=cfg.event_max_deliver,
                )
                stats = eventlog.pending_stats(repo.db, "tracker", [domain.SUBJECT_VOICE_EVENT])
                if n or stats["backlog"] or stats["quarantined"]:
                    logger.info("tracker event sweep delivered=%s %s", n, stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tracker event sweep failed")

    sweep_task = asyncio.create_task(sweep(), name="tracker-event-sweep")
    await service.Start()
    logger.info("tracker startup replay complete")
    startup_ready.set()

    try:
        await asyncio.Event().wait()
    finally:
        sweep_task.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
