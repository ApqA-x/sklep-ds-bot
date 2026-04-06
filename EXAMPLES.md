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
EVENT_SIGNING_SECRET=change-me
TRACKING_MODE=all
TRACKED_CHANNEL_IDS=
SERVICE_NAME=gateway
```

## How To Use

1. Copy `.env.example` to `.env`.
2. Set the Discord and database values for your environment.
3. Keep `TRACKING_MODE=all` if you want the bot to learn tracked channels from `/voice config` instead of startup config.
4. Run `docker compose up --build`.

## Variable Notes

- `DISCORD_TOKEN`, `DISCORD_APPLICATION_ID`, and `DISCORD_GUILD_ID` are required for the command service.
- `MONGO_URI`, `MONGO_DB`, and `NATS_URL` are shared by the Go services.
- `EVENT_SIGNING_SECRET` is used by the gateway event path.
- `TRACKING_MODE` and `TRACKED_CHANNEL_IDS` are startup defaults for the tracker.
- `SERVICE_NAME` selects the container role when reusing the shared config file.

## Minimal Local Setup

1. Copy `.env.example` to `.env`.
2. Fill in the Discord token, application ID, guild ID, and any service-specific values.
3. Run `docker compose up --build`.

## AI Notes

- This file is the quick-start reference.
- Keep examples aligned with the checked-in compose file.
- When the deployment shape changes, update this page first.
