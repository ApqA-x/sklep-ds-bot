"""R26-06 (п.4/6/8 брифа): интеграционный стенд на ОДНОРАЗОВЫХ ресурсах.

Unit-прогон (`python -B -m pytest tests -q`, addopts = "-m not integration")
эти тесты не запускает — они снимутся как deselected. Отдельный прогон:
    python -B -m pytest tests/test_deploy_stand.py -q -m integration
Требования: рабочий docker CLI + compose plugin и доступ к registry (образы
mongo:7, nats:2.10-alpine стенд тянет сам, если их нет локально).

Проверяем живьём то, что статикой не доказуемо:
  1. V26-17 — egress у «приложения» реально есть (HTTP-ответ от Discord), а
     infra (mongo/nats) остаётся без опубликованных портов и вне egress-сети;
  2. V26-16 — контейнерный env приходит ровно из выбранного --env-file файла
     (никакого второго источника), доказательство по ОТРЕНДЕРЕННОМУ docker
     compose config (без up, без мутаций);
  3. V26-14/V26-15 — повторный `up` идемпотентен по данным: том переживает
     `down` без -v и флаг внутри него остаётся виден; `down -v` том сносит.

Гигиена стенда:
  - каждый тест заводит СВОЙ compose-проект r2606_<hex6> с ЯВНЫМИ именами сетей
    и томов (имя = <проект>-… → предсказуемо и для проверок, и для уборки);
  - уборка только по своему префиксу; чужие проекты (dashboard-clone,
    dsbot-t17e2e) и тома dsbot-media / dsbot-prod-* / dsbot-staging-* не
    затрагиваются ни одной командой;
  - в assert-сообщения попадают имена ресурсов и булевы сравнения, значения
    env (в т.ч. маркеры и секреты) не печатаются.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

from test_deploy_artifacts import _write_env

pytestmark = pytest.mark.integration

TIMEOUT = 120
PREFIX = "r2606"
MONGO_IMAGE = "mongo:7"
NATS_IMAGE = "nats:2.10-alpine"
FLAG = "R2606_FLAG_PRESENT"


# ------------------------------------------------------------------ harness


@pytest.fixture(scope="module", autouse=True)
def docker_cli() -> None:
    """Пропуск (не падение), если docker недоступен ИЗ ЭТОГО процесса.

    Проверка в фикстуре, а не в skipif на импорте: модуль собирается pytest'ом
    и в unit-прогоне (там он deselect-ится, но импортируется), а дёргать
    `docker info` на каждом unit-прогоне не нужно.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI недоступен из этого процесса")
    info = _run(["docker", "info"])
    if info.returncode != 0:
        pytest.skip("docker info failed (нет доступа к daemon)")
    compose = _run(["docker", "compose", "version"])
    if compose.returncode != 0:
        pytest.skip("docker compose plugin недоступен")


def _run(args: list[str], *, cwd: Path | None = None,
         env: dict[str, str] | None = None,
         timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """docker вызываем напрямую (Windows: docker.exe из PATH, Linux: docker)."""
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(cwd) if cwd else None,
                          env=env, timeout=timeout)


def _compose(project: str, workdir: Path, *args: str,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-p", project, "-f", str(workdir / "compose.yml"),
                 *args], cwd=workdir, env=env)


def _project() -> str:
    # имя проекта docker compose: [a-z0-9][a-z0-9_-]*
    return f"{PREFIX}_{uuid.uuid4().hex[:6]}"


