from __future__ import annotations

from dataclasses import dataclass, field
import logging
from os import environ
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
    invite_rollout_guild_ids: list[str] = field(default_factory=list)
    event_signing_secret: str = ""
    tracking_mode: str = "all"
    tracked_channel_ids: list[str] = field(default_factory=list)
    invite_snapshot_sync_enabled: bool = False
    invite_live_attribution_enabled: bool = False
    userinfo_invite_reads_enabled: bool = False
    invite_reconciliation_enabled: bool = False


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
    )
    cfg.bot_admin_user_ids = parse_user_ids(source.get("BOT_ADMIN_USER_IDS", ""))
    cfg.invite_rollout_guild_ids = parse_user_ids(source.get("INVITE_ROLLOUT_GUILD_IDS", ""))
    # Tracking defaults are canonicalized to all-channel mode at runtime.
    cfg.tracking_mode = "all"
    cfg.tracked_channel_ids = []
    cfg.invite_snapshot_sync_enabled = _getenv_bool(source, "INVITE_SNAPSHOT_SYNC_ENABLED", False)
    cfg.invite_live_attribution_enabled = _getenv_bool(source, "INVITE_LIVE_ATTRIBUTION_ENABLED", False)
    cfg.userinfo_invite_reads_enabled = _getenv_bool(source, "USERINFO_INVITE_READS_ENABLED", False)
    cfg.invite_reconciliation_enabled = _getenv_bool(source, "INVITE_RECONCILIATION_ENABLED", False)
    if cfg.mongo_uri == "" or cfg.mongo_db == "" or cfg.nats_url == "":
        raise ValueError("missing required configuration")
    return cfg


def _getenv(source: Any, key: str, fallback: str) -> str:
    value = _clean(source.get(key, ""))
    return value or fallback


def _getenv_bool(source: Any, key: str, fallback: bool) -> bool:
    raw = _clean(source.get(key, ""))
    if raw == "":
        return fallback
    return raw.lower() in {"1", "true", "yes", "on"}


def invite_rollout_enabled(cfg: Config, guild_id: str) -> bool:
    guild_id = _clean(guild_id)
    if guild_id == "":
        return False
    rollout_guild_ids = list(getattr(cfg, "invite_rollout_guild_ids", []) or [])
    if len(rollout_guild_ids) == 0:
        return True
    return guild_id in rollout_guild_ids


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

    url = f"https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.put(url, json=command_payloads) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"discord command registration failed: {response.status} {body}")
