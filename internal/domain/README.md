# Domain Package

Shared event and model definitions for the system.

## What It Does

- Defines NATS subjects.
- Defines voice-session events.
- Defines MongoDB session, participant, and guild settings shapes.
- Defines summary output models.
- Provides shared session-summary aggregation used by `summary` and `commands`.

## How To Use

- Import these types from services and tests.
- Use them as the contract between packages.
- Keep them small and stable.

## How It Fits

- `gateway`, `tracker`, `writer`, and `commands` all depend on this package.
- `summary` and `commands` both use the shared session-summary builder here.
- It is the shared language of the system.

## AI Notes

- Add new fields only when the full flow needs them.
- Prefer extending shared models over duplicating them per service.
- If a field changes here, check all consumers before editing behavior.
