# Summary Package

Builds the recap text for a finished voice session.

## What It Does

- Loads the closed session and participant history.
- Uses shared summary aggregation from `internal/domain`.
- Formats the final message text.
- Stores summary generation state for replay-safe delivery.

## How To Use

- Create with `summary.New(repo, publisher)`.
- Call `HandleSessionClosed(ctx, payload)` when `session.closed` arrives.
- Call `Start(ctx)` on boot to replay unfinished summary generation.

## How It Fits

- Input comes from `tracker` through `session.closed`.
- Output is `session.summary` for `gateway`.
- It depends on Mongo for replay state and on `domain` for shared shapes.

## AI Notes

- Keep summary formatting here.
- Keep shared summary math in `internal/domain`.
- Do not move Discord posting into this package.
- If summary delivery rules change, update this package and the gateway consumer together.
