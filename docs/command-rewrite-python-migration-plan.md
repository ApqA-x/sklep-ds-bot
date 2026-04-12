# Command Rewrite And Python Migration Plan

Date: 2026-04-12
Status: planning only
Owner: future implementation agents

This file is a handoff plan. It intentionally does not change runtime behavior. The
working tree was already dirty before this plan was written; do not revert existing
changes unless the user explicitly asks for that.

## Inputs

- User command spec: external Desktop text file supplied in this task.
- GitHub source: `origin/main` fetched on 2026-04-12 and verified at
  `f2a9ca5b8f5c656edbeaddc45aadabd9164ead14`.
- Current branch: `codex/rewrite-python-services`, based on a Python rewrite commit
  `af3288b5cbd861b70cea5fb9eb5e6450778820c1`.
- Existing research: `docs/discord-command-ux-research.md`.
- Existing migration notes: `CODEX_WORKLOG.md`.

## Executive Summary

The Go to Python migration is mostly implemented on the current branch. `origin/main`
still contains the Go source under `cmd/` and `internal/`, while the current branch
has Python services under `services/` and shared package code under `voice_tracker/`.

The remaining work should be treated as:

1. Validate Python parity against the Go source from `origin/main`.
2. Replace the current public slash command catalog with the command set from the
   user spec.
3. Centralize command authorization so `ADMIN_ONLY` and `ALL_USER` are explicit,
   tested policies.
4. Add missing storage and Discord runtime integration for audit output, autorole,
   self-jump, dashboard, and userinfo.
5. Update docs, tests, CI expectations, and deploy config.

Default final public command catalog:

- `/audit`
- `/bot-setting`
- `/track`
- `/track-list`
- `/jump`
- `/inspect`
- `/autorole`
- `/dashboard`
- `/userinfo`

Product decision from 2026-04-12: remove `/shuffle` from the final public command
catalog. Existing shuffle code can be deleted later or left as private/dead code only
if that lowers implementation risk, but it must not be registered as a slash command.

## Current Architecture

Python package:

- `pyproject.toml`: package `voice-tracker`, Python `>=3.11`, dependencies
  `aiohttp`, `discord.py`, `nats-py`, and `pymongo`.
- `voice_tracker/domain.py`: shared contracts for settings, voice events, sessions,
  participants, and summaries.
- `voice_tracker/repository.py`: MongoDB repository for `guild_settings`,
  `processed_messages`, `voice_sessions`, and `voice_session_participants`.
- `voice_tracker/bus.py`: signed NATS envelope wrapper with issuer checks and dedupe.
- `voice_tracker/appcommands.py`: command catalog serialization and bulk overwrite
  payload conversion.
- `voice_tracker/commands.py`: current voice admin commands: `/settings`, `/track`,
  `/inspect`.
- `voice_tracker/shuffle.py`: current `/shuffle` service logic.

Runtime services:

- `services.gateway`: Discord voice-state intake, `voice.events` publisher, summary
  delivery subscriber.
- `services.tracker`: voice session lifecycle owner.
- `services.writer`: closed-session summary generator.
- `services.commands`: current `/settings`, `/track`, `/inspect` Discord handler.
- `services.shuffle`: current `/shuffle` Discord handler.

Operational shape:

- `Dockerfile` builds one Python image and selects the service through `SERVICE`.
- `docker-compose.yml` starts Mongo, NATS, and five app services.
- CI installs `.[test]`, runs `python -m compileall voice_tracker services tests`,
  then runs `pytest`.

Key architecture risk and decision:

- `services.commands` and `services.shuffle` both call
  `register_commands_http(..., commands())`. Discord bulk command registration is a
  full catalog replacement, so whichever service writes last becomes the visible
  command catalog.
- Decision: `services.commands` is the only service allowed to bulk register slash
  commands for the MVP. `services.shuffle` and any other service must not call
  `register_commands_http` or register `/shuffle`. They may keep private reusable
  runtime helpers only if that lowers migration risk.

## Current Command Inventory

Current public commands:

- `/settings`: show or change guild tracking settings. Runtime access is
  `BOT_ADMIN_USER_IDS` allowlist, Administrator, or Manage Guild.
- `/track`: add, remove, list, or clear tracked voice/stage channels. Runtime access
  is `BOT_ADMIN_USER_IDS` allowlist, Administrator, or Manage Guild.
- `/inspect`: inspect active and closed voice sessions. Runtime access is
  `BOT_ADMIN_USER_IDS` allowlist or Administrator.
