"""T13: инварианты deploy-артефактов (P01/P02/P03/P07/P08 статикой; P04-P06 — live).

validate_compose/validate_env — stdlib; вызываем их функции напрямую.
Отдельный integration-тест рендерит реальный compose через `docker compose config`
(docker CLI на хосте) и прогоняет по отрендеренному — это и есть проверка P01 на
настоящем рендере, без build/pull/up.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
import sys

sys.path.insert(0, str(DEPLOY / "scripts"))
import validate_compose  # noqa: E402
import validate_env  # noqa: E402

HEX64 = "a" * 64


def _svc(image=f"x@sha256:{HEX64}", **over):
    base = {
        "image": image,
        "restart": "unless-stopped",
        "networks": ["dsbot-data"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}},
        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "1G"}}},
    }
    base.update(over)
    return base


# R26-06: app-сервисы читают единый env-файл (V26-16), web — только свой
# интерполированный environment; egress-сеть — V26-17.
ENV_PATH = "/deploy/env"


def _bot(name: str, envfile: str = ENV_PATH, **over) -> dict:
    # compose v5 в рендере схлопывает env_file в environment; фикстура,
    # указывающая на существующий файл, обязана нести его ключи в environment,
    # иначе она не описывает валидный v5-рендер (слияние-проверка V26-16).
    # ENV_PATH по умолчанию несуществующий → фикстура остаётся «старой формой».
    env = dict(validate_compose._parse_env_values(envfile) or {})
    env.update(over.pop("environment", None) or {})
    base: dict = {"env_file": [{"path": envfile, "service": name}]}
    if env:
        base["environment"] = env
    base.update(over)
    return _svc(**base)


def _good_cfg(mode: str, envfile: str = ENV_PATH) -> dict:
    egress = ["dsbot-data", "dsbot-egress"]
    # R26-07: в ОТРЕНДЕРЕННОМ конфиге ${MONGO_BOT_URI}/${MONGO_WEB_URI}/
    # ${MONGO_MIGRATION_URI} схлопнуты в значения env-файла; фикстура обязана
    # нести ровно их (при недоступном файле — синтетические аутентифицированные
    # URI той же формы).
    env_vals = validate_compose._parse_env_values(envfile) or {}
    db = env_vals.get("MONGO_DB") or (
        "voice_tracker_staging" if mode == "staging" else "voice_tracker")
    bot_uri = env_vals.get("MONGO_BOT_URI") or (
        "mongodb://dsbot_app:pw-app@mongo:27017/?authSource=voice_tracker"
    )
    web_uri = env_vals.get("MONGO_WEB_URI") or (
        "mongodb://dsbot_web:pw-web@mongo:27017/?authSource=voice_tracker"
    )
    # review R26-07 (blocker 2): credentials одноразового runner-сервиса
    mig_uri = env_vals.get("MONGO_MIGRATION_URI") or (
        f"mongodb://dsbot_migration:pw-mig@mongo:27017/?authSource={db}"
    )
    services = {
        "mongo": _svc(command=["--auth"]),
        # R26-07: одноразовый bootstrap-job: restart "no", профиль bootstrap,
        # localhost exception — только с 127.0.0.1 внутри сетевого namespace mongo.
        "mongo-bootstrap": _svc(
            restart="no",
            profiles=["bootstrap"],
            network_mode="service:mongo",
            networks=[],
            environment={"MONGO_URI": "mongodb://127.0.0.1:27017"},
        ),
        # review R26-07 (blocker 2): одноразовый schema-раннер: профиль migrate,
        # restart "no", dsbot-data (хост mongo), ровно MONGO_URI+MONGO_DB без env_file.
        "schema-migrate": _svc(
            restart="no",
            profiles=["migrate"],
            networks=["dsbot-data"],
            environment={"MONGO_URI": mig_uri, "MONGO_DB": db},
        ),
        "nats": _svc(),
        "gateway": _bot("gateway", envfile, networks=egress,
                        volumes=[{"type": "volume", "source": "media", "target": "/data/media"}]),
        "tracker": _bot("tracker", envfile),
        "writer": _bot("writer", envfile),
        "commands": _bot("commands", envfile, networks=egress),
        "activity": _bot("activity", envfile, networks=egress),
        "stalker": _bot("stalker", envfile, networks=egress),
        "controlplane": _bot("controlplane", envfile, profiles=["controlplane"]),
        "web": _svc(
            networks=egress,
            ports=[{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8000, "published": "8000"}],
            volumes=[{"type": "volume", "source": "media", "target": "/data/media", "read_only": True}],
            environment={"WEB_ENV": "production"},
        ),
    }
    for name in validate_compose.BOT_ENVFILE_SERVICES:
        bot_env = services[name].setdefault("environment", {})
        bot_env["MONGO_URI"] = bot_uri
        # review R26-07 (blocker 3): x-bot-env якорит DSBOT_SCHEMA_MODE: verify и в
        # старой форме рендера, и в схлопнутой v5 (environment побеждает env_file) —
        # фикстура обязана моделировать ровно это.
        bot_env["DSBOT_SCHEMA_MODE"] = validate_compose.RUNTIME_SCHEMA_MODE
    services["web"]["environment"]["MONGO_URI"] = web_uri
    return {
        "name": validate_compose.EXPECTED_PROJECT[mode],
        "services": services,
        "networks": {
            "dsbot-data": {"name": "x", "internal": True},
            "dsbot-egress": {"name": "y", "internal": False},
        },
        "volumes": {"mongo-data": {"name": "v1"}, "media": {"name": "v2"}},
    }


# ------------------------------------------------------------- validate_compose


def test_rendered_good_config_passes_both_modes() -> None:
    assert validate_compose.check(_good_cfg("production"), "production") == []
    assert validate_compose.check(_good_cfg("staging"), "staging") == []


def test_build_and_tag_refs_and_bind_mounts_rejected() -> None:
    cfg = _good_cfg("production")
    cfg["services"]["gateway"]["build"] = {"context": "."}
    cfg["services"]["tracker"]["image"] = "ghcr.io/apqa-x/sklep-ds-bot/tracker:v0.21.1"
    cfg["services"]["activity"]["volumes"] = [{"type": "bind", "source": "../dsbot", "target": "/app"}]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "build:" in errors
    assert "immutable digest" in errors
    assert "bind mount" in errors


def test_port_leaks_and_exposure_rejected() -> None:
    cfg = _good_cfg("production")
    cfg["services"]["mongo"]["ports"] = [{"host_ip": "0.0.0.0", "target": 27017, "published": "27017"}]
    cfg["services"]["web"]["ports"] = [{"host_ip": "0.0.0.0", "target": 8000, "published": "8000"}]
    cfg["networks"]["dsbot-data"]["internal"] = False
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "publishes ports" in errors  # mongo
    assert "non-loopback" in errors  # web наружу
    assert "not internal" in errors


def test_controlplane_without_profile_and_legacy_services_rejected() -> None:
    cfg = _good_cfg("production")
    cfg["services"]["controlplane"].pop("profiles")
    cfg["services"]["dashboard-bff"] = _svc()
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "ADR-0004" in errors
    assert "whitelist" in errors


def test_missing_restart_limits_rotation_rejected() -> None:
    cfg = _good_cfg("production")
    cfg["services"]["writer"]["restart"] = "no"
    cfg["services"]["tracker"]["deploy"] = {}
    cfg["services"]["nats"]["logging"] = {"driver": "json-file", "options": {}}
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "restart policy" in errors
    assert "resource limits" in errors
    assert "rotation" in errors


def test_host_mongo_uri_and_prod_db_in_staging_rejected() -> None:
    cfg = _good_cfg("staging")
    cfg["services"]["gateway"]["environment"] = {
        "MONGO_URI": "mongodb://host.docker.internal:27017",
        "MONGO_DB": "voice_tracker",
    }
    errors = "\n".join(validate_compose.check(cfg, "staging"))
    assert "host.docker.internal" in errors
    assert "production DB" in errors


# ------------------------------------------------------------------ validate_env


def _write_env(tmp: Path, mode: str, name: str | None = None, **over) -> Path:
    db = "voice_tracker" if mode == "production" else "voice_tracker_staging"
    env = {
        "MONGO_IMAGE": f"mongo@sha256:{HEX64}",
        "NATS_IMAGE": f"nats@sha256:{HEX64}",
        "BOT_GATEWAY_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/gateway@sha256:{HEX64}",
        "BOT_TRACKER_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/tracker@sha256:{HEX64}",
        "BOT_WRITER_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/writer@sha256:{HEX64}",
        "BOT_COMMANDS_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/commands@sha256:{HEX64}",
        "BOT_ACTIVITY_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/activity@sha256:{HEX64}",
        "BOT_STALKER_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/stalker@sha256:{HEX64}",
        "BOT_CONTROLPLANE_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/controlplane@sha256:{HEX64}",
        "WEB_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot-web@sha256:{HEX64}",
        # R26-07: mongod под --auth — аутентифицированные URI (синтетические креды),
        # пароли пользователей плана migrate.py (DB_USER_<USERNAME.upper()>) и
        # image bootstrap-job'а.
        "BOOTSTRAP_IMAGE": f"ghcr.io/apqa-x/sklep-ds-bot/gateway@sha256:{HEX64}",
        "MONGO_BOT_URI": f"mongodb://dsbot_app:pw-app@mongo:27017/?authSource={db}",
        "MONGO_WEB_URI": f"mongodb://dsbot_web:pw-web@mongo:27017/?authSource={db}",
        "MONGO_ADMIN_URI": "mongodb://dsbot_root:pw-root@mongo:27017/admin?authSource=admin",
        # review R26-07 (blocker 2): MONGO_MIGRATION_URI — credentials compose-
        # сервиса schema-migrate (`migrate up`/`status`), пользователь рабочей БД
        "MONGO_MIGRATION_URI": f"mongodb://dsbot_migration:pw-mig@mongo:27017/?authSource={db}",
        # review R26-07 (blocker 1): backup/restore-пользователи созданы в рабочей
        # БД — authSource обязан быть MONGO_DB, не admin (сверяет validate_env)
        "MONGO_BACKUP_URI": f"mongodb://dsbot_backup:pw-bkp@mongo:27017/?authSource={db}",
        "MONGO_RESTORE_URI": f"mongodb://dsbot_restore:pw-rst@mongo:27017/?authSource={db}",
        "DB_USER_ROOT": "dsbot_root",
        "DB_PASS_ROOT": "pw-root",
        "DB_USER_DSBOT_APP": "pw-app",
        "DB_USER_DSBOT_WEB": "pw-web",
        "DB_USER_DSBOT_MIGRATION": "pw-mig",
        "DB_USER_DSBOT_BACKUP": "pw-bkp",
        "DB_USER_DSBOT_RESTORE": "pw-rst",
        "DSBOT_SCHEMA_MODE": "verify",
        "MONGO_VOLUME": f"dsbot-{mode}-mongo-data",
        "MEDIA_VOLUME": f"dsbot-{mode}-media",
        "DSBOT_UID": "10001",
        "DSBOT_GID": "10001",
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "a",
        "EVENT_SIGNING_SECRET": "s" * 32,
        "MONGO_DB": db,
        "DISCORD_CLIENT_ID": "c",
        "DISCORD_CLIENT_SECRET": "cs",
        "WEB_SESSION_SECRET": "x" * 40,
        "WEB_GUILD_ALLOWLIST": "1",
        "WEB_HOST_PORT": "8000" if mode == "production" else "8090",
    }
    env.update(over)
    path = tmp / (name or f".env.{mode}")
    path.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    return path


def test_env_examples_are_self_consistent(tmp_path) -> None:
    # раскомментированные ключи примеров проходят валидатор формы (значения-плейсхолдеры
    # секретов пустые — требуем только формат образов/томов/uid)
    for example, mode in (
        (DEPLOY / "production" / "env.example", "production"),
        (DEPLOY / "staging" / "env.staging.example", "staging"),
    ):
        text = example.read_text(encoding="utf-8")
        keys = {ln.split("=", 1)[0] for ln in text.splitlines() if "=" in ln and not ln.strip().startswith("#")}
        required = set(validate_env.COMMON_REQUIRED)
        assert required <= keys, (mode, sorted(required - keys))


def test_validate_env_passes_good_and_blocks_staging_on_prod_identity(tmp_path) -> None:
    good = _write_env(tmp_path, "staging")
    assert validate_env.check(str(good), "staging") == []
    bad = _write_env(tmp_path, "staging", MONGO_DB="voice_tracker")
    errors = "\n".join(validate_env.check(str(bad), "staging"))
    assert "PRODUCTION database name" in errors
    badvol = _write_env(tmp_path, "staging", MONGO_VOLUME="dsbot-prod-mongo-data", WEB_HOST_PORT="8000")
    errors = "\n".join(validate_env.check(str(badvol), "staging"))
    assert "prod volume" in errors and "8000" in errors


def test_validate_env_requires_digest_pinned_images(tmp_path) -> None:
    env = _write_env(tmp_path, "production", WEB_IMAGE="ghcr.io/apqa-x/sklep-ds-bot-web:0.2.0")
    errors = "\n".join(validate_env.check(str(env), "production"))
    assert "WEB_IMAGE" in errors and "sha256" in errors


def test_validate_env_rejects_bypass_auth(tmp_path) -> None:
    env = _write_env(tmp_path, "production", WEB_DEV_BYPASS_AUTH="1")
    assert any("WEB_DEV_BYPASS_AUTH" in e for e in validate_env.check(str(env), "production"))


def test_archive_env_requires_separate_old_bot_and_active_guild(tmp_path) -> None:
    valid = dict(
        WEB_GUILD_ALLOWLIST="111111111111111111,222222222222222222",
        WEB_ARCHIVE_GUILD_ALLOWLIST="222222222222222222",
        WEB_ARCHIVE_DISCORD_TOKEN="old-bot-token",
        WEB_ARCHIVE_DISCORD_APPLICATION_ID="333333333333333333",
    )
    assert validate_env.check(str(_write_env(tmp_path, "production", **valid)), "production") == []
    invalid = dict(valid, WEB_GUILD_ALLOWLIST="222222222222222222", WEB_ARCHIVE_DISCORD_TOKEN="t")
    errors = "\n".join(validate_env.check(str(_write_env(tmp_path, "production", **invalid)), "production"))
    assert "active guild" in errors
    assert "must differ" in errors


# ------------------------------------------------- R26-06: единый env (V26-16)


def test_v2616_rendered_bot_env_file_must_be_the_selected_file(tmp_path) -> None:
    """V26-16: контейнеры читают ровно выбранный env-файл, а не `.env` рядом с
    compose-YAML. В рендере env_file app-сервисов обязан резолвиться в него."""
    selected = str(tmp_path / "chosen.env")
    cfg = _good_cfg("production", envfile=selected)
    assert validate_compose.check(cfg, "production", env_file=selected) == []
    # другой файл (маркерный сценарий «два env-файла») — ошибка по каждому боту
    other = str(tmp_path / "other.env")
    errors = "\n".join(validate_compose.check(cfg, "production", env_file=other))
    assert "single source of env" in errors
    # относительный дефолт `.env` — именно то, что docker резолвил от каталога YAML
    rel = _good_cfg("production", envfile=".env")
    errors = "\n".join(validate_compose.check(rel, "production", env_file=selected))
    assert "is relative" in errors
    # отсутствие env_file у бота — тоже split-source
    cfg2 = _good_cfg("production")
    cfg2["services"]["tracker"].pop("env_file")
    errors = "\n".join(validate_compose.check(cfg2, "production", env_file=ENV_PATH))
    assert "[tracker] has no env_file" in errors


def test_v2616_compose_v5_render_merge_of_selected_env_into_bot_environment(tmp_path) -> None:
    """Факт живого прогона: docker compose v5 при `config --format json` НЕ
    отдаёт ключ env_file, а целиком схлопывает выбранный файл в environment.
    В этой форме V26-16 проверяет факт вливания: каждый ключ выбранного файла
    присутствует в environment бота со значением из файла; расхождение/пропажа
    ключа — ошибка, и её сообщение не раскрывает значений."""
    selected = _write_env(tmp_path, "staging")
    values = validate_compose._parse_env_values(str(selected))
    assert values

    def v5_render() -> dict:
        cfg = _good_cfg("staging", envfile=str(selected))
        for name in validate_compose.BOT_ENVFILE_SERVICES:
            cfg["services"][name].pop("env_file")  # v5-рендер: ключа нет ни у кого
        return cfg

    assert validate_compose.check(v5_render(), "staging", env_file=str(selected)) == []

    cfg = v5_render()
    cfg["services"]["tracker"]["environment"]["EVENT_SIGNING_SECRET"] = "из-другого-файла"
    cfg["services"]["writer"]["environment"].pop("DISCORD_TOKEN")
    errors = "\n".join(validate_compose.check(cfg, "staging", env_file=str(selected)))
    assert "[tracker] container env is not sourced from the selected env file" in errors
    assert "[writer] container env is not sourced from the selected env file" in errors
    assert "SECRET" in errors or "DISCORD_TOKEN" in errors  # имена ключей — можно
    assert "из-другого-файла" not in errors  # значения — нельзя

    # web слияние-проверке не подлежит (env_file не имеет по topology, п.5)
    cfg3 = v5_render()
    cfg3["services"]["web"]["environment"] = {"WEB_ENV": "production"}
    assert validate_compose.check(cfg3, "staging", env_file=str(selected)) == []


def test_v2616_env_file_keys_are_not_overridden_by_bot_environment() -> None:
    """Инвариант, на котором держится слияние-проверка V26-16 в v5-рендерe:
    ни один ключ env-файла НЕ перекрывается явным environment бота (иначе в
    схлопнутом рендере значение из файла честно отличалось бы). Сервис-специфичные
    SERVICE_NAME/MONGO_URI/NATS_URL/MEDIA_DIR обязаны отсутствовать в env-файлах.
    Review R26-07 (blocker 3): единственное разрешённое перекрытие —
    DSBOT_SCHEMA_MODE: verify из x-bot-env (runtime verify-only важнее единого
    источника env: env_file не имеет права включать ботам DDL-режим)."""
    bot_services = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")
    for compose_path, example in (
        (DEPLOY / "production" / "compose.yml", DEPLOY / "production" / "env.example"),
        (DEPLOY / "staging" / "compose.staging.yml", DEPLOY / "staging" / "env.staging.example"),
    ):
        doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        env_keys = validate_compose._parse_env_values(str(example))
        assert env_keys, example
        for key in ("SERVICE_NAME", "MONGO_URI", "NATS_URL", "MEDIA_DIR", "WEB_ENV"):
            assert key not in env_keys, (example.name, key)
        for name in bot_services:
            explicit = doc["services"][name].get("environment") or {}
            clash = sorted(set(explicit) & set(env_keys) - {"DSBOT_SCHEMA_MODE"})
            assert not clash, (compose_path.name, name, clash)
            # перекрытие единственное и ровно фиксированное (не интерполяция из env)
            assert explicit.get("DSBOT_SCHEMA_MODE") == validate_compose.RUNTIME_SCHEMA_MODE, (
                compose_path.name, name)


def test_v2616_web_has_no_env_file_and_no_backend_keys() -> None:
    """R26-06 п.5: web — без общего env_file, в environment ровно его ключи."""
    cfg = _good_cfg("production")
    cfg["services"]["web"]["env_file"] = [{"path": ENV_PATH, "service": "web"}]
    errors = "\n".join(validate_compose.check(cfg, "production", env_file=ENV_PATH))
    assert "[web] has env_file" in errors
    cfg2 = _good_cfg("production")
    cfg2["services"]["web"]["environment"] = {
        "WEB_ENV": "production",
        "BACKUP_AGE_KEY_FILE": "/keys/age.txt",
        "BACKUP_DIR": "/backup",
        "EVENT_SIGNING_SECRET": "nope",
        "MIGRATION_DSN": "mongodb://x",
        # review R26-07 (blocker 2): credentials runner'а — тоже backend-only
        "MONGO_MIGRATION_URI": "mongodb://dsbot_migration:pw@mongo:27017/?authSource=voice_tracker",
    }
    errors = "\n".join(validate_compose.check(cfg2, "production"))
    assert "backend-only keys" in errors
    assert "MONGO_MIGRATION_URI" in errors


def test_v2616_yaml_both_composes_use_single_env_placeholder() -> None:
    """Форма YAML (без docker): env_file у app-сервисов — ровно плейсхолдер
    ${DSBOT_ENV_FILE...}; нигде нет относительного `env_file: .env`; web вообще
    без env_file и получает дискретный набор ключей из wt-web api/config.py."""
    bot_services = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")
    for path in (DEPLOY / "production" / "compose.yml", DEPLOY / "staging" / "compose.staging.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = doc["services"]
        for name in bot_services:
            ef = services[name].get("env_file")
            assert isinstance(ef, list) and len(ef) == 1, (path.name, name, ef)
            assert str(ef[0]).startswith("${DSBOT_ENV_FILE"), (path.name, name, ef)
            assert "DSBOT_ENV_FILE:?" in str(ef[0]), (path.name, ef)
        assert "env_file" not in services["web"], path.name
        assert "env_file: .env" not in path.read_text(encoding="utf-8"), path.name
        web_env = services["web"]["environment"]
        # фактический набор wt-web/api/config.py (grep getenv/environ): включая
        # MONGO_DB и DISCORD_TOKEN (production-гард), без backend-only ключей
        for key in ("MONGO_URI", "MONGO_DB", "MEDIA_DIR", "WEB_ENV", "DISCORD_TOKEN",
                    "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "WEB_SESSION_SECRET",
                    "WEB_GUILD_ALLOWLIST", "WEB_PUBLIC_URL", "DISCORD_REDIRECT_URI"):
            assert key in web_env, (path.name, key)
        for key in ("BACKUP_DIR", "BACKUP_AGE_KEY_FILE", "EVENT_SIGNING_SECRET"):
            assert key not in web_env, (path.name, key)


def test_v2616_validate_env_rejects_dsbots_env_file_inside_env_file(tmp_path) -> None:
    """DSBOT_ENV_FILE внутри env-файла = второй источник пути, переопределяющий
    выбор оператора (он и так приходит из процесса)."""
    env = _write_env(tmp_path, "staging", DSBOT_ENV_FILE="/somewhere/else.env")
    errors = "\n".join(validate_env.check(str(env), "staging"))
    assert "DSBOT_ENV_FILE" in errors


# ------------------------------------------ R26-06: egress-сеть (V26-17)


def test_v2617_egress_network_and_membership_required() -> None:
    cfg = _good_cfg("production")
    cfg["networks"].pop("dsbot-egress")
    cfg["services"]["gateway"]["networks"] = ["dsbot-data"]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "dsbot-egress missing" in errors
    assert "[gateway] missing network dsbot-egress" in errors

    cfg = _good_cfg("production")
    cfg["networks"]["dsbot-egress"]["internal"] = True
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "is internal" in errors

    cfg = _good_cfg("production")
    cfg["services"]["mongo"]["networks"] = ["dsbot-data", "dsbot-egress"]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[mongo] must not be on dsbot-egress" in errors

    cfg = _good_cfg("production")
    cfg["services"]["web"]["networks"] = ["dsbot-data"]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[web] missing network dsbot-egress" in errors


def test_v2617_yaml_egress_topology() -> None:
    bot_egress = ("gateway", "commands", "activity", "stalker")
    data_only = ("mongo", "nats", "tracker", "writer", "controlplane", "schema-migrate")
    for path in (DEPLOY / "production" / "compose.yml", DEPLOY / "staging" / "compose.staging.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        nets = doc["networks"]
        assert "dsbot-egress" in nets, path.name
        assert nets["dsbot-egress"]["internal"] is False, path.name
        assert nets["dsbot-data"]["internal"] is True, path.name
        for name in (*bot_egress, "web"):
            assert sorted(doc["services"][name]["networks"]) == ["dsbot-data", "dsbot-egress"], (path.name, name)
        for name in data_only:
            # safe_load раскрывает merge-ключ <<: *app-defaults — сети видны явно
            svc_nets = doc["services"][name].get("networks") or ["dsbot-data"]
            assert "dsbot-egress" not in svc_nets, (path.name, name, svc_nets)
        assert "ports" not in doc["services"]["mongo"] and "ports" not in doc["services"]["nats"]


# ------------------------------- R26-06: staging isolation, exact fingerprints


def test_staging_rejects_exact_prod_volume_fingerprints_not_substrings(tmp_path) -> None:
    """Регрессия (п.4): исторический прод-media-volume называется РОВНО
    `dsbot-media` — подстроки "-prod" в нём нет, старый гард его пропускал."""
    sneaky = _write_env(tmp_path, "staging", MEDIA_VOLUME="dsbot-media")
    errors = "\n".join(validate_env.check(str(sneaky), "staging"))
    assert "MEDIA_VOLUME" in errors and "prod volume" in errors
    sneaky2 = _write_env(tmp_path, "staging", MONGO_VOLUME="dsbot-media")
    errors = "\n".join(validate_env.check(str(sneaky2), "staging"))
    assert "MONGO_VOLUME" in errors and "prod volume" in errors
    # allowlist-форма: том, не похожий ни на прод, ни на staging, тоже отвергается
    random = _write_env(tmp_path, "staging", MEDIA_VOLUME="just-a-volume")
    errors = "\n".join(validate_env.check(str(random), "staging"))
    assert "must start with" in errors and "dsbot-staging-" in errors
    # корректный staging проходит (в т.ч. с явным разрешённым DSBOT_PROJECT)
    good = _write_env(tmp_path, "staging", DSBOT_PROJECT="dsbot-staging")
    assert validate_env.check(str(good), "staging") == []


def test_staging_rejects_prod_project_identity(tmp_path) -> None:
    env = _write_env(tmp_path, "staging", DSBOT_PROJECT="dsbot-prod")
    errors = "\n".join(validate_env.check(str(env), "staging"))
    assert "DSBOT_PROJECT" in errors and "allowlist" in errors


def test_production_rejects_staging_named_volumes(tmp_path) -> None:
    env = _write_env(tmp_path, "production", MEDIA_VOLUME="dsbot-staging-media")
    errors = "\n".join(validate_env.check(str(env), "production"))
    assert "looks like a staging volume" in errors


def test_v2617_rendered_project_name_is_pinned() -> None:
    cfg = _good_cfg("staging")
    cfg["name"] = "dsbot-prod"
    errors = "\n".join(validate_compose.check(cfg, "staging"))
    assert "identity isolation" in errors and "dsbot-staging" in errors
    cfg = _good_cfg("production")
    cfg.pop("name")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "identity isolation" in errors


# ------------------------------- R26-07: Mongo --auth, env-URI, mongo-bootstrap


def test_r2607_mongo_without_auth_command_rejected() -> None:
    cfg = _good_cfg("production")
    cfg["services"]["mongo"].pop("command")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "must contain --auth" in errors


def test_r2607_unauthenticated_mongo_uri_in_environment_rejected() -> None:
    """Ни один service.environment не обязан был пережить безпарольный
    mongodb://mongo[:порт] после включения --auth."""
    cfg = _good_cfg("production")
    cfg["services"]["tracker"]["environment"]["MONGO_URI"] = "mongodb://mongo:27017"
    cfg["services"]["web"]["environment"]["MONGO_URI"] = "mongodb://mongo"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[tracker] MONGO_URI is an unauthenticated mongodb:// URI" in errors
    assert "[web] MONGO_URI is an unauthenticated mongodb:// URI" in errors


