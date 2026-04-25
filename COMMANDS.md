# Command README

Slash command reference for the current production command surface.

Last updated: April 25, 2026.

## Access Model

- `ADMIN_ONLY`: requires Discord `Administrator` at runtime.
- `ALL_USER`: no admin requirement, but command-specific validation still applies.

## Command Catalog

### ADMIN_ONLY

- `/settings show`
  - Show current bot settings.
- `/settings mode mode:all`
  - Show or confirm all-channel tracking mode. Only `all` is supported.
- `/settings summary-set channel:<text>`
  - Set summary/output channel.
- `/settings summary-clear`
  - Clear configured summary channel (fallback behavior applies).

- `/inspect channel:<voice|stage>`
  - Show active session details for one channel.

- `/autorole role:<role>`
  - Configure autorole (runtime safety checks enforce assignable/safe role).

- `/unmute add user:<member>`
  - Add member to auto-unmute list for automatic server mute/deafen clearing.
- `/unmute remove user:<member>`
  - Remove member from auto-unmute list.
- `/unmute list`
  - Show auto-unmute list.

### ALL_USER

- `/jump channel:<voice|stage>`
  - Move invoking user to target channel if visibility/permission checks pass.

- `/dashboard`
  - Show a paginated voice-time leaderboard with bottom arrow buttons.
  - Each row shows display name, clickable member mention, and total time as `hours:minutes:seconds`.

- `/userinfo user:<member>`
  - Show member profile summary and total voice time.
  - Presence status is shown only when Presence Intent is enabled for the commands service.

## Notes

- Tracking is all-channel; there are no public per-channel tracking commands.
- Command registration owner is `services.commands`.
- Runtime policy enforcement exists in command handlers; Discord UI visibility alone is not treated as authorization.
- `/inspect` supports additional internal route forms for compatibility/tests, but only the public route above is registered.