def _json_records(out: str) -> list[dict]:
    """`docker ps --format json` бывал и JSON-массивом, и NDJSON (как и
    `compose ps`) — разбираем обе формы."""
    raw = (out or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        rows: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
        return rows
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _has_published_ports(rec: dict) -> bool:
    """Признак публикации порта наружу в `docker ps --format json`.

    Формы вывода менялись по версиям (факт живого прогона, docker 29.8):
      - старых отдаёт `Publishers` списком (пустой список = не опубликовано);
      - docker 29.8 кладёт `Publishers: null`, а `Ports` — строкой, где видны
        и только EXPOSE-порты образа ("4222/tcp, 6222/tcp, 8222/tcp"), что
        НЕ является публикацией (у mongo/nats это ложное срабатывание).
    Опубликованный порт в строковой форме всегда содержит "->"
    ("0.0.0.0:32768->27017/tcp") — им и отличаем."""
    publishers = rec.get("Publishers")
    if isinstance(publishers, list):
        return any(True for _ in publishers)
    ports = rec.get("Ports")
    if isinstance(ports, str):
        return "->" in ports
    return bool(ports)


def _ensure_images(*images: str) -> None:
    for image in images:
        if _run(["docker", "image", "inspect", image]).returncode == 0:
            continue
        try:
            pulled = _run(["docker", "pull", image], timeout=900)
        except subprocess.TimeoutExpired:
            pulled = None
        if pulled is None or pulled.returncode != 0:
            # отсутствующий локально образ при недоступном/медленном registry —
            # внешняя зависимость стенда, а не поломка кода
            pytest.skip(f"образ {image} недоступен (локально нет, pull не удался)")


def _stand_dir(tmp_path: Path) -> Path:
    workdir = tmp_path / "stand"
    workdir.mkdir(exist_ok=True)
    return workdir


def _write_stand(tmp_path: Path, cfg: dict) -> Path:
    workdir = _stand_dir(tmp_path)
    (workdir / "compose.yml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8", newline="\n"
    )
    return workdir


def _wait_running(container: str, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        probe = _run(["docker", "inspect", "-f", "{{.State.Running}}", container])
        if probe.returncode == 0 and probe.stdout.strip().lower() == "true":
            return True
        time.sleep(2)
    return False


def _rec_id(rec: dict) -> str:
    return str(rec.get("Id") or rec.get("ID") or "")


def _rec_name(rec: dict) -> str:
    return str(rec.get("Names") or rec.get("Name") or rec.get("Service") or "")


def _pick(recs: list[dict], svc: str) -> dict:
    """Запись `docker ps` по имени сервиса: контейнер compose называется
    <проект>-<service>-<индекс>."""
    for rec in recs:
        if re.search(rf"(^|[-_]){re.escape(svc)}(-|\b)", _rec_name(rec)):
            return rec
    raise AssertionError(f"{svc}: нет записи в docker ps стенда")


def _short(container: str) -> str:
    return (container.strip().splitlines() or [""])[0].strip()[:12]


def _network_members(network: str) -> set[str]:
    """Участники сети: и ключи .Containers (id), и их имена — формы вывода docker
    CLI менялись (Id/ID), поэтому сверяем оба представления (префиксы id)."""
    got = _run(["docker", "network", "inspect", network, "--format", "{{json .Containers}}"])
    assert got.returncode == 0, f"network inspect {network}: {got.stderr[-200:]}"
    try:
        members = json.loads(got.stdout.strip() or "{}")
    except json.JSONDecodeError:
        members = {}
    out: set[str] = set()
    if isinstance(members, dict):
        for cid, info in members.items():
            out.add(str(cid)[:12].lower())
            if isinstance(info, dict):
                out.add(str(info.get("Name") or "").lower())
    return out


def _inspect_bool(target: str, field: str) -> bool | None:
    got = _run(["docker", "network", "inspect", target, "--format", "{{." + field + "}}"])
    text = got.stdout.strip().lower()
    if text in ("true", "false"):
        return text == "true"
    return None


def _teardown(project: str, workdir: Path, networks: list[str], volumes: list[str]) -> None:
    """Уборка ТОЛЬКО своих ресурсов (все имена содержат префикс проекта)."""
    _compose(project, workdir, "down", "-v", "--remove-orphans", "--timeout", "20")
    for net in networks:
        _run(["docker", "network", "rm", net])
    for vol in volumes:
        _run(["docker", "volume", "rm", vol])


# ------------------------------------------------- 1. V26-17: egress + закрытый data


def _egress_stand(project: str) -> dict:
    """Одноразовый макет топологии R26-17: app-«gateway» в двух сетях, mongo/nats
    только в internal data, портов наружу нет. Теговые имена вместо digest'ов —
    стенд проверяет СЕТЕВУЮ топологию, а не pinning (он покрыт unit-тестами)."""
    return {
        "name": project,
        "services": {
            "mongo": {
                "image": MONGO_IMAGE,
                "networks": ["dsbot-data"],
                "volumes": ["mongodata:/data/db"],
            },
            "nats": {
                "image": NATS_IMAGE,
                "networks": ["dsbot-data"],
            },
            "gateway": {
                # «приложение» без сборки: тот же alpine, но спит — нужен только
                # shell внутри него для проверок egress
                "image": NATS_IMAGE,
                "entrypoint": ["/bin/sh", "-c"],
                "command": ["sleep infinity"],
                "networks": ["dsbot-data", "dsbot-egress"],
            },
        },
        "networks": {
            "dsbot-data": {"name": f"{project}-data", "internal": True},
            "dsbot-egress": {"name": f"{project}-egress", "internal": False},
        },
        "volumes": {"mongodata": {"name": f"{project}-mongodata"}},
    }


def test_v2617_live_egress_and_closed_data_ports(tmp_path: Path) -> None:
    project = _project()
    cfg = _egress_stand(project)
    workdir = _write_stand(tmp_path, cfg)
    networks = [f"{project}-data", f"{project}-egress"]
    volumes = [f"{project}-mongodata"]
    try:
        _ensure_images(MONGO_IMAGE, NATS_IMAGE)
        up = _compose(project, workdir, "up", "-d")
        assert up.returncode == 0, f"stand up failed: {up.stderr[-500:]}"

        cid = _compose(project, workdir, "ps", "-q", "gateway").stdout.strip().splitlines()
        assert cid, "gateway-контейнер не найден в проекте стенда"
        assert _wait_running(cid[0]), "gateway не перешёл в running"

        # --- egress: из gateway обязано приходить что-то из внешнего мира.
        # BusyBox wget выходит с ненулевым кодом и на 401/404 — доказательством
        # служит сама строка HTTP-ответа (код != 000), а не код возврата.
        probe = _run(["docker", "exec", cid[0], "sh", "-c",
                      "wget -S -O /dev/null https://discord.com/api/v10/gateway 2>&1; "
                      f"echo rc=$?"])
        out = probe.stdout + probe.stderr
        codes = re.findall(r"HTTP/[\d.]+ (\d{3})", out)
        egress_http = any(c not in ("000",) for c in codes)
        if not egress_http:
            # второй носитель доказательства: резолв внешнего имени через
            # публичный DNS требует NAT-маршрута наружу
            dns = _run(["docker", "exec", cid[0], "sh", "-c",
                        "nslookup discord.com 8.8.8.8 2>&1; echo rc=$?"])
            dns_out = dns.stdout + dns.stderr
            egress_http = re.search(r"Name:\s*\S+", dns_out) is not None
        assert egress_http, (
            "нет доказательств egress из gateway (ни HTTP-ответа, ни внешнего "
            f"резолва); ответ wget: {out[-400:]!r}"
        )

        # --- infra наружу не публикуется
        ps = _run(["docker", "ps", "-a", "--format", "json",
                   "--filter", f"label=com.docker.compose.project={project}"])
        recs = _json_records(ps.stdout)
        assert recs, f"в проекте стенда никого не видно: {ps.stdout[:200]!r}"
        for svc in ("mongo", "nats"):
            rec = _pick(recs, svc)
            assert not _has_published_ports(rec), f"{svc}: опубликованные порты наружу (P07)"

        # --- и не состоит в egress-сети (по id контейнеров, формы json у docker
        # меняются: Name/Names и Id/ID → сравниваем префиксы id и имена)
        egress_ids = _network_members(f"{project}-egress")
        data_ids = _network_members(f"{project}-data")
        for svc in ("mongo", "nats"):
            rec = _pick(recs, svc)
            assert _rec_id(rec)[:12] not in egress_ids, f"{svc} в egress-сети (V26-17)"
        gw = _compose(project, workdir, "ps", "-q", "gateway").stdout.strip()
        assert _short(gw) in data_ids, "gateway не в dsbot-data"
        assert _short(gw) in egress_ids, "gateway не в dsbot-egress (V26-17)"
        assert _inspect_bool(f"{project}-data", "Internal") is True, "dsbot-data не internal"
        assert _inspect_bool(f"{project}-egress", "Internal") is False, "dsbot-egress internal (V26-17)"
    finally:
        _teardown(project, workdir, networks, volumes)


# --------------------------------------------- 2. V26-16: единый env в контейнере


def test_v2616_live_single_env_container_side(tmp_path: Path) -> None:
    """Только `compose config` (никакого up, без мутаций).

    Факт живого прогона (compose v5.5.1): при `config --format json` ключа
    env_file у сервиса нет вовсе, а выбранный --env-file файл ЦЕЛИКОМ схлопнут
    в environment сервиса. Поэтому контейнерная сторона V26-16 доказывается по
    ЗНАЧЕНИЯМ: у каждого бота в схлопнутом environment маркер выбранного файла
    B (не A), соседний с compose.yml .env (маркер DOT) не подхвачен НИ ОДНИМ
    ботом, web env_file не получил, а WEB_PUBLIC_URL интерполирован из B."""
    project = _project()
    workdir = _stand_dir(tmp_path)
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "deploy" / "staging" / "compose.staging.yml",
        workdir / "compose.yml",
    )
    # синтетические маркеры (не секреты) — могут появляться в assert
    marker_a, marker_b, marker_dot = "STANDA-4f1b", "STANDB-8a3c", "MARKERDOT-9c52"
    env_a = _write_env(tmp_path, "staging", name="envA.env",
                       EVENT_SIGNING_SECRET=marker_a,
                       WEB_PUBLIC_URL=f"http://127.0.0.1:8090/{marker_a}")
    env_b = _write_env(tmp_path, "staging", name="envB.env",
                       EVENT_SIGNING_SECRET=marker_b,
                       WEB_PUBLIC_URL=f"http://127.0.0.1:8090/{marker_b}")
    # второй (соседний) env-файл: compose обязан его ИГНОРИРОВАТЬ при явном
    # --env-file и env_file из DSBOT_ENV_FILE — это и есть «нет второго источника»
    (workdir / ".env").write_text(f"EVENT_SIGNING_SECRET={marker_dot}\n",
                                  encoding="utf-8", newline="\n")

    bots = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")
    networks = [f"{project}_dsbot-data", f"{project}_dsbot-egress"]
    try:
        # единый источник: один и тот же путь — и для интерполяции (--env-file),
        # и для контейнеров (${DSBOT_ENV_FILE})
        # --profile controlplane: иначе сервис под профилем не попадает в рендер
        # (compose config исключает непрофилированные по умолчанию сервисы)
        proc = _compose(project, workdir, "--profile", "controlplane",
                        "--env-file", str(env_b), "config",
                        "--format", "json",
                        env={**os.environ, "DSBOT_ENV_FILE": str(env_b)})
        assert proc.returncode == 0, f"compose config failed: {proc.stderr[-500:]}"
        cfg = json.loads(proc.stdout)
        services = cfg["services"]

        def env_files(name: str) -> list[str]:
            raw = services[name].get("env_file") or []
            if isinstance(raw, (str, dict)):
                raw = [raw]
            return [str(e.get("path") or e.get("source")) if isinstance(e, dict) else str(e)
                    for e in raw]

        def environment(name: str) -> dict[str, str]:
            raw = services[name].get("environment") or {}
            if isinstance(raw, list):
                raw = dict(item.split("=", 1) for item in raw
                           if isinstance(item, str) and "=" in item)
            return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}

        # compose v5 env_file в рендере не отдаёт — путь сверять нечем
        # (assert env_files(name)==1 убран); проверяем вливание содержимого
        for name in bots:
            merged = environment(name)
            assert merged.get("EVENT_SIGNING_SECRET") == marker_b, (
                f"{name}: container env не из выбранного env-файла B (V26-16)")
            assert marker_a not in merged.values(), f"{name}: env из невыбранного файла A"
            assert marker_dot not in merged.values(), (
                f"{name}: подхвачен соседний с compose.yml .env (второй источник)")
        assert not env_files("web"), "web получил env_file (R26-06 п.5)"
        web_env = environment("web")
        public_url = str(web_env.get("WEB_PUBLIC_URL") or "")
        # значения маркеров в сообщение не попадают — только булевы результаты
        assert marker_b in public_url, "web интерполирован НЕ из выбранного env-файла"
        assert marker_a not in public_url, "в рендер web попал маркер невыбранного env-файла"
        assert marker_dot not in web_env.values(), "web подхватил соседний .env"
    finally:
        _teardown(project, workdir, networks, [])


