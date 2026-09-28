from __future__ import annotations

import asyncio
import logging

from nats.aio.client import Client as NATS
from pymongo import MongoClient

from voice_tracker import supervise
from voice_tracker.bus import Bus
from voice_tracker import domain, eventlog
from voice_tracker.repository import Repository
from voice_tracker.runtime import configure_logging, load_config, require_event_signing_secret, wait_for_shutdown
from voice_tracker.tracker import Defaults, Service, decode_voice_event

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging("tracker")
    cfg = load_config()
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("tracker service starting")

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    # R26-07 (DB03): startup по умолчанию verify-only; DDL — только в явном bootstrap-режиме.
    if getattr(cfg, "schema_mode", "verify") == "bootstrap":
        repo.ensure_indexes(None)
    else:
        repo.verify_startup()

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
                    scan_limit=cfg.event_sweep_scan_limit,
                    gap_seconds=cfg.event_sweep_gap_seconds,
                )
                stats = await asyncio.to_thread(
                    eventlog.pending_stats, repo.db, "tracker", [domain.SUBJECT_VOICE_EVENT]
                )
                if n or stats["backlog"] or stats["quarantined"]:
                    logger.info("tracker event sweep delivered=%s %s", n, stats)
                supervisor.beat("tracker-event-sweep")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # R26-10.3: исключение проглочено, цикл жив, но итерация без
                # прогресса → наблюдаемая серия отказов (beat сбросит на успехе)
                supervisor.fail("tracker-event-sweep", exc)
                logger.exception("tracker event sweep failed")

    supervisor = supervise.Supervisor()
    await service.Start()
    logger.info("tracker startup replay complete")
    startup_ready.set()

    supervisor.spawn("tracker-event-sweep", sweep, critical=True)
    heartbeat = supervise.Heartbeat(
        repo.db,
        "tracker",
        supervisor,
        state_fn=lambda: {"nats": supervise.nats_state(bus.conn)},
    )
    supervise.attach(supervisor, heartbeat)
    try:
        # R26-12b: вечное ожидание снимаем только SIGTERM/SIGINT — иначе docker
        # stop убивал процесс по grace-таймауту и drain ниже не исполнялся.
        await wait_for_shutdown()
    finally:
        await supervisor.shutdown()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