- `/shuffle`: gather or redistribute voice members. Runtime access is
  `BOT_ADMIN_USER_IDS` allowlist, Administrator, or Move Members.

These access paths describe the current implementation only. The target rewrite
removes `BOT_ADMIN_USER_IDS`, Manage Guild, and Move Members as ways to satisfy
`ADMIN_ONLY`.

Target decision: remove `/shuffle` from the registered public command catalog.

Missing from the target spec:

- `/audit`
- `/bot-setting`
- `/track-list clear`
- `/jump`
- `/autorole`
- `/dashboard`
- `/userinfo`

Partially present:

- `/track add/remove/list` exists but is not strict admin-only today.
- `/inspect channel:<voice>` is close to current `/inspect active channel`.
- `/bot-setting` overlaps with current `/settings show`.

## Permission Policy

Use these terms consistently in code, tests, and docs.

`ADMIN_ONLY`:

- Runtime policy: Discord Administrator permission only.
- Do not allow `BOT_ADMIN_USER_IDS`, Manage Guild, custom role allowlists, or other
  delegation paths to satisfy `ADMIN_ONLY` in the target command set.
- Do not rely only on Discord command visibility or `default_member_permissions`.
- For role management, also validate role hierarchy and bot capability even when the
  invoking user is an administrator.
- Keep `ADMIN_ONLY` strict. If non-admin delegation is desired later, treat it as a
  separate product decision and do not add it during this MVP.

`ALL_USER`:

- Means no admin permission is required.
- It does not mean no security boundary.
- Still require guild-only execution, same-guild resolved objects, target resource
  visibility, and operation-specific safety checks.

Suggested Discord command visibility:

- `ADMIN_ONLY` commands: set `default_member_permissions` to Administrator where the
  local command dataclass supports it, while keeping runtime checks.
- `ALL_USER` commands: no admin-only `default_member_permissions`.

Implementation notes:

- Do not use `voice_tracker.botauth.is_allowlisted` for target `ADMIN_ONLY` command
  authorization. It may remain only for legacy/private code during migration if
  deleting it would increase implementation risk.
- Remove or mark `BOT_ADMIN_USER_IDS` as unused in docs/config if no remaining runtime
  path needs it after the command rewrite.
- If any `/shuffle` code remains during the transition, keep it out of the registered
  command catalog and do not expose its weaker allowlist path publicly.

## Target Command Matrix

| Command | Access | Current equivalent | Data needed | Implementation notes |
| --- | --- | --- | --- | --- |
| `/audit channel:<text>` | `ADMIN_ONLY` | Closest: `/settings summary-set` | reuse `guild_settings.summaryChannelId` or rename to `auditChannelId` with migration | Product decision: `/audit` replaces the current summary/output channel behavior. New implementations should treat the audit channel as the configured destination for bot output and session summaries. |
| `/bot-setting` | `ADMIN_ONLY` | Closest: `/settings show` | guild settings, bot version, guild install/created timestamp | Show audit channel, tracked channels, tracking mode, autorole role, version, and when settings were first created. |
| `/track add channel:<voice>` | `ADMIN_ONLY` | Exists as `/track add` | existing `trackedChannelIds` | Keep voice/stage channel type validation. Change permission policy to strict `ADMIN_ONLY`. |
| `/track remove channel:<voice>` | `ADMIN_ONLY` | Exists as `/track remove` | existing `trackedChannelIds` | Same as above. |
| `/track list` | `ADMIN_ONLY` | Exists as `/track list` | existing `trackedChannelIds` | Same as above. |
| `/track-list clear` | `ADMIN_ONLY` | Closest: `/track clear` | existing `trackedChannelIds` | Add top-level `track-list` command with `clear` subcommand. Optionally keep `/track clear` only as temporary alias if the user approves. |
| `/jump channel:<voice>` | `ALL_USER` | None | Discord runtime state/mover | Move only the invoking member. Validate target guild/type, require that the invoking user can see the channel, and require that the bot can view/connect/move. Product decision: do not require the invoking user to have Connect permission, because this command is intended to let users jump into visible channels via the bot. Allow jumping into full voice channels; do not block on channel capacity. Do not move arbitrary users. |
| `/inspect channel:<voice>` | `ADMIN_ONLY` | Closest: `/inspect active channel` | active sessions and participants | Simplify target command shape to one channel argument. If history is still needed, ask before keeping the current history subcommands. |
| `/autorole role:<role>` | `ADMIN_ONLY` | None | `guild_settings.autoRoleId`; Discord member join handler | Reject `@everyone`, managed roles, roles above or equal to the bot top role, and risky roles unless explicitly allowed. Add `on_member_join` in the gateway or a new service. |
| `/dashboard` | `ALL_USER` | None | aggregate voice totals | Paginated leaderboard by total voice time. Use ephemeral by default unless public sharing is approved. Pagination callbacks must be limited to the invoking user or admins. |
| `/userinfo user:<member>` | `ALL_USER` | None | selected member profile, roles, total voice time | Product decision: add a member argument so users can view another member. Keep hidden-channel data out of the response, truncate long role lists, suppress mention pings, and omit message count from the MVP. |

