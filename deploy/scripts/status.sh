#!/usr/bin/env bash
# T13 status: что запущено, готово ли, где смотреть логи. Только чтение.
# R26-06 (п.6): readiness — fail-closed. Обязательные сервисы (все, кроме
# controlplane, который опционален по ADR-0004) должны быть running и healthy
# (если healthcheck задан), а web обязан отвечать на /api/readyz. Любой сбой
# накапливается и даёт exit 1 в конце — «info-фуззи» больше не считается успехом.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
require_docker

REQUIRED="gateway tracker writer commands activity stalker web"
FAILED=""

compose ps

# `docker compose ps --format json` менял форму вывода между версиями
# (NDJSON-объекты / JSON-массив) — нормализуем через python3 (тот же деп,
# что уже требует preflight). Скрываем вывод docker от stdout: разбираем молча.
PS_JSON="$(compose ps --format json 2>/dev/null || true)"
ROWS="$(python3 - "$PS_JSON" <<'PY'
import json, sys
raw = sys.argv[1].strip()
rows = []
if raw:
    try:
        data = json.loads(raw)
        rows = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
for r in rows:
    if not isinstance(r, dict):
        continue
    svc = str(r.get("Service") or r.get("service") or "")
    state = str(r.get("State") or r.get("state") or "")
    health = str(r.get("Health") or r.get("health") or "")
    print(f"{svc}|{state}|{health}")
PY
)"

echo
info "readiness обязательных сервисов:"
for s in $REQUIRED; do
  row="$(awk -F'|' -v svc="$s" '$1 == svc { print; exit }' <<<"$ROWS")"
  if [ -z "$row" ]; then
    echo "[FAIL] $s: контейнер не найден (compose ps)"
    FAILED="$FAILED $s(not-found)"
    continue
  fi
  state="$(cut -d'|' -f2 <<<"$row")"
  health="$(cut -d'|' -f3 <<<"$row")"
  if [ "$state" != "running" ]; then
    echo "[FAIL] $s: state=$state"
    FAILED="$FAILED $s(state=$state)"
    continue
  fi
  # ""/"none" — healthcheck не задан (app-сервисы): достаточно running;
  # mongo/nats со healthcheck'ом обязаны быть healthy.
  if [ -n "$health" ] && [ "$health" != "none" ] && [ "$health" != "healthy" ]; then
    echo "[FAIL] $s: health=$health"
    FAILED="$FAILED $s(health=$health)"
    continue
  fi
  echo "[ok] $s: running${health:+ ($health)}"
done

# controlplane опционален (ADR-0004): запуск без явного профиля — тревожный сигнал.
if awk -F'|' '$1 == "controlplane" { found = 1 } END { exit !found }' <<<"$ROWS"; then
  info "ВНИМАНИЕ: controlplane запущен — по умолчанию его быть не должно (профиль controlplane)"
fi

echo
info "web readiness (/api/readyz):"
PORT="$(grep -Eo '^WEB_HOST_PORT=.*' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
PORT="${PORT:-8000}"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/readyz"; then
    echo
    echo "[ok] web /api/readyz"
  else
    echo "[FAIL] web /api/readyz недоступен"
    FAILED="$FAILED web(readyz)"
  fi
else
  if docker run --rm curlimages/curl:8.9.1 -fsS -m 5 "http://host.docker.internal:${PORT}/api/readyz"; then
    echo
    echo "[ok] web /api/readyz"
  else
    echo "[FAIL] web /api/readyz недоступен"
    FAILED="$FAILED web(readyz)"
  fi
fi

echo
info "heartbeat ботовых сервисов (признаки жизни; см. docs/runbook-health.md):"
for s in gateway tracker writer commands activity stalker; do
  if out="$(compose exec -T "$s" python -m voice_tracker.healthcheck --service "$s" 2>&1)"; then
    printf '%s\n' "$out" | sed "s/^/  [$s] /"
  else
    printf '%s\n' "$out" | sed "s/^/  [$s] /"
    echo "[FAIL] $s: heartbeat/exec завершился с ошибкой" >&2
    FAILED="$FAILED $s(heartbeat)"
  fi
done

BACKUP_DIR="$(sed -n 's/^BACKUP_DIR=//p' "$ENV_FILE" 2>/dev/null | head -1 || true)"
if [ -n "$BACKUP_DIR" ]; then
  echo
  info "свежесть точки восстановления (T14 B06):"
  "$DEPLOY_DIR/backup/backup_status.sh" "$PROFILE" || info "см. docs/runbook-backup.md"
fi

echo
info "логи: docker compose -p $PROJECT -f $COMPOSE_FILE logs --since 30m [--tail 200] <service>"

[ -z "$FAILED" ] || die "readiness failed:$FAILED"
info "STATUS OK — все обязательные сервисы подняты, web готов"
