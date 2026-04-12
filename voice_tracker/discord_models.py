from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INTERACTION_APPLICATION_COMMAND = "application_command"

MESSAGE_FLAG_EPHEMERAL = 1 << 6

PERMISSION_ADMINISTRATOR = 1 << 3
PERMISSION_MANAGE_GUILD = 1 << 5

CHANNEL_TYPE_GUILD_TEXT = "guild_text"
CHANNEL_TYPE_GUILD_VOICE = "guild_voice"
CHANNEL_TYPE_GUILD_STAGE_VOICE = "guild_stage_voice"

OPTION_TYPE_SUB_COMMAND_GROUP = "subcommand_group"
OPTION_TYPE_SUB_COMMAND = "subcommand"
OPTION_TYPE_STRING = "string"
OPTION_TYPE_CHANNEL = "channel"
OPTION_TYPE_INTEGER = "integer"


@dataclass(slots=True)
class ApplicationCommandOptionChoice:
    name: str
    value: Any


@dataclass(slots=True)
class ApplicationCommandOption:
    type: str
    name: str
    description: str = ""
    required: bool = False
    choices: list[ApplicationCommandOptionChoice] = field(default_factory=list)
    options: list["ApplicationCommandOption"] = field(default_factory=list)
    channel_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ApplicationCommand:
    name: str
    description: str
    options: list[ApplicationCommandOption] = field(default_factory=list)


@dataclass(slots=True)
class Channel:
    id: str = ""
    guild_id: str = ""
    type: str = ""


@dataclass(slots=True)
class User:
    id: str = ""


@dataclass(slots=True)
class Member:
    user: User | None = None
    permissions: int = 0


@dataclass(slots=True)
class ApplicationCommandInteractionDataResolved:
    channels: dict[str, Channel] = field(default_factory=dict)


@dataclass(slots=True)
class ApplicationCommandInteractionDataOption:
    name: str
    value: Any | None = None
    type: str = ""
    options: list["ApplicationCommandInteractionDataOption"] = field(default_factory=list)


@dataclass(slots=True)
class ApplicationCommandInteractionData:
    name: str = ""
    options: list[ApplicationCommandInteractionDataOption] = field(default_factory=list)
    resolved: ApplicationCommandInteractionDataResolved | None = None


@dataclass(slots=True)
class Interaction:
    type: str = ""
    guild_id: str = ""
    channel_id: str = ""
    member: Member | None = None
    user: User | None = None
    data: ApplicationCommandInteractionData = field(default_factory=ApplicationCommandInteractionData)


@dataclass(slots=True)
class InteractionCreate:
    interaction: Interaction | None = None

    @property
    def type(self) -> str:
        return "" if self.interaction is None else self.interaction.type

    @property
    def guild_id(self) -> str:
        return "" if self.interaction is None else self.interaction.guild_id

    @property
    def channel_id(self) -> str:
        return "" if self.interaction is None else self.interaction.channel_id

    @property
    def member(self) -> Member | None:
        return None if self.interaction is None else self.interaction.member

    @property
    def user(self) -> User | None:
        return None if self.interaction is None else self.interaction.user

    @property
    def data(self) -> ApplicationCommandInteractionData:
        if self.interaction is None:
            return ApplicationCommandInteractionData()
        return self.interaction.data

    def application_command_data(self) -> ApplicationCommandInteractionData:
        return self.data


@dataclass(slots=True)
class MessageEmbed:
    title: str = ""
    description: str = ""
    color: int = 0


@dataclass(slots=True)
class InteractionResponseData:
    flags: int = 0
    embeds: list[MessageEmbed] = field(default_factory=list)
    content: str = ""


@dataclass(slots=True)
class InteractionResponse:
    type: str = "channel_message_with_source"
    data: InteractionResponseData = field(default_factory=InteractionResponseData)