## Data Model Plan

Extend `domain.GuildSettings` and Mongo `guild_settings` carefully:

- `audit_channel_id` / `auditChannelId` only if the implementation renames the
  existing summary/output setting. Product decision: `/audit` replaces current
  summary/output channel behavior, so reusing `summary_channel_id` /
  `summaryChannelId` is acceptable and probably lower risk.
- `auto_role_id` / `autoRoleId`
- optional `bot_added_at` / `botAddedAt` if `created_at` is not sufficient

Keep backwards compatibility:

- Existing `summary_channel_id` / `summaryChannelId` is already used by session
  recap delivery and is the preferred compatibility path for `/audit`.
- If the field is renamed to audit, preserve reads from existing `summaryChannelId`
  and write both or migrate once in a controlled step.

Add read models for dashboard and userinfo:

- Option A: aggregate from `voice_session_participants` on demand.
  - Pros: no new write path, accurate for existing voice history.
  - Cons: can become slow as history grows; needs indexes by guild/user and maybe
    duration.
- Option B: maintain denormalized `user_stats`.
  - Pros: fast dashboard/userinfo.
  - Cons: new write/update path, backfill needed, more consistency risk.
- Decision: start with repository aggregate methods and add indexes. Move to
  denormalized `user_stats` only if tests or production data show queries are too
  slow.

Message counts:

- Out of scope for the MVP.
- Do not add message event intake or `guild_user_message_stats` storage during the
  command rewrite unless the user explicitly reopens the requirement.
- `/userinfo` should omit message count entirely for the MVP rather than showing an
  unavailable placeholder.
- If message counts are added later, first confirm exact Discord intents and decide
  whether historical backfill is possible.

Autorole:

- Store one role id first. Do not support multiple roles until a real requirement
  appears.
- Add repository methods for get/set autorole. Add explicit clear/unset support only
  later if the user approves that product behavior.
- Setting a new safe autorole replaces the previous `autoRoleId`; there must be only
  one active configured autorole at a time.
- Validate the new role before persisting. If validation fails, keep the previous
  setting unchanged rather than clearing it.
- Add gateway `on_member_join` handling or a separate `autorole` service.
- Log failures and do not crash the gateway if Discord rejects assignment.

Audit channel:

- Product decision: `/audit` replaces current summary/output channel behavior.
- Use the configured audit channel for session summaries and future bot output that
  needs a configured text channel.
- Setting a new audit channel replaces the previous configured audit/summary output
  channel after validation; there must be only one active configured output channel.
- Keep interactive command responses ephemeral unless the command explicitly needs to
  post in the audit channel.

## Runtime And Service Plan

Command registration:

- `services.commands` is the single owner for Discord bulk command registration.
- Treat bulk registration as "replace the whole Discord command catalog". If a second
  service starts later and writes an older or smaller catalog, Discord will keep that
  later catalog and the MVP commands can disappear or `/shuffle` can reappear.
- `services.shuffle` must not call `register_commands_http`, must not register
  `/shuffle`, and should be removed from command-registration startup paths.
- Other services may contain runtime helpers or event listeners, but they must not
  bulk overwrite the Discord command catalog.
- Add a lightweight verification step, such as a focused test or code search in the
  implementation checklist, that confirms `register_commands_http` is only called
  from the chosen registration path.

Command handling:

- Add a central command policy table:
  - command name
  - access class
  - default Discord permission bit
  - handler function
  - ephemeral/public response default
- Keep runtime checks close to handler dispatch, before side effects.
- Keep validation helpers for resolved channel/role/user objects.

`/jump`:

- Reuse safe pieces from `voice_tracker.shuffle`, but do not call gather/equal
  directly.
