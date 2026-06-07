# Contributing

Thanks for your interest in improving the Sungrow iSolarCloud integration!

## Development setup

This project targets Python 3.13 and Home Assistant's custom-component test
harness. Using [uv](https://docs.astral.sh/uv/) is the quickest path:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt
```

(Any virtualenv with `pip install -r requirements_test.txt` works too.)

## Before you push

Run the same checks CI runs:

```bash
.venv/bin/ruff check custom_components/
.venv/bin/ruff format --check custom_components/
.venv/bin/python -m pytest tests/
```

- Tests must pass and coverage must stay at or above the configured threshold
  (`fail_under` in `pyproject.toml`).
- Live tests (marked `live`) are skipped by default and require real iSolarCloud
  credentials in a `.env` file (see `.env.example`).
- A `.pre-commit-config.yaml` is provided; `pre-commit install` will run ruff on
  commit.

## Pull requests

- Use **Conventional Commits** for PR titles and commits (`fix:`, `feat:`,
  `chore:`, `docs:` …). This drives automated changelog and version bumps.
- Keep changes focused; add or update tests for any behaviour change.
- All PRs target `main` and must pass CI (lint, tests, HACS + hassfest validation)
  and at least one review before merge.

## Project layout

| Path | Purpose |
| --- | --- |
| `custom_components/sungrow/__init__.py` | Entry setup, token persistence, error handling |
| `custom_components/sungrow/auth.py` | Token-persisting `Auth` wrapper |
| `custom_components/sungrow/coordinator.py` | Per-plant `DataUpdateCoordinator` |
| `custom_components/sungrow/config_flow.py` | Config, reauth, and options flows |
| `custom_components/sungrow/sensor.py` | Sensor entities + device-class inference |
| `tests/` | Unit/integration tests |

## Reporting issues

Please use the issue templates and read
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) first.
