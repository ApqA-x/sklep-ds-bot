#!/usr/bin/env python3
"""T13: проверка env-файла окружения (pre-flight). Stdlib-only.

Смотрит только ИМЕНА ключей и ФОРМАТ значений (digest/числа) — секреты наружу не
печатаются никогда. staging не должен указывать на прод-тома/прод-базу (P03).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

DIGEST_RE = re.compile(r"^(?:[\w.\-]+/)?[\w.\-/]+@sha256:[0-9a-f]{64}$")

# R26-06 (п.4): точные отпечатки прод-идентичностей вместо подстроки "-prod".
# Исторический прод-volume называется ровно "dsbot-media" — подстроку "-prod"
# он не содержит и раньше проходил staging-гард. Теперь: exact-совпадение +
# allowlist-префикс для staging-томов.
PROTECTED_VOLUMES = {"dsbot-media", "dsbot-prod-mongo-data"}
PROTECTED_DB = "voice_tracker"
PROTECTED_PROJECT = "dsbot-prod"
STAGING_VOLUME_PREFIX = "dsbot-staging-"
ALLOWED_STAGING_PROJECTS = {"dsbot-staging"}

IMAGE_KEYS = [
    "MONGO_IMAGE",
    "NATS_IMAGE",
    "BOT_GATEWAY_IMAGE",
    "BOT_TRACKER_IMAGE",
    "BOT_WRITER_IMAGE",
    "BOT_COMMANDS_IMAGE",
    "BOT_ACTIVITY_IMAGE",
    "BOT_STALKER_IMAGE",
    "BOT_CONTROLPLANE_IMAGE",
    "WEB_IMAGE",
]
COMMON_REQUIRED = IMAGE_KEYS + [
    "MONGO_VOLUME",
    "MEDIA_VOLUME",
    "DSBOT_UID",
    "DSBOT_GID",
    "DISCORD_TOKEN",
    "DISCORD_APPLICATION_ID",
    "EVENT_SIGNING_SECRET",
    "MONGO_DB",
    # web (production-гарды T02 проверяет само приложение; здесь — наличие ключей)
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "WEB_SESSION_SECRET",
    "WEB_GUILD_ALLOWLIST",
]


def parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check(path: str, mode: str) -> list[str]:
    errors: list[str] = []
    if not os.path.isfile(path):
        return [f"env file not found: {path}"]
    env = parse_env_file(path)

    for key in COMMON_REQUIRED:
        if not env.get(key):
            errors.append(f"missing required key: {key}")
    if env.get("WEB_DEV_BYPASS_AUTH"):
        errors.append("WEB_DEV_BYPASS_AUTH must be absent/empty in production/staging")
    if "DSBOT_ENV_FILE" in env:
        # V26-16: интерполяция env_file берёт DSBOT_ENV_FILE из процесса (его
        # подставляют deploy-скрипты). Ключ внутри env-файла = второй источник
        # пути, который молча переопределяет выбор оператора.
        errors.append("DSBOT_ENV_FILE must not be defined inside the env file "
                      "(it is set by deploy scripts from the environment)")
    for key in IMAGE_KEYS:
        value = env.get(key, "")
        if value and not DIGEST_RE.match(value):
            errors.append(f"{key}: must be registry/name@sha256:<64 hex> (no tags, no latest)")

    def is_int(v: str) -> bool:
        return v.isdigit()

    if not is_int(env.get("DSBOT_UID", "")) or not is_int(env.get("DSBOT_GID", "")):
        errors.append("DSBOT_UID/DSBOT_GID must be numeric uid/gid")

    if mode == "production":
        if env.get("MONGO_DB") and env["MONGO_DB"] == "voice_tracker_staging":
            errors.append("production MONGO_DB looks like staging")
        for key in ("MONGO_VOLUME", "MEDIA_VOLUME"):
            v = env.get(key, "")
            if v and v.startswith(STAGING_VOLUME_PREFIX):
                errors.append(f"production {key} looks like a staging volume: {v!r}")
    if mode == "staging":
        if env.get("MONGO_DB") == PROTECTED_DB:
            errors.append("staging MONGO_DB is the PRODUCTION database name")
        # R26-06 (п.4): exact fingerprints вместо подстроки "-prod" — исторический
        # прод-volume "dsbot-media" не содержит "-prod" и раньше проходил гард.
        for key in ("MONGO_VOLUME", "MEDIA_VOLUME"):
            v = env.get(key, "")
            if not v:
                continue
            if v in PROTECTED_VOLUMES or v == PROTECTED_PROJECT:
                errors.append(f"staging {key} references a prod volume (protected "
                              f"identity, exact match): {v!r}")
            elif not v.startswith(STAGING_VOLUME_PREFIX):
                errors.append(f"staging {key} must start with {STAGING_VOLUME_PREFIX!r} "
                              f"(allowlist form of isolation): {v!r}")
        project = env.get("DSBOT_PROJECT", "")
        if project and project not in ALLOWED_STAGING_PROJECTS:
            errors.append(f"staging DSBOT_PROJECT {project!r} is not in the allowlist "
                          f"{sorted(ALLOWED_STAGING_PROJECTS)}")
        port = env.get("WEB_HOST_PORT", "")
        if port == "":
            errors.append("staging WEB_HOST_PORT must be set explicitly (different from prod 8000)")
        elif port == "8000":
            errors.append("staging WEB_HOST_PORT collides with prod ingress 8000")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["production", "staging"], required=True)
    parser.add_argument("env_file")
    args = parser.parse_args(argv)
    errors = check(args.env_file, args.mode)
    if errors:
        for err in errors:
            print(f"ENV-FILE PROBLEM: {err}", file=sys.stderr)
        return 1
    print(f"validate_env OK (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
