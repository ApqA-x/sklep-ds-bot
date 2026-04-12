# Examples

## Docker Compose

Use the included `docker-compose.yml` with a `.env` file based on `.env.example`.

```yaml
services:
  mongo:
    image: mongo:7
    ports:
      - "27017:27017"
    volumes:
      - mongo-data:/data/db

  nats:
    image: nats:2
    ports:
      - "4222:4222"

  gateway:
    build:
      context: .
      args:
        SERVICE: gateway
    env_file:
      - .env
    depends_on:
      - mongo
      - nats

  tracker:
    build:
      context: .
      args:
        SERVICE: tracker
    env_file:
      - .env
    depends_on:
      - mongo
      - nats

  writer:
    build:
      context: .
      args:
        SERVICE: writer
    env_file:
      - .env
    depends_on:
      - mongo
      - nats

  commands:
    build:
      context: .
      args:
        SERVICE: commands
    env_file:
      - .env
    depends_on:
      - mongo
      - nats

volumes:
  mongo-data:
```

Start it with:

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
# Legacy compatibility allowlist only. Target ADMIN_ONLY commands do not use it.
BOT_ADMIN_USER_IDS=
EVENT_SIGNING_SECRET=replace-with-a-long-random-secret
TRACKING_MODE=all
TRACKED_CHANNEL_IDS=
```

## How To Use

1. Copy `.env.example` to `.env`.
2. Set the Discord, database, and NATS values for your environment.
3. Keep `TRACKING_MODE=all` if you want the bot to learn tracked channels from slash commands instead of startup config.
4. Run `docker compose up --build`.

## Variable Notes

- `DISCORD_TOKEN`, `DISCORD_APPLICATION_ID`, and `DISCORD_GUILD_ID` are required for the command services.
- `DISCORD_APPLICATION_ID` is used by the single command registration owner, `services.commands`.
- `BOT_ADMIN_USER_IDS` is legacy compatibility only. It does not grant access to target `ADMIN_ONLY` commands.
- `MONGO_URI`, `MONGO_DB`, and `NATS_URL` are shared by the Python services.
- `EVENT_SIGNING_SECRET` is used by the gateway event path.
- `TRACKING_MODE` and `TRACKED_CHANNEL_IDS` are startup defaults for the tracker.

## Minimal Local Setup

1. Copy `.env.example` to `.env`.
2. Fill in the Discord token, application ID, guild ID, and any service-specific values.
3. Run `docker compose up --build`.

## Operational Notes

- `services.commands` is the only service that should bulk register the Discord slash-command catalog.
- The audit/output channel should be configured through `/audit` after startup.
- `BOT_ADMIN_USER_IDS` remains in the example environment for compatibility, but it is not part of the target `ADMIN_ONLY` policy.

## AI Notes

- This file is the quick-start reference.
- Keep examples aligned with the checked-in compose file.
- When the deployment shape changes, update this page first.
