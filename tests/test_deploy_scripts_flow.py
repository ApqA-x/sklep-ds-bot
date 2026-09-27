"""R26-06: flow-тесты deploy-скриптов на подставном docker (V26-15, V26-16).

V26-15: `deploy.sh <profile> --apply` обязан ВЫПОЛНЯТЬ pull → up -d → status;
без флага — не мутировать ничего. Раньше ветка применения сравнивала APPLY=1 с
"--apply" и была недостижима: любой запуск = dry-run с exit 0.
V26-16: единый источник env — путь, выбранный DSBOT_ENV_FILE, обязан доходить до
всех compose-вызовов (--env-file), включая каталоги с пробелами; второй env-файл
не должен попадать ни в аргументы, ни в вывод (значения секретов не печатаются).

Приём harness'а — как в flow-тестах restore (ветка fix/r26-08-restore):
подставной `docker` пишет аргументы каждого вызова в лог-файл (разделитель
\\x1e — безопасен для путей с пробелами); проверка ТОЛЬКО по логу вызовов,
не по выходному тексту. python3 валидаторам нужен настоящий (честнее фейка) —
preflight гоняет реальные validate_env.py/validate_compose.py на фикстурах
рендера. На Windows bash-мост через `wsl --exec` (пути C:/x → /mnt/c/x), на
Linux — прямой bash; без моста — skip.

Каталог фикстур harness (бинарники + логи) — НАТИВНЫЙ POSIX (на Windows это
/tmp внутри WSL, не tmp_path pytest'а): на drvfs (/mnt/c, /mnt/d) unix-права не
работают, os.chmod(0o755) не даёт exec-бит, и `command -v docker` подставной
бинарник не находит. env-файлы фикстур при этом остаются в tmp_path — их
скрипты только ЧИТАЮТ (чтение с drvfs доступно).

status.sh (п.6) здесь же: fail-closed readiness — отсутствие обязательного
сервиса в `compose ps` или недоступный /api/readyz обязаны давать exit != 0.

R26-07: фикстура рендера приведена к форме auth-релиза — mongod под `--auth`,
MONGO_URI ботов/web РОВНО из значений MONGO_BOT_URI/MONGO_WEB_URI выбранного
env-файла (их сверяет validate_compose, когда файл читается), одноразовый
mongo-bootstrap под профилем `bootstrap`.
Review R26-07 (blocker 2): в фикстуре есть и одноразовый schema-migrate под
профилем `migrate` (MONGO_URI ровно из MONGO_MIGRATION_URI, MONGO_DB, без
общего env_file): controlplane в рендере под профилем, поэтому validate_compose
обязан видеть в нём ОБА profile-сервиса — отсутствие runner'а он считает
пропажей единственной легальной точки `migrate up`/`status` (preflight → red).
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from test_deploy_artifacts import HEX64, _write_env

# test_deploy_artifacts при импорте добавляет deploy/scripts в sys.path — здесь
# пользуемся СОБСТВЕННЫМ парсером валидатора, чтобы фикстура и сверка в
# validate_compose не разъезжались в нормализации значений env-файла.
import validate_compose

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
WIN = os.name == "nt"
CALL_SEP = "\x1e"

# Минимальный PATH внутри WSL (фейковый bin подставляется ПЕРЕД ним).
WSL_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RENDERED_NAME = "rendered.json"
PS_NAME = "ps.jsonl"
LOG_NAME = "docker-calls.log"


def _p(path) -> str:
    """Путь для bash-моста: на Windows C:\\x → /mnt/c/x; на Linux — как есть."""
    s = str(path)
    if WIN:
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
        if m:
            s = "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return s


def _bash_cmd(script: str) -> list[str]:
    return ["wsl", "--exec", "bash", "-c", script] if WIN else ["bash", "-c", script]


def _make_posix_root() -> str:
    """Каталог, где unix-права настоящие: /tmp внутри WSL (Windows) или /tmp (Linux)."""
    if WIN:
        proc = subprocess.run(
            _bash_cmd('d=$(mktemp -d /tmp/r2606.XXXXXX) && chmod 700 "$d" && printf %s "$d"'),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        root = proc.stdout.strip()
        if proc.returncode != 0 or not root:
            raise RuntimeError(f"no WSL-native tmp dir for fake binaries: {proc.stderr[-400:]}")
        return root
    return tempfile.mkdtemp(prefix="r2606.", dir="/tmp")


def _remove_posix_root(root: str) -> None:
    # защита от пустого/чужого пути: удаляем только свой каталог из /tmp
    if not re.fullmatch(r"/tmp/r2606[.-][A-Za-z0-9]+", root or ""):
        return
    if WIN:
        subprocess.run(_bash_cmd(f"rm -rf {shlex.quote(root)}"), capture_output=True)
    else:
        shutil.rmtree(root, ignore_errors=True)


def _write_posix(path: str, text: str, *, executable: bool = False) -> None:
    """Файл внутри posix-корня harness. На Windows — байтами в stdin bash
    (text=True перевёл бы \\n в \\r\\n и сломала бы shebang фейкового docker)."""
    payload = text.encode("utf-8")
    if WIN:
        script = f"mkdir -p {shlex.quote(posixpath.dirname(path))} && cat > {shlex.quote(path)}"
        if executable:
            script += f" && chmod 755 {shlex.quote(path)}"
        proc = subprocess.run(_bash_cmd(script), input=payload, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"cannot write fixture {path}: {proc.stderr[-400:]!r}")
    else:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        if executable:
            os.chmod(p, 0o755)


def _read_posix(path: str) -> str:
    if WIN:
        proc = subprocess.run(
            _bash_cmd(f"cat {shlex.quote(path)}"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return proc.stdout if proc.returncode == 0 else ""
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _require_bridge() -> None:
    if WIN:
        if shutil.which("wsl") is None:
            pytest.skip("no bash bridge: wsl not found")
        probe = subprocess.run(
            ["wsl", "--exec", "bash", "-lc",
             "command -v bash python3 awk sed grep mktemp cat tr sort cut head >/dev/null"],
            capture_output=True,
        )
        if probe.returncode != 0:
            pytest.skip("bash bridge incomplete: wsl lacks bash/python3/coreutils")
    else:
        if shutil.which("bash") is None:
            pytest.skip("bash not found")


FAKE_DOCKER = """#!/usr/bin/env bash
# R26-06 flow harness: пишет аргументы каждого вызова в лог (разделитель \\036,
# перевод строки = конец вызова) и имитирует ответы: compose version/info/buildx
# (preflight), config --format json (рендер-фикстура), config --services,
# ps (фикстура/таблица), exec (ok), pull/up (успех; факт — только в логе).
set -u
log="${FAKE_DOCKER_LOG:?}"
line=""
for a in "$@"; do line="${line}${a}"$'\\036'; done
printf '%s\\n' "$line" >> "$log"
has() { local n="$1"; shift; local a; for a in "$@"; do [ "$a" = "$n" ] && return 0; done; return 1; }
_arch="$(uname -m)"; case "$_arch" in x86_64) _arch=amd64 ;; aarch64) _arch=arm64 ;; esac
if has buildx "$@"; then
  printf 'Name: fake/registry\\nPlatform: linux/%s\\n' "$_arch"
  exit 0
