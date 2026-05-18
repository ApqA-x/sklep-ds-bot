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
- `/settings soundboard state:on|off`
  - Toggle only soundboard kick enforcement.
  - Does not connect/disconnect the bot from voice by itself.
- `/settings summary-set channel:<text>`
  - Set summary/output channel.
- `/settings summary-clear`
  - Clear configured summary channel (fallback behavior applies).

- `/settings activity-channel-set channel:<text>`
  - Set the text channel used by `services.activity` embeds.
- `/settings activity-channel-clear`
  - Disable activity message destination.
- `/settings activity mode:off|minimal|full`
  - Control activity feed intensity with one setting.
  - `off`: no activity feed messages.
  - `minimal`: member join/leave + invite-used attribution messages.
  - `full`: all activity events (join/leave/invite create/invite delete/invite used).

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

- `/trusted add user:<member>`
  - Add member to the trusted users list for sensitive commands.
- `/trusted remove user:<member>`
  - Remove member from the trusted users list.
- `/trusted list`
  - Show trusted users.

### ALL_USER

- `/jump channel:<voice|stage>`
  - Move invoking user to target channel if visibility/permission checks pass.

- `/dashboard`
  - Show a paginated voice-time leaderboard with bottom arrow buttons.
  - Each row shows display name, clickable member mention, and total time as `hours:minutes:seconds`.

- `/userinfo user:<member>`
  - Show member profile summary and total voice time.
  - Presence status is shown only when Presence Intent is enabled for the commands service.

- `/stalker start user:<member>`
  - Start DM notifications for the target member's voice join/move/leave and guild join/leave events.
  - Invoking user must already be on the trusted users list.
  - Requires the `services.stalker` worker to be running and the watcher's Discord DMs to be open.
- `/stalker stop user:<member>`
  - Stop DM notifications for that member.
- `/stalker list`
  - Show the current invoking user's stalked members.

## Notes

- Tracking is all-channel; there are no public per-channel tracking commands.
- Invite attribution internals are managed automatically and are not part of the public command surface.
- Activity feed settings are command-driven and persisted per guild.
- Trusted users are command-driven and persisted per guild.
- Voice connection management is independent from enforcement toggles.
- Soundboard enforcement runs only when both conditions are true: managed voice connection is active and `/settings soundboard` is `on`.
- Command registration owner is `services.commands`.
- Runtime policy enforcement exists in command handlers; Discord UI visibility alone is not treated as authorization.
- `/inspect` supports additional internal route forms for compatibility/tests, but only the public route above is registered.
- `/stalker` only observes voice and guild lifecycle signals currently emitted by the bot runtime.
