"""T12: надзор фоновых задач и признаки работоспособности сервиса.

Модель:
- Supervisor держит бесконечные задачи (sweep/heartbeat/…) живыми: необработанное
  исключение или неожиданное завершение coro наблюдаемы (структурированная строка
  лога + счётчики в снапшоте) и повторяются с экспоненциальным backoff и полным
  джиттером (иначе синхронно рестартанувшие сервисы долбят упавшую зависимость
  одним гребнем — restart storm).
- Heartbeat — периодический (НЕ event-driven) запись состояния в
  bot_runtime_heartbeats: «loop жив, зависимости подключены, критичные таски не
  умерли». Тишина пользовательских событий не делает сервис мёртвым (R03).
- shutdown: cancel + await с таймаутом, затем закрываем транспорт.

В JSON/логи наружу — только имена типов ошибок и счётчики; никогда токенов,
URI с credentials и содержимого сообщений (R06).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

HEARTBEAT_COLLECTION = "bot_runtime_heartbeats"
DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_UNHEALTHY_AFTER = 3
# задача продержалась дольше этого срока → считаем её восстановившейся
DEFAULT_HEALTHY_RUN_SECONDS = 120.0


def backoff_seconds(
    attempt: int,
    *,
    initial: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> float:
    """Экспоненциальный backoff с полным джиттером: uniform(base/2, base)."""
    base = min(cap, initial * (2 ** max(attempt - 1, 0)))
    return random.uniform(base / 2.0, base)


def _error_name(exc: BaseException) -> str:
    return type(exc).__name__


class TaskHandle:
    __slots__ = (
        "name",
        "critical",
        "restarts",
        "consecutive_failures",
        "last_error_type",
        "last_error_at",
        "last_tick_at",
        "running",
        "_task",
    )

    def __init__(self, name: str, critical: bool) -> None:
        self.name = name
        self.critical = critical
        self.restarts = 0
        self.consecutive_failures = 0
        self.last_error_type: str | None = None
        self.last_error_at: str | None = None
        self.last_tick_at: str | None = None
        self.running = False
        self._task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "critical": self.critical,
            "running": self.running,
            "restarts": self.restarts,
            "consecutiveFailures": self.consecutive_failures,
            "lastErrorType": self.last_error_type,
            "lastErrorAt": self.last_error_at,
            "lastTickAt": self.last_tick_at,
        }


class Supervisor:
    """Запускает «вечные» coro-фабрики и перезапускает их при падении.

    Критичная задача с >= unhealthy_after подряд идущими сбоями (каждый сбой
    короче healthy_run_seconds) переводит сервис в not-ready: процесс жив, но
    работать не может — внешний наблюдатель (heartbeat/healthcheck) это видит.
    """

    def __init__(
        self,
        *,
        initial_backoff: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
        unhealthy_after: int = DEFAULT_UNHEALTHY_AFTER,
        healthy_run_seconds: float = DEFAULT_HEALTHY_RUN_SECONDS,
    ) -> None:
        self._initial = initial_backoff
        self._cap = backoff_cap
        self._unhealthy_after = max(1, int(unhealthy_after))
        self._healthy_run = healthy_run_seconds
        self._handles: dict[str, TaskHandle] = {}
        self._closed = False

    def spawn(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        critical: bool = False,
    ) -> TaskHandle:
        if self._closed:
            raise RuntimeError("supervisor is shut down")
        if name in self._handles:
            raise RuntimeError(f"duplicate supervised task name {name!r}")
        handle = TaskHandle(name, critical)
        self._handles[name] = handle
        handle._task = asyncio.create_task(self._run(handle, factory), name=f"supervise:{name}")
        return handle

    def beat(self, name: str) -> None:
        """Явный признак прогресса из тела задачи: сбрасывает счётчик отказов."""
        handle = self._handles.get(name)
        if handle is not None:
            handle.consecutive_failures = 0
            handle.last_tick_at = datetime.now(UTC).isoformat(timespec="seconds")

    def _run(self, handle: TaskHandle, factory: Callable[[], Awaitable[None]]) -> Awaitable[None]:
        return self._run_loop(handle, factory)

    async def _run_loop(self, handle: TaskHandle, factory: Callable[[], Awaitable[None]]) -> None:
        attempt = 0
        while True:
            handle.running = True
            started = time.monotonic()
            try:
                await factory()
                # «вечная» задача завершилась сама — это аномалия, не успех
                raise RuntimeError("supervised loop returned")
            except asyncio.CancelledError:
                handle.running = False
                raise
            except Exception as exc:
                handle.running = False
                handle.restarts += 1
                attempt += 1
                if time.monotonic() - started >= self._healthy_run:
                    handle.consecutive_failures = 1
                else:
                    handle.consecutive_failures += 1
                handle.last_error_type = _error_name(exc)
                handle.last_error_at = datetime.now(UTC).isoformat(timespec="seconds")
                logger.warning(
                    "supervise event=task_failed task=%s critical=%s attempt=%s consecutive=%s error=%s",
                    handle.name,
                    handle.critical,
                    attempt,
                    handle.consecutive_failures,
                    handle.last_error_type,
                )
            if self._closed:
                return
            delay = backoff_seconds(attempt, initial=self._initial, cap=self._cap)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                handle.running = False
                raise

    def unhealthy_tasks(self) -> list[str]:
        return [
            h.name
            for h in self._handles.values()
            if h.critical and h.consecutive_failures >= self._unhealthy_after
        ]

    def snapshot(self) -> list[dict[str, Any]]:
        return [h.snapshot() for h in self._handles.values()]

    async def shutdown(self, timeout: float = 5.0) -> None:
        """cancel + await всех надзираемых задач; «увёртывающиеся» ждём до timeout."""
        self._closed = True
        tasks = [h._task for h in self._handles.values() if h._task is not None]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logger.warning("supervise event=shutdown_straggler task=%s", task.get_name())
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "supervise event=shutdown_error task=%s error=%s",
                    task.get_name(),
                    _error_name(exc),
                )


# ------------------------------------------------------------------ heartbeats


def nats_state(conn: Any) -> dict[str, Any]:
    """Состояние NATS-подключения по открытым атрибутам клиента (R04)."""

    def flag(attr: str) -> bool | None:
        try:
            return bool(getattr(conn, attr))
        except Exception:
            return None

    return {
        "connected": flag("is_connected"),
        "reconnecting": flag("is_reconnecting"),
        "closed": flag("is_closed"),
    }


def discord_state(client: Any) -> dict[str, Any]:
    """Состояние Discord-gateway: закрыт ли клиент и последний измеренный latency.

    Это НЕ пользовательские события: gateway сам держит heartbeat-пинг Discord."""
    out: dict[str, Any] = {}
    try:
        out["closed"] = bool(client.is_closed())
    except Exception:
        out["closed"] = None
    try:
        out["latencyMs"] = round(float(client.latency) * 1000.0, 1)
    except Exception:
        out["latencyMs"] = None
    return out


class Heartbeat:
    """Периодическая запись признаков жизни в Mongo (worker-scoped upsert).

    Сбои БД не убивают цикл: они считаются (dbErrors в самом heartbeat недоступен,
    когда БД лежит, — именно это и есть сигнал) и повторяются на следующем тике.
    """

    def __init__(
        self,
        db: Any,
        worker: str,
        supervisor: Supervisor,
        *,
        state_fn: Callable[[], dict[str, Any]] | None = None,
        interval_seconds: float = 15.0,
        task_name: str | None = None,
    ) -> None:
        self.db = db
        self.worker = worker
        self.supervisor = supervisor
        self.state_fn = state_fn
        self.interval = float(interval_seconds)
        self.task_name = task_name or f"{worker}-heartbeat"
        self._beats = 0
        self._db_errors = 0

    async def run(self) -> None:
        while True:
            self._beats += 1
            doc: dict[str, Any] = {
                "worker": self.worker,
                "updated_at": datetime.now(UTC),
                "loops": self.supervisor.snapshot(),
                "heartbeatErrors": self._db_errors,
            }
            if self.state_fn is not None:
                try:
                    doc["deps"] = self.state_fn()
                except Exception as exc:
                    doc["deps"] = {"error": _error_name(exc)}
            try:
                self.db[HEARTBEAT_COLLECTION].replace_one({"worker": self.worker}, doc, upsert=True)
                self._db_errors = 0
                self.supervisor.beat(self.task_name)
            except Exception as exc:
                self._db_errors += 1
                logger.warning(
                    "supervise event=heartbeat_write_failed worker=%s consecutive=%s error=%s",
                    self.worker,
                    self._db_errors,
                    _error_name(exc),
                )
            await asyncio.sleep(self.interval)


def attach(supervisor: Supervisor, heartbeat: Heartbeat) -> TaskHandle:
    """Заводит heartbeat под надзор (сам heartbeat критичным не считаем: он —
    наблюдатель, а не рабочий контур; но его гибель должна быть видна)."""
    return supervisor.spawn(heartbeat.task_name, heartbeat.run, critical=False)
