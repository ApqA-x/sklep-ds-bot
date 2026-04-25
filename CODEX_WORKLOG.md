# Codex Worklog

Updated: 2026-04-25, Europe/Berlin.

Current task: keep maintainer memory aligned with the repo-root `Problems` file and the active review artifact, track the paginated `/dashboard` leaderboard UX change, and capture the planned voice-enforcement design for soundboard detection.

## Current State

- The user problem source is the repo-root `Problems` file.
- Active review artifact: `docs/problem-review-plan-2026-04-25.md`.
- The active problem set is limited to four items:
  - remove legacy commands from the product surface and docs
  - extend auto-unmute to the reported headphones/deafen case
  - fix tracking so all channels are tracked instead of one live channel with stale others
  - fix `/userinfo` status and banner behavior
- `/dashboard` now has a Discord-only paginated embed with bottom arrow buttons, clickable member mentions, and `hours:minutes:seconds` totals.
- Soundboard enforcement cannot work unless the bot is connected to the target voice channel. Discord only emits the voice-channel effect event for channels the bot is in.
- The repo currently has no voice-connection management layer and no `PyNaCl` dependency, so `/connect` and `/disconnect` are a real new feature, not a small settings toggle.
- Recommended implementation shape:
  - add guild settings fields for `soundboard_enforcement_enabled`, `managed_voice_channel_id`, and `managed_voice_connected_at`
  - add admin commands `/settings soundboard on|off`, `/connect`, and `/disconnect`
  - keep voice connection ownership in `services/gateway.py`, not in `services/commands.py`
  - add a gateway voice manager that joins one configured voice channel, rejects external moves by reconnecting to the managed channel, and leaves after 5 minutes alone or immediately on `/disconnect`
  - keep enforcement modules (starting with soundboard) independent from connection ownership so more enforcement types can be added later
  - persist enough guild state in Mongo so reconnect/restart behavior is deterministic

## Voice Enforcement Planning Notes

- Local review conclusion:
  - the bot should treat voice enforcement as one managed channel per guild
  - slash commands should only mutate desired state; gateway should execute and supervise the actual voice connection
  - sticky anti-move behavior should be based on bot voice-state updates, not just command-time checks
  - idle disconnect should be driven by a scheduled task in gateway that checks whether the managed channel has been bot-only for 5 minutes
- Agent workstreams launched for planning:
  - command/settings surface review for `/settings soundboard on|off`, `/connect`, `/disconnect`
  - gateway/runtime review for persistent voice connection, anti-move behavior, and idle leave
  - test-surface review for command routing, gateway events, and voice mocks
- The planning agents did not return before the user interrupted, so this memory entry records the local synthesis that implementation should follow.
- Product decision confirmed by user:
  - `/connect` must take exactly one required `channel` argument (no invoker-channel fallback mode)
  - if the bot is moved away or kicked/disconnected from voice, gateway must automatically reconnect it to the managed channel while managed connection state is active
  - `/settings soundboard on|off` toggles only soundboard enforcement behavior (kick/disconnect on soundboard), not connection stickiness

## Implementation Status (Completed)

- Implemented command surface:
  - added top-level `/connect channel:<voice>` and `/disconnect`
  - extended `/settings` with `soundboard state:on|off`
- Implemented modular state model in guild settings:
  - `soundboard_enforcement_enabled`
  - `managed_voice_channel_id`
  - `managed_voice_connected_at`
- Implemented gateway modular runtime:
  - `ManagedVoiceController` owns sticky voice connection and reconnect-on-move/kick behavior
  - `SoundboardEnforcement` is a separate module that only handles soundboard kick behavior when enabled
  - connection ownership and enforcement are decoupled for future enforcement modules
- Reconcile behavior:
  - periodic gateway reconcile enforces managed voice state from persisted settings
  - gateway sets/clears `managed_voice_connected_at` on connection establish/clear
- Test status:
  - updated command catalog/policy/routing tests for new commands and settings subcommand
  - added gateway tests for managed reconnect and soundboard enforcement module
  - targeted suite passed (`120 passed`)

## Voice Enforcement Plan Continuation (Draft)

- Phase 0 (decision applied): `/connect channel:<voice>` is required; gateway must auto-recover on move + kick/disconnect; enforcement toggles are independent modules.
- Phase 1 (settings + schema):
  - extend guild settings schema with `soundboard_enforcement_enabled`, `managed_voice_channel_id`, `managed_voice_connected_at`
  - define separate state transitions for enforcement (`soundboard on/off`) vs connection (`connect/disconnect`) plus restart/channel deletion
  - add migration/defaulting behavior so existing guild docs stay valid
- Phase 2 (command surface):
  - implement `/settings soundboard on|off` as enforcement-module toggle only
  - implement `/connect channel:<voice>` and `/disconnect` as control-plane commands that mutate desired state and request gateway reconciliation
  - keep all voice session actions out of `services/commands.py`
- Phase 3 (gateway voice manager):
  - add a per-guild managed voice controller in `services/gateway.py`
  - on managed connection active + channel configured, ensure bot is connected to the managed channel
  - on bot move/kick/disconnect, reconcile by reconnecting if managed connection is still active
  - run soundboard enforcement only when both: (a) bot is connected to the managed channel, and (b) `soundboard_enforcement_enabled = true`
  - schedule idle leave after 5 minutes when channel is bot-only; cancel timer if a member rejoins
- Phase 4 (determinism + restart behavior):
  - restore managed state from Mongo on startup and reconcile actual connection against desired state
  - avoid duplicate connect attempts with a per-guild lock/backoff strategy
  - persist `managed_voice_connected_at` when connection is established/cleared
- Phase 5 (tests + docs):
  - command tests for required `channel` argument, permissions, validation, and desired-state writes
  - gateway tests for reconnect-on-move, reconnect-on-kick/disconnect, idle timeout, and enforcement preconditions
  - update `README.md`, `COMMANDS.md`, `AGENTS_README.md` with final behavior and admin workflow

## Resolved Product Decision

- `/connect` uses one required voice-channel argument.
- `/connect` and `/disconnect` control only connection ownership and sticky behavior.
- `/settings soundboard on|off` controls only whether soundboard usage causes member disconnects.
- Gateway ownership is strict: if the bot is moved or kicked/disconnected, it must automatically return to the managed channel while managed connection is active.
- Reconnect suppression cases: only `/disconnect` or explicit managed channel clear should stop auto-return.

## Documentation Notes

- Maintainer docs should describe legacy commands as removed work, not intentionally retained compatibility aliases.
- The review artifact should stay scoped to the four user-listed problems unless `Problems` changes.
- Keep memory concise; broader infra and test-review details belong in the review artifact, not here.
- If voice enforcement work starts, update `README.md`, `COMMANDS.md`, and `AGENTS_README.md` together with `CODEX_WORKLOG.md`.

## Files To Check When Resuming

- `docs/problem-review-plan-2026-04-25.md`
- `CODEX_WORKLOG.md`
- `AGENTS_README.md`
- `docs/README.md`
