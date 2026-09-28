from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
import logging
from os import environ
import signal
from typing import Any


def _clean(value: str | None) -> str:
    return (value or "").strip()


def parse_user_ids(raw: str | None) -> list[str]:
    raw = _clean(raw)
    if raw == "":
        return []
    tokens: list[str] = []
    current: list[str] = []
    for char in raw:
        if char.isspace() or char in {",", ";"}:
            if current:
                tokens.append("".join(current))
                current.clear()
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))

    seen: set[str] = set()
    user_ids: list[str] = []
    for token in tokens:
        token = token.strip()
        token = token.removeprefix("<@!").removeprefix("<@").removeprefix("<").removesuffix(">")
        token = token.strip()
        if token == "" or token in seen:
            continue
        seen.add(token)
        user_ids.append(token)
    return user_ids


def configure_logging(service_name: str = "", env: Any = None) -> None:
    source = environ if env is None else env
    requested_level = _clean(source.get("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, requested_level, None)
    if not isinstance(level, int):
        requested_level = "INFO"
        level = logging.INFO

    root_logger = logging.getLogger()
    if len(root_logger.handlers) == 0:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root_logger.setLevel(level)

    logging.getLogger(__name__).info(
        "logging configured service=%s level=%s",
        service_name or "unknown",
        requested_level,
    )


@dataclass(slots=True)
class Config:
    service_name: str = "tracker"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "voice_tracker"
    nats_url: str = "nats://localhost:4222"
    discord_token: str = ""
    discord_application_id: str = ""
    discord_guild_id: str = ""
    bot_admin_user_ids: list[str] = field(default_factory=list)
    event_signing_secret: str = ""
    media_dir: str = ""
    # T16 (L05): порог свободного места для НОВЫХ вложений; 0 = проверка выключена.
    # Существующий архив при любом значении не удаляется (решение владельца — D07).
    media_min_free_bytes: int = 2 * 1024 * 1024 * 1024
    tracking_mode: str = "all"
    tracked_channel_ids: list[str] = field(default_factory=list)
    # T09: envelope freshness (wire), период догрузки из журнала, потолок попыток deliver
    event_max_age_seconds: int = 3600
    event_sweep_interval_seconds: int = 15
    event_max_deliver: int = 8
    # R26-01: ширина окна gap-прохода (0 = выключен) и потолок строк страницы
    event_sweep_gap_seconds: int = 120
    event_sweep_scan_limit: int = 2000
    # E09 (R26-02): окно «живого» heartbeat другого instance для отказа старту
    # второго gateway; 0 = guard выключен (только по явному решению оператора).
    gateway_singleton_max_age_seconds: int = 90
    # R26-07 (DB06/V26-18): режим работы со схемой на startup. "verify" (по
    # умолчанию) — ТОЛЬКО read-only сверка: runtime-роли намеренно
    # НЕ имеют createIndex/dropIndex/dropCollection, требовать DDL на startup
    # нельзя, и записей startup тоже не делает (CRUD-backfill revision —
    # миграция M7 runner'а). "bootstrap" — полный ensure_indexes (dev-стенд без
    # auth и job-runner'ный первый запуск); DDL на проде — `migrate up` с
    # migration-ролью (compose-сервис schema-migrate, профиль migrate).
    schema_mode: str = "verify"


def load_config(env: Any = None) -> Config:
    source = environ if env is None else env
    cfg = Config(
        service_name=_getenv(source, "SERVICE_NAME", "tracker"),
        mongo_uri=_getenv(source, "MONGO_URI", "mongodb://localhost:27017"),
        mongo_db=_getenv(source, "MONGO_DB", "voice_tracker"),
        nats_url=_getenv(source, "NATS_URL", "nats://localhost:4222"),
        discord_token=_getenv(source, "DISCORD_TOKEN", ""),
        discord_application_id=_clean(source.get("DISCORD_APPLICATION_ID", "")),
        discord_guild_id=_clean(source.get("DISCORD_GUILD_ID", "")),
        event_signing_secret=_clean(source.get("EVENT_SIGNING_SECRET", "")),
        media_dir=_clean(source.get("MEDIA_DIR", "")),
    )
    cfg.bot_admin_user_ids = parse_user_ids(source.get("BOT_ADMIN_USER_IDS", ""))
    # Tracking defaults are canonicalized to all-channel mode at runtime.
    cfg.tracking_mode = "all"
    cfg.tracked_channel_ids = []
    cfg.event_max_age_seconds = _getenv_int(source, "EVENT_MAX_AGE_SECONDS", cfg.event_max_age_seconds)
    cfg.event_sweep_interval_seconds = _getenv_int(
        source, "EVENT_SWEEP_INTERVAL_SECONDS", cfg.event_sweep_interval_seconds
    )
    cfg.event_max_deliver = _getenv_int(source, "EVENT_MAX_DELIVER", cfg.event_max_deliver)
    cfg.event_sweep_gap_seconds = _getenv_int(
        source, "EVENT_SWEEP_GAP_SECONDS", cfg.event_sweep_gap_seconds, allow_zero=True
    )
    cfg.event_sweep_scan_limit = _getenv_int(source, "EVENT_SWEEP_SCAN_LIMIT", cfg.event_sweep_scan_limit)
    cfg.gateway_singleton_max_age_seconds = _getenv_int(
        source, "GATEWAY_SINGLETON_MAX_AGE_SECONDS", cfg.gateway_singleton_max_age_seconds, allow_zero=True
    )
    cfg.media_min_free_bytes = _getenv_int(
        source, "MEDIA_MIN_FREE_BYTES", cfg.media_min_free_bytes, allow_zero=True
    )
    # R26-07 (DB03): ровно два допустимых значения. Опечатка — явная ошибка старта,
    # а не тихая подмена: молчаливый выбор режима мог бы либо включить DDL на проде,
    # либо выключить его на dev-стенде без ведома оператора. В текст ошибки значение
    # не поднимается — в env рядом лежат секреты.
    mode = _clean(source.get("DSBOT_SCHEMA_MODE", "")).lower()
    if mode == "":
        cfg.schema_mode = "verify"
    elif mode in ("verify", "bootstrap"):
        cfg.schema_mode = mode
    else:
        raise ValueError(
            'DSBOT_SCHEMA_MODE must be "verify" (runtime startup: schema check only, no DDL) '
            'or "bootstrap" (dev-stand / first job-runner run: create indexes)'
        )
    if cfg.mongo_uri == "" or cfg.mongo_db == "" or cfg.nats_url == "":
        raise ValueError("missing required configuration")
    return cfg


def _getenv_int(source: Any, key: str, fallback: int, *, allow_zero: bool = False) -> int:
    raw = _clean(source.get(key, ""))
    if raw == "":
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    if value < 0:
        return fallback
    return value if (value > 0 or allow_zero) else fallback


def _getenv(source: Any, key: str, fallback: str) -> str:
    value = _clean(source.get(key, ""))
    return value or fallback


def _getenv_bool(source: Any, key: str, fallback: bool) -> bool:
    raw = _clean(source.get(key, ""))
    if raw == "":
        return fallback
    return raw.lower() in {"1", "true", "yes", "on"}


def require_event_signing_secret(secret: str) -> str:
    value = _clean(secret)
    if value == "" or value.lower() in {"change-me", "changeme"} or len(value) < 16:
        raise SystemExit("EVENT_SIGNING_SECRET must be a long random secret")
    return value


def wait_for_bot_user_id(ready: Any, timeout: float) -> str:
    import asyncio

    async def _wait() -> str:
        try:
            return await asyncio.wait_for(ready.get(), timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("timeout waiting for discord ready event") from exc

    return asyncio.run(_wait())


async def register_commands_http(token: str, app_id: str, guild_id: str, command_payloads: list[dict[str, Any]]) -> None:
    import aiohttp

    guild_id = _clean(guild_id)
    if guild_id:
        url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands"
    else:
        url = f"https://discord.com/api/v10/applications/{app_id}/commands"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.put(url, json=command_payloads) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"discord command registration failed: {response.status} {body}")


# R26-12b: сигналы штатной остановки контейнера (docker stop -> SIGTERM,
# Ctrl+C в dev -> SIGINT).
_SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


async def wait_for_shutdown(work: Awaitable[Any] | None = None) -> Any:
    """Гранд-стоп сервиса: ждёт SIGTERM/SIGINT и отдаёт управление в drain.

    PID 1 в bot-контейнере — python (exec-form CMD), поэтому сигнал из
    `docker stop` приходит напрямую в этот процесс. Свой обработчик обязателен:
    CPython по умолчанию НЕ перехватывает SIGTERM, дефолтная реакция ядра —
    немедленное завершение, и `finally` с supervisor.shutdown()/bus.aclose()
    не исполнялся бы никогда (ровно это и было дефектом R26-12b).

    `work` — бессрочный awaitable сервиса (client.connect()/client.start()):
    если передан, ждём «что раньше» — завершилось ли само `work` (его результат
    или ошибка уходят наружу, как и раньше) или пришёл останавливающий сигнал.
    По сигналу `work` отменяется, чтобы `finally` вызывающего дренировал фоновые
    задачи; возврат по сигналу ошибкой не считается.

    Обработчики снимаются перед возвратом: после возврата disposition сигналов
    снова та, что была до вызова (в контейнере — дефолтная).
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def wake_from_signal(*_args: Any) -> None:
        # Резервный путь (Windows): обработчик сигнала выполняется между
        # байткодами в основном потоке, а цикл может стоять в ожидании ввода —
        # будим его явно через call_soon_threadsafe.
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            pass

    saved: list[tuple[signal.Signals, bool, Any]] = []
    for sig in _SHUTDOWN_SIGNALS:
        previous = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, stop.set)
            via_loop = True
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # Windows (Proactor-цикл) add_signal_handler не реализует —
            # остаётся signal.signal.
            signal.signal(sig, wake_from_signal)
            via_loop = False
        saved.append((sig, via_loop, previous))
    waiter = asyncio.ensure_future(stop.wait())
    task = None if work is None else asyncio.ensure_future(work)
    try:
        if task is None:
            await waiter
            return None
        done, _pending = await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logging.getLogger(__name__).info(
                "service loop ended with error after shutdown signal", exc_info=True
            )
        return None
    finally:
        if not waiter.done():
            waiter.cancel()
        for sig, via_loop, previous in saved:
            if via_loop:
                try:
                    loop.remove_signal_handler(sig)
                except (RuntimeError, ValueError, OSError):
                    pass
            try:
                signal.signal(sig, previous)
            except (TypeError, ValueError, OSError):
                # disposition была выставлена на C-уровне (getsignal -> None) —
                # возвращаем хотя бы дефолт ОС, своё вешать обратно нельзя.
                signal.signal(sig, signal.SIG_DFL)
