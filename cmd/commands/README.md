# Commands Service

Owns the `/voice` admin command tree for guild settings and live session inspection.

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

## Commands

- `/voice config mode show|set`
- `/voice config channels add|remove|list|clear`
- `/voice config summary-channel set|clear`
- `/voice inspect settings`
- `/voice inspect sessions`
- `/voice inspect session`

## How It Fits

- Input: Discord interactions.
- Output: guild settings in MongoDB.
- Depends on: `internal/commands`, `internal/mongo`, `internal/config`, and Discord permissions.

## AI Notes

- This is the control plane for per-guild configuration.
- Keep permission checks here.
- Add future admin commands here before exposing any external API.
