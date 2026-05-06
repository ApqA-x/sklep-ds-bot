# Command README

Slash command reference for the current production command surface.

Last updated: May 7, 2026.

## Access Model

- `ADMIN_ONLY`: requires Discord `Administrator` at runtime.
- `ALL_USER`: no admin requirement, but command-specific validation still applies.

## Command Catalog

### ADMIN_ONLY

- `/settings show`
  - Show current bot settings.
- `/settings mode mode:all`
  - Show or confirm all-channel tracking mode. Only `all` is supported.
- `/settings soundboard state:on|off`
  - Toggle only soundboard kick enforcement.
  - Does not connect/disconnect the bot from voice by itself.
- `/settings summary-set channel:<text>`
  - Set summary/output channel.
- `/settings summary-clear`
  - Clear configured summary channel (fallback behavior applies).

- `/settings invite-snapshot state:on|off`
  - Toggle invite snapshot sync used for attribution seeding.
- `/settings invite-live state:on|off`
  - Toggle live invite attribution on member joins.
  - Turning this on auto-requires snapshot sync.
- `/settings invite-userinfo state:on|off`
  - Toggle invite attribution lines in `/userinfo`.
- `/settings invite-reconcile state:on|off`
  - Toggle periodic invite catalog reconciliation from audit logs.

- `/settings activity-channel-set channel:<text>`
  - Set the text channel used by `services.activity` embeds.
- `/settings activity-channel-clear`
  - Disable activity message destination.
- `/settings activity-member-join state:on|off`
  - Toggle member join activity embeds.
- `/settings activity-member-leave state:on|off`
  - Toggle member leave activity embeds.
- `/settings activity-invite-create state:on|off`
  - Toggle invite create activity embeds.
- `/settings activity-invite-delete state:on|off`
  - Toggle invite delete activity embeds.
- `/settings activity-invite-used state:on|off`
  - Toggle invite-used attribution activity embeds.

- `/connect channel:<voice|stage>`
  - Set managed voice channel and enable sticky voice presence.
  - Gateway keeps the bot in that channel (auto-return on move/kick/disconnect).

- `/disconnect`
  - Clear managed voice channel and stop sticky voice behavior.
  - Gateway disconnects the bot from voice.

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
- Invite and activity settings are command-driven and persisted per guild.
- Voice connection management is independent from enforcement toggles.
- Soundboard enforcement runs only when both conditions are true: managed voice connection is active and `/settings soundboard` is `on`.
- Command registration owner is `services.commands`.
- Runtime policy enforcement exists in command handlers; Discord UI visibility alone is not treated as authorization.
- `/inspect` supports additional internal route forms for compatibility/tests, but only the public route above is registered.
