# AGENTS.md

Entry point for AI coding agents (Codex, Cursor, Claude Code, Copilot, …) working in this
repository.

**`CLAUDE.md` is the canonical, detailed architecture guide — read it first.** This file
only covers what an agent must not get wrong, so the two don't drift: put architecture
detail in `CLAUDE.md`, not here. `.github/copilot-instructions.md` carries the same
rules in a shorter form for Copilot.

## What this is

A **Home Assistant custom integration** (`custom_components/sungrow`) for Sungrow
inverters via the iSolarCloud API, built on the `sungrow-isolarcloud` library (a fork of
`pysolarcloud`, imported as `pysolarcloud`). Distributed via HACS; quality scale
**platinum**. Three transports share one entity layer: `cloud_only` (OAuth app),
`cloud_user` (unofficial account login), `modbus_only` (local Modbus).

## Environment

**Python 3.14** (Home Assistant requires ≥3.14 since 2026.3; the test harness pins
`homeassistant==2026.9.1`). Ruff line length 120; mypy `strict`.

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements_test.txt

.venv/bin/ruff check custom_components/ tests/
.venv/bin/ruff format --check custom_components/ tests/
.venv/bin/mypy
.venv/bin/python -m pytest tests/        # coverage gate: fail_under in pyproject.toml
```

## Must-follow rules

1. **Never remove token persistence.** `pysolarcloud` rotates the refresh token in
   memory; the `token_updater` callback in `__init__.py` writes it back to the config
   entry. Removing it reintroduces "entities unavailable after reboot" (#14/#15/#20/#21).
2. **Keep the library pinned** as `sungrow-isolarcloud==X.Y.Z` in `manifest.json` **and**
   `requirements_test.txt`.
3. **Nest devices with `via_device_id`**, never `via_device` (deprecated, removed in HA
   2027.8) — go through `device_helpers.build_device_info_for()`.
4. **Keep `strings.json` and every `translations/*.json` in sync** (a test enforces key
   parity across all languages).
5. **Add/update tests for any behaviour change.** Tests mock `SungrowAuth`/`Plants` — see
   `tests/conftest.py`.
6. **Conventional Commits** for commit and PR titles (`fix:`/`feat:`/`chore:`/`docs:`) —
   this drives release-please and the changelog.
7. **Sign your commits.** `main` requires signed commits; an unsigned commit cannot be
   merged.
8. **`main` is protected** — work on a branch and open a PR. Required checks: `lint`,
   `test`, `hacs_validate`.
9. **Keep the docs honest.** Behaviour changes must update the matching doc:
   `docs/TROUBLESHOOTING.md`, `docs/local-modbus.md`, `docs/SENSORS.md`, `README.md`, and
   `custom_components/sungrow/quality_scale.yaml` (which must reflect reality — e.g. the
   integration *does* register services).

## Where things live

`CLAUDE.md` has the full per-module map (entry lifecycle, polling/data, local Modbus,
entities, config flow, naming/classification) and the critical invariants. Read it before
changing anything non-trivial.
