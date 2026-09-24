"""T01.2: guard интеграционного стенда — fail-closed на прод-ресурсы.

Проверка в коде (а не «только переменная shell»: её можно забыть/подставить
чужую): стенд — это выделенный порт 27099 проекта dsbot-teststand и БД с
явным тестовым именем. Любое совпадение с известными прод/dev-ресурсами —
AssertionError, тест не выполняется, а падает.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# единственный разрешённый наряд для интеграционных тестов (docker-compose.test.yml в wt-dsbot)
TEST_MONGO_PORT = 27099
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
    if port != TEST_MONGO_PORT:
        raise AssertionError(f"stand guard: ожидался стендовый порт {TEST_MONGO_PORT}, получен {port}")


def guard_db_name(name: str, pattern: re.Pattern[str] = DEFAULT_DB_RE) -> None:
    if name in PRODUCTION_DB_NAMES:
        raise AssertionError(f"stand guard: имя БД {name!r} совпадает с рабочей/прод БД")
    if not pattern.match(name):
        raise AssertionError(f"stand guard: имя БД {name!r} не соответствует тестовому шаблону {pattern.pattern!r}")
