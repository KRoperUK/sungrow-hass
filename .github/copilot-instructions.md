# GitHub Copilot instructions

This repo is a **Home Assistant custom integration** for Sungrow inverters via the
iSolarCloud cloud API, built on the `pysolarcloud` library. Code lives in
`custom_components/sungrow/`.

## Module map

- `__init__.py` — entry setup/unload; builds auth + per-plant coordinators;
  **persists rotated tokens** to the config entry; maps errors to
  `ConfigEntryNotReady` (retry) vs `ConfigEntryAuthFailed` (reauth).
- `auth.py` — `SungrowAuth(pysolarcloud.Auth)` with a `token_updater` callback.
- `coordinator.py` — `SungrowPlantCoordinator`; `is_auth_error()` classification.
- `config_flow.py` — user, reauth, and options (polling interval) flows.
- `sensor.py` — entities + `infer_device_class()` (units → device/state class).
- `const.py` — domain, config keys, gateways, scan-interval defaults.

## Must-follow rules

1. **Never drop token persistence.** `pysolarcloud` rotates the refresh token in
   memory on refresh; the `token_updater` callback writes it back to the config
   entry. Removing it reintroduces the "entities unavailable after reboot" bug.
2. **Keep `pysolarcloud` pinned** in `manifest.json` (and `requirements_test.txt`).
3. **Keep `strings.json` and `translations/en.json` in sync.**
4. **Add/update tests** for any behaviour change. Tests mock `SungrowAuth` and
   `Plants` (see `tests/conftest.py`).
5. **Conventional Commits** for commits and PR titles.
6. **`main` is protected** — work on a branch and open a PR. CI runs ruff
   (lint + format), pytest with coverage, and HACS/hassfest validation.

## Local checks (match CI)

```bash
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
pytest
```

Style: Python 3.12+/3.13, ruff line length 120. See `CLAUDE.md` for the full guide
and `docs/TROUBLESHOOTING.md` for user-facing auth/setup guidance.