def test_r2607_bootstrap_service_contract_rejected_variants() -> None:
    # отсутствие (когда прочие profile-сервисы в рендере видны) — падение
    cfg = _good_cfg("production")
    cfg["services"].pop("mongo-bootstrap")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "mongo-bootstrap" in errors and "missing from render" in errors
    # без профиля — «автозапуск» и поломка повторных up
    cfg = _good_cfg("production")
    cfg["services"]["mongo-bootstrap"].pop("profiles")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "must sit behind the \"bootstrap\" profile" in errors
    # resident-restart у одноразового job — запрещён
    cfg = _good_cfg("production")
    cfg["services"]["mongo-bootstrap"]["restart"] = "unless-stopped"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "one-shot bootstrap must have restart" in errors


def test_r2607_bootstrap_loopback_uri_is_exact() -> None:
    """Passwordless loopback — исключительное право mongo-bootstrap (localhost
    exception): его MONGO_URI обязан оставаться ровно mongodb://127.0.0.1:27017,
    а у прочих сервисов любой 127.0.0.1-URI — ошибка (review R26-07, blocker 2)."""
    cfg = _good_cfg("production")
    cfg["services"]["mongo-bootstrap"]["environment"]["MONGO_URI"] = (
        "mongodb://dsbot_migration:pw-mig@127.0.0.1:27017/?authSource=voice_tracker")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[mongo-bootstrap] MONGO_URI must be exactly" in errors
    cfg = _good_cfg("production")
    cfg["services"]["gateway"]["environment"]["MONGO_URI"] = "mongodb://127.0.0.1:27017"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[gateway] MONGO_URI is a loopback URI" in errors


