from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from voice_tracker.sleep_executor import SleepTimerExecutor
from voice_tracker.supervise import SingleWriterLeaseLost


CLAIMED = {
    "_id": "456:123", "guildId": "456", "targetUserId": "123",
    "dueAt": datetime.now(UTC) - timedelta(seconds=1),
    "status": "executing", "revision": 2,
}


class Store:
    def __init__(self, claimed=None):
        self.claimed = dict(claimed or CLAIMED)
        self.finishes = []
        self.claims = 0
        self.recover_calls = []

    def claim_due(self, **kwargs):
        self.claims += 1
        claimed, self.claimed = self.claimed, None
        return claimed

    def finish(self, claimed, **kwargs):
        self.finishes.append((claimed, kwargs))
        return True

    def mark_stale_unknown(self, **kwargs):
        self.recover_calls.append(kwargs)
        return 2


class Presence:
    ready = True

    def __init__(self, proven=True):
        self.proven = proven
        self.checks = 0

    async def proves_presence(self, *_args, **_kwargs):
        self.checks += 1
        return self.proven


class Lease:
    owner = "gateway-owner"
    fence = 7

    def __init__(self, fail_at=0):
        self.checks = 0
        self.fail_at = fail_at

    def ensure_current(self):
        self.checks += 1
        if self.checks == self.fail_at:
            raise SingleWriterLeaseLost("lost")


class Channel:
    id = 99

    def __init__(self, can_move=True):
        self.can_move = can_move

    def permissions_for(self, _member):
        return SimpleNamespace(move_members=self.can_move)


class Client:
    user = SimpleNamespace(id=100)

    def __init__(self, channel=None, *, ready=True, member_exists=True):
        self.ready = ready
        self.channel = channel or Channel()
        self.member = SimpleNamespace(id=123, voice=SimpleNamespace(channel=self.channel)) if member_exists else None
        self.guild = SimpleNamespace(
            id=456, unavailable=False, me=SimpleNamespace(id=100),
            get_member=lambda user_id: self.member if user_id == 123 else None,
        )

    def is_ready(self):
        return self.ready

    def get_guild(self, guild_id):
        return self.guild if guild_id == 456 else None


def executor(*, store=None, presence=None, client=None, lease=None):
    return SleepTimerExecutor(
        store=store or Store(), presence=presence or Presence(),
        client=client or Client(), lease=lease or Lease(),
        allowed_guild_id="456", discord_token="test-token", settle_seconds=0,
    )


@pytest.mark.asyncio
async def test_proven_stay_calls_discord_once_and_finishes() -> None:
    store = Store()
    presence = Presence()
    worker = executor(store=store, presence=presence)
    requests = []

    async def disconnect(guild_id, user_id):
        requests.append((guild_id, user_id))
        return "disconnected", "discord_patch_succeeded"

    worker._disconnect_once = disconnect
    assert await worker.run_once() is True
    assert requests == [("456", "123")]
    assert presence.checks == 2
    assert store.finishes[0][1]["status"] == "disconnected"
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_late_or_unproved_voice_never_calls_discord() -> None:
    store = Store()
    worker = executor(store=store, presence=Presence(False))
    worker._disconnect_once = lambda *_args: pytest.fail("must not call Discord")
    assert await worker.run_once() is True
    assert store.finishes[0][1]["status"] == "unknown"


@pytest.mark.asyncio
async def test_absent_member_or_missing_permission_is_terminal_without_patch() -> None:
    missing = Store()
    worker = executor(store=missing, client=Client(member_exists=False))
    assert await worker.run_once()
    assert missing.finishes[0][1]["status"] == "skipped"

    forbidden = Store()
    worker = executor(store=forbidden, client=Client(channel=Channel(can_move=False)))
    assert await worker.run_once()
    assert forbidden.finishes[0][1]["reason"] == "move_members_permission_missing"


@pytest.mark.asyncio
async def test_gateway_not_ready_does_not_claim_and_lost_lease_never_finishes() -> None:
    store = Store()
    worker = executor(store=store, client=Client(ready=False))
    assert await worker.run_once() is False
    assert store.claims == 0

    store = Store()
    worker = executor(store=store, lease=Lease(fail_at=2))
    with pytest.raises(SingleWriterLeaseLost):
        await worker.run_once()
    assert store.claims == 1 and store.finishes == []


@pytest.mark.asyncio
async def test_recover_marks_local_and_prior_fence_unknown() -> None:
    store = Store()
    worker = executor(store=store)
    assert await worker.recover_stale() == 2
    assert store.recover_calls == [{"current_fence": 7, "current_owner": "gateway-owner"}]


@pytest.mark.asyncio
async def test_raw_patch_never_retries_ambiguous_server_response(monkeypatch) -> None:
    calls = []

    class Response:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bot test-token"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def patch(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr("voice_tracker.sleep_executor.aiohttp.ClientSession", Session)
    worker = executor()
    assert await worker._disconnect_once("456", "123") == ("unknown", "discord_server_result_uncertain")
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["json"] == {"channel_id": None}