fi
if has config "$@"; then
  if [ "${FAKE_DOCKER_FAIL_CONFIG:-0}" = "1" ]; then
    echo "fake docker: compose config refused" >&2
    exit 17
  fi
  if has --services "$@"; then
    printf 'gateway\\ntracker\\nwriter\\ncommands\\nactivity\\nstalker\\nweb\\ncontrolplane\\nmongo\\nnats\\n'
  else
    cat "${FAKE_DOCKER_CONFIG_JSON:?}"
  fi
  exit 0
fi
if has info "$@"; then
  if has --format "$@"; then echo linux; fi
  exit 0
fi
if has ps "$@"; then
  if has --format "$@"; then cat "${FAKE_DOCKER_PS_JSON:?}"; else echo "NAME  STATUS"; fi
  exit 0
fi
if has exec "$@"; then echo ok; exit 0; fi
exit 0
"""

FAKE_CURL = """#!/usr/bin/env bash
set -u
if [ "${FAKE_CURL_FAIL:-0}" = "1" ]; then exit 22; fi
echo '{"status":"ready"}'
exit 0
"""


# R26-07: синтетические fallback'ы той же формы, что пишет _write_env("staging")
# — применяются, когда env-файл НЕ читается pytest-процессом (первичный рендер
# "PLACEHOLDER", WSL-путь /mnt/c/... из-под Windows python). Для нечитаемого
# --env-file валидатор не делает exact-match (env_values=None), а проверка на
# безпарольный URI требует лишь наличия credentials.
FIXTURE_MONGO_BOT_URI = "mongodb://dsbot_app:pw-app@mongo:27017/?authSource=voice_tracker_staging"
FIXTURE_MONGO_WEB_URI = "mongodb://dsbot_web:pw-web@mongo:27017/?authSource=voice_tracker_staging"
FIXTURE_MONGO_ADMIN_URI = "mongodb://dsbot_root:pw-root@mongo:27017/admin?authSource=admin"
# review R26-07 (blocker 2): credentials одноразового runner'а schema-migrate —
# той же рабочей БД, что бот/web (validate_env: authSource обязан равняться MONGO_DB)
FIXTURE_MONGO_MIGRATION_URI = (
    "mongodb://dsbot_migration:pw-mig@mongo:27017/?authSource=voice_tracker_staging")


def _fixture_env_values(envfile: str) -> dict[str, str]:
    """Значения выбранного env-файла фикстуры — парсером самого валидатора.
    envfile приходит POSIX-путём (bash-сторона harness'а), а функция исполняется
    в pytest-процессе: на Windows дополнительно пробуем реверс /mnt/c/x → C:/x
    (_write_env пишет в tmp_path — на Windows это C:/..., нативный путь
    читается оттуда). Не читается ничего — {} (синтетические fallback-константы)."""
    candidates = [envfile]
    m = re.match(r"^/mnt/([A-Za-z])/(.*)$", envfile)
    if m:
        candidates.append(f"{m.group(1).upper()}:/{m.group(2)}")
    for cand in candidates:
        values = validate_compose._parse_env_values(cand)
        if values:
            return values
    return {}


def _rendered_fixture(envfile: str) -> dict:
    """Форма вывода `docker compose config --format json` (env_file — список
    объектов {"path","service"}; валидатор принимает и list[str]) — валидный
    staging-релиз: egress, единый env, name=dsbot-staging; R26-07 — mongod под
    --auth, MONGO_URI ботов/web ровно из env-ключей MONGO_BOT_URI/MONGO_WEB_URI,
    одноразовый mongo-bootstrap под профилем bootstrap и одноразовый
    schema-migrate под профилем migrate (review R26-07, blocker 2)."""
    egress = ["dsbot-data", "dsbot-egress"]
    env_values = _fixture_env_values(envfile)
    bot_uri = env_values.get("MONGO_BOT_URI") or FIXTURE_MONGO_BOT_URI
    web_uri = env_values.get("MONGO_WEB_URI") or FIXTURE_MONGO_WEB_URI
    mig_uri = env_values.get("MONGO_MIGRATION_URI") or FIXTURE_MONGO_MIGRATION_URI

    def infra(name: str, image: str) -> dict:
        return {
            "name": name,
            "image": image,
            "restart": "unless-stopped",
            "networks": ["dsbot-data"],
            "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "3"}},
            "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "1G"}}},
        }

    def bot(name: str, *, nets: list[str] | None = None) -> dict:
        svc = infra(name, f"ghcr.io/apqa-x/sklep-ds-bot/{name}@sha256:{HEX64}")
        svc["networks"] = nets if nets is not None else ["dsbot-data"]
        svc["env_file"] = [{"path": envfile, "service": name}]
        # R26-07: MONGO_URI — фактическое значение MONGO_BOT_URI выбранного
        # env-файла (dsbot_app, без DDL-роли); controlplane тоже бот-уровня.
        svc["environment"] = {
            "MONGO_URI": bot_uri,
            "NATS_URL": "nats://nats:4222",
            "MEDIA_DIR": "/data/media",
            "SERVICE_NAME": name,
        }
        return svc

    services = {
        "mongo": infra("mongo", f"mongo@sha256:{'1' * 64}"),
        "nats": infra("nats", f"nats@sha256:{'2' * 64}"),
        "gateway": bot("gateway", nets=egress),
        "tracker": bot("tracker"),
        "writer": bot("writer"),
        "commands": bot("commands", nets=egress),
        "activity": bot("activity", nets=egress),
        "stalker": bot("stalker", nets=egress),
        "controlplane": bot("controlplane"),
    }
    services["mongo"]["command"] = ["--auth"]
    services["controlplane"]["profiles"] = ["controlplane"]
    services["gateway"]["volumes"] = [
        {"type": "volume", "source": "media", "target": "/data/media", "read_only": False}
    ]
    # R26-07: контракт одноразового job'а начальных прав — тот же, что в живом
    # рендере: профиль bootstrap, restart "no", localhost exception только через
    # 127.0.0.1 (сетевой namespace mongo-сервиса). В обычный `config --services`
    # (вывод FAKE_DOCKER ниже) не входит — непрофилированный up его не видит.
    boot = infra("mongo-bootstrap", f"ghcr.io/apqa-x/sklep-ds-bot/gateway@sha256:{HEX64}")
    boot["restart"] = "no"
    boot["profiles"] = ["bootstrap"]
    boot["network_mode"] = "service:mongo"
    boot["networks"] = []
    boot["env_file"] = [{"path": envfile, "service": "mongo-bootstrap"}]
    boot["environment"] = {
        "MONGO_URI": "mongodb://127.0.0.1:27017",
        "MONGO_DB": "voice_tracker_staging",
    }
    services["mongo-bootstrap"] = boot
    # Review R26-07 (blocker 2): одноразовый schema-раннер — единственная легальная
    # точка `migrate up`/`migrate status`. Тот же контракт, что в живом рендере:
    # профиль migrate, restart "no", dsbot-data БЕЗ network_mode (безпарольный
    # loopback — право только mongo-bootstrap), MONGO_URI ровно MONGO_MIGRATION_URI
    # + MONGO_DB и БЕЗ общего env_file (раннеру не нужны чужие секреты).
    mig = infra("schema-migrate", f"ghcr.io/apqa-x/sklep-ds-bot/gateway@sha256:{HEX64}")
    mig["restart"] = "no"
    mig["profiles"] = ["migrate"]
    mig["environment"] = {
        "MONGO_URI": mig_uri,
        "MONGO_DB": "voice_tracker_staging",
    }
    services["schema-migrate"] = mig
    services["web"] = infra("web", f"ghcr.io/apqa-x/sklep-ds-bot-web@sha256:{HEX64}")
    services["web"]["networks"] = egress
    # R26-07: web — фактическое значение MONGO_WEB_URI (dsbot_web, без DDL-роли)
    services["web"]["environment"] = {
        "MONGO_URI": web_uri,
        "MEDIA_DIR": "/data/media",
        "WEB_ENV": "production",
        "MONGO_DB": "voice_tracker_staging",
    }
    services["web"]["ports"] = [
        {"mode": "ingress", "host_ip": "127.0.0.1", "target": 8000, "published": "8090"}
    ]
    services["web"]["volumes"] = [
        {"type": "volume", "source": "media", "target": "/data/media", "read_only": True}
    ]
    return {
        "name": "dsbot-staging",
        "networks": {
            "dsbot-data": {"name": "r2606flow_dsbot-data", "internal": True},
            "dsbot-egress": {"name": "r2606flow_dsbot-egress", "internal": False},
        },
        "volumes": {
            "mongo-data": {"name": "dsbot-staging-mongo-data"},
            "media": {"name": "dsbot-staging-media"},
        },
        "services": services,
    }


def _ps_rows(ndjson: bool, include_web: bool = True, array_form: bool = False) -> str:
    services = ["gateway", "tracker", "writer", "commands", "activity", "stalker", "mongo", "nats"]
    if include_web:
        services.append("web")
    rows = [
        {"Service": s, "Container": f"{s}-1", "Image": f"x@sha256:{HEX64}",
         "State": "running", "Health": "healthy", "ExitCode": "", "Name": f"r2606-{s}-1",
         "Publishers": []}
        for s in services
    ]
    if array_form:
        return json.dumps(rows)
    return "".join(json.dumps(r) + "\n" for r in rows)


class Harness:
    """Фейковый docker/curl и логи фикстур — в нативном POSIX-каталоге (root),
    а не в tmp_path: см. модульный docstring про exec-бит на drvfs. env-файлы
    тестов остаются в tmp_path (Path), bash читает их по /mnt/c-пути."""

    def __init__(self, tmp: Path, root: str) -> None:
        _require_bridge()
        self.tmp = tmp
        self.root = root
        self.bindir = f"{root}/bin"
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
            _write_posix(f"{self.bindir}/{name}", body, executable=True)
        self.log = f"{root}/{LOG_NAME}"
        self.rendered = f"{root}/{RENDERED_NAME}"
        self.ps = f"{root}/{PS_NAME}"
        _write_posix(self.rendered, json.dumps(_rendered_fixture("PLACEHOLDER")))
        _write_posix(self.ps, _ps_rows(ndjson=True))

    def set_rendered_envfile(self, envfile: str) -> None:
        _write_posix(self.rendered, json.dumps(_rendered_fixture(envfile)))

    def run(self, script: str, args: list[str], *, envfile: Path | None = None,
            fail_config: bool = False, curl_fail: bool = False,
            ps_rows: str | None = None) -> subprocess.CompletedProcess:
        self.set_rendered_envfile(_p(envfile) if envfile else "UNUSED")
        if ps_rows is not None:
            _write_posix(self.ps, ps_rows)
        env = os.environ.copy()
        base_path = WSL_BASE_PATH if WIN else env.get("PATH", "")
        # каталог с фейками — ПЕРВЫМ, иначе скрипт увидит настоящий docker хоста
        env["PATH"] = f"{self.bindir}:{base_path}"
        env["FAKE_DOCKER_LOG"] = self.log
        env["FAKE_DOCKER_CONFIG_JSON"] = self.rendered
        env["FAKE_DOCKER_PS_JSON"] = self.ps
        if fail_config:
            env["FAKE_DOCKER_FAIL_CONFIG"] = "1"
        if curl_fail:
            env["FAKE_CURL_FAIL"] = "1"
        if envfile is not None:
            env["DSBOT_ENV_FILE"] = _p(envfile)
        # PATH достраиваем внутри bash явно (а не только переменной окружения):
        # так не зависит от того, как wsl.exe транслирует Windows PATH в сессию,
        # и при этом сохраняются пути пользователя (python3 из pyenv/venv).
        # FAKE_*/DSBOT_ENV_FILE — так же инлайном в bash-строку: wsl.exe НЕ
        # пробрасывает произвольные Windows env-переменные в сессию (фейковый
        # docker дохал на "${FAKE_DOCKER_LOG:?}" ещё до вывода), env= выше
        # остаётся только для Linux-ветки и безвреден для wsl.exe.
        quoted = " ".join(shlex.quote(x) for x in (_p(DEPLOY / "scripts" / script), *args))
        exports = [f"export FAKE_DOCKER_LOG={shlex.quote(self.log)}",
                   f"FAKE_DOCKER_CONFIG_JSON={shlex.quote(self.rendered)}",
                   f"FAKE_DOCKER_PS_JSON={shlex.quote(self.ps)};"]
        if fail_config:
            exports.append("export FAKE_DOCKER_FAIL_CONFIG=1;")
        if curl_fail:
            exports.append("export FAKE_CURL_FAIL=1;")
        if envfile is not None:
            exports.append(f"export DSBOT_ENV_FILE={shlex.quote(_p(envfile))};")
        inner = (" ".join(exports)
                 + f' PATH={shlex.quote(self.bindir)}:{WSL_BASE_PATH}:"$PATH";'
                 f" export PATH; exec bash {quoted}")
        cmd = (["wsl", "--exec"] if WIN else []) + ["bash", "-c", inner]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, cwd=str(self.tmp))

    def calls(self) -> list[list[str]]:
        out: list[list[str]] = []
        for line in _read_posix(self.log).split("\n"):
            if not line:
                continue
            parts = line.split(CALL_SEP)
            if parts and parts[-1] == "":
                parts.pop()
            out.append(parts)
        return out


@pytest.fixture()
def h(tmp_path: Path) -> Iterator[Harness]:
    _require_bridge()
    root = _make_posix_root()
    try:
        yield Harness(tmp_path, root)
    finally:
        _remove_posix_root(root)


def _compose_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "compose" in c]


def _has(c: list[str], *sub: str) -> bool:
    return all(x in c for x in sub)


def _pull_calls(h: Harness) -> list[list[str]]:
    return [c for c in _compose_calls(h.calls()) if _has(c, "pull")]


def _up_calls(h: Harness) -> list[list[str]]:
    return [c for c in _compose_calls(h.calls()) if _has(c, "up", "-d")]


def _env_file_values(h: Harness) -> list[str]:
    values = []
    for c in _compose_calls(h.calls()):
        if "--env-file" in c:
            values.append(c[c.index("--env-file") + 1])
    return values


# ------------------------------------------------------------------ V26-15


def test_v2615_dry_run_without_flag_makes_no_mutating_calls(h: Harness, tmp_path: Path) -> None:
    """Без --apply: exit 0, в логе docker нет НИ ОДНОГО compose pull/up
    (только чтения preflight/plan: config)."""
    env = _write_env(tmp_path, "staging")
    proc = h.run("deploy.sh", ["staging"], envfile=env)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert _pull_calls(h) == []
    assert _up_calls(h) == []
    assert any(_has(c, "config") for c in _compose_calls(h.calls()))  # preflight жил


def test_v2615_apply_performs_exactly_one_pull_one_up_then_status(h: Harness, tmp_path: Path) -> None:
    """V26-15: с --apply выполняются ровно одна `compose pull --quiet` и один
    `compose up -d --remove-orphans`, затем ps из status.sh; порядок по логу."""
    env = _write_env(tmp_path, "staging")
    proc = h.run("deploy.sh", ["staging", "--apply"], envfile=env)
    assert proc.returncode == 0, proc.stderr[-800:]
    pulls, ups = _pull_calls(h), _up_calls(h)
    assert len(pulls) == 1 and _has(pulls[0], "pull", "--quiet"), pulls
    assert len(ups) == 1 and _has(ups[0], "up", "-d", "--remove-orphans"), ups
    calls = h.calls()
    i_pull = next(i for i, c in enumerate(calls) if _has(c, "pull"))
    i_up = next(i for i, c in enumerate(calls) if _has(c, "up", "-d"))
    i_ps = next((i for i, c in enumerate(calls) if _has(c, "ps")), None)
    assert i_ps is not None and i_pull < i_up < i_ps  # status.sh после up


def test_v2615_preflight_failure_blocks_every_mutation(h: Harness, tmp_path: Path) -> None:
    """Сбой `compose config` в preflight → deploy.sh --apply с ненулевым кодом
    и НОЛЬ pull/up в логе (dry-run-«успех» раньше маскировал и это)."""
    env = _write_env(tmp_path, "staging")
    proc = h.run("deploy.sh", ["staging", "--apply"], envfile=env, fail_config=True)
    assert proc.returncode != 0
    assert _pull_calls(h) == []
    assert _up_calls(h) == []


def test_v2615_extra_argument_rejected(h: Harness, tmp_path: Path) -> None:
    env = _write_env(tmp_path, "staging")
    proc = h.run("deploy.sh", ["staging", "--apply", "extra"], envfile=env)
    assert proc.returncode != 0
    assert _pull_calls(h) == []
    assert _up_calls(h) == []


def test_v2616_missing_env_file_fails_before_any_docker(h: Harness, tmp_path: Path) -> None:
    """deploy.sh обязан die-нуть на отсутствующем env ДО preflight/docker
    (dry-run staging без env не должен «успешно» доходить до compose)."""
    missing = tmp_path / "definitely-missing" / ".env"
    proc = h.run("deploy.sh", ["staging"], envfile=missing)
    assert proc.returncode != 0
    assert h.calls() == []


# ------------------------------------------------------------------ V26-16


def test_v2616_selected_env_file_reaches_every_compose_call(h: Harness, tmp_path: Path) -> None:
    """Путь с пробелами + ДВА env-файла: в логе docker каждый compose-вызов
    получает --env-file ровно выбранного файла; путь второго (A) не встречается
    нигде, и ни stdout, ни stderr не печатают marker-значения секретов."""
    spaced_dir = tmp_path / "with space"
    spaced_dir.mkdir()
    marker_a, marker_b = "MARKERA-9e1f", "MARKERB-7c2d"
    env_b = _write_env(spaced_dir, "staging", name=".env", WEB_PUBLIC_URL=f"http://127.0.0.1:8090/{marker_b}")
    env_a = _write_env(tmp_path, "staging", name="other.env", WEB_PUBLIC_URL=f"http://127.0.0.1:8090/{marker_a}")
    assert " " in _p(env_b)
    proc = h.run("deploy.sh", ["staging"], envfile=env_b)
    assert proc.returncode == 0, proc.stderr[-800:]
    values = _env_file_values(h)
    assert values, "deploy.sh вообще не звал compose с --env-file"
    assert all(v == _p(env_b) for v in values), values
    joined = "\n".join("\x00".join(c) for c in h.calls())
    assert _p(env_a) not in joined and str(env_a) not in joined
    out = proc.stdout + proc.stderr
    assert marker_a not in out and marker_b not in out


# ------------------------------------------------------------ status.sh п.6


def test_status_failclosed_when_required_service_absent(h: Harness, tmp_path: Path) -> None:
    env = _write_env(tmp_path, "staging")
    proc = h.run("status.sh", ["staging"], envfile=env,
                 ps_rows=_ps_rows(ndjson=True, include_web=False))
    assert proc.returncode != 0
    assert "[FAIL] web" in proc.stdout


def test_status_failclosed_when_readyz_down(h: Harness, tmp_path: Path) -> None:
    env = _write_env(tmp_path, "staging")
    proc = h.run("status.sh", ["staging"], envfile=env, curl_fail=True)
    assert proc.returncode != 0
    assert "[FAIL] web /api/readyz" in proc.stdout


def test_status_green_with_array_form_ps_and_readyz_ok(h: Harness, tmp_path: Path) -> None:
    """Композиция вывода `ps --format json` (JSON-массив вместо NDJSON) обязана
    разбираться; полный зелёный набор + готовый web → exit 0."""
    env = _write_env(tmp_path, "staging")
    proc = h.run("status.sh", ["staging"], envfile=env,
                 ps_rows=_ps_rows(ndjson=True, include_web=True, array_form=True))
    assert proc.returncode == 0, proc.stdout[-800:] + proc.stderr[-400:]
    for s in ("gateway", "tracker", "writer", "commands", "activity", "stalker", "web"):
        assert f"[ok] {s}" in proc.stdout, s
