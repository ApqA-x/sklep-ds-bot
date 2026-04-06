# Voice Tracker

Modular Discord voice-session tracker built around Go services, MongoDB, and NATS.

## What It Does

- Detects the first join and last leave of Discord voice channels.
- Stores session and participant history in MongoDB.
- Builds a recap when a session ends.
- Posts the recap back to Discord.
- Lets admins configure tracked channels and the recap channel through `/voice`.
- Lets moderators reshuffle live voice members through `/shuffle`.
- Lets allowlisted bot admins bypass guild permission checks through `BOT_ADMIN_USER_IDS`.

## Services

- `cmd/gateway`: Discord entrypoint. Converts Discord events into NATS messages and posts session summaries.
- `cmd/tracker`: Owns voice-session lifecycle and participant timing in MongoDB.
- `cmd/writer`: Turns a closed session into a formatted recap.
- `cmd/commands`: Owns the `/voice` guild admin command tree.
- `cmd/shuffle`: Owns the `/shuffle` reshuffle command tree.

## Runtime Dependencies

- MongoDB: persistent state and replay markers.
- NATS: service-to-service event bus.
- Discord API: voice state intake, slash commands, and recap delivery.

## CI/CD

- `CI` runs tests on every push and pull request.
- After `CI` succeeds on `main`, `Release` publishes immutable `ghcr.io/robinlant/sklep-ds-bot/<service>:<sha>` images for `gateway`, `tracker`, `writer`, and `commands`, then records the next semver git tag from Conventional Commit messages.

## Event Flow

1. Gateway receives a Discord voice-state update.
2. Gateway publishes `voice.events`.
3. Tracker consumes the event and updates `voice_sessions` / `voice_session_participants`.
4. Tracker emits `session.closed` when a channel becomes empty.
5. Writer consumes the close event, reads MongoDB, and generates a summary.
6. Writer stores the summary data and emits `session.summary`.
7. Gateway consumes `session.summary` and posts the recap.

## Configuration

See `.env.example` for the shared environment layout and `EXAMPLES.md` for a copy-and-run walkthrough.

## Examples

- `EXAMPLES.md` (compose usage, `.env` example, and setup notes)
- `infra/docker-compose.yml` (editable local stack)

## Commands

- `/shuffle equal two|three|four` balances exactly 2, 3, or 4 voice channels.
- `/voice config mode show|set`
- `/voice config channels add|remove|list|clear`
- `/voice config summary-channel set|clear`
- `/voice inspect settings`
- `/voice inspect sessions`
- `/voice inspect session`

## For AI Agents

- Keep services thin and role-focused.
- Treat `internal/domain` as the shared contract layer.
- Treat NATS subjects as the service boundary.
- Do not move persistence logic into `cmd/*` unless the service owns the state.
- Prefer minimal changes that preserve the event flow above.

## Service Docs

- `cmd/gateway/README.md`
- `cmd/tracker/README.md`
- `cmd/writer/README.md`
- `cmd/commands/README.md`
- `cmd/shuffle/README.md`
- `internal/bus/README.md`
- `internal/domain/README.md`
- `internal/mongo/README.md`
- `internal/summary/README.md`
- `internal/shuffle/README.md`
