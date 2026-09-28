"""R26-12b: SIGTERM должен доходить до python и будить drain-путь сервисов.

Дефект был в том, что `CMD ["sh", "-c", ...]` делал PID 1 = dash, который не
форвардит SIGTERM: `docker stop` убивал контейнер по grace-таймауту (exit=137),
и `finally: supervisor.shutdown(); bus.aclose(); mongo close()` не исполнялся
никогда. Держим три уровня инвариантов: форму CMD (анти-дрейф сборки), наличие
общего wait_for_shutdown внутри try-with-finally во всех 7 сервисах бота и
саму механику helper'а (просыпается по сигналу, снимает обработчики за собой).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import signal

import pytest

from voice_tracker import runtime

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# ровно те 7 сервиса, что собираются из bot-образа (build-arg SERVICE)
BOT_SERVICES = ("gateway", "tracker", "writer", "commands", "activity", "stalker", "controlplane")

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
