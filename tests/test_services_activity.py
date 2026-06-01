from __future__ import annotations

import json
from datetime import UTC, datetime

from services import activity
from voice_tracker import domain


class _Repo:
    def __init__(self, settings: domain.GuildSettings | None = None) -> None:
        self.settings = settings

    def get_guild_settings(self, _ctx, _guild_id: str):
        return self.settings


def test_event_enabled_uses_guild_settings() -> None:
    settings = domain.GuildSettings(
        guild_id="g1",
        activity_event_types=[domain.ACTIVITY_EVENT_MEMBER_JOIN, domain.ACTIVITY_EVENT_INVITE_USED],
    )
    repo = _Repo(settings)

    assert activity._event_enabled(repo, "g1", domain.ACTIVITY_EVENT_MEMBER_JOIN) is True
    assert activity._event_enabled(repo, "g1", domain.ACTIVITY_EVENT_INVITE_DELETE) is False


def test_embed_description_for_exact_invite_used_includes_inviter() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_INVITE_USED,
        guild_id="g1",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Owner",
        invite_code="abc",
        invite_url="https://discord.gg/abc",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
    )

    description = activity._embed_description(event)

    assert "Alice <@42>" in description
    assert "Owner <@7>" in description
    assert "https://discord.gg/abc" in description


def test_activity_channel_id_returns_empty_when_not_configured() -> None:
    repo = _Repo(domain.GuildSettings(guild_id="g1", activity_channel_id=""))

    assert activity._activity_channel_id(repo, "g1") == ""


def test_activity_channel_id_uses_category_override_before_global() -> None:
    repo = _Repo(
        domain.GuildSettings(
            guild_id="g1",
            activity_channel_id="global",
            activity_category_channel_ids={domain.ACTIVITY_CATEGORY_MESSAGES: "messages"},
        )
    )

    assert activity._activity_channel_id(repo, "g1", domain.ACTIVITY_EVENT_MESSAGE_DELETE) == "messages"
    assert activity._activity_channel_id(repo, "g1", domain.ACTIVITY_EVENT_MEMBER_JOIN) == "global"


def test_activity_event_from_payload_decodes_json_bytes() -> None:
    payload = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_INVITE_USED,
        guild_id="g1",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        member_user_id="42",
        invite_code="abc",
        attribution_status=domain.INVITE_ATTRIBUTION_STATUS_EXACT,
    ).to_dict()

    event = activity._activity_event_from_payload(json_bytes(payload))

    assert event.event_type == domain.ACTIVITY_EVENT_INVITE_USED
    assert event.guild_id == "g1"
    assert event.member_user_id == "42"


def test_message_create_embed_puts_written_content_front_and_center() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_MESSAGE_CREATE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={
            "channel_id": "100",
            "message_id": "200",
            "content": "hello admin log",
        },
    )

    description = activity._embed_description(event)

    assert "**Author:** Alice <@42>" in description
    assert "**Channel:** <#100>" in description
    assert "**Content:**" in description
    assert "hello admin log" in description
    assert "https://discord.com/channels/g1/100/200" in description


def test_message_update_embed_renders_styled_diff() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_MESSAGE_UPDATE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={
            "channel_id": "100",
            "message_id": "200",
            "before_content": "same line\nold text",
            "after_content": "same line\nnew text",
        },
    )

    description = activity._embed_description(event)

    assert "**Author:** Alice <@42>" in description
    assert "**Channel:** <#100>" in description
    assert "**Changes:**" in description
    assert "```diff" in description
    assert "  same line" in description
    assert "- old text" in description
    assert "+ new text" in description
    assert "https://discord.com/channels/g1/100/200" in description


def test_message_delete_embed_includes_deleted_content_when_available() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_MESSAGE_DELETE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={"channel_id": "100", "message_id": "200", "content": "bad deleted text"},
    )

    description = activity._embed_description(event)

    assert "**Deleted content:**" in description
    assert "bad deleted text" in description
    assert "https://discord.com/channels/g1/100/200" in description


