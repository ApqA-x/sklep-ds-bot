from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import discord
from pymongo import MongoClient

from voice_tracker.appcommands import commands as application_commands
from voice_tracker.commands import Service as VoiceService, VOICE_COMMAND_NAMES, can_use_voice_command, parse_voice_route
from voice_tracker.discord_models import (
    ApplicationCommandInteractionData,
    ApplicationCommandInteractionDataOption,
    ApplicationCommandInteractionDataResolved,
    Channel,
    Interaction,
    InteractionCreate,
    Member,
    PERMISSION_ADMINISTRATOR,
    User,
)
from voice_tracker.repository import Repository
from voice_tracker.runtime import load_config, register_commands_http


logger = logging.getLogger(__name__)

TARGET_COMMAND_NAMES = {
    "audit",
    "bot-setting",
    "track",
    "track-list",
    "jump",
    "inspect",
    "autorole",
    "dashboard",
    "userinfo",
}

SUPPORTED_COMMAND_NAMES = VOICE_COMMAND_NAMES | TARGET_COMMAND_NAMES
def _build_client(token: str) -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.voice_states = True
    return discord.Client(intents=intents)


async def main() -> None:
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")
    if cfg.discord_application_id == "":
        raise SystemExit("DISCORD_APPLICATION_ID is required")
    if cfg.discord_guild_id == "":
        raise SystemExit("DISCORD_GUILD_ID is required")

    print(f"commands service starting guild={cfg.discord_guild_id}")
    mongo_client = MongoClient(cfg.mongo_uri)
    repo = Repository(mongo_client[cfg.mongo_db])
    repo.ensure_indexes(None)
    service = VoiceService(repo)
    registered_commands = _public_command_payloads()

    client = _build_client(cfg.discord_token)

    @client.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        data = getattr(interaction, "data", None)
        if not isinstance(data, dict) or data.get("name") not in SUPPORTED_COMMAND_NAMES:
            return
        if str(getattr(interaction, "guild_id", "") or "") != cfg.discord_guild_id:
            return
        model = _interaction_model(interaction)
        root, command, options = parse_voice_route(model.application_command_data())
        try:
            content = await _dispatch_command(client, service, interaction, model, root, command, options, cfg.bot_admin_user_ids)
        except ValueError as exc:
            content = str(exc)
        except Exception:
            logger.exception("command failed")
            content = "Command failed. Check service logs."
        if content == "":
            content = "Done."
        embed = discord.Embed(title="Voice Tracker", description=content, color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    await client.login(cfg.discord_token)
    try:
        await register_commands_http(cfg.discord_token, cfg.discord_application_id, cfg.discord_guild_id, registered_commands)
        print(f"application commands registered count={len(registered_commands)} guild={cfg.discord_guild_id}")
        await client.connect()
    finally:
        await client.close()
        mongo_client.close()


def _interaction_model(interaction: discord.Interaction) -> InteractionCreate:
    data = getattr(interaction, "data", {}) or {}
    guild_id = str(getattr(interaction, "guild_id", "") or "")
    permissions = getattr(getattr(interaction, "permissions", None), "value", 0) or 0
    user = getattr(interaction, "user", None)
    user_id = str(getattr(user, "id", "") or "")
    return InteractionCreate(
        interaction=Interaction(
            type="application_command",
            guild_id=guild_id,
            channel_id=str(getattr(interaction, "channel_id", "") or ""),
            member=Member(user=User(id=user_id), permissions=permissions),
            user=User(id=user_id),
            data=ApplicationCommandInteractionData(
                name=str(data.get("name", "")),
                options=_options(data.get("options", [])),
                resolved=ApplicationCommandInteractionDataResolved(channels=_channels(data.get("resolved", {}).get("channels", {}))),
            ),
        )
    )


def _options(raw_options: list[dict]) -> list[ApplicationCommandInteractionDataOption]:
    return [
        ApplicationCommandInteractionDataOption(
            name=str(option.get("name", "")),
            value=option.get("value"),
            type=str(option.get("type", "")),
            options=_options(option.get("options", [])),
        )
        for option in raw_options or []
    ]


def _channels(raw_channels: dict) -> dict[str, Channel]:
    return {
        str(channel_id): Channel(
            id=str(channel_id),
            guild_id=str(channel.get("guild_id", "") or channel.get("guildId", "")),
            type=_channel_type(channel.get("type")),
        )
        for channel_id, channel in (raw_channels or {}).items()
    }


def _channel_type(value: object) -> str:
    if isinstance(value, int):
        return {0: "guild_text", 2: "guild_voice", 13: "guild_stage_voice"}.get(value, str(value))
    return str(value)


async def _dispatch_command(
    client: discord.Client,
    service: VoiceService,
    interaction: discord.Interaction,
    model: InteractionCreate,
    root: str,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
    bot_admin_user_ids: list[str],
) -> str:
    if root == "audit":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_audit_command(service, model, options)
    if root == "bot-setting":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_bot_setting_command(service, model, command, options)
    if root == "track-list":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return _dispatch_track_list_command(service, model, command, options)
    if root == "jump":
        return await _dispatch_jump_command(client, interaction, model, options)
    if root == "autorole":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_autorole_command(service, interaction, model, options)
    if root == "dashboard":
        return await _dispatch_service_method(service, "handle_dashboard_command", model, command, options)
    if root == "userinfo":
        return await _dispatch_service_method(service, "handle_userinfo_command", model, command, options)
    if root == "track":
        if command in {"add", "remove", "list"}:
            if not _is_admin_only(model):
                return "Insufficient permissions."
            return _dispatch_track_command(service, model, command, options)
        if command == "clear":
            return _dispatch_legacy_voice_command(service, model, root, command, options, bot_admin_user_ids)
    if root == "inspect" and command == "channel":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return _dispatch_inspect_channel_command(service, model, options)
    if root in {"settings", "inspect"} or (root == "track" and command == "clear"):
        return _dispatch_legacy_voice_command(service, model, root, command, options, bot_admin_user_ids)
    return "Unknown command."


def _dispatch_legacy_voice_command(
    service: VoiceService,
    model: InteractionCreate,
    root: str,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
    bot_admin_user_ids: list[str],
) -> str:
    if not can_use_voice_command(model, bot_admin_user_ids, root, command):
        return "Insufficient permissions."
    if model.channel_id:
        service.remember_fallback_summary_channel(None, model.guild_id, model.channel_id)
    return service.handle_voice_command(None, model, root, command, options)


def _dispatch_track_command(
    service: VoiceService,
    model: InteractionCreate,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    if command not in {"add", "remove", "list"}:
        return "Unknown track command."
    if command == "add":
        return service.handle_track_command(None, model, "add", options)
    if command == "remove":
        return service.handle_track_command(None, model, "remove", options)
    return service.handle_track_command(None, model, "list", options)


def _dispatch_track_list_command(
    service: VoiceService,
    model: InteractionCreate,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    if command != "clear":
        return "Unknown track-list command."
    return service.handle_track_command(None, model, "clear", options)


def _dispatch_inspect_channel_command(
    service: VoiceService,
    model: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    return service.handle_inspect_command(None, model, "active.channel", options)


async def _dispatch_audit_command(
    service: VoiceService,
    model: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    channel_id = _option_string(options, "channel")
    if channel_id == "":
        raise ValueError("channel is required")
    settings = service.set_summary_channel(None, model.guild_id, channel_id)
    return service.describe_settings(settings)


async def _dispatch_bot_setting_command(
    service: VoiceService,
    model: InteractionCreate,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    handler = getattr(service, "handle_bot_setting_command", None)
    if callable(handler):
        result = handler(None, model, command, options)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result or "")
    settings = service.get_guild_settings(None, model.guild_id)
    return service.describe_settings(settings)


async def _dispatch_autorole_command(
    service: VoiceService,
    interaction: discord.Interaction,
    model: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    role_id = _option_string(options, "role")
    if role_id == "":
        raise ValueError("role is required")
    guild = getattr(interaction, "guild", None)
    if guild is None:
        raise ValueError("guild is required")
    role = await _resolve_role(guild, role_id)
    if role is None:
        raise ValueError("unable to resolve role")
    bot_member = await _bot_member(interaction)
    if bot_member is None:
        raise ValueError("bot member is unavailable")
    if not _role_is_safe_for_autorole(role, bot_member):
        raise ValueError("role is not safe for autorole")
    _persist_autorole(service.repo, model.guild_id, role_id)
    return f"Autorole set to {role.mention}."


async def _dispatch_jump_command(
    client: discord.Client,
    interaction: discord.Interaction,
    model: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    channel_id = _option_string(options, "channel")
    if channel_id == "":
        raise ValueError("channel is required")
    guild = getattr(interaction, "guild", None)
    if guild is None:
        raise ValueError("guild is required")
    target = await _resolve_voice_target_channel(guild, channel_id)
    if target is None:
        raise ValueError("unable to resolve channel")
    member = await _resolve_invoking_member(interaction)
    if member is None:
        raise ValueError("unable to resolve invoking member")
    if getattr(member, "voice", None) is None or getattr(member.voice, "channel", None) is None:
        raise ValueError("you must be in a voice channel to use /jump")
    if not _member_can_view_channel(member, target):
        raise ValueError("target channel is not visible to you")
    bot_member = await _bot_member(interaction, client)
    if bot_member is None:
        raise ValueError("bot member is unavailable")
    _ensure_bot_can_move(bot_member, target)
    await member.move_to(target)
    return f"Moved you to {target.mention}."


async def _dispatch_service_method(
    service: VoiceService,
    method_name: str,
    model: InteractionCreate,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    handler = getattr(service, method_name, None)
    if callable(handler):
        result = handler(None, model, command, options)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result or "")
    return "Command unavailable."


def _option_string(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    for option in options:
        if option.name == name and isinstance(option.value, str):
            return option.value.strip()
    return ""


def _is_admin_only(model: InteractionCreate) -> bool:
    return bool(model.member and model.member.permissions & PERMISSION_ADMINISTRATOR)


def _persist_autorole(repo: Repository | None, guild_id: str, role_id: str) -> None:
    if repo is None:
        return
    guild_id = (guild_id or "").strip()
    role_id = (role_id or "").strip()
    if guild_id == "" or role_id == "":
        return
    now = datetime.now(UTC)
    repo.guild_settings.update_one(
        {"_id": guild_id},
        {
            "$set": {
                "autoRoleId": role_id,
                "updatedAt": now,
            },
            "$setOnInsert": {"createdAt": now},
        },
        upsert=True,
    )


async def _resolve_role(guild: discord.Guild, role_id: str) -> discord.Role | None:
    snowflake = _maybe_int(role_id)
    role = guild.get_role(snowflake) if snowflake is not None else None
    if role is not None:
        return role
    try:
        roles = await guild.fetch_roles()
    except Exception:
        return None
    for candidate in roles:
        if str(candidate.id) == role_id:
            return candidate
    return None


async def _resolve_voice_target_channel(guild: discord.Guild, channel_id: str) -> discord.abc.GuildChannel | None:
    snowflake = _maybe_int(channel_id)
    channel = guild.get_channel(snowflake) if snowflake is not None else None
    if channel is None:
        try:
            channel = await guild.fetch_channel(snowflake) if snowflake is not None else None
        except Exception:
            return None
    if channel is None:
        return None
    channel_type = getattr(channel, "type", None)
    if channel_type not in {discord.ChannelType.voice, discord.ChannelType.stage_voice}:
        return None
    return channel


async def _resolve_invoking_member(interaction: discord.Interaction) -> discord.Member | None:
    member = getattr(interaction, "user", None)
    if isinstance(member, discord.Member):
        return member
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return None
    user_id = getattr(member, "id", None)
    if user_id is None:
        return None
    cached = guild.get_member(int(user_id))
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(int(user_id))
    except Exception:
        return None


async def _bot_member(interaction: discord.Interaction, client: discord.Client | None = None) -> discord.Member | None:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return None
    me = getattr(guild, "me", None)
    if isinstance(me, discord.Member):
        return me
    user = getattr(client, "user", None)
    if user is None:
        return me
    cached = guild.get_member(int(user.id))
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(int(user.id))
    except Exception:
        return me


def _ensure_bot_can_move(bot_member: discord.Member, channel: discord.abc.GuildChannel) -> None:
    permissions = channel.permissions_for(bot_member)
    if not permissions.view_channel:
        raise ValueError("bot cannot view the target channel")
    if not permissions.connect:
        raise ValueError("bot cannot connect to the target channel")
    if not permissions.move_members:
        raise ValueError("bot cannot move members in the target channel")


def _member_can_view_channel(member: discord.Member, channel: discord.abc.GuildChannel) -> bool:
    return bool(channel.permissions_for(member).view_channel)


def _role_is_safe_for_autorole(role: discord.Role, bot_member: discord.Member) -> bool:
    if role.is_default():
        return False
    if getattr(role, "managed", False):
        return False
    if role.permissions.administrator:
        return False
    if role.position >= bot_member.top_role.position:
        return False
    return role.is_assignable()


def _maybe_int(value: str) -> int | None:
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _public_command_payloads() -> list[dict[str, object]]:
    return list(application_commands())


if __name__ == "__main__":
    asyncio.run(main())
