import pytest

from voice_tracker.runtime import load_config


def test_load_bot_admin_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "<@123>, 456\n<@!789>")

    cfg = load_config()

    assert cfg.bot_admin_user_ids == ["123", "456", "789"]


def test_load_canonicalizes_tracking_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACKING_MODE", "specific")
    monkeypatch.setenv("TRACKED_CHANNEL_IDS", "c2, c1, c2")

    cfg = load_config()

    assert cfg.tracking_mode == "all"
    assert cfg.tracked_channel_ids == []


def test_media_min_free_bytes_default_and_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEDIA_MIN_FREE_BYTES", raising=False)
    assert load_config().media_min_free_bytes == 2 * 1024 * 1024 * 1024

    # 0 — осознанное отключение проверки, не «мусорное» значение
    monkeypatch.setenv("MEDIA_MIN_FREE_BYTES", "0")
    assert load_config().media_min_free_bytes == 0

    monkeypatch.setenv("MEDIA_MIN_FREE_BYTES", "1048576")
    assert load_config().media_min_free_bytes == 1048576


def test_media_min_free_bytes_falls_back_on_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_MIN_FREE_BYTES", "two-gb")
    assert load_config().media_min_free_bytes == 2 * 1024 * 1024 * 1024

    monkeypatch.setenv("MEDIA_MIN_FREE_BYTES", "-5")
    assert load_config().media_min_free_bytes == 2 * 1024 * 1024 * 1024


def test_load_uses_defaults_when_env_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGO_URI", "")
    monkeypatch.setenv("MONGO_DB", "")
    monkeypatch.setenv("NATS_URL", "")

    cfg = load_config()

    assert cfg.mongo_uri == "mongodb://localhost:27017"
    assert cfg.mongo_db == "voice_tracker"
    assert cfg.nats_url == "nats://localhost:4222"


# R26-07 (DB03): режим работы со схемой на startup. Боевые роли Mongo не имеют DDL,
# поэтому verify-only обязан быть дефолтом, а bootstrap — только явным решением.
def test_schema_mode_defaults_to_verify() -> None:
    assert load_config({}).schema_mode == "verify"


def test_schema_mode_reads_bootstrap() -> None:
    assert load_config({"DSBOT_SCHEMA_MODE": "bootstrap"}).schema_mode == "bootstrap"
    # регистр и пробелы не превращают явное решение в опечатку
    assert load_config({"DSBOT_SCHEMA_MODE": " BOOTSTRAP "}).schema_mode == "bootstrap"
    assert load_config({"DSBOT_SCHEMA_MODE": "verify"}).schema_mode == "verify"


def test_schema_mode_rejects_unknown_value_without_leaking_it() -> None:
    junk = "bootstrap-please-mongodb://user:pass@host"

    with pytest.raises(ValueError) as excinfo:
        load_config({"DSBOT_SCHEMA_MODE": junk})

    message = str(excinfo.value)
    assert "DSBOT_SCHEMA_MODE" in message
    assert "verify" in message and "bootstrap" in message
    # значение не поднимается в текст ошибки: в env рядом лежат секреты
    assert junk not in message and "pass" not in message