def test_r2607_blocker2_schema_migrate_contract_rejected_variants() -> None:
    """Рендер-контракт schema-migrate (review R26-07, blocker 2) проверяется
    fail-closed по каждому пункту обвязки."""
    # отсутствие (когда прочие profile-сервисы в рендере видны) — у runner'а
    # не остаётся легальной точки migrate up/status
    cfg = _good_cfg("production")
    cfg["services"].pop("schema-migrate")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "schema-migrate" in errors and "missing from render" in errors
    # без профиля — автозапуск на каждом up
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"].pop("profiles")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "must sit behind the \"migrate\" profile" in errors
    # resident-restart у одноразового job запрещён
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["restart"] = "unless-stopped"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "one-shot schema-migrate runner must have restart" in errors
    # безпарольный URI на mongo — только loopback mongo-bootstrap легален
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["environment"]["MONGO_URI"] = "mongodb://mongo:27017"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[schema-migrate] MONGO_URI is an unauthenticated mongodb:// URI" in errors
    assert "MONGO_URI must carry the MONGO_MIGRATION_URI credentials" in errors
    # loopback-URI (даже без credentials) runner'у запрещён — это не его контракт
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["environment"]["MONGO_URI"] = "mongodb://127.0.0.1:27017"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[schema-migrate] MONGO_URI is a loopback URI" in errors
    assert "MONGO_URI must carry the MONGO_MIGRATION_URI credentials" in errors
    # нет MONGO_DB — runner не знает целевую базу
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["environment"].pop("MONGO_DB")
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "[schema-migrate] MONGO_DB must be set" in errors
    # network_mode как у bootstrap — второй безпарольно-loopback-вход в mongod
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["network_mode"] = "service:mongo"
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "network_mode: service:mongo is the" in errors
    # вне dsbot-data хост `mongo` из URI нерезолвим; в egress сервису нечего делать
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["networks"] = ["dsbot-egress"]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "must be on dsbot-data" in errors
    assert "must not be on dsbot-egress" in errors
    # общий env_file — runner получил бы все секреты вместо двух нужных ключей
    cfg = _good_cfg("production")
    cfg["services"]["schema-migrate"]["env_file"] = [
        {"path": ENV_PATH, "service": "schema-migrate"}]
    errors = "\n".join(validate_compose.check(cfg, "production"))
    assert "must not get the shared env_file" in errors


