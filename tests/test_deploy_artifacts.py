"""T13: инварианты deploy-артефактов (P01/P02/P03/P07/P08 статикой; P04-P06 — live).

validate_compose/validate_env — stdlib; вызываем их функции напрямую.
Отдельный integration-тест рендерит реальный compose через `docker compose config`
(docker CLI на хосте) и прогоняет по отрендеренному — это и есть проверка P01 на
настоящем рендере, без build/pull/up.
"""
from __future__ import annotations

import json
import os
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
    services = {
        "mongo": _svc(),
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
        "MONGO_VOLUME": f"dsbot-{mode}-mongo-data",
        "MEDIA_VOLUME": f"dsbot-{mode}-media",
        "DSBOT_UID": "10001",
        "DSBOT_GID": "10001",
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "a",
        "EVENT_SIGNING_SECRET": "s" * 32,
        "MONGO_DB": "voice_tracker" if mode == "production" else "voice_tracker_staging",
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
    SERVICE_NAME/MONGO_URI/NATS_URL/MEDIA_DIR обязаны отсутствовать в env-файлах."""
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
            explicit = set((doc["services"][name].get("environment") or {}))
            clash = sorted(explicit & set(env_keys))
            assert not clash, (compose_path.name, name, clash)


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
    }
    errors = "\n".join(validate_compose.check(cfg2, "production"))
    assert "backend-only keys" in errors


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
    data_only = ("mongo", "nats", "tracker", "writer", "controlplane")
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
    for n in ("gateway", "tracker", "writer", "commands", "activity", "stalker", "web", "mongo", "nats"):
        assert n in services
    assert services["web"]["environment"]["MONGO_URI"] == "mongodb://mongo:27017"
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
    errors = validate_compose.check(cfg, mode, env_file=str(env))
    assert errors == [], "\n".join(errors)
