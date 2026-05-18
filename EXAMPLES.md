# Examples

Practical local run examples for Voice Tracker.

Last updated: May 7, 2026.

## Docker Compose

Use the checked-in `docker-compose.yml` with `.env` based on `.env.example`.

```bash
docker compose up --build
```

## Example `.env`

```env
MONGO_URI=mongodb://mongo:27017
MONGO_DB=voice_tracker
NATS_URL=nats://nats:4222
DISCORD_TOKEN=your-bot-token
DISCORD_APPLICATION_ID=your-application-id
DISCORD_GUILD_ID=your-guild-id
# Legacy compatibility allowlist only.
BOT_ADMIN_USER_IDS=
EVENT_SIGNING_SECRET=replace-with-a-long-random-secret
```

## Usage Notes

- Tracking is all-channel.
- Configure summary/output destination with `/settings summary-set`.
- Invite attribution internals are automatic and do not require command-level tuning.
- Configure activity output with `/settings activity-channel-set` and `/settings activity mode:off|minimal|full`.
- Configure trusted access with `/trusted add|remove|list`.
- Trusted users can manage DM watchers with `/stalker start|stop|list`.
- `/stalker` delivery requires the `services.stalker` worker to be running and the watcher's DMs to be open.

## Minimal Local Setup

1. Copy `.env.example` to `.env`.
2. Fill Discord + database + NATS values.
3. Start stack with `docker compose up --build`.
4. In Discord, run `/settings show` to verify live configuration.

## Operational Notes

- `services.commands` is the only command registration owner.
- `services.activity` posts member/invite lifecycle embeds to the configured activity channel.
- `services.stalker` sends DM alerts for watched members using the persisted trusted users allowlist.
- Auto-unmute clears server mute/deafen state for users on the configured list.
- `/userinfo` presence status depends on Presence Intent being enabled.
- `BOT_ADMIN_USER_IDS` is not the primary admin authorization path.
