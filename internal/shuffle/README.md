# Shuffle Service

Implements live voice redistribution for the `/shuffle` command tree.

## What It Does

- Reads current voice occupants from Discord state.
- Filters out excluded users and bots.
- Balances the remaining users across the requested channels.
- Gathers members from every accessible voice channel or chosen source channels into one destination channel.
- Moves members with Discord voice member moves.

## How It Fits

- Input: live voice state and slash-command parameters.
- Output: a balanced move plan, or a gather plan into one destination channel.
- Depends on: Discord state cache, member move permissions, and the Discord API.

## AI Notes

- Keep the service generic and reusable.
- Do not mix it with tracker/session persistence.
- Return a hard error when movable users are fewer than the target channels.
- Keep gather commands picker-based so moderators do not need to paste raw IDs.
- Skip inaccessible source channels during gather-all instead of hard-failing.
