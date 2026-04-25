# Agents README

Maintainer notes for agent-assisted updates in this repository.

Last updated: April 25, 2026.

## Scope

Use this file as the operational checklist when an agent updates product behavior, command surface, or service wiring.

## Required Doc Sync

When behavior changes, update these files in the same PR:

- `README.md`: architecture, setup, high-level behavior.
- `COMMANDS.md`: command names, access level, and compatibility status.
- `EXAMPLES.md`: runtime examples and environment guidance.
- `.env.example`: configuration defaults and comments.

## Command Changes Checklist

- Confirm command names/subcommands in `voice_tracker/commands.py`.
- Confirm command registration source remains `services.commands`.
- Confirm access model (`ADMIN_ONLY` vs `ALL_USER`) still matches runtime checks.
- If commands are removed, remove them from maintainer and user-facing docs instead of describing them as retained aliases.

## Reliability/Operations Checklist

- If startup/retry behavior changes in services, document it in `README.md`.
- If CI policy or lanes change, update the `README.md` CI section.

## Documentation Hygiene

- Remove obsolete ad-hoc planning docs once work is shipped.
- Keep historical design research in `docs/` if still useful, otherwise archive or delete.
- Keep user-facing docs concise and accurate to the current code, not planned future behavior.

## Review Artifacts

- Active repo review/planning artifacts should live in `docs/` and be linked from `docs/README.md`.
- If a user references an issue/problem file that is missing on disk, record that blocker explicitly in the review artifact instead of inventing the source material.
