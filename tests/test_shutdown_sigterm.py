"""R26-12b: SIGTERM должен доходить до python и будить drain-путь сервисов.

Дефект был в том, что `CMD ["sh", "-c", ...]` делал PID 1 = dash, который не
форвардит SIGTERM: `docker stop` убивал контейнер по grace-таймауту (exit=137),
и `finally: supervisor.shutdown(); bus.aclose(); mongo close()` не исполнялся
никогда. Держим четыре уровня инвариантов: форму CMD (анти-дрейф сборки), наличие
общего wait_for_shutdown внутри try-with-finally во всех 7 сервисах бота, саму
механику helper'а (просыпается по сигналу, снимает обработчики за собой) и
явное закрытие discord client в drain (r2: в discord.py 2.7.1 отмена
connect()/start() НЕ закрывает ни websocket, ни HTTP-сессию — только close()).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

import services.controlplane as controlplane
import services.gateway as gateway
from test_services_gateway import (
    FakeBus,
    FakeClient,
    FakeMongoClient,
    FakeNATS,
    FakeRepo,
    _noop,
    _yielding_noop,
)
from voice_tracker import runtime

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# ровно те 7 сервиса, что собираются из bot-образа (build-arg SERVICE)
BOT_SERVICES = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")

# из них discord client живут эти пять — они и обязаны закрывать его в drain
DISCORD_SERVICES = ("gateway", "activity", "commands", "stalker", "controlplane")

# os.kill(pid, SIGTERM) под Windows не доставляет сигнал, а убивает процесс
# через TerminateProcess — реальные POSIX-сигналы проверяем только на POSIX.
POSIX_SIGNALS = pytest.mark.skipif(
    os.name != "posix", reason="доставка SIGTERM через os.kill доступна только на POSIX"
)


def _container_cmd_argv() -> list[str]:
    """argv контейнерного CMD. CMD внутри HEALTHCHECK — не CMD контейнера."""
    lines = [line for line in DOCKERFILE.splitlines() if line.startswith("CMD")]
    assert len(lines) == 1, f"в Dockerfile должен быть ровно один контейнерный CMD: {lines}"
    payload = lines[0][len("CMD"):].strip()
    assert payload.startswith("["), f"CMD должен быть exec-form (JSON-массив): {lines[0]}"
    return json.loads(payload)


def test_t1_dockerfile_cmd_is_exec_form_python() -> None:
    """PID 1 = python: shell-обёртка клядёт SIGTERM себе и drain недостижим."""
    assert 'CMD ["sh"' not in DOCKERFILE, "в Dockerfile снова shell-обёртка CMD"
    argv = _container_cmd_argv()
    assert argv[0] == "python", f"PID 1 должен быть python, а не {argv[0]!r}"
    assert not argv[0].startswith("sh"), f"PID 1 не должен быть shell: {argv}"
    joined = " ".join(argv[1:])
    # сервис резолвится из env уже внутри python: ${SERVICE} в exec-form не раскроется
    assert "${SERVICE}" not in joined
    assert "run_module" in joined and "services." in joined and "SERVICE" in joined


@pytest.mark.parametrize("service", BOT_SERVICES)
def test_t3_every_service_awaits_shutdown_inside_drain_try(service: str) -> None:
    """Shutdown-путь подключён ко всем сервисам — и именно в try с finally-drain."""
    path = ROOT / "services" / f"{service}.py"
    source = path.read_text(encoding="utf-8")
    assert "wait_for_shutdown" in source, f"{path.name}: helper R26-12b не подключён"

    main = next(
        (
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
        ),
        None,
    )
    assert main is not None, f"{path.name}: async def main() не найден"
    assert any(_is_shutdown_await(node) for node in ast.walk(main)), (
        f"{path.name}: в main() нет await wait_for_shutdown(...)"
    )

    drains = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Try)
        and any(_is_shutdown_await(n) for stmt in node.body for n in ast.walk(stmt))
        and any(isinstance(n, ast.Await) for stmt in node.finalbody for n in ast.walk(stmt))
    ]
    assert drains, f"{path.name}: await wait_for_shutdown(...) вне try, чей finally дренирует"


def _is_shutdown_await(node: ast.AST) -> bool:
    if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return False
    return name == "wait_for_shutdown"


def _is_client_close_await(node: ast.AST) -> bool:
    """await client.close() — Name-получатель `client`, метод `close`."""
    if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "close"
        and isinstance(func.value, ast.Name)
        and func.value.id == "client"
    )


@pytest.mark.parametrize("service", DISCORD_SERVICES)
def test_t6_discord_services_close_client_in_drain(service: str) -> None:
    """R26-12b r2: drain обязан явно закрывать discord client.

    В discord.py 2.7.1 start()/connect() не имеют finally-close: отмена
    бессрочной работы по сигналу НЕ закрывает ни websocket, ни HTTP-сессию.
    Только отдельный Client.close() (эталон — activity.py). test_t3 проверял
    лишь «есть какой-то await в finally» — этого мало: здесь точечно ищем
    await client.close() в finally того же try, что ждёт wait_for_shutdown.
    """
    path = ROOT / "services" / f"{service}.py"
    source = path.read_text(encoding="utf-8")
    main = next(
        (
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
        ),
        None,
    )
    assert main is not None, f"{path.name}: async def main() не найден"

    drains = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Try)
        and any(_is_shutdown_await(n) for stmt in node.body for n in ast.walk(stmt))
    ]
    assert drains, f"{path.name}: нет try, чьё тело ждёт await wait_for_shutdown(...)"
    for try_node in drains:
        assert any(
            _is_client_close_await(n) for stmt in try_node.finalbody for n in ast.walk(stmt)
        ), f"{path.name}: в finally drain-try нет await client.close() — клиент утекает незакрытым"


@POSIX_SIGNALS
async def test_t2_wait_for_shutdown_returns_on_sigterm() -> None:
    """SIGTERM из `docker stop` будит ожидание и НЕ поднимает исключение."""
    waiter = asyncio.create_task(runtime.wait_for_shutdown())
    # один тик цикла: helper регистрирует хендлеры до первого await
    await asyncio.sleep(0)
    assert signal.getsignal(signal.SIGTERM) != signal.SIG_DFL, "хендлер SIGTERM не поставлен"

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(waiter, timeout=5) is None


@POSIX_SIGNALS
async def test_t2b_sigterm_cancels_work_so_drain_runs() -> None:
    """Бессрочная работа (client.connect()) по сигналу отменяется, а не ждёт grace-kill."""
    cancelled = asyncio.Event()

    async def eternal() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    waiter = asyncio.create_task(runtime.wait_for_shutdown(eternal()))
    await asyncio.sleep(0)
    assert signal.getsignal(signal.SIGTERM) != signal.SIG_DFL, "хендлер SIGTERM не поставлен"

    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(waiter, timeout=5) is None
    assert cancelled.is_set(), "работа не отменена — drain в finally сервиса не дождётся"


@POSIX_SIGNALS
async def test_t4_sigterm_disposition_rolled_back_after_repeated_calls() -> None:
    """Helper снимает свои обработчики: после возврата SIGTERM снова дефолтный."""
    original = signal.getsignal(signal.SIGTERM)
    try:
        # фиксируем базовую линию сам: базовый handler pytest'а тут ни при чём
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        for attempt in (1, 2):
            waiter = asyncio.create_task(runtime.wait_for_shutdown())
            await asyncio.sleep(0)
            assert signal.getsignal(signal.SIGTERM) != signal.SIG_DFL, f"pass {attempt}: не поставлен"

            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(waiter, timeout=5)
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, (
                f"pass {attempt}: helper оставил свой обработчик (течёт при повторных вызовах)"
            )
    finally:
        signal.signal(signal.SIGTERM, original)


async def test_t5_work_race_keeps_result_error_and_restores_handlers() -> None:
    """Гонка «connect() vs сигнал» без реальных сигналов (работает и под Windows).

    Результат и ошибка работы уходят наружу как раньше — штатный сбой connect()
    по-прежнему роняет сервис, а не молча считается остановкой.
    """
    signals = (signal.SIGTERM, signal.SIGINT)
    baseline = {sig: signal.getsignal(sig) for sig in signals}
    try:

        async def work() -> str:
            for sig, previous in baseline.items():
                assert signal.getsignal(sig) != previous, "на время ожидания хендлер не поставлен"
            return "connected"

        assert await runtime.wait_for_shutdown(work()) == "connected"
        for sig, previous in baseline.items():
            assert signal.getsignal(sig) == previous, f"{sig}: helper не снял обработчик"

        async def broken() -> None:
            raise RuntimeError("connect failed")

        with pytest.raises(RuntimeError, match="connect failed"):
            await runtime.wait_for_shutdown(broken())
        for sig, previous in baseline.items():
            assert signal.getsignal(sig) == previous, f"{sig}: helper не снял обработчик"
    finally:
        for sig, previous in baseline.items():
            signal.signal(sig, previous)


# ---------------------------------------------------------------------------
# R26-12b r2: drain реально закрывает discord client (поведение, не только AST).
# Fake'и ниже НЕ закрывают client из connect()/start() — как discord.py 2.7.1:
# флаг closed=True появляется только если main() в finally await'ит close().
# ---------------------------------------------------------------------------


async def _drive_gateway_drain(monkeypatch) -> FakeClient:
    """Прогон gateway.main() до конца drain-а с фейками (инфраструктура _boot_gateway).

    connect() резолвится сразу → wait_for_shutdown возвращается → finally
    исполняется целиком; возвращаем client, чтобы тест проверил флаг close()."""
    monkeypatch.setattr(gateway, "configure_logging", lambda _name: None)
    monkeypatch.setattr(gateway, "load_config", lambda: SimpleNamespace(
        discord_token="token",
        event_signing_secret="secret",
        discord_guild_id="123",
        mongo_uri="mongodb://example",
        mongo_db="db",
        nats_url="nats://example",
        event_max_age_seconds=3600,
        event_sweep_interval_seconds=15,
        event_max_deliver=8,
        media_dir="",
        media_min_free_bytes=0,
    ))
    monkeypatch.setattr(gateway, "require_event_signing_secret", lambda _secret: None)
    monkeypatch.setattr(gateway, "MongoClient", lambda _uri: FakeMongoClient("mongodb://example"))
    monkeypatch.setattr(gateway, "Repository", lambda _db: FakeRepo({}))
    monkeypatch.setattr(gateway, "NATS", FakeNATS)
    monkeypatch.setattr(gateway, "Bus", FakeBus)
    monkeypatch.setattr(gateway.discord, "Client", FakeClient)
    monkeypatch.setattr(gateway, "_deliver_pending", _noop)
    # заглушка sleep ОБЯЗАНА уступать цикл: без yield фоновые циклы голодают loop
    monkeypatch.setattr(gateway.asyncio, "sleep", _yielding_noop)

    FakeClient.instances.clear()
    await gateway.main()

    assert FakeClient.instances, "main() не создал discord client"
    return FakeClient.instances[0]


async def test_t7_gateway_main_closes_discord_client_in_drain(monkeypatch) -> None:
    client = await _drive_gateway_drain(monkeypatch)
    assert client.closed, (
        "gateway.main() вышел из drain без await client.close(): "
        "websocket и HTTP-сессия discord утекают (discord.py 2.7.1 сам не закрывает)"
    )


class _CPJournalCollection:
    def replace_one(self, *_args, **_kwargs) -> None:
        return None

    def update_one(self, *_args, **_kwargs) -> None:
        return None

    def insert_one(self, *_args, **_kwargs) -> None:
        return None

    def find_one(self, *_args, **_kwargs):
        return None


class _CPJournalDb:
    """db для ControlPlane: доступ и db[name], и db.<collection> (heartbeat-запись)."""

    def __init__(self) -> None:
        self._collections: dict[str, _CPJournalCollection] = {}

    def _col(self, name: str) -> _CPJournalCollection:
        return self._collections.setdefault(name, _CPJournalCollection())

    def __getitem__(self, name: str) -> _CPJournalCollection:
        return self._col(name)

    def __getattr__(self, name: str) -> _CPJournalCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._col(name)


class _CPMongoClient:
    def __init__(self, _uri: str) -> None:
        self.db = _CPJournalDb()
        self.closed = False

    def __getitem__(self, _name: str):
        return self.db

    def close(self) -> None:
        self.closed = True


class _CPRepository:
    def __init__(self, _db) -> None:
        return None

    def verify_startup(self) -> None:
        return None

    def ensure_indexes(self, _ctx) -> None:
        return None


class _CPNATS:
    def __init__(self) -> None:
        self.subscriptions: list[str] = []
        self.drained = False

    async def connect(self, _url: str) -> None:
        return None

    async def subscribe(self, subject: str, *, cb=None) -> None:
        self.subscriptions.append(subject)
        return None

    async def drain(self) -> None:
        self.drained = True

    async def close(self) -> None:
        return None


class _CPClient:
    """discord.Client для controlplane: start() НЕ закрывает client — как в 2.7.1."""

    tracked: list["_CPClient"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.closed = False
        self.guilds: list[object] = []
        _CPClient.tracked.append(self)

    def event(self, callback):
        setattr(self, callback.__name__, callback)
        return callback

    async def start(self, _token: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


async def _drive_controlplane_drain(monkeypatch):
    monkeypatch.setattr(controlplane, "configure_logging", lambda _name: None)
    monkeypatch.setattr(controlplane, "load_config", lambda: SimpleNamespace(
        discord_token="token",
        mongo_uri="mongodb://example",
        mongo_db="db",
        nats_url="nats://example",
        event_signing_secret="secret",
    ))
    monkeypatch.setattr(controlplane, "require_event_signing_secret", lambda secret: secret)
    monkeypatch.setattr(controlplane, "MongoClient", lambda _uri: _CPMongoClient("mongodb://example"))
    monkeypatch.setattr(controlplane, "Repository", _CPRepository)
    monkeypatch.setattr(controlplane, "NATS", _CPNATS)
    monkeypatch.setattr(controlplane.discord, "Client", _CPClient)
    monkeypatch.setattr(controlplane.asyncio, "sleep", _yielding_noop)

    _CPClient.tracked.clear()
    await controlplane.main()

    assert _CPClient.tracked, "main() не создал discord client"
    return _CPClient.tracked[0]


async def test_t8_controlplane_main_closes_discord_client_in_drain(monkeypatch) -> None:
    # SIGTERM не нужен: start() резолвится сразу, main() проходит try и попадает
    # в finally — ровно там, где до правки r2 client оставался незакрытым.
    client = await _drive_controlplane_drain(monkeypatch)
    assert client.closed, (
        "controlplane.main() вышел из drain без await client.close(): "
        "websocket и HTTP-сессия discord утекают (discord.py 2.7.1 сам не закрывает)"
    )
