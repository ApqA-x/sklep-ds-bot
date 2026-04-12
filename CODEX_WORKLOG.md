# Codex Worklog

Updated: 2026-04-12, Europe/Warsaw.

Current task: rewrite the whole project from Go to Python without changing the product logic, split the rewrite into subagent tasks, then verify it with code correctness, security, and QA review agents.

## Current State

- Old Go source and old package README files under `cmd/` and `internal/` were removed after the Python rewrite.
- New Python domain/service package lives in `voice_tracker/`.
- New runnable service entrypoints live in `services/`.
- New pytest coverage lives in `tests/`.
- Project metadata is now in `pyproject.toml`; setuptools package discovery is explicit for `voice_tracker*` and `services*`.
- CI in `.github/workflows/ci.yml` was migrated from Go to Python with pip caching, compile checks, and pytest.
- `Dockerfile` was migrated to Python and still accepts the `SERVICE` build arg used by compose.
- `docker-compose.yml` now binds Mongo/NATS development ports to `127.0.0.1`.
- Local verification is blocked in this environment: `python`, `python3`, `py`, and `pytest` are unavailable on PATH.
- On 2026-04-12, docs were updated for the new command catalog and access policy: `ADMIN_ONLY` vs `ALL_USER`, and the `services.commands` registration owner note.

## Subagents Used

- Rewrite agents:
  - tracker and summary rewrite
  - commands, config, and botauth rewrite
  - bus, repository, gateway, runtime, and service entrypoint rewrite
- Review agents:
  - code correctness agent
  - security agent
  - QA agent

## Review Findings Integrated

- Fixed discord.py intents in `services/gateway.py` and `services/commands.py`.
- Fixed gateway voice event guild id derivation in `voice_tracker/gateway.py`.
- Added gateway test coverage for member-derived guild id in `tests/test_gateway.py`.
- Added event signing secret validation in `voice_tracker/runtime.py`.
- Changed example event secret placeholders in `.env.example` and `EXAMPLES.md`.
- Wrapped NATS dedupe and handler errors in `voice_tracker/bus.py` so callback failures log and return.
- Added gateway summary event validation against Mongo state before sending in `services/gateway.py`.
- Added a TTL index for processed NATS messages in `voice_tracker/repository.py`.
- Replaced unexpected user-facing exception bodies in command services with generic responses and server logs; `ValueError` remains user-facing for validation errors.
- Consolidated legacy `voice_tracker.config.load()` behind `voice_tracker.runtime.load_config()`.

## Remaining Risks / Next Checks

- Run `python -m compileall voice_tracker services` once Python is available.
- Run `python -m pytest` once Python and test dependencies are available.
- Consider adding service entrypoint import smoke tests after Python is available locally.
- The shared event signing secret still validates issuer names but is not a strong per-service trust boundary; fixing that would be a design change, not a literal rewrite.
- Compose is safer for local development after localhost binding, but production Mongo/NATS auth/TLS should be configured outside this rewrite.

## 2026-04-11 PR Fix

- Rebasing onto `origin/main` resolved GitHub merge conflicts in `.env.example` and `README.md`; the original branch carried a pre-merge docker-stack commit that is now already in `main`.
- Fixed the `compileall` failure in `voice_tracker/bus.py` by replacing `match/case` over imported constants with `if/elif`. Bare names in Python `case` patterns are captures, not constant comparisons.

## Files To Inspect First When Resuming

- `CODEX_WORKLOG.md`
- `pyproject.toml`
- `voice_tracker/runtime.py`
- `voice_tracker/bus.py`
- `voice_tracker/repository.py`
- `services/gateway.py`
- `services/commands.py`
- `tests/test_gateway.py`
