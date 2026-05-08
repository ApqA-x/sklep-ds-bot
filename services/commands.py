from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated and slated for removal in Python 3.13",
        category=DeprecationWarning,
    )
    import discord
from pymongo import MongoClient

from voice_tracker.appcommands import commands as application_commands
from voice_tracker.commands import (
    STATUS_COMMAND_NAME,
    Service as VoiceService,
    VOICE_COMMAND_NAMES,
    can_use_voice_command,
    format_duration,
    parse_voice_route,
)
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
from voice_tracker.runtime import configure_logging, load_config, register_commands_http


logger = logging.getLogger(__name__)

LEGACY_ROOT_COMMAND_NAMES = {
    "audit",
    "bot-setting",
    "track",
    "track-list",
}

TARGET_COMMAND_NAMES = {
    "settings",
    "jump",
    "inspect",
    "autorole",
    "unmute",
    "dashboard",
    "userinfo",
    STATUS_COMMAND_NAME,
}

SUPPORTED_COMMAND_NAMES = (VOICE_COMMAND_NAMES | TARGET_COMMAND_NAMES) - LEGACY_ROOT_COMMAND_NAMES
DASHBOARD_PAGE_SIZE = 10


@dataclass(slots=True)
class InteractionMessage:
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None
    content: str | None = None
    ephemeral: bool = True