def test_r2607_blocker2_schema_migrate_uri_must_be_the_env_migration_uri(tmp_path) -> None:
    """MONGO_URI schema-migrate обязан ровно совпадать с MONGO_MIGRATION_URI,
    MONGO_DB — с MONGO_DB выбранного env-файла: подмена на runtime-URI (нет DDL —
    runner не работает) или admin-URI (root — избыточные привилегии, DB06) —
    падение по ИМЕНИ ключа без раскрытия значений."""
    selected = _write_env(tmp_path, "staging")
    cfg = _good_cfg("staging", envfile=str(selected))
    assert validate_compose.check(cfg, "staging", env_file=str(selected)) == []
    for wrong in ("mongodb://dsbot_app:pw-app@mongo:27017/?authSource=voice_tracker_staging",
                  "mongodb://dsbot_root:pw-root@mongo:27017/admin?authSource=admin"):
        cfg2 = _good_cfg("staging", envfile=str(selected))
        cfg2["services"]["schema-migrate"]["environment"]["MONGO_URI"] = wrong
        errors = "\n".join(validate_compose.check(cfg2, "staging", env_file=str(selected)))
        assert ("[schema-migrate] MONGO_URI does not match the selected env file's "
                "MONGO_MIGRATION_URI") in errors, errors
        assert "pw-app" not in errors and "pw-root" not in errors
    cfg3 = _good_cfg("staging", envfile=str(selected))
    cfg3["services"]["schema-migrate"]["environment"]["MONGO_DB"] = "voice_tracker"
    errors = "\n".join(validate_compose.check(cfg3, "staging", env_file=str(selected)))
    assert ("[schema-migrate] MONGO_DB does not match the selected env file's "
            "MONGO_DB") in errors
    assert "[schema-migrate] staging must not write the production DB" in errors


