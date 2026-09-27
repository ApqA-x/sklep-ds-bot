"""T01.2: guard интеграционного стенда — fail-closed на прод-ресурсы.

Проверка в коде (а не «только переменная shell»: её можно забыть/подставить
чужую): стенд — это порт из allowlist стендовых и БД с явным тестовым
именем. Любое совпадение с известными прод/dev-ресурсами — AssertionError,
тест не выполняется, а падает.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Разрешённые стендовые порты (R26-07): 27099 — общий стенд без auth
# (docker-compose.test.yml в wt-dsbot), 27098 — одноразовый auth-стенд
# mongod --auth (deploy/scripts/r2607_auth_stand.sh + tests/test_mongo_auth_stand.py).
ALLOWED_TEST_PORTS = {27099, 27098}
TEST_MONGO_HOSTS = {"127.0.0.1", "localhost"}

# известные НЕ-стендовые инстансы: 27017 — прод Windows-mongod, 27018 — dev-стенд веба
PRODUCTION_PORTS = {27017, 27018}
# рабочие БД приложений (prod MONGO_DB, служебные имена Mongo)
PRODUCTION_DB_NAMES = {"voice_tracker", "admin", "local", "config"}

DEFAULT_DB_RE = re.compile(r"^voice_tracker_t\w+_[0-9a-f]{6,}$")


def guard_mongo_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.hostname not in TEST_MONGO_HOSTS:
        raise AssertionError(f"stand guard: хост {parsed.hostname!r} не loopback-стенд {sorted(TEST_MONGO_HOSTS)}")
    port = parsed.port or 27017
    if port in PRODUCTION_PORTS:
        raise AssertionError(f"stand guard: порт {port} — известный прод/dev инстанс, тестам сюда нельзя")
    if port not in ALLOWED_TEST_PORTS:
        raise AssertionError(
            f"stand guard: порт {port} не в allowlist стендовых {sorted(ALLOWED_TEST_PORTS)}")


def guard_db_name(name: str, pattern: re.Pattern[str] = DEFAULT_DB_RE) -> None:
    if name in PRODUCTION_DB_NAMES:
        raise AssertionError(f"stand guard: имя БД {name!r} совпадает с рабочей/прод БД")
    if not pattern.match(name):
        raise AssertionError(f"stand guard: имя БД {name!r} не соответствует тестовому шаблону {pattern.pattern!r}")
