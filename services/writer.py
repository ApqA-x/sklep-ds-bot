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

STARTUP_BACKOFF_INITIAL_SECONDS = 1.0
STARTUP_BACKOFF_MAX_SECONDS = 30.0


def _startup_backoff_seconds(attempt: int) -> float:
    return min(STARTUP_BACKOFF_INITIAL_SECONDS * (2 ** max(attempt - 1, 0)), STARTUP_BACKOFF_MAX_SECONDS)


async def _start_with_retry(cfg, repo: Repository) -> tuple[Bus, Service]:
    attempt = 0
    while True:
        attempt += 1
        nats = NATS()
        bus = Bus(nats, cfg.event_signing_secret, "writer")
        service = Service(repo, bus)

        async def _handle_session_closed(payload: bytes) -> None:
            try:
                logger.info("session closed event received payload_size=%s", len(payload))
                await service.HandleSessionClosed(payload)
                logger.info("session closed event processed payload_size=%s", len(payload))
            except Exception:
                logger.exception("summary generation failed payload_size=%s", len(payload))
                raise

        try:
            await nats.connect(cfg.nats_url)
            await bus.subscribe(None, domain.SUBJECT_SESSION_CLOSED, repo, _handle_session_closed)
            await service.Start()
            logger.info("writer startup dependencies ready attempts=%s", attempt)
            return bus, service
        except asyncio.CancelledError:
            try:
                await bus.aclose()
            except Exception:
                logger.exception("writer shutdown during startup failed while closing bus")
            raise
        except Exception:
            delay = _startup_backoff_seconds(attempt)
            logger.exception(
                "writer startup dependency setup failed attempts=%s retry_in=%.1fs",
                attempt,
                delay,
            )
            try:
                await bus.aclose()
            except Exception:
                logger.exception("writer startup retry cleanup failed attempts=%s", attempt)
            await asyncio.sleep(delay)


async def main() -> None:
    configure_logging("writer")
    cfg = load_config()
    require_event_signing_secret(cfg.event_signing_secret)
    logger.info("writer service starting")

    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)

    bus, service = await _start_with_retry(cfg, repo)

    async def sweep_pending() -> None:
        while True:
            await asyncio.sleep(60)
            logger.info("writer pending summary sweep starting")
            await service.Start()
            logger.info("writer pending summary sweep finished")

    sweep = asyncio.create_task(sweep_pending())
    try:
        await asyncio.Event().wait()
    finally:
        sweep.cancel()
        await bus.aclose()
        mongo_client.close()


if __name__ == "__main__":
    asyncio.run(main())
