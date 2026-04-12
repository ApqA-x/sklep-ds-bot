# Voice Tracker

Modular Discord voice-session tracker built around Python services, MongoDB, and NATS.

## What It Does

- Detects the first join and last leave of Discord voice and stage channels.
- Stores session and participant history in MongoDB.
- Builds recaps when a session ends.
- Posts recaps back to the configured audit/output channel.
- Lets admins manage voice tracking, audit output, autorole, inspection, and command metadata through `/audit`, `/bot-setting`, `/track`, `/track-list`, `/inspect`, and `/autorole`.
- Lets any user jump into a visible voice/stage channel, view aggregate dashboards, and inspect member voice stats through `/jump`, `/dashboard`, and `/userinfo`.

## Services

- `services.gateway`: Discord entrypoint. Converts Discord voice events into NATS messages, posts session summaries, and handles member-join side effects when enabled.
- `services.tracker`: Owns voice-session lifecycle and participant timing in MongoDB.
- `services.writer`: Turns a closed session into a formatted recap.
- `services.commands`: Owns the public Discord slash-command registration and the admin/public command handlers.

## Runtime Dependencies

- MongoDB: persistent state, guild settings, sessions, participants, and replay markers.
- NATS: service-to-service event bus.
- Discord API: voice state intake, slash commands, and recap delivery.

## CI/CD

- `CI` installs Python dependencies, compiles the package, and runs pytest.
- After `CI` succeeds on `main`, `Release` publishes immutable `ghcr.io/robinlant/sklep-ds-bot/<service>:<sha>` images for `gateway`, `tracker`, `writer`, and `commands`, then records the next semver git tag from Conventional Commit messages.

## Event Flow

1. Gateway receives a Discord voice-state update.
2. Gateway publishes `voice.events`.
3. Tracker consumes the event and updates `voice_sessions` / `voice_session_participants`.
4. Tracker emits `session.closed` when a tracked channel becomes empty.
5. Writer consumes the close event, reads MongoDB, and generates a summary.
6. Writer stores the summary data and emits `session.summary`.
7. Gateway consumes `session.summary` and posts the recap.

## Configuration

See `.env.example` for the shared environment layout and `EXAMPLES.md` for a copy-and-run walkthrough.

Important values:

- `DISCORD_TOKEN`: Discord bot token.
- `DISCORD_APPLICATION_ID`: Discord application ID used by `services.commands` for guild-scoped bulk command registration.
- `DISCORD_GUILD_ID`: the single guild currently supported by this deployment.
- `BOT_ADMIN_USER_IDS`: legacy compatibility allowlist only. It is not an authorization path for target `ADMIN_ONLY` commands.
- `TRACKING_MODE`: startup default, usually `all`, `none`, or `specific`.
- `TRACKED_CHANNEL_IDS`: optional startup list for `specific` mode.
- `EVENT_SIGNING_SECRET`: shared event signing secret for the NATS envelope path.

## Local Development

Install Python 3.12, then run:

```bash
python -m pip install -e ".[test]"
pytest
```

## Examples

- `EXAMPLES.md` (compose usage, `.env` example, and setup notes)
- `docker-compose.yml` (editable local stack)

## Command Catalog

### ADMIN_ONLY

- `/audit channel:<text>` sets the configured audit/output channel for summaries and bot output.
- `/bot-setting` shows the current command settings, tracked channels, audit channel, and autorole configuration.
- `/track add channel:<voice|stage>` adds one tracked channel.
- `/track remove channel:<voice|stage>` removes one tracked channel.
- `/track list` shows the tracked channel list.
- `/track-list clear` clears the tracked channel list.
- `/inspect channel:<voice|stage>` shows active session details for one voice or stage channel.
- `/autorole role:<role>` configures the role that will be assigned on member join.

### ALL_USER

- `/jump channel:<voice|stage>` moves the invoking user into a visible voice or stage channel.
- `/dashboard` shows a guild voice-time leaderboard.
- `/userinfo user:<member>` shows the selected member's profile and voice totals.

## Recommended First Setup

1. Run `/audit` to choose where summaries and other bot output should go.
2. Run `/bot-setting` to confirm the current guild settings and registration state.
3. Configure the tracked channels with `/track add`, `/track remove`, `/track list`, and `/track-list clear`.
4. Configure `/autorole` if you want members to receive a role on join.
5. Use `/jump`, `/dashboard`, and `/userinfo` as the user-facing commands.

If no audit channel is configured, recaps cannot be delivered until one is set. Keep that destination in place before relying on voice-session summaries.

## Command Notes

- `ADMIN_ONLY` commands should be hidden in Discord UI with `default_member_permissions`, but runtime authorization still needs to enforce Administrator-only access.
- `ALL_USER` commands still need guild-only, visibility, and safety checks even though they do not require admin permissions.
- `/userinfo` should suppress unwanted mentions and omit message counts in the MVP.
- `/dashboard` should stay focused on aggregate voice totals and safe leaderboard output.
- `/jump` should only move the invoking user and should never move arbitrary members.
- `/audit` replaces the old summary/output channel behavior.

## Permissions

- `ADMIN_ONLY` means Discord Administrator at runtime.
- `ALL_USER` means no admin gate, but operation-specific validation still applies.
- `default_member_permissions` does not replace runtime checks.
- `BOT_ADMIN_USER_IDS` is not part of the target admin policy.
- `services.commands` is the only service that should bulk overwrite the Discord command catalog.

## For AI Agents

- Keep services thin and role-focused.
- Treat `voice_tracker.domain` as the shared contract layer.
- Treat NATS subjects as the service boundary.
- Do not move persistence logic into `services/*` unless the service owns the state.
- Prefer minimal changes that preserve the event flow above.