class DashboardView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_user_id: str,
        rows: list[object],
        client: discord.Client | None = None,
        guild: discord.Guild | None = None,
        page: int = 1,
        page_size: int = DASHBOARD_PAGE_SIZE,
    ) -> None:
        super().__init__(timeout=300)
        self.client = client
        self.guild = guild
        self.owner_user_id = owner_user_id
        self.rows = list(rows)
        self.page = max(1, page)
        self.page_size = max(1, page_size)
        self.total_pages = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        self.resolved_display_names: dict[str, str] = {}
        self._sync_buttons()

    async def build_embed(self, guild: discord.Guild | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="Ranking Top",
            description=await _dashboard_description(
                self.client,
                guild or self.guild,
                self.rows,
                self.page,
                self.page_size,
                resolved_display_names=self.resolved_display_names,
            ),
            color=0x2B2D42,
        )
        embed.set_footer(text=f"Page {self.page} of {self.total_pages} • Total members: {len(self.rows)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        interaction_user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
        return interaction_user_id == self.owner_user_id

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        self.page = min(max(1, page), self.total_pages)
        self._sync_buttons()
        await interaction.response.edit_message(embed=await self.build_embed(getattr(interaction, "guild", None)), view=self)

    def _sync_buttons(self) -> None:
        self.first_page.disabled = self.page <= 1
        self.previous_page.disabled = self.page <= 1
        self.next_page.disabled = self.page >= self.total_pages
        self.last_page.disabled = self.page >= self.total_pages

    @discord.ui.button(label="<<", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._change_page(interaction, 1)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._change_page(interaction, self.page + 1)

    @discord.ui.button(label=">>", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._change_page(interaction, self.total_pages)


def _build_client(token: str) -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.presences = True
    intents.voice_states = True
    return discord.Client(intents=intents)


async def main() -> None:
    configure_logging("commands")
    cfg = load_config()
    if cfg.discord_token == "":
        raise SystemExit("DISCORD_TOKEN is required")
    if cfg.discord_application_id == "":
        raise SystemExit("DISCORD_APPLICATION_ID is required")
    if cfg.discord_guild_id == "":
        raise SystemExit("DISCORD_GUILD_ID is required")

    logger.info("commands service starting guild=%s", cfg.discord_guild_id)
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
        context = _command_context(interaction, root, command, options)
        logger.info("command received %s", context)
        try:
            result = await _dispatch_command(
                client,
                service,
                interaction,
                model,
                root,
                command,
                options,
                cfg.bot_admin_user_ids,
            )
        except ValueError as exc:
            logger.warning("command rejected %s error=%s", context, exc)
            result = str(exc)
        except Exception:
            logger.exception("command failed %s", context)
            result = "Command failed. Check service logs."
        else:
            if isinstance(result, InteractionMessage):
                logger.info("command completed %s response=interaction_message", context)
            elif isinstance(result, discord.Embed):
                logger.info("command completed %s response=embed", context)
            else:
                logger.info("command completed %s response=text", context)
        if isinstance(result, InteractionMessage):
            kwargs: dict[str, object] = {"ephemeral": result.ephemeral}
            if result.content is not None:
                kwargs["content"] = result.content
            if result.embed is not None:
                kwargs["embed"] = result.embed
            if result.view is not None:
                kwargs["view"] = result.view
            await interaction.response.send_message(**kwargs)
            return
        if isinstance(result, discord.Embed):
            await interaction.response.send_message(embed=result, ephemeral=True)
            return
        content = result
        if content == "":
            content = "Done."
        embed = discord.Embed(title="Voice Tracker", description=content, color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    await client.login(cfg.discord_token)
    try:
        await register_commands_http(cfg.discord_token, cfg.discord_application_id, cfg.discord_guild_id, registered_commands)
        logger.info(
            "application commands registered count=%s guild=%s",
            len(registered_commands),
            cfg.discord_guild_id,
        )
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
) -> str | discord.Embed | InteractionMessage:
    options = _normalize_snowflake_options(options)
    if root == "jump":
        return await _dispatch_jump_command(client, interaction, model, options)
    if root == "connect":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_connect_command(client, service, interaction, model, options)
    if root == "disconnect":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_disconnect_command(service, model)
    if root == STATUS_COMMAND_NAME:
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_status_command(client, options)
    if root == "autorole":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return await _dispatch_autorole_command(service, interaction, model, options)
    if root == "unmute":
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return _dispatch_unmute_command(service, model, command, options)
    if root == "dashboard":
        return await _dispatch_dashboard_command(client, service, model, interaction)
    if root == "userinfo":
        return await _dispatch_userinfo_command(
            client,
            service,
            model,
            interaction,
            options,
        )
    if root == "inspect" and (command == "channel" or (command == "" and _option_string(options, "channel") != "")):
        if not _is_admin_only(model):
            return "Insufficient permissions."
        return _dispatch_inspect_channel_command(service, model, options)
    if root in {"settings", "inspect"}:
        remember_fallback = root == "settings" and command == "summary-clear"
        return _dispatch_legacy_voice_command(
            service,
            model,
            root,
            command,
            options,
            bot_admin_user_ids,
            remember_fallback=remember_fallback,
        )
    logger.warning("unknown command route %s", _command_context(interaction, root, command, options))
    return "Unknown command."


def _dispatch_legacy_voice_command(
    service: VoiceService,
    model: InteractionCreate,
    root: str,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
    bot_admin_user_ids: list[str],
    remember_fallback: bool = False,
) -> str:
    if not can_use_voice_command(model, bot_admin_user_ids, root, command):
        return "Insufficient permissions."
    if remember_fallback and model.channel_id:
        service.remember_fallback_summary_channel(None, model.guild_id, model.channel_id)
    return service.handle_voice_command(None, model, root, command, options)


def _dispatch_unmute_command(
    service: VoiceService,
    model: InteractionCreate,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    if command not in {"add", "remove", "list"}:
        return "Unknown unmute command."
    return service.handle_unmute_command(None, model, command, options)


def _dispatch_inspect_channel_command(
    service: VoiceService,
    model: InteractionCreate,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    return service.handle_inspect_command(None, model, "active.channel", options)


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


async def _dispatch_connect_command(
    client: discord.Client,
    service: VoiceService,
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
    bot_member = await _bot_member(interaction, client)
    if bot_member is None:
        raise ValueError("bot member is unavailable")
    _ensure_bot_can_connect(bot_member, target)

    settings = service.set_managed_voice_channel(None, model.guild_id, str(target.id))
    return (
        f"Managed voice channel set to {target.mention}. "
        f"Soundboard enforcement is {'on' if settings.soundboard_enforcement_enabled else 'off'}."
    )


async def _dispatch_disconnect_command(service: VoiceService, model: InteractionCreate) -> str:
    service.clear_managed_voice_channel(None, model.guild_id)
    return "Managed voice connection disabled."


async def _dispatch_status_command(
    client: discord.Client,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    requested = _option_string(options, "state")
    if requested == "":
        raise ValueError("state is required")
    status_key, status_value = _presence_status(requested)
    await client.change_presence(status=status_value)
    label = _presence_status_label(status_key)
    if status_key == "offline":
        return f"Bot status set to {label} (mapped to invisible)."
    return f"Bot status set to {label}."


async def _dispatch_userinfo_command(
    client: discord.Client,
    service: VoiceService,
    model: InteractionCreate,
    interaction: discord.Interaction,
    options: list[ApplicationCommandInteractionDataOption],
) -> discord.Embed:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        raise ValueError("guild is required")
    requested_user_id = _option_string(options, "user")
    fallback_user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
    user_id = requested_user_id or fallback_user_id
    if user_id == "":
        raise ValueError("user is required")
    member = await _resolve_member_by_id(guild, user_id)
    fetched_user = await _fetch_user_by_id(client, user_id)
    display_name = _sanitize_public_text(_userinfo_display_name(member, fetched_user, user_id)) or user_id
    username = _sanitize_public_text(_userinfo_username(member, fetched_user, user_id)) or user_id
    status_label = _userinfo_status(member)
    profile = _userinfo_profile(service, model.guild_id, user_id)
    invite_reads_enabled = True
    total_voice_time = _userinfo_total_voice_time(profile)
    joined_at = _format_discord_datetime(getattr(member, "joined_at", None))
    created_at = _format_discord_datetime(_userinfo_created_at(member, fetched_user))
    avatar_url = _userinfo_avatar_url(member, fetched_user)
    banner_url = _userinfo_banner_url(member, fetched_user)
    lines = [
        f"User ID: `{user_id}`",
        f"Username: {username}",
        f"Status: {status_label}",
        *_userinfo_nickname_lines(profile, member),
        await _userinfo_roles_line(profile, member, guild),
        f"Total voice time: {total_voice_time}",
        *_userinfo_invite_lines(profile, invite_reads_enabled=invite_reads_enabled),
        f"Joined at: {joined_at}",
        f"Registered at: {created_at}",
    ]
    embed = discord.Embed(title=f"Information about {display_name}", description="\n".join(lines), color=0x5865F2)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    if banner_url:
        embed.set_image(url=banner_url)
    embed.set_footer(text="Voice Tracker")
    return embed


async def _dispatch_dashboard_command(
    client: discord.Client,
    service: VoiceService,
    model: InteractionCreate,
    interaction: discord.Interaction,
) -> InteractionMessage | str:
    rows = service.list_dashboard_totals(None, model.guild_id)
    if len(rows) == 0:
        return "No voice totals yet."
    owner_user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "")
    view = DashboardView(
        client=client,
        guild=getattr(interaction, "guild", None),
        owner_user_id=owner_user_id,
        rows=rows,
    )
    return InteractionMessage(embed=await view.build_embed(), view=view)


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


async def _dashboard_description(
    client: discord.Client | None,
    guild: discord.Guild | None,
    rows: list[object],
    page: int,
    page_size: int,
    *,
    resolved_display_names: dict[str, str] | None = None,
) -> str:
    page = max(1, page)
    page_size = max(1, page_size)
    start = (page - 1) * page_size
    visible_rows = rows[start : start + page_size]
    if len(visible_rows) == 0:
        return "No voice totals yet."
    display_names = await asyncio.gather(
        *[
            _dashboard_display_name(client, guild, row, resolved_display_names)
            for row in visible_rows
        ]
    )
    lines = ["Sorted by voice time", ""]
    for index, (row, display_name) in enumerate(zip(visible_rows, display_names, strict=False), start=start + 1):
        user_id = str(getattr(row, "user_id", "") or "")
        clickable_tag = f"<@{user_id}>" if user_id else ""
        label = display_name or ("Unknown user" if user_id else "")
        if label and clickable_tag:
            lines.append(f"**#{index}.** {label} {clickable_tag}")
        elif label:
            lines.append(f"**#{index}.** {label}")
        else:
            lines.append(f"**#{index}.** Unknown user")
        lines.append(f"Hours: `{_dashboard_duration(getattr(row, 'total_for', None))}`")
        if index != start + len(visible_rows):
            lines.append("")
    return "\n".join(lines).strip()


def _dashboard_duration(value: object) -> str:
    if isinstance(value, timedelta):
        total_seconds = max(0, int(value.total_seconds()))
    elif isinstance(value, (int, float)):
        total_seconds = max(0, int(value))
    else:
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _normalize_snowflake_options(
    options: list[ApplicationCommandInteractionDataOption],
) -> list[ApplicationCommandInteractionDataOption]:
    normalized: list[ApplicationCommandInteractionDataOption] = []
    for option in options:
        value = option.value
        if option.name in {"channel", "user", "role"} and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        normalized.append(
            ApplicationCommandInteractionDataOption(
                name=option.name,
                value=value,
                type=option.type,
                options=_normalize_snowflake_options(option.options),
            )
        )
    return normalized


def _userinfo_profile(service: VoiceService, guild_id: str, user_id: str) -> object | None:
    return service.get_member_profile(None, guild_id, user_id)


def _userinfo_total_voice_time(profile: object | None) -> str:
    if profile is None:
        return format_duration(timedelta())
    if isinstance(profile, dict):
        total_for = profile.get("total_for", profile.get("totalFor"))
    else:
        total_for = getattr(profile, "total_for", getattr(profile, "totalFor", None))
    return format_duration(total_for or timedelta())


async def _userinfo_roles_line(profile: object | None, member: discord.Member | None, guild: discord.Guild) -> str:
    roles = await _userinfo_roles(profile, member, guild)
    if len(roles) == 0:
        return "Roles: none"
    value = ", ".join(roles[:10])
    if len(roles) > 10:
        value = f"{value}, +{len(roles) - 10} more"
    if member is None:
        return f"Roles (stored): {value}"
    return f"Roles: {value}"


async def _userinfo_roles(profile: object | None, member: discord.Member | None, guild: discord.Guild) -> list[str]:
    if member is not None:
        names: list[str] = []
        for role in list(getattr(member, "roles", []) or []):
            is_default = getattr(role, "is_default", None)
            if callable(is_default) and bool(is_default()):
                continue
            cleaned = _sanitize_public_text(str(getattr(role, "name", "") or ""))
            if cleaned:
                names.append(cleaned)
        return names
    if profile is None:
        return []
    if isinstance(profile, dict):
        raw_roles = profile.get("roles", [])
    else:
        raw_roles = getattr(profile, "roles", [])
    role_ids = [str(raw or "").strip() for raw in list(raw_roles or []) if str(raw or "").strip()]
    if len(role_ids) == 0:
        return []
    role_lookup: dict[str, object] = {}
    for role in list(getattr(guild, "roles", []) or []):
        role_id = str(getattr(role, "id", "") or "")
        if role_id:
            role_lookup[role_id] = role
    try:
        for role in await guild.fetch_roles():
            role_id = str(getattr(role, "id", "") or "")
            if role_id:
                role_lookup[role_id] = role
    except Exception:
        pass
    safe_roles: list[str] = []
    for role_id in role_ids:
        role_obj = role_lookup.get(role_id)
        if role_obj is None:
            if not role_id.isdigit():
                role_name = _sanitize_public_text(role_id)
                if role_name:
                    safe_roles.append(role_name)
            continue
        role_name = _sanitize_public_text(str(getattr(role_obj, "name", "") or ""))
        if role_name == "":
            continue
        safe_roles.append(role_name)
    return safe_roles


def _userinfo_nickname_lines(profile: object | None, member: discord.Member | None) -> list[str]:
    current_nickname = _sanitize_public_text(str(getattr(member, "nick", "") or "")) if member is not None else ""
    if current_nickname == "":
        current_nickname = _userinfo_profile_text(profile, "nickname", "current_nickname", "currentNickname")
    history = _userinfo_profile_list(profile, "nickname_history", "nicknameHistory")
    lines: list[str] = []
    if current_nickname != "":
        label = "Nickname" if member is not None else "Nickname (stored)"
        lines.append(f"{label}: {current_nickname}")
    if len(history) > 0:
        label = "Past nicknames" if member is not None else "Past nicknames (stored)"
        lines.append(f"{label}: {', '.join(history[:10])}")
    return lines


def _userinfo_invite_lines(profile: object | None, *, invite_reads_enabled: bool = True) -> list[str]:
    if not invite_reads_enabled:
        return ["Invite attribution: disabled by configuration"]
    attribution_status = _userinfo_invite_attribution_status(profile)
    if attribution_status == "exact":
        invite_used = _userinfo_profile_text(profile, "invite_url", "inviteUrl") or "unknown"
        invite_creator = _userinfo_invite_creator(profile) or "unknown"
    else:
        invite_used = attribution_status
        invite_creator = attribution_status
    return [
        f"Invite used: {invite_used}",
        f"Invite created by: {invite_creator}",
        f"Invite attribution: {attribution_status}",
    ]


def _userinfo_invite_attribution_status(profile: object | None) -> str:
    status = _userinfo_profile_text(profile, "attribution_status", "attributionStatus").lower()
    if status in {"exact", "ambiguous", "unknown"}:
        return status
    return "unknown"


def _userinfo_invite_creator(profile: object | None) -> str:
    inviter_name = _userinfo_profile_text(profile, "inviter_name", "inviterName")
    inviter_user_id = _userinfo_profile_text(profile, "inviter_user_id", "inviterUserId")
    if inviter_name and inviter_user_id:
        return f"{inviter_name} ({inviter_user_id})"
    return inviter_name or inviter_user_id


def _userinfo_profile_text(profile: object | None, *names: str) -> str:
    if profile is None:
        return ""
    if isinstance(profile, dict):
        for name in names:
            value = profile.get(name)
            if value is None:
                continue
            cleaned = _sanitize_public_text(str(value))
            if cleaned:
                return cleaned
        return ""
    for name in names:
        value = getattr(profile, name, None)
        if value is None:
            continue
        cleaned = _sanitize_public_text(str(value))
        if cleaned:
            return cleaned
    return ""


def _userinfo_profile_list(profile: object | None, *names: str) -> list[str]:
    if profile is None:
        return []
    value = None
    if isinstance(profile, dict):
        for name in names:
            value = profile.get(name)
            if value is not None:
                break
    else:
        for name in names:
            value = getattr(profile, name, None)
            if value is not None:
                break
    if value is None:
        return []
    safe_values: list[str] = []
    for item in list(value or []):
        cleaned = _sanitize_public_text(str(item))
        if cleaned == "" or cleaned in safe_values:
            continue
        safe_values.append(cleaned)
    return safe_values


def _option_string(options: list[ApplicationCommandInteractionDataOption], name: str) -> str:
    for option in options:
        if option.name != name:
            continue
        if isinstance(option.value, str):
            return option.value.strip()
        if isinstance(option.value, int) and not isinstance(option.value, bool):
            return str(option.value)
    return ""


def _presence_status(value: str) -> tuple[str, discord.Status]:
    key = value.lower().strip().replace("_", "-").replace(" ", "-")
    aliases = {
        "online": "online",
        "idle": "idle",
        "away": "idle",
        "dnd": "dnd",
        "do-not-disturb": "dnd",
        "donotdisturb": "dnd",
        "disturb": "dnd",
        "invisible": "invisible",
        "offline": "offline",
    }
    canonical = aliases.get(key, "")
    if canonical == "":
        raise ValueError("state must be one of: online, idle, dnd, invisible, offline")
    statuses = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.invisible,
    }
    return canonical, statuses[canonical]


def _presence_status_label(value: str) -> str:
    labels = {
        "online": "online",
        "idle": "idle",
        "dnd": "do-not-disturb",
        "invisible": "invisible",
        "offline": "offline",
    }
    return labels.get(value, value)


def _command_context(
    interaction: discord.Interaction,
    root: str,
    command: str,
    options: list[ApplicationCommandInteractionDataOption],
) -> str:
    interaction_id = str(getattr(interaction, "id", "") or "-")
    guild_id = str(getattr(interaction, "guild_id", "") or "-")
    channel_id = str(getattr(interaction, "channel_id", "") or "-")
    user_id = str(getattr(getattr(interaction, "user", None), "id", "") or "-")
    option_summary = _options_summary(options)
    return (
        f"interaction_id={interaction_id} guild_id={guild_id} channel_id={channel_id} "
        f"user_id={user_id} root={root or '-'} command={command or '-'} options={option_summary}"
    )


def _options_summary(options: list[ApplicationCommandInteractionDataOption]) -> str:
    if len(options) == 0:
        return "[]"
    return "[" + ", ".join(_option_summary(option) for option in options) + "]"


def _option_summary(option: ApplicationCommandInteractionDataOption) -> str:
    if len(option.options) > 0:
        return f"{option.name}={_options_summary(option.options)}"
    value = option.value
    if value is None:
        return f"{option.name}=<none>"
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 64:
        text = text[:61] + "..."
    return f"{option.name}={text!r}"


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


def _ensure_bot_can_connect(bot_member: discord.Member, channel: discord.abc.GuildChannel) -> None:
    permissions = channel.permissions_for(bot_member)
    if not permissions.view_channel:
        raise ValueError("bot cannot view the target channel")
    if not permissions.connect:
        raise ValueError("bot cannot connect to the target channel")


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


async def _resolve_member_by_id(guild: discord.Guild, user_id: str) -> discord.Member | None:
    snowflake = _maybe_int(user_id)
    if snowflake is None:
        return None
    cached = guild.get_member(snowflake)
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(snowflake)
    except Exception:
        return None


async def _fetch_user_by_id(client: discord.Client, user_id: str) -> discord.User | None:
    snowflake = _maybe_int(user_id)
    if snowflake is None:
        return None
    try:
        return await client.fetch_user(snowflake)
    except Exception:
        return client.get_user(snowflake)


async def _dashboard_display_name(
    client: discord.Client | None,
    guild: discord.Guild | None,
    row: object,
    resolved_display_names: dict[str, str] | None = None,
) -> str:
    user_id = str(getattr(row, "user_id", "") or "")
    if user_id and resolved_display_names is not None and user_id in resolved_display_names:
        return resolved_display_names[user_id]

    display_name = ""
    if guild is not None and user_id != "":
        member = await _resolve_member_by_id(guild, user_id)
        display_name = _sanitize_public_text(_dashboard_member_name(member))
    if display_name == "" and user_id != "" and client is not None:
        user = await _fetch_user_by_id(client, user_id)
        display_name = _sanitize_public_text(_dashboard_user_name(user))
    if display_name == "":
        display_name = _dashboard_stored_name(row, user_id)
    if display_name == user_id:
        display_name = ""
    if user_id and resolved_display_names is not None:
        resolved_display_names[user_id] = display_name
    return display_name


def _dashboard_member_name(member: discord.Member | None) -> str:
    if member is None:
        return ""
    return str(member.display_name or "")


def _dashboard_user_name(user: discord.User | None) -> str:
    if user is None:
        return ""
    for attr in ("display_name", "global_name", "name"):
        value = str(getattr(user, attr, "") or "").strip()
        if value:
            return value
    return ""


def _dashboard_stored_name(row: object, user_id: str) -> str:
    stored_name = _sanitize_public_text(str(getattr(row, "user_name", getattr(row, "display_name", "")) or ""))
    if stored_name == "" or stored_name == user_id:
        return ""
    return stored_name


def _userinfo_display_name(member: discord.Member | None, user: discord.User | None, fallback_user_id: str) -> str:
    if member is not None:
        return str(member.display_name or member.name or fallback_user_id)
    if user is not None:
        return str(user.display_name or user.name or fallback_user_id)
    return fallback_user_id


def _userinfo_username(member: discord.Member | None, user: discord.User | None, fallback_user_id: str) -> str:
    source = member if member is not None else user
    if source is None:
        return fallback_user_id
    username = str(getattr(source, "name", "") or "").strip()
    if username == "":
        return fallback_user_id
    global_name = str(getattr(source, "global_name", "") or "").strip()
    if global_name and global_name != username:
        return f"{username} ({global_name})"
    return username


def _userinfo_status(member: discord.Member | None) -> str:
    if member is None:
        return "⚫ Not in server"
    status_key = str(getattr(member, "status", "unknown"))
    labels = {
        "online": "🟢 Online",
        "idle": "🟡 Idle",
        "dnd": "🔴 Do Not Disturb",
        "offline": "⚫ Offline",
        "invisible": "⚫ Invisible",
    }
    return labels.get(status_key, status_key.title())


def _userinfo_created_at(member: discord.Member | None, user: discord.User | None) -> datetime | None:
    if user is not None and getattr(user, "created_at", None) is not None:
        return user.created_at
    if member is not None:
        return getattr(member, "created_at", None)
    return None


def _userinfo_avatar_url(member: discord.Member | None, user: discord.User | None) -> str:
    if member is not None and getattr(member, "display_avatar", None) is not None:
        return str(member.display_avatar.url)
    if user is not None and getattr(user, "display_avatar", None) is not None:
        return str(user.display_avatar.url)
    return ""


def _userinfo_banner_url(member: discord.Member | None, user: discord.User | None) -> str:
    if member is not None:
        member_banner = getattr(member, "display_banner", None)
        if member_banner is not None:
            return str(member_banner.url)
    if user is not None:
        user_banner = getattr(user, "banner", None)
        if user_banner is not None:
            return str(user_banner.url)
    return ""


def _format_discord_datetime(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    unix_ts = int(value.timestamp())
    return f"<t:{unix_ts}:F> (<t:{unix_ts}:R>)"


def _sanitize_public_text(value: str) -> str:
    text = str(value or "")
    for token in ("@everyone", "@here"):
        text = text.replace(token, "")
    text = _strip_discord_mentions(text)
    text = " ".join(text.split())
    return text.strip()


def _strip_discord_mentions(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "<":
            end = text.find(">", index + 1)
            if end != -1 and text[index + 1 : end + 1].startswith("@"):
                index = end + 1
                continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _public_command_payloads() -> list[dict[str, object]]:
    return list(application_commands())


if __name__ == "__main__":
    asyncio.run(main())
