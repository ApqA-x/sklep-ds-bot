# Examples

## Docker Compose

Use the included `docker-compose.yml` with a `.env` file based on `.env.example`.

```yaml
services:
  mongo:
    image: mongo:7
    ports:
      - "27017:27017"

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
```

Start it with:

```bash
docker compose up --build
```

## Minimal Local Setup

1. Copy `.env.example` to `.env`.
2. Fill in the Discord token, application ID, guild ID, and signing secret.
3. Run `docker compose up --build`.

## AI Notes

- This file is the quick-start reference.
- Keep examples aligned with the checked-in compose file.
- When the deployment shape changes, update this page first.
