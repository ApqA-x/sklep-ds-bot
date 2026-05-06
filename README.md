# Voice Tracker

Discord voice-session tracker built as Python microservices with MongoDB and NATS.

Last updated: May 7, 2026.

## What Changed

Recent production stabilization changes include:

- Migrated invite behavior toggles from env-first controls to guild settings managed by Discord commands.
- Added a new `services.activity` service that consumes `activity.events` and posts member/invite lifecycle embeds.
- Added settings controls for invite snapshot/live/userinfo/reconciliation and activity channel/event-type toggles.
- Gateway now publishes activity events for member join/leave, invite create/delete, and invite attribution outcomes.

- Removed legacy root commands `/audit`, `/bot-setting`, `/track`, and `/track-list` from the public command surface.
- Tracking is all-channel.
- Fixed `/inspect` routing/permission edge cases for top-level options.
- Auto-unmute handling covers server-controlled mute and deafen states for listed users.
- `/userinfo` shows presence status only when Presence Intent is enabled.
- `/dashboard` now renders a paginated leaderboard with arrow buttons and `hours:minutes:seconds` totals.
- Added `/connect channel:<voice>` and `/disconnect` for sticky managed voice connection.
- Added `/settings soundboard on|off` as an independent enforcement toggle.
- Gateway now auto-reconciles managed voice state and auto-returns the bot to the configured channel after move/kick/disconnect.
- Soundboard enforcement is modular and only active when enabled.
- Added repository read models for dashboard and user profile totals.
- Hardened tracker leave/close fallback behavior when runtime state is missing.
- Added startup retry/backoff in writer service and expanded CI test lanes.

## Services

- `services.gateway`: consumes Discord voice-state updates, supervises sticky managed voice connections, applies voice enforcement modules, publishes `voice.events`, and delivers session summaries.
- `services.tracker`: owns session/participant lifecycle and emits `session.closed`.
- `services.writer`: consumes `session.closed`, builds summary text, emits `session.summary`.
- `services.commands`: owns slash command registration and command execution.
- `services.activity`: consumes `activity.events` and posts configurable activity embeds.

Shared package code is in `voice_tracker/`.

## Event Flow

1. Gateway receives Discord voice-state updates.
2. Gateway publishes `voice.events` to NATS.
3. Tracker updates sessions/participants in MongoDB.
4. Tracker emits `session.closed` when a voice channel session ends.
5. Writer generates summary payload and emits `session.summary`.
6. Gateway receives `session.summary` and posts it to the configured output channel.
7. Gateway emits `activity.events` for member/invite lifecycle events.
8. Activity service receives `activity.events` and posts embeds to the configured activity channel.

## Command Docs

Full command reference lives in [COMMANDS.md](COMMANDS.md).

## Runtime Dependencies

- Python 3.11+
- MongoDB
- NATS
- davey (required by current discord.py voice connection)
- PyNaCl (legacy voice backend support)
- Discord bot token + app/guild IDs

## Configuration

Use `.env.example` as baseline.

Important variables:

- `DISCORD_TOKEN`
- `DISCORD_APPLICATION_ID`
- `DISCORD_GUILD_ID`
- `MONGO_URI`, `MONGO_DB`
- `NATS_URL`
- `EVENT_SIGNING_SECRET`

`BOT_ADMIN_USER_IDS` remains for compatibility but is not the primary admin policy path.

Invite and activity behavior is configured at runtime through `/settings` and persisted in `guild_settings`.

## Local Development

```bash
python -m pip install -e ".[test]"
pytest -q
```

## Docker Compose

```bash
docker compose up --build
```

See [EXAMPLES.md](EXAMPLES.md) for sample environment and compose usage.

## Deployment Source Of Truth

Production Docker Swarm deployment is managed from the sibling infra repository `../sklep-bot-k`.
Treat that repo as the source of truth for `docker-stack.yaml`, release promotion, and remote deploy workflow.
The local `stack.yaml` in this repo is not the active production deployment contract.

## CI

`CI` workflow currently runs:

- fast lanes: Python 3.11 and 3.12
- forward canary: Python 3.14 (non-blocking)
- deprecation-strict lane: `pytest -W error::DeprecationWarning` (non-blocking)

`Release` workflow publishes service images for `gateway`, `tracker`, `writer`, `commands`, and `activity`.

## Documentation Map

- [README.md](README.md): project overview and setup
- [COMMANDS.md](COMMANDS.md): slash command catalog and behavior
- [EXAMPLES.md](EXAMPLES.md): compose and env examples
- [AGENTS_README.md](AGENTS_README.md): maintainer notes for agent-driven updates
- `docs/`: historical research and migration notes
