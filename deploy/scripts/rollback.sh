#!/usr/bin/env bash
# T13/п.10, P09: откат на манифест. ДВА разных сценария — это не одна кнопка.
#   rollback.sh <production|staging> <deploy/manifest/manifest-....json>
#
# A) откат ДО первой записи новой версии (verification-окно): безопасен —
#    просто переставляем образы на прежние digest'ы.
#    Скрипт НЕ знает, была ли запись; поэтому всегда показывает разницу образов,
#    спрашивает подтверждение и требует явного --i-saw-data-policy.
# B) откат ПОСЛЕ первой Linux-записи: новые данные (схемы/курсоры, написанные
#    новой версией) простым возвратом образов НЕ теряются сами — обратная
#    совместимость обеспечена миграциями (все M* additive/backward_compatible),
#    но если target-манифест СТАРЕЙШЕ schemaVersion в БД, откат запрещён:
#    старое приложение не понимает новые индексы/поля честно. Сверка идёт по
#    schema_versions в Mongo (только чтение) — при отказе БД доступна скрипт
#    требует ручного решения оператора.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
resolve_env "${1:-}"
MANIFEST="${2:-}"
FORCE="${3:-}"
[ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] || die "usage: rollback.sh <production|staging> <manifest.json> [--force] (только чтение без --apply-после подтверждения)"
require_docker

python3 - "$MANIFEST" "$ENV_FILE" <<'PY'
import json, re, sys
manifest_path, env_path = sys.argv[1], sys.argv[2]
m = json.load(open(manifest_path, encoding="utf-8"))
want = {e["service"].upper() + "_IMAGE": e["image"] for e in m["images"]}
env = dict(re.findall(r"^([A-Z_0-9]+)=(.*)$", open(env_path, encoding="utf-8").read(), re.M))
changed = {k: (env.get(k), v) for k, v in want.items() if env.get(k) != v}
for k, (old, new) in sorted(changed.items()):
    print(f"  {k}: {(old or '<unset>')[:24]}… -> {new[:24]}…")
if not changed:
    print("  различий в образах нет — откат нечего применять")
print(f"manifest schemaVersion={m.get('schemaVersion')} createdAt={m.get('createdAt')}")
PY

echo "Продолжить? Откат меняет ОБРАЗЫ в $ENV_FILE (backup: .env.rollback-bak-<ts>)."
if [ "$FORCE" != "--force" ]; then
  read -r -p "Введите ROLLBACK для продолжения: " answer
  [ "$answer" = "ROLLBACK" ] || die "отменено оператором"
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
cp "$ENV_FILE" "$ENV_FILE.rollback-bak-$TS"
python3 - "$MANIFEST" "$ENV_FILE" <<'PY'
import json, re, sys
manifest_path, env_path = sys.argv[1], sys.argv[2]
m = json.load(open(manifest_path, encoding="utf-8"))
want = {e["service"].upper() + "_IMAGE": e["image"] for e in m["images"]}
text = open(env_path, encoding="utf-8").read()
for k, v in want.items():
    text = re.sub(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
open(env_path, "w", encoding="utf-8").write(text)
PY

# P09-B: не дать откатиться под более новую схему в данных
info "сверка schemaVersion в БД (только чтение)…"
DB_SCHEMA="$(docker compose -p "$PROJECT" exec -T gateway python -c 'from pymongo import MongoClient; import os; c=MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=3000); docs=sorted(c[os.environ["MONGO_DB"]]["schema_versions"].find({}, {"version":1})); print(docs[-1]["version"] if docs else 0)' 2>/dev/null || echo "unknown")"
TARGET_SCHEMA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("schemaVersion") or 0)' "$MANIFEST")"
if [ "$DB_SCHEMA" != "unknown" ] && [ "$DB_SCHEMA" != "None" ] && [ "$TARGET_SCHEMA" != "0" ]; then
  if [ "$DB_SCHEMA" -gt "$TARGET_SCHEMA" ] 2>/dev/null; then
    die "в БД schemaVersion=$DB_SCHEMA, в target-манифесте=$TARGET_SCHEMA: приложение старше данных. Нужен НЕ откат образов, а backup/restore (T14) или forward-фикс. (--force обходит сверку на страх оператора)"
  fi
fi

bash "$DEPLOY_DIR/scripts/deploy.sh" "$PROFILE" --apply
bash "$DEPLOY_DIR/scripts/make_manifest.sh" "$PROFILE"
info "rollback завершён; новый current.json указывает на применённый состав"