# --------------------------- 3. V26-14/V26-15: тома переживают повторный up/down


def test_v2614_live_named_volume_survives_redeploy(tmp_path: Path) -> None:
    """Жизнеспособность данных при повторном выкате (e2e-часть V26-15):
    up → флаг в volume → down БЕЗ -v → том и флаг целы → повторный up → том тот
    же (данные видны) → down -v → том снесён. Чистым docker compose API:
    deploy.sh покрыт unit flow-тестами, здесь важен именно цикл томов."""
    project = _project()
    media_vol = f"{project}-media"
    cfg = {
        "name": project,
        "services": {
            "gateway": {
                "image": NATS_IMAGE,
                "entrypoint": ["/bin/sh", "-c"],
                "command": ["sleep infinity"],
                "volumes": ["media:/data/media"],
            },
        },
        "volumes": {"media": {"name": media_vol}},
    }
    workdir = _write_stand(tmp_path, cfg)
    try:
        _ensure_images(NATS_IMAGE)
        up = _compose(project, workdir, "up", "-d")
        assert up.returncode == 0, f"first up failed: {up.stderr[-500:]}"
        assert _volume_exists(media_vol), "том не создан после up"

        def touch() -> subprocess.CompletedProcess:
            return _run(["docker", "run", "--rm", "-v", f"{media_vol}:/m",
                         "--entrypoint", "/bin/sh", NATS_IMAGE, "-c",
                         f"touch /m/{FLAG} && ls /m"])

        def read_flag() -> str:
            got = _run(["docker", "run", "--rm", "-v", f"{media_vol}:/m",
                        "--entrypoint", "/bin/sh", NATS_IMAGE, "-c",
                        f"test -f /m/{FLAG} && echo PRESENT || echo ABSENT"])
            return (got.stdout + got.stderr).strip()

        assert touch().returncode == 0, "не удалось записать флаг в volume"

        # down без -v: контейнеры/сети уходят, том обязан остаться
        down = _compose(project, workdir, "down", "--timeout", "20")
        assert down.returncode == 0, f"down failed: {down.stderr[-500:]}"
        assert _volume_exists(media_vol), "down без -v удалил data volume"
        assert read_flag() == "PRESENT", "после down без -v данные в volume пропали"

        # повторный up: идемпотентен и НЕ пересоздаёт том
        again = _compose(project, workdir, "up", "-d")
        assert again.returncode == 0, f"second up failed: {again.stderr[-500:]}"
        assert _volume_exists(media_vol), "повторный up потерял том"
        cid = _compose(project, workdir, "ps", "-q", "gateway").stdout.strip().splitlines()
        assert cid and _wait_running(cid[0]), "после повторного up gateway не running"
        # данные видны из уже контейнера проекта (монт того же тома)
        inside = _run(["docker", "exec", cid[0], "sh", "-c", f"test -f /data/media/{FLAG} && echo PRESENT || echo ABSENT"])
        assert inside.stdout.strip() == "PRESENT", "повторный up не подмонтовал прежний том"

        # down -v — том сносится (иначе ретенция/переезд неуправляемы)
        purge = _compose(project, workdir, "down", "-v", "--timeout", "20")
        assert purge.returncode == 0, f"down -v failed: {purge.stderr[-500:]}"
        assert not _volume_exists(media_vol), "down -v оставил volume"
    finally:
        # сеть в этом стенде не объявлена явно → compose создаёт <project>_default
        _teardown(project, workdir, [f"{project}_default"], [media_vol])


def _volume_exists(name: str) -> bool:
    got = _run(["docker", "volume", "ls", "--quiet", "--filter", f"name={name}"])
    return name in [ln.strip() for ln in got.stdout.splitlines() if ln.strip()]
