# Codex Worklog

Updated: 2026-05-07, Europe/Berlin.

Current task: keep maintainer memory concise and aligned with shipped behavior.

## Current State

- Voice management is shipped as modular control + enforcement:
  - `/connect channel:<voice>` enables sticky managed voice connection.
  - `/disconnect` clears managed voice connection ownership.
  - `/settings soundboard on|off` toggles enforcement only.
- Gateway owns voice connection lifecycle and reconnect behavior.
- Soundboard enforcement runs only when the rule is enabled and the bot is connected to the managed channel.
- Production voice dependency hotfix is shipped in `v0.10.1` (`davey` + `PyNaCl` present in runtime images).
- Invite behavior toggles are command-driven through `/settings` and persisted in `guild_settings`.
- Gateway emits `activity.events`, and `services.activity` posts configurable member/invite lifecycle embeds.

## Memory Hygiene Policy

- Remove resolved planning/memory files once work is shipped.
- Remove solved debug screenshot PNGs before merge unless a PNG is explicitly required documentation.
- Keep only active review artifacts in `docs/README.md`; move long-term reference material to `docs/archive/`.
- Keep this file short and state-focused; detailed implementation history belongs in git history.

## Files To Check When Resuming

- `CODEX_WORKLOG.md`
- `AGENTS_README.md`
- `docs/README.md`
