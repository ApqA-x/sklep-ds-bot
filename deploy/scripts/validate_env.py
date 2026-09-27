#!/usr/bin/env python3
"""T13: проверка env-файла окружения (pre-flight). Stdlib-only.

Смотрит только ИМЕНА ключей и ФОРМАТ значений (digest/числа) — секреты наружу не
печатаются никогда. staging не должен указывать на прод-тома/прод-базу (P03).

R26-07: mongod поднят с --auth — env-файл обязан нести аутентифицированные URI
(MONGO_BOT_URI/MONGO_WEB_URI/MONGO_ADMIN_URI/MONGO_MIGRATION_URI/
MONGO_BACKUP_URI/MONGO_RESTORE_URI),
пароли пользователей плана migrate.py (DB_USER_ROOT/DB_PASS_ROOT и
DB_USER_<USERNAME в ВЕРХНИЙ РЕГИСТР> — ровно так читает USER_PLAN), образ
bootstrap-job'а (BOOTSTRAP_IMAGE, digest) и DSBOT_SCHEMA_MODE РОВНО "verify".
Review R26-07 (blocker 3): deploy-профиль production/staging не может нести
bootstrap — это mutating-режим runtime'а (ensure_indexes = DDL под app-
креденшеллами), локальная dev-возможность вне deploy-профилей (compose к тому
же якорит verify в x-bot-env, и validate_compose сверяет рендер).
Набор одинаков у production и staging: staging репетирует ровно тот прогон, что
пойдёт на прод (изоляция же — прод-специфичные проверки томов/базы/порта ниже).
Review R26-07 (blocker 1): пользователи плана (app/web/migration/backup/restore)
создаются ensure_users В РАБОЧЕЙ БД, поэтому authSource их URI обязан равняться
MONGO_DB — роль backup/restore, живущая в admin, туда пользователя не переносит, и
authSource=admin даёт Authentication failed на живом mongod (доказано стендом).
MONGO_ADMIN_URI из правила исключён: root создаётся localhost exception в admin.
Review R26-07 (blocker 2): MONGO_MIGRATION_URI — credentials единственный
легальный для compose-сервиса schema-migrate (`migrate up`/`migrate status`);
его отсутствие или безпарольное значение = runner на проде не запускается,
поэтому ключ обязателен и проверяется наравне с runtime-URI. Начальный
localhost-exception bootstrap (mongo-bootstrap, mongodb://127.0.0.1 внутри
сетевого namespace mongod) этого не касается: там пользователь ещё не создан.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import unquote

DIGEST_RE = re.compile(r"^(?:[\w.\-]+/)?[\w.\-/]+@sha256:[0-9a-f]{64}$")
# R26-07: аутентифицированный Mongo-URI = scheme://<userinfo>@… (без credentials
# mongod с --auth клиента не пустит; плейсхолдер REPLACE_ME формат не ломает).
AUTHED_URI_RE = re.compile(r"^mongodb(?:\+srv)?://[^/@]+@[^/]+")

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
    # R26-07: image одноразового mongo-bootstrap (compose `image:` того же профиля)
    "BOOTSTRAP_IMAGE",
]
# R26-07: контракт Mongo --auth. Нужны ОБОИМ профилям: и production, и staging
# compose интерполируют ${MONGO_BOT_URI}/${MONGO_WEB_URI} и сервис mongo-bootstrap
# (${BOOTSTRAP_IMAGE}); staging репетирует ровно тот прогон, что пойдёт на прод.
# Review R26-07 (blocker 2): MONGO_MIGRATION_URI — credentials compose-сервиса
# schema-migrate (`migrate up`/`migrate status`), обязателен там же.
MONGO_URI_KEYS = [
    "MONGO_BOT_URI",
    "MONGO_WEB_URI",
    "MONGO_ADMIN_URI",
    "MONGO_MIGRATION_URI",
    "MONGO_BACKUP_URI",
    "MONGO_RESTORE_URI",
]
# migrate.py читает пароли пользователей плана строго как
# DB_USER_<USERNAME В ВЕРХНИЙ РЕГИСТР> из USER_PLAN (dsbot_app/dsbot_web/
# dsbot_migration/dsbot_backup/dsbot_restore); DB_USER_ROOT/DB_PASS_ROOT —
# имя и пароль root для localhost exception. Имена ниже сверены с USER_PLAN.
MONGO_USER_KEYS = [
    "DB_USER_ROOT",
    "DB_PASS_ROOT",
    "DB_USER_DSBOT_APP",
    "DB_USER_DSBOT_WEB",
    "DB_USER_DSBOT_MIGRATION",
    "DB_USER_DSBOT_BACKUP",
    "DB_USER_DSBOT_RESTORE",
]
# Review R26-07 (blocker 3): для deploy-профилей (production/staging — ровно они
# принимает --mode) допустимо ТОЛЬКО это значение. bootstrap — режим локального
# dev вне deploy-профилей: в runtime он вызывает Repository.ensure_indexes(),
# то есть writes/DDL под runtime app-креденшеллами, чего verify-only контракт
# прода не допускает.
DEPLOY_SCHEMA_MODE = "verify"
# Review R26-07 (blocker 1): эти пользователи создаются ensure_users в рабочей
# БД (client[MONGO_DB]) — authSource их URI обязан быть MONGO_DB. MONGO_ADMIN_URI
# (root, localhost exception) исключён: он аутентифицируется в admin.
# Review R26-07 (blocker 2): dsbot_migration — тоже пользователь рабочей БД
# (USER_PLAN в migrate.py), его URI читает compose-сервис schema-migrate.
WORKDB_AUTH_URI_KEYS = ("MONGO_BOT_URI", "MONGO_WEB_URI", "MONGO_MIGRATION_URI",
                        "MONGO_BACKUP_URI", "MONGO_RESTORE_URI")
AUTHSOURCE_RE = re.compile(r"[?&]authSource=([^&]*)")
# path-база URI без authSource: scheme://<userinfo>@host[:port]/<db>[?…]
URI_PATH_DB_RE = re.compile(r"^mongodb(?:\+srv)?://[^@]*@[^/?]+/([^?]+)")
COMMON_REQUIRED = IMAGE_KEYS + MONGO_URI_KEYS + MONGO_USER_KEYS + [
    "MONGO_VOLUME",
    "MEDIA_VOLUME",
    "DSBOT_UID",
    "DSBOT_GID",
    "DISCORD_TOKEN",
    "DISCORD_APPLICATION_ID",
    "EVENT_SIGNING_SECRET",
    "MONGO_DB",
    # R26-07 (blocker 3): runtime deploy-профиля — строго verify (DDL недоступен);
    # bootstrap отвергается ниже, он живёт только в локальном dev вне профилей.
    "DSBOT_SCHEMA_MODE",
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

    # R26-07: URI обязаны нести credentials (mongod с --auth), значения не
    # печатаются — только имена ключей.
    for key in MONGO_URI_KEYS:
        value = env.get(key, "")
        if value and not AUTHED_URI_RE.match(value):
            errors.append(f"{key}: must be an authenticated mongodb://<user>@<host> URI "
                          "(mongod runs with --auth, R26-07)")

    # Review R26-07 (blocker 1): app/web/migration/backup/restore создаются
    # migrate.py в рабочей БД, поэтому их URI обязан аутентифицироваться против
    # MONGO_DB (authSource=<MONGO_DB> или path-база <MONGO_DB>). Формат URI уже
    # проверен выше; значения в сообщения не попадают.
    workdb = env.get("MONGO_DB", "").strip()
    if workdb:
        for key in WORKDB_AUTH_URI_KEYS:
            value = env.get(key, "").strip()
            if not value or not AUTHED_URI_RE.match(value):
                continue
            m = AUTHSOURCE_RE.search(value)
            if m:
                if unquote(m.group(1)) != workdb:
                    errors.append(f"{key}: authSource must equal MONGO_DB — plan users are "
                                  "created in the working database (a role living in admin "
                                  "does not move the user there; authSource=admin fails "
                                  "authentication on live mongod)")
                continue
            pm = URI_PATH_DB_RE.match(value)
            if pm is None:
                errors.append(f"{key}: must authenticate against MONGO_DB — add "
                              "?authSource=<MONGO_DB> (plan users are not in admin)")
            elif unquote(pm.group(1)) != workdb:
                errors.append(f"{key}: URI path database must equal MONGO_DB "
                              "(or use ?authSource=<MONGO_DB>)")
    # Review R26-07 (blocker 3): ровно "verify" (без приведения регистра — value
    # сверяется как есть после strip; пустое значение уже поймано required-проверкой).
    # bootstrap и всё остальное — ошибка; значение в сообщение не печатается.
    schema_mode_value = env.get("DSBOT_SCHEMA_MODE", "").strip()
    if schema_mode_value and schema_mode_value != DEPLOY_SCHEMA_MODE:
        errors.append('DSBOT_SCHEMA_MODE must be exactly "verify" in production/staging '
                      'deploy profiles — runtime startup is verify-only (R26-07); '
                      '"bootstrap" runs DDL (ensure_indexes) under runtime credentials '
                      "and is a local-dev mode outside deploy profiles")

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
        # R26-07: ровно тот же P03-запрет внутри URI — staging не должен
        # аутентифицироваться в прод-базу (authSource=voice_tracker exact).
        for key in MONGO_URI_KEYS:
            v = env.get(key, "")
            if re.search(rf"authSource={re.escape(PROTECTED_DB)}(?:&|$)", v):
                errors.append(f"staging {key} authenticates against the PRODUCTION "
                              "database (authSource)")
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
