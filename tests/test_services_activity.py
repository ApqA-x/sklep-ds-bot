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


def json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload).encode("utf-8")
