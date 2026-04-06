# Mongo Package

MongoDB repository layer for sessions, participants, guild settings, and message dedupe.

## What It Does

- Stores and reads guild settings.
- Stores voice sessions and participant intervals, including closed-session history lookups.
- Stores replay markers and summary delivery state.
- Provides the persistence methods used by the services.

## How To Use

- Create a repository with `NewRepository(db)`.
- Call `EnsureIndexes(ctx)` at startup.
- Use the dedicated methods for sessions, participants, settings, and delivery markers.

## How It Fits

- `tracker` owns voice session writes.
- `writer` reads session history and stores summary state.
- `gateway` reads guild settings and summary delivery state.
- `commands` updates guild settings and reads closed-session history.

## AI Notes

- Keep this package as the only place that knows collection names.
- Add methods here when a service needs persistent state or history lookups.
- Avoid spreading Mongo queries into service code.