- Add a focused self-move service method with this contract:
  - input: guild id, invoking user id, target channel id
  - validates target channel is voice/stage in the same guild
  - validates invoking member can view the target channel
  - intentionally does not require the invoking member to have Connect permission
  - validates bot can view/connect/move members
  - ignores target channel capacity for normal users, per product decision
  - moves only invoking user
- Response should be ephemeral.

`/dashboard`:

- Build a page model independent from Discord UI components.
- Page size target: 10 rows.
- Include rank, display name, total voice time.
- Do not show hidden channel details to all users.
- Use Discord components only after response models support fields/footer/components
  or use native discord.py views in service code with testable domain page builders.

`/userinfo`:

- Add `user:<member>` as an argument.
- Include avatar, roles, and total voice time for the selected member.
- Role list can be long. Truncate safely and avoid pings through allowed mentions.
- Do not include message count in the MVP.

`/autorole`:

- Validate role before persisting.
- Persisting a new safe role overwrites the previous autorole setting. Do not keep
  multiple active autoroles or merge the old and new settings.
- If the submitted role is unsafe or invalid, leave the previous setting intact.
- Add join-time assignment with defensive error handling.
- Add explicit output telling the admin which role was configured.

`/bot-setting`:

- Should read from one settings view builder so `/audit`, `/track`, and `/autorole`
  changes remain visible in one place.
- Include version from package metadata or an environment variable only if already
  available; otherwise add it as a follow-up task.

## Go To Python Migration Plan

Treat Python migration as implemented but not fully signed off.

Before implementation:

1. Fetch latest GitHub refs again: `git fetch origin main`.
2. Compare Go source from `origin/main` against Python modules:
   - `internal/domain/*` -> `voice_tracker/domain.py`
   - `internal/mongo/repository.go` -> `voice_tracker/repository.py`
   - `internal/bus/nats.go` -> `voice_tracker/bus.py`
   - `internal/tracker/service.go` -> `voice_tracker/tracker.py`
   - `internal/summary/service.go` -> `voice_tracker/summary.py`
   - `internal/gateway/service.go` -> `voice_tracker/gateway.py`
   - `internal/commands/*` -> `voice_tracker/commands.py`
   - `internal/shuffle/*` -> `voice_tracker/shuffle.py`
3. Confirm Mongo schema compatibility:
   - collection names
   - field names
   - partial unique active-session indexes
   - dedupe indexes and TTL
   - summary delivery claim fields
4. Confirm NATS contract compatibility:
   - subject names
   - envelope signing
   - issuer mapping
   - payload field names
5. Confirm Docker/CI parity:
   - service names and image tags
   - env vars
   - release workflow matrix

No Go files need to be restored if parity tests pass.

## Suggested Agent Work Breakdown

Agent A: command catalog and auth policy

- Own files: `voice_tracker/appcommands.py`, `voice_tracker/commands.py`,
  `voice_tracker/botauth.py`, `tests/test_appcommands.py`, `tests/test_commands.py`.
- Build target command payloads and central permission policy.
- Implement `ADMIN_ONLY` as Discord Administrator-only. Do not allow
  `BOT_ADMIN_USER_IDS`, Manage Guild, or custom role allowlists for target admin
  commands.
- Add `default_member_permissions` tests.
- Do not implement dashboard data queries or Discord movement.

Agent B: storage and read models

- Own files: `voice_tracker/domain.py`, `voice_tracker/repository.py`,
  repository-focused tests.
- Add or alias the audit/output setting, and add autorole storage.
- Add aggregate methods for dashboard and userinfo voice totals.
- Do not add message stats storage for the MVP.

Agent C: Discord runtime integration

- Own files: `services/commands.py`, `services/gateway.py`, possibly a new service
  only if needed.
- Implement interaction dispatch for new commands, self-jump runtime behavior,
  and autorole member join handling.
- Coordinate with Agent A on handler names and models.
- Ensure `services.commands` is the only service that bulk registers Discord slash
  commands.

Agent D: dashboard and userinfo UX

- Own files: response/page builders, dashboard/userinfo tests, any view helpers.
- Build pagination as testable pure logic first.
- Keep Discord component code thin and callback ownership restricted.

Agent E: docs, operations, and migration validation

- Own files: `README.md`, `EXAMPLES.md`, `.env.example`, `stack.yaml`,
  `CODEX_WORKLOG.md`, CI docs if needed.
