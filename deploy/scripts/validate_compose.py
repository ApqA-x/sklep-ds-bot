#!/usr/bin/env python3
"""T13: статическая проверка ОТРЕНДЕРЕННОГО production/staging compose (P01/P02/P07).

Читает JSON со stdin (`docker compose ... config --format json`), проверяет
инварианты и ничего не выводит из env (значения секретов не проходят через скрипт).
Совместим с system python3 (stdlib-only) — запускать можно на чистом хосте.

R26-06 дополнения:
  - V26-16 (--env-file PATH): у app-сервисов env_file в рендере обязан указывать
    ровно на выбранный env-файл (единый источник env), относительный дефолт
    `.env` отвергается; web env_file иметь не обязан и не должен (п.5).
    Факт живого прогона: docker compose v5 при `config --format json` НЕ отдаёт
    ключ env_file и целиком схлопывает выбранный файл в environment сервиса —
    в этой форме проверяется ФАКТ вливания содержимого выбранного файла
    (каждый ключ env-файла присутствует в environment со значением из файла);
  - V26-17: сеть dsbot-egress существует, НЕ internal; web/gateway/commands/
    activity/stalker в ней состоят; mongo/nats/tracker/writer/controlplane — нет;
  - п.4: project name в рендере зафиксирован (production=dsbot-prod,
    staging=dsbot-staging) — изоляция идентичностей P03.

R26-07 дополнения (Mongo --auth):
  - mongo обязан запускаться с command: ["--auth"];
  - ни один environment не содержит безпарольный URI на контейнерный mongo
    (mongodb://mongo[:порт] без credentials) — runtime-URI обязаны приходить из
    env-файла: bot-сервисы → MONGO_BOT_URI, web → MONGO_WEB_URI (при доступном
    env-файле значения MONGO_URI сверяются с этими ключами). Исключение —
    mongodb://127.0.0.1 у mongo-bootstrap: единственный легальный путь
    localhost exception;
  - mongo-bootstrap: profile "bootstrap", restart "no" (одноразовый job, не
    автозапуск). Если рендер данной версии compose отсеивает profile-сервисы
    (ни один сервис не вернул profiles), контракт bootstrap покрывают YAML-тесты.

Выход: 0 = инварианты соблюдены; 1 = нарушение (перечислены); 2 = неверный ввод.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

DIGEST_RE = re.compile(r"^(?:[\w.\-]+/)?[\w.\-/]+@sha256:[0-9a-f]{64}$")
# R26-07: безпарольный URI на контейнерный mongo (host ровно "mongo", без
# userinfo) — с --auth нерабочий, а его наличие значит, что секция не мигрировала
# на env-URI. 127.0.0.1 (localhost exception mongo-bootstrap) сюда не попадает.
UNAUTHENTICATED_MONGO_URI_RE = re.compile(r"^mongodb://mongo(?::\d+)?(?:[/?].*)?$")
ALLOWED_SERVICES = {
    "mongo",
    "mongo-bootstrap",
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
# R26-07: одноразовый bootstrap-job начальных прав — НЕ resident-сервис:
# restart "no", профиль bootstrap (в обычный up не входит).
BOOTSTRAP_SERVICE = "mongo-bootstrap"
BOOTSTRAP_PROFILE = "bootstrap"
IMAGE_VARS = {
    "mongo": "MONGO_IMAGE",
    "mongo-bootstrap": "BOOTSTRAP_IMAGE",
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
# R26-06/V26-16: сервисы, читающие общий env-файл (единый источник env).
BOT_ENVFILE_SERVICES = (
    "gateway",
    "tracker",
    "writer",
    "commands",
    "activity",
    "stalker",
    "controlplane",
)
# R26-06/V26-17: Discord REST/Gateway/OAuth требует NAT-egress.
EGRESS_NETWORK = "dsbot-egress"
EGRESS_SERVICES = {"gateway", "commands", "activity", "stalker", "web"}
NO_EGRESS_SERVICES = {"mongo", "nats", "tracker", "writer", "controlplane"}
# R26-06/п.5: web получает ровно ключи своего конфига (wt-web api/config.py);
# backend-only ключи в его environment — ошибка topology.
WEB_FORBIDDEN_ENV_KEYS = {
    "BACKUP_DIR",
    "BACKUP_AGE_KEY_FILE",
    "EVENT_SIGNING_SECRET",
    "DSBOT_ENV_FILE",
    "DSBOT_UID",
    "DSBOT_GID",
    "MONGO_IMAGE",
    "NATS_IMAGE",
}
EXPECTED_PROJECT = {"production": "dsbot-prod", "staging": "dsbot-staging"}


def _service_ports(svc: dict) -> list:
    return list(svc.get("ports") or [])


def _port_host_ip(p: dict | str) -> str:
    if isinstance(p, str):
        # short syntax "127.0.0.1:8000:8000" / "8000:8000"
        parts = p.split(":")
        return parts[0] if len(parts) == 3 else "0.0.0.0"
    return str(p.get("host_ip") or p.get("ip") or "0.0.0.0")


def _service_networks(svc: dict) -> set[str]:
    """Имена сетей сервиса в обеих формах рендера (dict-map и short list)."""
    nets = svc.get("networks")
    if isinstance(nets, dict):
        return {str(k) for k in nets}
    out: set[str] = set()
    for n in nets or []:
        if isinstance(n, dict):
            out.update(str(k) for k in n)
        else:
            out.add(str(n))
    return out


def _env_file_paths(svc: dict) -> list[str]:
    """Пути env_file из рендера: list[str] или list[dict] (compose >= 2.24
    кладёт объекты {"path": ..., "service": ...}) — принимаем обе формы,
    значение-ключ берём из path/source."""
    raw = svc.get("env_file")
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    paths: list[str] = []
    for e in raw:
        if isinstance(e, dict):
            p = e.get("path") or e.get("source") or ""
        else:
            p = str(e)
        if str(p).strip():
            paths.append(str(p))
    return paths


def _environment(svc: dict) -> dict[str, str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                out[k] = v
        return out
    return {str(k): ("" if v is None else str(v)) for k, v in env.items()}


def _parse_env_values(path: str) -> dict[str, str] | None:
    """Ключи/значения выбранного env-файла (тот же stdlib-цикл, что
    validate_env.parse_env_file — quoting-артефакты снимаются им же) для
    слияние-проверки V26-16 в рендере compose v5. Значения наружу не печатаются.
    None — файл не читается с диска (слияние-проверка на нём невозможна)."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return None
    return values


