# Commands Service

Owns the `/voice` admin command tree for guild settings, live session inspection, and closed-session history.

## What It Does

- Registers and handles Discord slash commands.
- Stores guild settings in MongoDB.
- Controls which channels are tracked and where summaries are posted.

## How To Use

- Run locally with `go run ./cmd/commands`.
- In Docker Compose, this is the `commands` service.
- Use the shared `.env.example` / `EXAMPLES.md` setup for MongoDB, NATS, and Discord credentials.
- Requires Discord token, application ID, guild ID, and MongoDB.
- Register commands before using `/voice` in the target guild.
- `/shuffle` is handled by the separate `cmd/shuffle` service.

## Commands

- `/voice config mode show|set`
- `/voice config channels add|remove|list|clear`
- `/voice config summary-channel set|clear`
- `/voice inspect settings`
- `/voice inspect sessions`
- `/voice inspect session`
- `/voice inspect history` lists recent closed sessions for one voice channel.
- `/voice inspect recent-session` inspects one closed session by recency index.

## How It Fits

- Input: Discord interactions.
- Output: ephemeral Discord responses and guild settings in MongoDB.
- Depends on: `internal/commands`, `internal/mongo`, `internal/config`, and Discord permissions.

## AI Notes

- This is the control plane for per-guild configuration.
- Keep permission checks here.
- Add future admin commands here before exposing any external API.
