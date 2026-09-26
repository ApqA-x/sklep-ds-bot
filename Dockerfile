FROM python:3.12-slim

# T15/п.1: единый механизм locking — uv.lock из этого репо. --frozen запрещает
# тихую перегенерацию: несовпадение pyproject↔lock ломает сборку, а не молча
# ставит «примерно те же» версии. Только системный интерпретатор образа
# (UV_PYTHON_PREFERENCE=only-system), без second-download.
ENV UV_PYTHON_PREFERENCE=only-system \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /app

# слой кэша зависимостей: код меняется чаще, чем лок
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY voice_tracker ./voice_tracker
COPY services ./services
# проект (setuptools) — в venv /app/.venv
RUN uv sync --frozen
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# T15/п.9: контейнер не от root. compose задаёт `user:` из env (T13), но smoke
# прогон CI07 и ручной docker run получают безопасный дефолт; uid 10001 =
# DSBOT_UID по умолчанию в deploy/.env.example.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dsbot
USER 10001:10001

ARG SERVICE
ENV SERVICE=${SERVICE}

# T12: health = «свежий heartbeat этого воркера в Mongo» (loop жив + запись
# проходит), а не «порт отвечает». unhealthy сам по себе контейнер не рестартит —
# рестарт обеспечивает restart policy (см. docs/runbook-health.md); health-статус
# — это readiness-сигнал для оператора/монитора.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD python -m voice_tracker.healthcheck || exit 1

CMD ["sh", "-c", "python -m services.${SERVICE}"]
