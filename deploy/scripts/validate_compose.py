#!/usr/bin/env python3
"""T13: статическая проверка ОТРЕНДЕРЕННОГО production/staging compose (P01/P02/P07).

Читает JSON со stdin (`docker compose ... config --format json`), проверяет
инварианты и ничего не выводит из env (значения секретов не проходят через скрипт).
Совместим с system python3 (stdlib-only) — запускать можно на чистом хосте.

Выход: 0 = инварианты соблюдены; 1 = нарушение (перечислены); 2 = неверный ввод.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

DIGEST_RE = re.compile(r"^(?:[\w.\-]+/)?[\w.\-/]+@sha256:[0-9a-f]{64}$")
ALLOWED_SERVICES = {
    "mongo",
    "nats",
    "gateway",
    "tracker",
    "writer",
    "commands",
    "activity",
    "stalker",
    "web",
    "controlplane",
}
IMAGE_VARS = {
    "mongo": "MONGO_IMAGE",
    "nats": "NATS_IMAGE",
    "gateway": "BOT_GATEWAY_IMAGE",
    "tracker": "BOT_TRACKER_IMAGE",
    "writer": "BOT_WRITER_IMAGE",
    "commands": "BOT_COMMANDS_IMAGE",
    "activity": "BOT_ACTIVITY_IMAGE",
    "stalker": "BOT_STALKER_IMAGE",
    "controlplane": "BOT_CONTROLPLANE_IMAGE",
    "web": "WEB_IMAGE",
}


def _service_ports(svc: dict) -> list:
    return list(svc.get("ports") or [])


def _port_host_ip(p: dict | str) -> str:
    if isinstance(p, str):
        # short syntax "127.0.0.1:8000:8000" / "8000:8000"
        parts = p.split(":")
        return parts[0] if len(parts) == 3 else "0.0.0.0"
    return str(p.get("host_ip") or p.get("ip") or "0.0.0.0")


def check(cfg: dict, mode: str) -> list[str]:
    errors: list[str] = []
    services = cfg.get("services") or {}
    if not services:
        return ["no services in rendered config"]

    unknown = set(services) - ALLOWED_SERVICES
    if unknown:
        errors.append(f"services outside the release whitelist: {sorted(unknown)}")

    required = {"mongo", "nats", "gateway", "tracker", "writer", "commands", "activity", "stalker", "web"}
    missing = required - set(services)
    if missing:
        errors.append(f"required services missing: {sorted(missing)}")

    for name, svc in services.items():
        # P01: никаких build: и source bind-монтов
        if svc.get("build"):
            errors.append(f"[{name}] has build: (production forbids building from tree)")
        image = str(svc.get("image") or "")
        if not DIGEST_RE.match(image):
            errors.append(f"[{name}] image is not an immutable digest ref: {image!r}")
        if name != "web" and _service_ports(svc):
            errors.append(f"[{name}] publishes ports; only web ingress may (P07)")
        restart = svc.get("restart") or svc.get("restart_policy") or ""
        if not restart or restart in {"no", "never"}:
            errors.append(f"[{name}] missing restart policy")
        resources = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if not resources.get("cpus") and not resources.get("memory"):
            errors.append(f"[{name}] no resource limits")
        logging_cfg = svc.get("logging") or {}
        options = logging_cfg.get("options") or {}
        if logging_cfg.get("driver") == "json-file" and not (
            options.get("max-size") and options.get("max-file")
        ):
            errors.append(f"[{name}] json-file logging without rotation")
        for vol in svc.get("volumes") or []:
            source = vol.get("source") if isinstance(vol, dict) else str(vol).split(":", 1)[0]
            kind = vol.get("type") if isinstance(vol, dict) else None
            if kind == "bind" or (source or "").startswith((".", "/", "~")):
                errors.append(f"[{name}] bind mount {source!r} — code/config must live in images")

    # P02: controlplane только под профилем (off by default)
    cp = services.get("controlplane")
    if cp is not None and not cp.get("profiles"):
        errors.append("controlplane without profiles: starts by accident (ADR-0004)")

    # P07: ingress web — только loopback
    web = services.get("web") or {}
    for p in _service_ports(web):
        host_ip = _port_host_ip(p if isinstance(p, (dict, str)) else p)
        if host_ip not in {"127.0.0.1", "localhost", "::1"}:
            errors.append(f"web published on non-loopback {host_ip!r}; TLS/public binding is the reverse proxy's job")

    # сети: data-план изолирован
    networks = cfg.get("networks") or {}
    data = networks.get("dsbot-data") or {}
    if not data.get("internal"):
        errors.append("network dsbot-data is not internal")

    # MONGO_DB: прод и staging не должны пересекаться с прод-базой
    env_like: dict[str, str] = {}
    for name, svc in services.items():
        e = svc.get("environment") or {}
        for k, v in e.items():
            if v is not None:
                env_like[f"{name}.{k}"] = str(v)
    if mode == "staging":
        for name in ("gateway", "tracker", "writer", "commands", "activity", "stalker", "web"):
            db = env_like.get(f"{name}.MONGO_DB")
            if db is not None and db == "voice_tracker":
                errors.append(f"[{name}] staging must not write the production DB voice_tracker")
        if env_like.get("web.WEB_ENV") not in (None, "production"):
            errors.append("staging web must run production-grade config (WEB_ENV=production)")
    if mode == "production":
        if env_like.get("web.WEB_ENV") not in (None, "production"):
            errors.append("production web must set WEB_ENV=production")

    # URI зависимостей зафиксированы на внутренние контейнеры: молчаливая смена
    # URI на host-базу = скрытая миграция данных (переезд — только T14 backup/restore)
    for name, svc in services.items():
        uri = str((svc.get("environment") or {}).get("MONGO_URI") or "")
        if "host.docker.internal" in uri:
            errors.append(f"[{name}] MONGO_URI points at host.docker.internal — prod DB migration must be an explicit backup/restore step (T14), not a silent URI")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["production", "staging"], required=True)
    parser.add_argument("--json-file", default="-", help="rendered compose JSON (default: stdin)")
    args = parser.parse_args(argv)

    try:
        if args.json_file == "-":
            cfg = json.load(sys.stdin)
        else:
            with open(args.json_file, encoding="utf-8") as fh:
                cfg = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"validate_compose: cannot read rendered config: {exc}", file=sys.stderr)
        return 2

    errors = check(cfg, args.mode)
    if errors:
        for err in errors:
            print(f"COMPOSE-INVARIANT VIOLATION: {err}", file=sys.stderr)
        return 1
    print(f"validate_compose OK (mode={args.mode}, services={len(cfg.get('services') or {})})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