- Update command docs, env vars, deploy notes, and migration checklist.
- Verify `EXAMPLES.md` port notes match `docker-compose.yml`.

Important coordination rule:

- Do not have two agents edit the same file at the same time unless they explicitly
  coordinate a sequence. `voice_tracker/commands.py` and `tests/test_commands.py` are
  likely conflict hotspots.

## Testing Plan

Add or update tests in this order:

1. Command payload shape:
   - exact top-level target names
   - options and channel/role types
   - no old commands unless retained by product decision
   - admin commands have expected default member permissions
2. Permission matrix:
   - plain user denied for every `ADMIN_ONLY` command
   - `BOT_ADMIN_USER_IDS`/legacy allowlisted non-admin user denied
   - Administrator allowed
   - Manage Guild without Administrator denied
   - `ALL_USER` allowed only after operation-specific checks pass
3. Storage:
   - guild settings round trip for audit channel and autorole
   - setting a new audit channel replaces the previous configured output channel
   - setting a new safe autorole replaces the previous `autoRoleId`
   - invalid autorole replacement leaves the previous setting unchanged
   - aggregate voice totals
4. `/jump` safety:
   - rejects text channels
   - rejects cross-guild channels
   - rejects hidden channels for the invoking user
   - does not reject solely because the invoking user lacks Connect permission
   - rejects missing bot Move Members permission
   - never moves a user other than the invoker
   - allows full target channels when the invoking user can see the channel
5. `/autorole` safety:
   - rejects unsafe role targets
   - persists a safe role
   - handles join-time Discord assignment failures without crashing
6. Dashboard/userinfo:
   - deterministic ordering and page boundaries
   - empty states
   - `/userinfo user:<member>` uses the selected member, not always the invoker
   - long role/name truncation
   - callback ownership for pagination
7. Service integration:
   - `services.commands` interaction conversion and dispatch
   - `services.gateway` join listener if autorole is added
   - command registration owner does not race other services
   - no other service calls `register_commands_http` for the public catalog

Final verification commands in an environment with Python installed:

```bash
python -m pip install -e ".[test]"
python -m compileall voice_tracker services tests
pytest
```

If Docker is available:

```bash
docker compose build
docker compose config
```

## Documentation Plan

Update after implementation:

- `README.md`: replace current command docs with target command set and permission
  matrix.
- `EXAMPLES.md`: update setup flow and ensure local port examples match compose.
- `.env.example`: add new env vars if version or registration owner behavior needs
  them; remove or mark `BOT_ADMIN_USER_IDS` as unused if no remaining runtime path
  needs it.
- `stack.yaml`: document `IMAGE_TAG` expectations if not already covered elsewhere.
- `CODEX_WORKLOG.md`: append implementation summary and any unresolved risks.

## Open Questions For The User

Answered decisions:

- `/shuffle` should be removed from the final public command catalog.
- `/jump` should allow entry into a full channel when the invoking user can see that
  channel.
- `/jump` intentionally does not require the invoking user to have Connect permission.
- Use Discord Administrator permission only for `ADMIN_ONLY`; do not allow
  `BOT_ADMIN_USER_IDS`, Manage Guild, or custom role allowlists for target admin
  commands.
- `/userinfo` should include `user:<member>`.
- `/userinfo` should omit message count from the MVP.
- `/audit` replaces the existing summary/output channel behavior.
- `services.commands` is the only service that bulk registers Discord slash commands.
- New `/autorole` and `/audit` settings replace the previous configured value after
  successful validation, leaving only one active value.

Remaining questions:

- None at this planning level. Implementation agents should ask before making product
  changes beyond this document.

## Definition Of Done

- Public slash command catalog matches the accepted target matrix.
- Runtime authorization implements `ADMIN_ONLY` and `ALL_USER` policies with tests.
- `ADMIN_ONLY` requires Discord Administrator permission and does not accept
  `BOT_ADMIN_USER_IDS`, Manage Guild, or custom role allowlists.
- `services.commands` is the only public slash command registration owner; no other
  service bulk overwrites the Discord command catalog.
- Go to Python parity is checked against `origin/main`.
- Mongo data migration is backwards compatible with existing settings and session
  data.
- New audit and autorole settings replace the previous configured values after
  successful validation.
- `/userinfo` omits message count in the MVP.
- Dashboard and userinfo do not leak hidden-channel or unsafe mention data.
- CI passes with compileall and pytest.
- Docs describe the new command set, permission model, and deployment/env changes.
