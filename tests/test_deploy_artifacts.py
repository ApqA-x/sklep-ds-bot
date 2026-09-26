"""T13: инварианты deploy-артефактов (P01/P02/P03/P07/P08 статикой; P04-P06 — live).

validate_compose/validate_env — stdlib; вызываем их функции напрямую.
Отдельный integration-тест рендерит реальный compose через `docker compose config`
(docker CLI на хосте) и прогоняет по отрендеренному — это и есть проверка P01 на
настоящем рендере, без build/pull/up.
"""
from __future__ import annotations

import json
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


def _good_cfg(mode: str) -> dict:
    services = {
        "mongo": _svc(),
        "nats": _svc(),
        "gateway": _svc(volumes=[{"type": "volume", "source": "media", "target": "/data/media"}]),
        "tracker": _svc(),
        "writer": _svc(),
        "commands": _svc(),
        "activity": _svc(),
        "stalker": _svc(),
        "controlplane": _svc(profiles=["controlplane"]),
        "web": _svc(
            ports=[{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8000, "published": "8000"}],
            volumes=[{"type": "volume", "source": "media", "target": "/data/media", "read_only": True}],
            environment={"WEB_ENV": "production"},
        ),
    }
    return {
        "services": services,
        "networks": {"dsbot-data": {"name": "x", "internal": True}},
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
    # копия compose + .env рядом — как на реальном хосте (env_file резолвится от
    # каталога compose-файла)
    stage = tmp_path / mode
    stage.mkdir()
    compose = stage / "compose.yml"
    shutil.copyfile(src, compose)
    env = _write_env(stage, mode, name=".env")
    proc = subprocess.run(
        ["docker", "compose", "-p", f"t13-render-{mode}", "-f", str(compose), "--env-file", str(env),
         "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=str(stage),
    )
    if proc.returncode != 0:
        pytest.fail(f"compose config failed: {proc.stderr[:800]}")
    cfg = json.loads(proc.stdout)
    errors = validate_compose.check(cfg, mode)
    assert errors == [], "\n".join(errors)