def test_reaction_remove_embed_includes_actor_emoji_and_message_link() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_REACTION_REMOVE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Bob",
        metadata={"channel_id": "100", "message_id": "200", "emoji": "thumbsup"},
    )

    description = activity._embed_description(event)

    assert "**Actor:** Bob <@7>" in description
    assert "**Message author:** Alice <@42>" in description
    assert "thumbsup" in description
    assert "https://discord.com/channels/g1/100/200" in description


def test_voice_join_embed_includes_member_channel_and_user_id() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_VOICE_JOIN,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={"channel_id": "300"},
    )

    description = activity._embed_description(event)

    assert "**Member:** Alice <@42>" in description
    assert "**Channel:** <#300>" in description
    assert "**User ID:** 42" in description


def test_voice_move_embed_includes_actor_when_available() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_VOICE_MOVE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Mod",
        metadata={"previous_channel_id": "300", "channel_id": "301"},
    )

    description = activity._embed_description(event)

    assert "**Member:** Alice <@42>" in description
    assert "**From:** <#300>" in description
    assert "**To:** <#301>" in description
    assert "**Moved by:** Mod <@7>" in description


def test_voice_move_embed_marks_unknown_actor_when_audit_log_unavailable() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_VOICE_MOVE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={"previous_channel_id": "300", "channel_id": "301"},
    )

    description = activity._embed_description(event)

    assert "**Moved by:** unknown (audit log unavailable)" in description


def test_member_leave_embed_includes_leave_context_roles_and_server_age() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_MEMBER_LEAVE,
        guild_id="g1",
        occurred_at=datetime(2026, 1, 3, 12, 0, tzinfo=UTC),
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Mod",
        metadata={
            "leave_reason": "kicked",
            "joined_at": "2026-01-01T12:00:00Z",
            "roles": "Staff <@&9>, Muted <@&8>",
        },
    )

    description = activity._embed_description(event)

    assert "**Member:** Alice <@42>" in description
    assert "**Result:** kicked" in description
    assert "**Kicked by:** Mod <@7>" in description
    assert "**Time on server:** 48h0m0s" in description
    assert "**Roles:** Staff <@&9>, Muted <@&8>" in description


def test_member_leave_embed_does_not_show_actor_for_plain_leave() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_MEMBER_LEAVE,
        guild_id="g1",
        occurred_at=datetime(2026, 1, 3, 12, 0, tzinfo=UTC),
        member_user_id="42",
        member_name="Alice",
        metadata={"leave_reason": "leaved", "joined_at": "2026-01-01T12:00:00Z", "roles": ""},
    )

    description = activity._embed_description(event)

    assert "**Result:** leaved" in description
    assert "by:**" not in description
    assert "**Roles:** none" in description


def test_profile_role_embed_includes_actor_and_role_changes() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_PROFILE_ROLES_UPDATE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Mod",
        metadata={"added_roles": "Staff <@&9>", "removed_roles": "Muted <@&8>"},
    )

    description = activity._embed_description(event)

    assert "**Member:** Alice <@42>" in description
    assert "**Changed by:** Mod <@7>" in description
    assert "**Added:** Staff <@&9>" in description
    assert "**Removed:** Muted <@&8>" in description


def test_profile_embed_marks_unknown_actor_when_audit_log_unavailable() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_PROFILE_NICKNAME_UPDATE,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        metadata={"before_nickname": "Old", "after_nickname": "New"},
    )

    description = activity._embed_description(event)

    assert "**Changed by:** unknown (audit log unavailable)" in description


def test_reaction_embed_prefers_actor_avatar_thumbnail() -> None:
    event = domain.ActivityEvent(
        event_type=domain.ACTIVITY_EVENT_REACTION_ADD,
        guild_id="g1",
        member_user_id="42",
        member_name="Alice",
        actor_user_id="7",
        actor_name="Bob",
        metadata={
            "channel_id": "100",
            "message_id": "200",
            "emoji": "thumbsup",
            "member_avatar_url": "https://example.com/member.png",
            "actor_avatar_url": "https://example.com/actor.png",
        },
    )

    embed = activity._build_embed(event)

    assert embed.thumbnail.url == "https://example.com/actor.png"


def json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload).encode("utf-8")
