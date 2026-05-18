from __future__ import annotations

from datetime import UTC, datetime

from services import stalker
from voice_tracker import domain


class _Repo:
    def __init__(self, trusted_user_ids: list[str]) -> None:
        self.trusted_user_ids = list(trusted_user_ids)
        self.deleted_watchers: list[tuple[str, str]] = []

    def get_trusted_user_ids(self, _ctx, _guild_id: str) -> list[str]:
        return list(self.trusted_user_ids)

    def delete_stalker_subscriptions_by_watcher(self, _ctx, guild_id: str, watcher_user_id: str) -> int:
        self.deleted_watchers.append((guild_id, watcher_user_id))
        return 1


def test_voice_event_message_describes_join_move_and_leave() -> None:
    joined = stalker._voice_event_message(
        domain.VoiceStateEvent(guild_id="g1", user_id="42", user_name="Alice", channel_id="c2", previous_channel_id=""),
        "Guild One",
        "",
        "General",
    )
    moved = stalker._voice_event_message(
        domain.VoiceStateEvent(guild_id="g1", user_id="42", user_name="Alice", channel_id="c2", previous_channel_id="c1"),
        "Guild One",
        "Lobby",
        "General",
    )
    left = stalker._voice_event_message(
        domain.VoiceStateEvent(guild_id="g1", user_id="42", user_name="Alice", channel_id="", previous_channel_id="c1"),
        "Guild One",
        "Lobby",
        "",
    )

    assert joined == "Stalker update: Alice (<@42>) joined voice channel General in Guild One."
    assert moved == "Stalker update: Alice (<@42>) moved from voice channel Lobby to General in Guild One."
    assert left == "Stalker update: Alice (<@42>) left voice channel Lobby in Guild One."


def test_activity_event_message_describes_guild_join_and_leave() -> None:
    joined = stalker._activity_event_message(
        domain.ActivityEvent(event_type=domain.ACTIVITY_EVENT_MEMBER_JOIN, guild_id="g1", member_user_id="42", member_name="Alice"),
        "Guild One",
    )
    left = stalker._activity_event_message(
        domain.ActivityEvent(event_type=domain.ACTIVITY_EVENT_MEMBER_LEAVE, guild_id="g1", member_user_id="42", member_name="Alice"),
        "Guild One",
    )

    assert joined == "Stalker update: Alice (<@42>) joined Guild One."
    assert left == "Stalker update: Alice (<@42>) left Guild One."


def test_build_dm_embed_matches_voice_tracker_style() -> None:
    occurred_at = datetime(2026, 5, 18, 17, 0, tzinfo=UTC)

    embed = stalker._build_dm_embed("Stalker update: Alice joined.", occurred_at)

    assert embed.title == "Voice Tracker"
    assert embed.description == "Stalker update: Alice joined."
    assert embed.color.value == 0x5865F2
    assert embed.footer.text == "Voice Tracker"
    assert embed.timestamp == occurred_at


def test_active_watcher_user_ids_filters_and_revokes_untrusted_watchers() -> None:
    repo = _Repo(["trusted-1"])
    subscriptions = [
        domain.StalkerSubscription(guild_id="g1", watcher_user_id="trusted-1", target_user_id="u9"),
        domain.StalkerSubscription(guild_id="g1", watcher_user_id="revoked-2", target_user_id="u9"),
    ]

    watcher_user_ids = stalker._active_watcher_user_ids(repo, "g1", subscriptions)

    assert watcher_user_ids == ["trusted-1"]
    assert repo.deleted_watchers == [("g1", "revoked-2")]
