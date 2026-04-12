from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Callable


@dataclass(slots=True)
class CommandOption:
    type: int | str
    name: str
    description: str = ""
    required: bool = False
    choices: list[dict[str, Any]] = field(default_factory=list)
    channel_types: list[int | str] = field(default_factory=list)
    options: list["CommandOption"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "description": self.description,
        }
        if self.required:
            payload["required"] = True
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.channel_types:
            payload["channel_types"] = list(self.channel_types)
        if self.options:
            payload["options"] = [option.to_dict() for option in self.options]
        return payload


@dataclass(slots=True)
class ApplicationCommand:
    name: str
    description: str = ""
    options: list[CommandOption] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.options:
            payload["options"] = [option.to_dict() for option in self.options]
        return payload


command_catalog: Callable[[], list[Any]] = lambda: default_commands()


def register_commands(
    _ctx: Any,
    session: Any,
    app_id: str,
    guild_id: str,
    catalog: Callable[[], list[Any]] | None = None,
) -> Any:
    if session is None or not str(app_id).strip() or not str(guild_id).strip():
        raise ValueError("application id and guild id are required")
    commands = (catalog or command_catalog)()
    validate_commands(commands)
    overwrite = getattr(session, "application_command_bulk_overwrite", None)
    if callable(overwrite):
        return overwrite(app_id, guild_id, commands)
    return None


def commands() -> list[Any]:
    return command_catalog()


def default_commands() -> list[Any]:
    from .commands import voice_application_commands

    voice_commands = [command_to_dict(command) for command in voice_application_commands()]
    return voice_commands


def validate_commands(commands: list[Any]) -> None:
    if len(commands) == 0:
        raise ValueError("no application commands to register")
    seen: set[str] = set()
    for index, command in enumerate(commands):
        name = _command_name(command)
        if name == "":
            raise ValueError(f"invalid application command at index {index}")
        if name in seen:
            raise ValueError(f'duplicate application command "{name}"')
        seen.add(name)


def _command_name(command: Any) -> str:
    if command is None:
        return ""
    if isinstance(command, dict):
        return str(command.get("name", "")).strip()
    return str(getattr(command, "name", "")).strip()


def command_to_dict(command: Any) -> dict[str, Any]:
    if isinstance(command, dict):
        return dict(command)
    if is_dataclass(command):
        payload: dict[str, Any] = {}
        for item in fields(command):
            value = getattr(command, item.name)
            if value is None or value == "" or value == [] or value == () or value is False:
                continue
            key = _payload_key(item.name)
            payload[key] = _field_to_payload(key, value)
        return payload
    if hasattr(command, "to_dict") and callable(command.to_dict):
        raw_payload = command.to_dict()
        if not isinstance(raw_payload, dict):
            raise TypeError(f"unsupported command payload {type(raw_payload)!r}")
        return dict(raw_payload)
    raise TypeError(f"unsupported command type {type(command)!r}")


def _is_command_like(value: Any) -> bool:
    return isinstance(value, dict) or hasattr(value, "to_dict") or is_dataclass(value)


def _field_to_payload(key: str, value: Any) -> Any:
    if key == "type":
        return _scalar_to_payload(value)
    if key == "channel_types":
        if isinstance(value, (list, tuple)):
            return [_scalar_to_payload(child) for child in value]
        return _scalar_to_payload(value)
    if isinstance(value, (list, tuple)):
        return [command_to_dict(child) if _is_command_like(child) else _choice_to_dict(child) for child in value]
    if _is_command_like(value):
        return command_to_dict(value)
    return _enum_value(value)


def _choice_to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _enum_value(getattr(value, field.name)) for field in fields(value)}
    return _enum_value(value)


def _enum_value(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        return value.value
    return value


def _payload_key(name: str) -> str:
    if name == "channel_types":
        return "channel_types"
    if name == "default_member_permissions":
        return "default_member_permissions"
    return name


def _scalar_to_payload(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
        value = value.value
    if isinstance(value, str):
        option_types = {
            "subcommand": 1,
            "subcommand_group": 2,
            "string": 3,
            "integer": 4,
            "channel": 7,
        }
        channel_types = {
            "guild_text": 0,
            "guild_voice": 2,
            "guild_stage_voice": 13,
        }
        return option_types.get(value, channel_types.get(value, value))
    return value