def test_r2607_blocker2_yaml_compose_schema_migrate_wiring() -> None:
    """Форма YAML (оба compose): schema-migrate — ${BOOTSTRAP_IMAGE:?…},
    restart "no", профиль ["migrate"], networks ["dsbot-data"], без network_mode,
    env_file, портов и volumes; environment ровно {MONGO_URI, MONGO_DB} с
    плейсхолдерами ${MONGO_MIGRATION_URI:?…}/${MONGO_DB:?}; depends_on mongo
    service_healthy. mongo-bootstrap остаётся passwordless loopback
    (mongodb://127.0.0.1:27017) и migration-URI не получает."""
    for path in (DEPLOY / "production" / "compose.yml",
                 DEPLOY / "staging" / "compose.staging.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        mig = doc["services"]["schema-migrate"]
        assert mig["image"].startswith("${BOOTSTRAP_IMAGE:?"), path.name
        assert mig["restart"] == "no", path.name
        assert mig["profiles"] == ["migrate"], path.name
        assert mig["networks"] == ["dsbot-data"], path.name
        assert "network_mode" not in mig and "env_file" not in mig, path.name
        assert "ports" not in mig and "volumes" not in mig, path.name
        assert set(mig["environment"]) == {"MONGO_URI", "MONGO_DB"}, path.name
        assert mig["environment"]["MONGO_URI"].startswith("${MONGO_MIGRATION_URI:?"), path.name
        assert mig["environment"]["MONGO_DB"] == "${MONGO_DB:?}", path.name
        assert mig["user"] == "${DSBOT_UID:?}:${DSBOT_GID:?}", path.name
        assert mig["depends_on"]["mongo"]["condition"] == "service_healthy", path.name
        boot = doc["services"]["mongo-bootstrap"]
        assert boot["environment"]["MONGO_URI"] == "mongodb://127.0.0.1:27017", path.name
        assert "MONGO_MIGRATION_URI" not in str(boot), path.name


def test_r2607_profile_filtered_render_tolerates_absent_bootstrap() -> None:
    """Рендеры compose, отсеивающие неактивные profile-сервисы, не показывают ни
    controlplane, ни mongo-bootstrap, ни schema-migrate — отсутствие не должно
    быть ложной тревогой (контракт держат YAML-тесты)."""
    cfg = _good_cfg("production")
    cfg["services"].pop("mongo-bootstrap")
    cfg["services"].pop("controlplane")
    cfg["services"].pop("schema-migrate")
    assert validate_compose.check(cfg, "production") == []


def test_r2607_bot_uri_must_be_the_env_bot_uri(tmp_path) -> None:
    """MONGO_URI бота в рендере обязан ровно совпадать с MONGO_BOT_URI выбранного
    env-файла (web — с MONGO_WEB_URI): подмена на URI с DDL-ролью (migration) —
    скрытая эскалация прав, падает по имени ключа без раскрытия значений."""
    selected = _write_env(tmp_path, "production")
    cfg = _good_cfg("production", envfile=str(selected))
    assert validate_compose.check(cfg, "production", env_file=str(selected)) == []
    cfg["services"]["gateway"]["environment"]["MONGO_URI"] = (
        "mongodb://dsbot_migration:pw-mig@mongo:27017/?authSource=voice_tracker"
    )
    cfg["services"]["web"]["environment"]["MONGO_URI"] = (
        "mongodb://dsbot_app:pw-app@mongo:27017/?authSource=voice_tracker"
    )
    errors = "\n".join(validate_compose.check(cfg, "production", env_file=str(selected)))
    assert "[gateway] MONGO_URI does not match the selected env file's MONGO_BOT_URI" in errors
    assert "[web] MONGO_URI does not match the selected env file's MONGO_WEB_URI" in errors
    # секреты в сообщение не попадают
    assert "pw-mig" not in errors and "pw-app" not in errors


def test_r2607_rendered_runtime_schema_mode_pinned_verify(tmp_path) -> None:
    """Review R26-07 (blocker 3): в ОТРЕНДЕРЕННОМ конфиге каждый runtime-бот обязан
    нести environment DSBOT_SCHEMA_MODE ровно "verify": без этого правка якоря или
    строка env_file переводят runtime в mutating bootstrap (DDL под app-креденшеллами).
    bootstrap-значение и пропажа ключа падают; сообщения называют сервис и ключ."""
    for mode in ("production", "staging"):
        assert validate_compose.check(_good_cfg(mode), mode) == []
        cfg = _good_cfg(mode)
        cfg["services"]["writer"]["environment"]["DSBOT_SCHEMA_MODE"] = "bootstrap"
        cfg["services"]["controlplane"]["environment"].pop("DSBOT_SCHEMA_MODE")
        errors = "\n".join(validate_compose.check(cfg, mode))
        assert '[writer] environment.DSBOT_SCHEMA_MODE is not exactly "verify"' in errors
        assert '[controlplane] environment.DSBOT_SCHEMA_MODE is missing' in errors
        # заодно: v5-рендер (env_file схлопнут в environment) с корректным
        # env-файлом проходит, а bootstrap в environment вместо якорного verify —
        # падает той же инвариант-петлёй
        selected = _write_env(tmp_path, mode, name=f".env.pin.{mode}")
        cfg2 = _good_cfg(mode, envfile=str(selected))
        for name in validate_compose.BOT_ENVFILE_SERVICES:
            cfg2["services"][name].pop("env_file")
        assert validate_compose.check(cfg2, mode, env_file=str(selected)) == []
        cfg2["services"]["tracker"]["environment"]["DSBOT_SCHEMA_MODE"] = "bootstrap"
        errors = "\n".join(validate_compose.check(cfg2, mode, env_file=str(selected)))
        assert '[tracker] environment.DSBOT_SCHEMA_MODE is not exactly "verify"' in errors


def test_r2607_yaml_bot_env_anchor_hardwires_verify() -> None:
    """Review R26-07 (blocker 3): в x-bot-env (&bot-env) добавлен литерал
    DSBOT_SCHEMA_MODE: verify (не интерполяция), поэтому ни один из семи runtime-ботов
    не получает bootstrap из env_file; одноразовые job'ы (mongo-bootstrap,
    schema-migrate) якорь не наследуют и ключа не имеют."""
    bot_services = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")
    for path in (DEPLOY / "production" / "compose.yml",
                 DEPLOY / "staging" / "compose.staging.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["x-bot-env"]["DSBOT_SCHEMA_MODE"] == "verify", path.name
        assert "${" not in str(doc["x-bot-env"]["DSBOT_SCHEMA_MODE"]), path.name
        for name in bot_services:
            assert doc["services"][name]["environment"]["DSBOT_SCHEMA_MODE"] == "verify", (
                path.name, name)
        assert "DSBOT_SCHEMA_MODE" not in doc["services"]["mongo-bootstrap"]["environment"], path.name
        assert "DSBOT_SCHEMA_MODE" not in doc["services"]["schema-migrate"]["environment"], path.name


def test_r2607_yaml_compose_auth_uris_and_bootstrap() -> None:
    """Форма YAML: mongod с --auth; x-bot-env → ${MONGO_BOT_URI...}, web →
    ${MONGO_WEB_URI...}; безпарольный URI на mongo-контейнер не встречается и в
    комментариях; mongo-bootstrap — restart "no" + профиль bootstrap +
    network_mode: service:mongo (localhost exception только с 127.0.0.1)."""
    for path in (DEPLOY / "production" / "compose.yml", DEPLOY / "staging" / "compose.staging.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        assert doc["services"]["mongo"]["command"] == ["--auth"], path.name
        assert "mongodb://mongo:27017" not in text, path.name
        assert "MONGO_URI: mongodb://mongo" not in text, path.name
        assert doc["x-bot-env"]["MONGO_URI"].startswith("${MONGO_BOT_URI:?"), path.name
        assert doc["services"]["web"]["environment"]["MONGO_URI"].startswith("${MONGO_WEB_URI:?"), path.name
        boot = doc["services"]["mongo-bootstrap"]
        assert boot["image"].startswith("${BOOTSTRAP_IMAGE:?"), path.name
        assert boot["restart"] == "no", path.name
        assert boot["profiles"] == ["bootstrap"], path.name
        assert boot["network_mode"] == "service:mongo", path.name
        assert boot["environment"]["MONGO_URI"] == "mongodb://127.0.0.1:27017", path.name
        assert str(boot["env_file"][0]).startswith("${DSBOT_ENV_FILE:?"), path.name
        assert boot["user"] == "${DSBOT_UID:?}:${DSBOT_GID:?}", path.name
        assert boot["depends_on"]["mongo"]["condition"] == "service_healthy", path.name


def test_r2607_env_requires_auth_uris_passwords_and_schema_mode(tmp_path) -> None:
    good = _write_env(tmp_path, "production")
    assert validate_env.check(str(good), "production") == []
    # отсутствие любого нового обязательного ключа — падение
    for key in ("MONGO_BOT_URI", "MONGO_WEB_URI", "MONGO_ADMIN_URI", "MONGO_MIGRATION_URI",
                "MONGO_BACKUP_URI", "MONGO_RESTORE_URI", "DB_USER_ROOT", "DB_PASS_ROOT",
                "DB_USER_DSBOT_APP",
                "DB_USER_DSBOT_WEB", "DB_USER_DSBOT_MIGRATION", "DB_USER_DSBOT_BACKUP",
                "DB_USER_DSBOT_RESTORE", "BOOTSTRAP_IMAGE", "DSBOT_SCHEMA_MODE"):
        bad = _write_env(tmp_path, "production", name=f".env.miss.{key}", **{key: ""})
        errors = "\n".join(validate_env.check(str(bad), "production"))
        assert f"missing required key: {key}" in errors, key
    # staging требует тот же auth-набор (репетиция прода) и не пускает прод-authSource
    good_st = _write_env(tmp_path, "staging")
    assert validate_env.check(str(good_st), "staging") == []
    sneaky = _write_env(tmp_path, "staging",
                        MONGO_BOT_URI="mongodb://dsbot_app:pw@mongo:27017/?authSource=voice_tracker")
    errors = "\n".join(validate_env.check(str(sneaky), "staging"))
    assert "MONGO_BOT_URI authenticates against the PRODUCTION database" in errors


def test_r2607_env_rejects_unauthenticated_uri_value(tmp_path) -> None:
    env = _write_env(tmp_path, "production", MONGO_BOT_URI="mongodb://mongo:27017")
    errors = "\n".join(validate_env.check(str(env), "production"))
    assert "MONGO_BOT_URI: must be an authenticated" in errors


def test_r2607_env_plan_user_uris_must_auth_against_working_db(tmp_path) -> None:
    """Review R26-07 (blocker 1): dsbot_app/dsbot_web/dsbot_migration/dsbot_backup/
    dsbot_restore создаются ensure_users В РАБОЧЕЙ БД (роли backup/restore из admin только
    ВЫДАНЫ им — пользователя туда не переносят). authSource=admin на этих URI —
    гарантированный Authentication failed на живом mongod; валидатор обязан
    отвергнуть это до деплоя. MONGO_ADMIN_URI (root в admin) — исключение."""
    for key in ("MONGO_BOT_URI", "MONGO_WEB_URI", "MONGO_MIGRATION_URI",
                "MONGO_BACKUP_URI", "MONGO_RESTORE_URI"):
        bad = _write_env(tmp_path, "production", name=f".env.src.{key}",
                         **{key: "mongodb://u:p@mongo:27017/?authSource=admin"})
        errors = "\n".join(validate_env.check(str(bad), "production"))
        assert f"{key}: authSource must equal MONGO_DB" in errors, (key, errors)
    # сам root-URI правилами рабочей БД не связан (создан localhost exception в admin)
    root_admin = _write_env(tmp_path, "production", name=".env.root",
                            MONGO_ADMIN_URI="mongodb://dsbot_root:pw@mongo:27017/admin?authSource=admin")
    assert validate_env.check(str(root_admin), "production") == []
    # path-форма вместо authSource: обязана указывать на рабочую БД; "/admin" — ошибка
    pathdb = _write_env(tmp_path, "production", name=".env.pathdb",
                        MONGO_BACKUP_URI="mongodb://u:p@mongo:27017/admin")
    errors = "\n".join(validate_env.check(str(pathdb), "production"))
    assert "MONGO_BACKUP_URI: URI path database must equal MONGO_DB" in errors
    good_path = _write_env(tmp_path, "production", name=".env.pathok",
                           MONGO_BACKUP_URI="mongodb://u:p@mongo:27017/voice_tracker")
    assert validate_env.check(str(good_path), "production") == []
    # пустой path без authSource (= admin по умолчанию) — тоже ошибка контракта
    nosrc = _write_env(tmp_path, "production", name=".env.nosrc",
                       MONGO_RESTORE_URI="mongodb://u:p@mongo:27017/")
    errors = "\n".join(validate_env.check(str(nosrc), "production"))
    assert "MONGO_RESTORE_URI: must authenticate against MONGO_DB" in errors
    # URL-encoded authSource совпадает после decode
    encoded = _write_env(tmp_path, "production", name=".env.encoded",
                         MONGO_BACKUP_URI="mongodb://u:p@mongo:27017/?authSource=voice%5Ftracker")
    assert validate_env.check(str(encoded), "production") == []


def test_r2607_env_rejects_non_verify_schema_mode(tmp_path) -> None:
    """Review R26-07 (blocker 3): deploy-профили production/staging принимают
    РОВНО DSBOT_SCHEMA_MODE=verify. bootstrap — mutating-режим runtime'а
    (ensure_indexes = DDL под app-креденшеллами) — остаётся локальной
    dev-возможностью вне deploy-профилей (voice_tracker/runtime.py его не
    трогает), поэтому здесь он отвергается наравне с мусором; пустое значение
    ловится required-проверкой. Сообщения — без значений (рядом секреты)."""
    for mode in ("production", "staging"):
        assert validate_env.check(str(_write_env(tmp_path, mode, name=f".env.ok.{mode}")),
                                  mode) == []
        for junk in ("bootstrap", "BOOTSTRAP", "Verify", "garbage", "bootstrap "):
            bad = _write_env(tmp_path, mode, name=f".env.bad.{mode}.{junk.strip() or junk}",
                             DSBOT_SCHEMA_MODE=junk)
            errors = validate_env.check(str(bad), mode)
            joined = "\n".join(errors)
            assert 'DSBOT_SCHEMA_MODE must be exactly "verify"' in joined, (mode, junk)
        # значение в сообщение не поднимается (в env рядом лежат секреты);
        # «bootstrap» в тексте — фиксированная документация правила, не эхо
        echoed = _write_env(tmp_path, mode, name=".env.echo", DSBOT_SCHEMA_MODE="s3cr3t-mode")
        joined = "\n".join(validate_env.check(str(echoed), mode))
        assert "s3cr3t-mode" not in joined
        empty = _write_env(tmp_path, mode, name=f".env.empty.{mode}", DSBOT_SCHEMA_MODE="")
        assert "missing required key: DSBOT_SCHEMA_MODE" in "\n".join(
            validate_env.check(str(empty), mode))


def test_r2607_env_requires_digest_pinned_bootstrap_image(tmp_path) -> None:
    env = _write_env(tmp_path, "production", BOOTSTRAP_IMAGE="python:3.12-slim")
    errors = "\n".join(validate_env.check(str(env), "production"))
    assert "BOOTSTRAP_IMAGE" in errors and "sha256" in errors


def test_r2607_auth_stand_script_contract() -> None:
    """Review R26-07: одноразовый auth-стенд обязан (a) создавать права ТОЧНОЙ
    production-точкой входа `migrate users --bootstrap` (не самописным
    mongosh-генератором, который мог «спрятать» расхождение с ROOT_ROLE_PLAN —
    blocker 2), (b) экспортировать ВСЕ URI плана, включая backup/restore с
    authSource=рабочая БД (blocker 1), (c) держать секреты вне stdout, кроме
    eval-блока, и не публиковать прод-порт 27017 на хост."""
    text = (DEPLOY / "scripts" / "r2607_auth_stand.sh").read_text(encoding="utf-8")
    # Запрет относится только к исполняемому тексту стенда: строковые литералы и
    # код не бывают комментариями, а шапка обязана объяснять, почему права создаёт
    # production-код (в т.ч. что `createRole` на mongo:7 отвергает `aggregate`) —
    # документирующий комментарий не имеет права ронять контракт по подстроке.
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    # (a) production-путь начальных прав; ручного создания ролей/грантов нет
    assert "exec python -B -m voice_tracker.migrate users --bootstrap" in code
    for manual in ("createRole", "createUser", "grantRolesToUser",
                   "grantRoles", "updateRole", "dropRole", "changeUserPassword"):
        assert manual not in code, f"стенд не обязан генерировать права вручную: {manual}"
    # единственный живой mongosh — ожидание готовности кластера, не провижнрование
    assert code.count("mongosh") == 1 and "'db.adminCommand({ping:1}).ok'" in code
    # (b) полный набор export'ов для eval
    for key in ("TEST_MONGO_ADMIN_URI", "TEST_MONGO_APP_URI", "TEST_MONGO_WEB_URI",
                "TEST_MONGO_MIGRATION_URI", "TEST_MONGO_BACKUP_URI",
                "TEST_MONGO_RESTORE_URI", "TEST_MONGO_ROOT_LOCAL"):
        assert f"export {key}=" in text, key
    assert "authSource=$DB'" in text  # backup/restore — против рабочей БД стенда
    # (c) публикация только на стендовый порт loopback; 27017 хоста не используется
    assert '-p "127.0.0.1:${PORT}:27017"' in text and "PORT=27098" in text


# Маркерный контракт миграционной фазы: ТОТ ЖЕ список, что сверяет живыми
# значениями tests/test_mongo_auth_stand.py (R2607_MARKERS). Фиксируется здесь
# независимо (дублем литерала), чтобы расхождение «скрипт печатает одно, тест
# читает другое» падало статикой, а не зелёным прогоном без живых проверок.
R2607_MARKER_CONTRACT: tuple[str, ...] = (
    "R2607_MIGRATE_UP_RC",
    "R2607_MIGRATE_STATUS_RC",
    "R2607_MIGRATE_UP_APPLIED",
    "R2607_SCHEMA_LATEST",
    "R2607_SCHEMA_VERSION",
    "R2607_BACKFILL_STEP",
    "R2607_BACKFILL_STATUS",
    "R2607_BACKFILL_PENDING",
    "R2607_BACKFILL_DONE",
    "R2607_LEGACY_ID",
    "R2607_LEGACY_REVISION",
    "R2607_PASSWORDLESS_RC",
    "R2607_PASSWORDLESS_DENIED",
)


def test_r2607_auth_stand_marker_contract() -> None:
    """Review R26-07, blocker 2: миграционная фаза обязана выпускать наружу
    ПОЛНЫЙ набор несекретных признаков прогона и обязан быть закрытым:
    (а) whitelist bash = ровно набор, который печатает программа фазы = ровно
        набор, который читает pytest (иначе маркер «есть в скрипте, но не
        печатается» или «напечатан, но не в whitelist» роняет стенд);
    (б) каждый маркер обязателен (completeness-цикл) и проверяется по существу
        гейтами в bash — стенд не имеет права отдать eval-блок с rc!=0/drift;
    (в) значения проходят фильтр алфавита и сверку «не равно ни одному паролю»,
        а в stdout стенда попадают только как export'ы eval-блока."""
    text = (DEPLOY / "scripts" / "r2607_auth_stand.sh").read_text(encoding="utf-8")
    m = re.search(r'^R2607_MIG_KEYS="([^"]*)"$', text, re.M)
    assert m, "стенд обязан держать whitelist маркеров одной строкой R2607_MIG_KEYS=..."
    whitelist = tuple(m.group(1).split())
    assert whitelist == R2607_MARKER_CONTRACT, whitelist
    assert all(re.fullmatch(r"R2607_[A-Z0-9_]+", k) for k in whitelist)
    assert len(set(whitelist)) == len(whitelist)

    # (а) программа фазы печатает ровно whitelist, ничего сверх него
    printed = tuple(re.findall(r'print\(f"(R2607_[A-Z0-9_]+)=', text))
    assert printed == R2607_MARKER_CONTRACT, printed

    # (б) обязательность каждого маркера + гейты по существу
    assert 'миграционная фаза не вернула обязательный маркер $key' in text
    for gate in ('[ "$V_UP_RC" = "0" ]',
                 '[ "$V_STATUS_RC" = "0" ]',
                 '[ "$V_STATUS" = "done" ]',
                 '[ "$V_BACKFILLED" -ge 1 ]',
                 '[ "$V_LEGACY_REV" = "0" ]',
                 '[ "$V_LATEST" = "$V_SV" ]',
                 '[ "$V_NEG_RC" != "0" ]',
                 '[ "$V_NEG_KIND" = "unauthorized" ]'):
        assert gate in text, gate
    # applied count сверяется с планом миграций на стороне helper'а (печатается
    # R2607_MIGRATE_UP_APPLIED), а migrations обязан содержать backfill-шаг
    assert 'status.migrations не содержит M{BACKFILL_ID}' in text
    assert 'want = ["applied"] * len(migrate.MIGRATIONS)' in text

    # (в) фильтр значений + анти-секрет-сверка + публикация только eval-блоком
    assert '*[!A-Za-z0-9_.+-]*) die "маркер $key: значение вне безопасного набора' in text
    assert 'case "$val" in *"$pw"*) die "маркер $key совпадает с паролем' in text
    assert "export ${key}='${val}'" in text
    assert """printf '%s' "$R2607_EXPORTS\"""" in text
    # безпарольный прогон обязан быть именно CLI-отказом, а не «пропущен за неимением»
    assert 'neg = run_cli(["status", "--uri", LOCAL_URI, "--db", DB], BASE_ENV)' in text


def test_r2607_auth_stand_migration_role_plan_uses_real_actions() -> None:
    """Живой failure 2026-09-27: Mongo 7 createRole отвергает `aggregate`
    (Unrecognized action, BadValue code 2) — как и `getMore`. Стендовый текст не
    имеет права утверждать, что миграционной роли нужен отдельный read-грант:
    M3/M4 (агрегация precheck'а дублей) и M7 (count_documents) авторизуются
    find'ом из RUNTIME_ROLE_ACTIONS (ADR-0005 п.1)."""
    from voice_tracker import migrate

    forbidden = {"aggregate", "getMore"}
    for role, actions in migrate.ROLE_PLAN.items():
        assert not (set(actions) & forbidden), (role, sorted(set(actions) & forbidden))
    text = (DEPLOY / "scripts" / "r2607_auth_stand.sh").read_text(encoding="utf-8")
    assert not re.search(r"без\s+`?aggregate`?\s+у миграционной роли", text), (
        "шапка стенда всё ещё обещает падение M3/M4/M7 без aggregate — этого "
        "гранта в плане нет и быть не может")


# ------------------------------------------------------------- compose-файлы как YAML


@pytest.mark.parametrize(
    "path",
    [DEPLOY / "production" / "compose.yml", DEPLOY / "staging" / "compose.staging.yml"],
)
def test_compose_sources_have_no_build_no_tags_no_bind(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = doc["services"]
    for name, svc in services.items():
        assert "build" not in svc, name
        image = svc["image"]
        assert image.startswith("${") and image.endswith("}") or "@sha256:" in image, (name, image)
        for vol in svc.get("volumes") or []:
            assert isinstance(vol, str) and vol.count(":") in (1, 2), (name, vol)
            source = vol.split(":")[0]
            assert not source.startswith((".", "/")), (name, vol)
    # mongo/nats без портов; web — единственный с портами
    assert "ports" not in services["mongo"] and "ports" not in services["nats"]
    with_ports = {n for n, s in services.items() if s.get("ports")}
    assert with_ports == {"web"}
    assert services["controlplane"]["profiles"] == ["controlplane"]
    assert doc["networks"]["dsbot-data"]["internal"] is True
    for n in ("gateway", "tracker", "writer", "commands", "activity", "stalker", "web",
              "mongo", "mongo-bootstrap", "schema-migrate", "nats"):
        assert n in services
    # R26-07: web-URI — плейсхолдер env-ключа (аутентифицированный dsbot_web)
    assert services["web"]["environment"]["MONGO_URI"].startswith("${MONGO_WEB_URI:?")
    assert "host.docker.internal" not in path.read_text(encoding="utf-8")
    # media: writable только у gateway; web ro
    gw = services["gateway"].get("volumes") or []
    assert any("media:/data/media" == v for v in gw)
    assert any(v == "media:/data/media:ro" for v in services["web"]["volumes"])
    for n in ("tracker", "writer", "commands", "activity", "stalker"):
        assert not services[n].get("volumes"), n


@pytest.mark.parametrize("path", sorted((DEPLOY / "scripts").glob("*.sh")))
def test_scripts_use_explicit_project_and_env(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if path.name == "_common.sh":
        assert '-p "$PROJECT"' in text and "-f" in text and "--env-file" in text
        return
    assert "source \"$(dirname \"$0\")/_common.sh\"" in text, path.name


# ------------------------------- реальный рендер через docker compose (integration)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["production", "staging"])
def test_real_compose_render_passes_invariants(tmp_path, mode: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not available")
    src = DEPLOY / mode / ("compose.yml" if mode == "production" else "compose.staging.yml")
    # R26-06/V26-16: env_file больше НЕ резолвится от каталога compose-файла —
    # контейнеры читают ровно DSBOT_ENV_FILE, который подставляют deploy-скрипты
    # (и как --env-file для интерполяции, и как значение ${DSBOT_ENV_FILE}).
    # -p передаём ЯВНО значением, равным name: из YAML (config ничего не
    # создаёт, конфликта нет): рендер обязан отдавать в name именно его
    # (validate_compose п.4), и так тест не зависит от того, читает ли данная
    # версия compose "name:" при отсутствии -p. Случайный -p перебивал name и
    # давал ложное падение «rendered project name != EXPECTED_PROJECT».
    stage = tmp_path / mode
    stage.mkdir()
    compose = stage / "compose.yml"
    shutil.copyfile(src, compose)
    env = _write_env(stage, mode, name=f".env.{mode}")
    proc = subprocess.run(
        ["docker", "compose", "-p", validate_compose.EXPECTED_PROJECT[mode],
         "-f", str(compose), "--env-file", str(env),
         "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=str(stage),
        env={**os.environ, "DSBOT_ENV_FILE": str(env)},
    )
    if proc.returncode != 0:
        pytest.fail(f"compose config failed: {proc.stderr[:800]}")
    cfg = json.loads(proc.stdout)
    # Review R26-07 (blocker 3): hard-wire обязан быть виден именно в живом
    # рендере: environment каждого runtime-бота (приоритетнее env_file) — verify.
    # Профильные сервисы (controlplane) данная версия compose может отсеивать —
    # проверяем присутствующих, как и сам валидатор.
    for name in validate_compose.BOT_ENVFILE_SERVICES:
        svc = cfg["services"].get(name)
        if svc is None:
            continue
        got = validate_compose._environment(svc).get("DSBOT_SCHEMA_MODE")
        assert got == validate_compose.RUNTIME_SCHEMA_MODE, (mode, name, got)
    errors = validate_compose.check(cfg, mode, env_file=str(env))
    assert errors == [], "\n".join(errors)
