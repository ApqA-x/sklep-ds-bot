FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY voice_tracker ./voice_tracker
COPY services ./services

RUN pip install --no-cache-dir .

ARG SERVICE
ENV SERVICE=${SERVICE}

# T12: health = «свежий heartbeat этого воркера в Mongo» (loop жив + запись
# проходит), а не «порт отвечает». unhealthy сам по себе контейнер не рестартит —
# рестарт обеспечивает restart policy (см. docs/runbook-health.md); health-статус
# — это readiness-сигнал для оператора/монитора.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD python -m voice_tracker.healthcheck || exit 1

CMD ["sh", "-c", "python -m services.${SERVICE}"]