def _canon_path(path: str) -> str:
    """Нормализация пути env-файла для сверки рендера с выбором скрипта:
    realpath + Windows-буква → /mnt/<drive> (docker.exe под WSL рендерит
    C:\\...\\env, а скрипт знает /mnt/c/.../env)."""
    p = str(path).strip().strip('"').replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)$", p)
    if m:
        p = "/mnt/" + m.group(1).lower() + m.group(2)
    p = os.path.realpath(os.path.expanduser(p)).replace("\\", "/")
    return p.rstrip("/").lower()


def _is_abs(path: str) -> bool:
    p = str(path).replace("\\", "/")
    return p.startswith("/") or bool(re.match(r"^[A-Za-z]:/", p))


def check(cfg: dict, mode: str, env_file: str | None = None) -> list[str]:
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

    # п.4 (R26-06): имя проекта в рендере зафиксировано — staging не может
    # рендериться под прод-проектом и наоборот (P03).
    want_name = EXPECTED_PROJECT.get(mode)
    got_name = cfg.get("name")
    if got_name != want_name:
        errors.append(f"rendered project name {got_name!r} != {want_name!r} (identity isolation, P03)")

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
        if name == BOOTSTRAP_SERVICE:
            # R26-07: одноразовый job, resident-политика запрещена в обратную
            # сторону — restart "no" обязателен (иначе пересоздаётся на каждом up).
            if restart not in {"no", "never"}:
                errors.append(f"[{name}] one-shot bootstrap must have restart: \"no\" (R26-07)")
        else:
            if not restart or restart in {"no", "never"}:
                errors.append(f"[{name}] missing restart policy")
        resources = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if name != BOOTSTRAP_SERVICE and not resources.get("cpus") and not resources.get("memory"):
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

    # R26-06/V26-17: egress-сеть для Discord-клиентов; infra остаётся закрытым.
    egress = networks.get(EGRESS_NETWORK)
    if egress is None:
        errors.append(f"network {EGRESS_NETWORK} missing: no app egress — "
                      "Discord REST/Gateway/OAuth physically impossible")
    elif egress.get("internal"):
        errors.append(f"network {EGRESS_NETWORK} is internal — it defeats its own purpose (NAT egress required)")
    for name in sorted(EGRESS_SERVICES):
        if name in services and EGRESS_NETWORK not in _service_networks(services[name]):
            errors.append(f"[{name}] missing network {EGRESS_NETWORK} (no egress, V26-17)")
    for name in sorted(NO_EGRESS_SERVICES):
        if name in services and EGRESS_NETWORK in _service_networks(services[name]):
            errors.append(f"[{name}] must not be on {EGRESS_NETWORK} (internal-only by design, V26-17)")

    # R26-06/V26-16: единый источник env. app-сервисы читают env_file, и в
    # рендере этот путь обязан быть ровно выбранным файлом (не дефолтным
    # `.env` рядом с compose-файлом). web — без env_file вообще (п.5).
    # docker compose v5 ключ env_file в `config --format json` НЕ отдаёт вовсе,
    # зато схлопывает выбранный файл в environment — тогда проверяется факт
    # вливания содержимого (значения выбранных ключей наружу не печатаются).
    env_values: dict[str, str] | None = None
    if env_file and os.path.isfile(env_file):
        env_values = _parse_env_values(env_file)
        if env_values is None:
            errors.append("selected env file is not readable — V26-16 merge "
                          "check is impossible")
    if "web" in services and _env_file_paths(web):
        errors.append("[web] has env_file: web must receive its keys via "
                      "interpolated environment only (R26-06 п.5)")
    for name in BOT_ENVFILE_SERVICES:
        svc = services.get(name)
        if svc is None:
            continue
        paths = _env_file_paths(svc)
        if not paths:
            if env_values is None:
                errors.append(f"[{name}] has no env_file (single source of env, V26-16)")
                continue
            # Форма рендера compose v5: env_file не виден, содержимое выбранного
            # файла целиком в environment. Инвариант YAML (покрыт тестом): ни
            # один ключ env-файла не перекрывается явным environment бота
            # (SERVICE_NAME/MONGO_URI/NATS_URL/MEDIA_DIR в env-файлах отсутствуют),
            # поэтому сверяются все ключи файла; service-специфичные ключи
            # environment — лишние, они не проверяются.
            rendered = _environment(svc)
            offenders = sorted(
                k for k, v in env_values.items()
                if (k in rendered and rendered[k] != v) or (k not in rendered and v)
            )
            if offenders:
                # только ИМЕНА ключей — значения (секреты) в сообщение не попадают
                errors.append(f"[{name}] container env is not sourced from the selected "
                              f"env file (V26-16): keys {offenders}")
        for p in paths:
            if not _is_abs(p):
                errors.append(f"[{name}] env_file {p!r} is relative (compose default `.env` "
                              "resolves next to the compose file — forbidden, V26-16)")
            elif env_file and _canon_path(p) != _canon_path(env_file):
                errors.append(f"[{name}] env_file does not point at the env file selected "
                              "by the deploy scripts (single source of env, V26-16)")

    # R26-06/п.5: backend-only ключи не должны попадать в environment web.
    if web:
        bad = sorted(
            k for k in _environment(web)
            if k in WEB_FORBIDDEN_ENV_KEYS or k.startswith("MIGRATION")
        )
        if bad:
            errors.append(f"[web] backend-only keys must not be in its environment: {bad}")

    # MONGO_DB: прод и staging не должны пересекаться с прод-базой
    env_like: dict[str, str] = {}
    for name, svc in services.items():
        for k, v in _environment(svc).items():
            if v:
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
        uri = str(_environment(svc).get("MONGO_URI") or "")
        if "host.docker.internal" in uri:
            errors.append(f"[{name}] MONGO_URI points at host.docker.internal — prod DB migration must be an explicit backup/restore step (T14), not a silent URI")

    # ------------------------------------------------ R26-07: Mongo --auth ----
    mongo_svc = services.get("mongo") or {}
    cmd = mongo_svc.get("command")
    tokens = cmd if isinstance(cmd, list) else str(cmd or "").split()
    if "--auth" not in [str(t) for t in tokens]:
        errors.append("[mongo] command must contain --auth (R26-07: runtime URI "
                      "authentication is meaningless against an auth-less mongod)")

    # безпарольный URI на контейнерный mongo: с --auth нерабочий, а его наличие
    # в environment значит, что секция не переехала на env-URI (MONGO_BOT_URI /
    # MONGO_WEB_URI). Сообщения — только ИМЕНА ключей, значения (секреты) не печатаются.
    for name, svc in services.items():
        uri = str(_environment(svc).get("MONGO_URI") or "")
        if UNAUTHENTICATED_MONGO_URI_RE.match(uri):
            errors.append(f"[{name}] MONGO_URI is an unauthenticated mongodb:// URI — "
                          "R26-07 requires the credentialed URI from the env file "
                          "(MONGO_BOT_URI for bot services, MONGO_WEB_URI for web)")

    # x-bot-env/services обязаны резолвиться ровно в env-ключи своего уровня.
    # В рендере плейсхолдер ${MONGO_BOT_URI...} схлопнут в значение env-файла,
    # поэтому при доступном env-файле сверяем значение MONGO_URI с ними.
    if env_values is not None:
        for ref_key, names in (("MONGO_BOT_URI", BOT_ENVFILE_SERVICES), ("MONGO_WEB_URI", ("web",))):
            want = env_values.get(ref_key, "")
            if not want:
                continue
            for name in names:
                svc = services.get(name)
                if svc is None:
                    continue
                uri = str(_environment(svc).get("MONGO_URI") or "")
                if uri and uri != want:
                    errors.append(f"[{name}] MONGO_URI does not match the selected env "
                                  f"file's {ref_key} (R26-07 least privilege)")

    # mongo-bootstrap: contract одноразового job'а (restart/profile проверены и
    # выше по циклу, тут — сам факт присутствия в рендере и профиль).
    boot = services.get(BOOTSTRAP_SERVICE)
    if boot is None:
        # рендеры compose, отсеивающие неактивные profile-сервисы, не покажут и
        # controlplane — тогда отсутствие bootstrap не ложная тревога; контракт
        # покрывают YAML-тесты test_deploy_artifacts.
        if any(s.get("profiles") for s in services.values()):
            errors.append(f"[{BOOTSTRAP_SERVICE}] missing from render while other "
                          "profiled services are present (R26-07 one-shot bootstrap job)")
    else:
        if BOOTSTRAP_PROFILE not in (boot.get("profiles") or []):
            errors.append(f"[{BOOTSTRAP_SERVICE}] must sit behind the \"{BOOTSTRAP_PROFILE}\" "
                          "profile — auto-start breaks repeated ups (R26-07)")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["production", "staging"], required=True)
    parser.add_argument("--json-file", default="-", help="rendered compose JSON (default: stdin)")
    parser.add_argument("--env-file", default=None,
                        help="env file selected by the deploy scripts; when set, every "
                             "app-service env_file in the render must resolve to it (V26-16)")
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

    errors = check(cfg, args.mode, env_file=args.env_file)
    if errors:
        for err in errors:
            print(f"COMPOSE-INVARIANT VIOLATION: {err}", file=sys.stderr)
        return 1
    print(f"validate_compose OK (mode={args.mode}, services={len(cfg.get('services') or {})})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
