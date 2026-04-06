# Shuffle Service

Owns the `/shuffle` command tree for live voice redistribution.

## What It Does

- Registers and handles the shuffle slash commands.
- Reads live voice occupancy from Discord state.
- Moves members across the requested voice channels.
- Gathers members into one destination channel from either all voice channels or selected source channels.
- Skips explicitly excluded users and bots.

## How To Use

- Run locally with `go run ./cmd/shuffle`.
- In Docker Compose, this is the `shuffle` service.
- Use the shared `.env.example` / `EXAMPLES.md` setup for Discord credentials.
- Requires Discord token, application ID, and guild ID.

## Commands

- `/shuffle gather all` means gather everyone from every accessible voice channel into one destination channel.
- `/shuffle gather select` means choose the destination channel and pick source channels with Discord channel pickers.
- `/shuffle equal two|three|four` means rebalance exactly 2, 3, or 4 voice channels.
- The gather flow uses channel pickers, so moderators never need to paste channel IDs.
- `/shuffle equal two`
- `/shuffle equal three`
- `/shuffle equal four`

## How It Fits

- Input: Discord interactions and live voice state.
- Output: voice member moves.
- Depends on: `internal/shuffle`, `internal/appcommands`, and Discord permissions.

## AI Notes

- Keep the command fixed and low-friction.
- Hard-fail when there are not enough movable users.
- Do not persist shuffle plans or member lists.
