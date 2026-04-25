from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from voice_tracker import appcommands, commands as command_module


PLAN_PATH = Path(__file__).resolve().parents[1] / "docs" / "archive" / "command-rewrite-python-migration-plan.md"


def _require_callable(*names: str):
    for name in names:
        if hasattr(command_module, name):
            symbol = getattr(command_module, name)
            assert callable(symbol), f"expected callable symbol {name!r}"
            return symbol
    raise AssertionError(f"missing expected symbol: one of {names!r}")


def _command_payload(command: Any) -> dict[str, Any]:
    payload = appcommands.command_to_dict(command)
    assert isinstance(payload, dict), f"expected dict payload, got {type(payload)!r}"
    return payload


def _option_by_name(options: list[Any], name: str) -> dict[str, Any]:
    for option in options:
        if str(option.get("name", "")) == name:
            return dict(option)
    raise AssertionError(f"missing option {name!r}")


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item)
        return
    if is_dataclass(value):
        for item in fields(value):
            yield from _walk_strings(getattr(value, item.name))
        return
    if hasattr(value, "__dict__"):
        for item in vars(value).values():
            yield from _walk_strings(item)


def _joined_text(value: Any) -> str:
    return "\n".join(_walk_strings(value)).lower()


def _first_present(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    raise AssertionError(f"missing any of {names!r}")


def _invoke_builder(builder: Any, model: Any) -> Any:
    attempts = [
        ((model,), {}),
        ((), {"model": model}),
        ((), {"page": model}),
        ((), {"payload": model}),
        ((), {"data": model}),
    ]
    last_error: Exception | None = None
    for args, kwargs in attempts:
        try:
            return builder(*args, **kwargs)
        except TypeError as exc:
            last_error = exc
    raise AssertionError(f"unable to call {builder!r} with any supported model shape") from last_error


def test_plan_documents_message_count_exclusion_for_userinfo() -> None:
    text = PLAN_PATH.read_text(encoding="utf-8")
    assert "/userinfo" in text
    assert "omit message count from the MVP" in text


def test_userinfo_command_requires_member_option_and_has_no_message_count_labels() -> None:
    builder = _require_callable("userinfo_application_command", "UserInfoApplicationCommand")
    payload = _command_payload(builder())

    assert payload["name"] == "userinfo"
    user_option = _option_by_name(payload.get("options", []), "user")
    assert user_option.get("required") is True
    assert _first_present(user_option, "type") in {6, "user", "member", "guild_member"}

    payload_text = _joined_text(payload)
    assert "message count" not in payload_text
    assert "message_count" not in payload_text


def test_userinfo_command_registers_only_canonical_root_route() -> None:
    routes = command_module.registered_voice_command_routes()

    assert ("userinfo", "") in routes
    assert ("userinfo", "user") not in routes


def test_dashboard_page_builder_limits_rows_and_hides_hidden_channel_details() -> None:
    builder = _require_callable("build_dashboard_page", "dashboard_page_builder", "BuildDashboardPage")
    model = SimpleNamespace(
        rows=[
            SimpleNamespace(
                rank=index,
                display_name=("Alice @everyone" if index == 1 else f"Member {index}"),
                total_voice_time=f"{index}h",
                hidden_channel_ids=["secret-voice"] if index == 1 else [],
            )
            for index in range(1, 12)
        ],
        page=1,
        page_size=10,
        viewer=SimpleNamespace(id="viewer-1", is_admin=False),
    )

    page = _invoke_builder(builder, model)
    rows = _first_present(page, "rows", "entries", "fields")
    assert len(rows) == 10

    first_row = rows[0]
    assert _first_present(first_row, "rank") == 1
    assert str(_first_present(first_row, "display_name")).startswith("Alice")
    assert "secret-voice" not in _joined_text(page)
    assert "@everyone" not in _joined_text(page)
    assert "<@" not in _joined_text(page)


def test_userinfo_page_builder_excludes_hidden_details_and_unsafe_mentions() -> None:
    builder = _require_callable("build_userinfo_page", "userinfo_page_builder", "BuildUserInfoPage")
    model = SimpleNamespace(
        user=SimpleNamespace(id="u1", display_name="Alice @here", avatar_url="https://example.invalid/avatar.png"),
        roles=["@everyone", "Helpers", "<@1234567890>"],
        total_voice_time="2h0m0s",
        hidden_channel_ids=["secret-voice"],
        message_count=999,
        message_count_label="message count",
    )

    page = _invoke_builder(builder, model)
    text = _joined_text(page)

    assert "secret-voice" not in text
    assert "<#secret-voice>" not in text
    assert "@everyone" not in text
    assert "@here" not in text
    assert "<@1234567890>" not in text
    assert "message count" not in text
    assert "message_count" not in text
