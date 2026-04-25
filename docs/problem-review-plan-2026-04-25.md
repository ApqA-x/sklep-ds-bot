# Problem Review And Remediation Plan

Updated: April 25, 2026.

## Source

This plan is based on the repo-root [Problems](/Users/maksymtarasovets/source/sklep-ds-bot/Problems) file.

## Verified Inputs

Problem list from `Problems`:

1. Remove legacy commands instead of keeping them as deprecated compatibility shims.
2. Extend auto-unmute to include the reported headphone/deafen case.
3. Fix voice tracking so all channels are tracked instead of one channel appearing live while others go stale.
4. Fix `/userinfo` status and banner behavior.

## Findings By Problem

### 1. Legacy Commands Are Still Fully Alive

Current docs and runtime references still treat legacy commands as part of the supported surface. The user requirement is stricter: if a legacy command is removed, docs should erase it rather than describe it as a kept alias or compatibility path.

Legacy commands are still described or implemented as active surface in several places:
- registered in the public slash-command catalog
- accepted by runtime routing
- covered by command policy
- asserted by tests
- documented as compatibility behavior instead of removed behavior

Main code references:

- [voice_tracker/commands.py:798](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:798)
- [voice_tracker/commands.py:818](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:818)
- [voice_tracker/commands.py:829](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:829)
- [voice_tracker/commands.py:837](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:837)
- [voice_tracker/commands.py:858](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:858)
- [services/commands.py:42](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:42)
- [services/commands.py:188](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:188)

Impact:

- `/audit`
- `/bot-setting`
- `/track`
- `/track-list`

remain part of the apparent product surface. The implementation plan should treat them as removal work, not alias-preservation work.

### 2. Auto-Unmute Only Handles Guild Mute

The current listener only checks `VoiceState.mute` and only calls `member.edit(mute=False)`.

Main code references:

- [services/gateway.py:163](/Users/maksymtarasovets/source/sklep-ds-bot/services/gateway.py:163)
- [services/gateway.py:238](/Users/maksymtarasovets/source/sklep-ds-bot/services/gateway.py:238)
- [services/gateway.py:255](/Users/maksymtarasovets/source/sklep-ds-bot/services/gateway.py:255)

Most likely interpretation:

- if “headphones mute” means guild deafen, the code misses it and can be extended safely
- if it means `self_deaf` or `self_mute`, the bot should not attempt to override it because that is user-controlled

### 3. Tracker Still Honors Legacy Per-Channel Settings

This is the most likely reason one channel appears tracked while others are stale.

The tracker still accepts and obeys:

- `TRACKING_MODE`
- `TRACKED_CHANNEL_IDS`
- persisted guild `trackingMode`
- persisted guild `trackedChannelIds`

Main code references:

- [voice_tracker/runtime.py:94](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/runtime.py:94)
- [voice_tracker/domain.py:67](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/domain.py:67)
- [voice_tracker/tracker.py:176](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/tracker.py:176)
- [voice_tracker/tracker.py:226](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/tracker.py:226)
- [voice_tracker/commands.py:602](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py:602)

Normalization to `all` currently happens only when commands touch settings.
That means old stored state or prod env can still suppress tracking for other channels.

### 4. `/userinfo` Offline Status And Missing Banner Have Separate Root Causes

False offline:

- the command client does not enable presence intent
- `/userinfo` reads `member.status` anyway

Banner missing:

- `_fetch_user_by_id()` prefers cached `client.get_user()`
- cached users may not have hydrated `banner`
- `fetch_user()` is the reliable path for banner hydration

Main code references:

- [services/commands.py:58](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:58)
- [services/commands.py:410](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:410)
- [services/commands.py:664](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:664)
- [services/commands.py:698](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:698)
- [services/commands.py:728](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py:728)

## Recommended Execution Plan

1. Canonicalize tracking to `all` in the tracker/runtime path itself so old env or persisted guild settings cannot keep most channels stale.
2. Extend auto-unmute to clear server-controlled deafen as well as server mute, while explicitly avoiding `self_mute` and `self_deaf`.
3. Fix `/userinfo` banner loading by preferring `fetch_user()` for banner-sensitive reads, then make an explicit product decision on status behavior.
4. Remove legacy command registration, routing, policy branches, tests, and docs so the public surface matches the user's "remove means remove" requirement.
5. Reconcile production state after the tracker fix by checking persisted tracking settings and cleaning stale session state if needed.

## Concrete File Plan

### Legacy Command Removal

- [voice_tracker/commands.py](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/commands.py)
- [services/commands.py](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py)
- [tests/test_appcommands.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_appcommands.py)
- [tests/test_mvp_command_catalog.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_mvp_command_catalog.py)
- [tests/test_commands.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_commands.py)
- [tests/test_regression_plan_routes.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_regression_plan_routes.py)
- [COMMANDS.md](/Users/maksymtarasovets/source/sklep-ds-bot/COMMANDS.md)
- [README.md](/Users/maksymtarasovets/source/sklep-ds-bot/README.md)
- [EXAMPLES.md](/Users/maksymtarasovets/source/sklep-ds-bot/EXAMPLES.md)
- [AGENTS_README.md](/Users/maksymtarasovets/source/sklep-ds-bot/AGENTS_README.md)

### Tracking And Unmute

- [voice_tracker/runtime.py](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/runtime.py)
- [voice_tracker/domain.py](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/domain.py)
- [voice_tracker/tracker.py](/Users/maksymtarasovets/source/sklep-ds-bot/voice_tracker/tracker.py)
- [services/gateway.py](/Users/maksymtarasovets/source/sklep-ds-bot/services/gateway.py)
- [tests/test_config.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_config.py)
- [tests/test_tracker.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_tracker.py)
- [tests/test_services_gateway.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_services_gateway.py)

### Userinfo

- [services/commands.py](/Users/maksymtarasovets/source/sklep-ds-bot/services/commands.py)
- [tests/test_services_commands_userinfo.py](/Users/maksymtarasovets/source/sklep-ds-bot/tests/test_services_commands_userinfo.py)
- [README.md](/Users/maksymtarasovets/source/sklep-ds-bot/README.md)

## Notes

- The tracking bug and unmute bug are operational issues and should be fixed before command-surface cleanup.
- Legacy command removal should be documented as actual removal, not as intentional alias retention.
- The `userinfo` status fix depends on product choice: enable presence intent or remove status output.
