# Design: cloud_user dispatch capability spike (#271)

**Status:** Approved for implementation (Approach A — spike-first)  
**Date:** 2026-07-18  
**Issue:** [sungrow-hass#271](https://github.com/KRoperUK/sungrow-hass/issues/271) (Phase 5 of epic #267)

## Goal

Determine whether a plain iSolarCloud **user account** (app/web login via `UserAuth`) can **read and/or write** EMS control parameters (e.g. energy management mode 10003), so Phase 5 can either implement dispatch or clearly remain read-only.

## Non-goals

- Home Assistant number/select entities, heartbeat lifecycle, services
- Shipping a stable public `UserControl` API
- Modbus local control (#220)
- Copying or adapting GPL sources (GoSungrow)

## Approach

Library-only live probe in **pysolarcloud**:

1. **Read-only discovery** of candidate endpoints over the existing AES/RSA user envelope.
2. **Optional single idempotent write** only when `SUNGROW_USER_WRITE_OK=1`.
3. **Report** Supported / Read-only / Unsupported / Inconclusive with redacted evidence.

## Architecture

| Component | Responsibility |
| --- | --- |
| `UserAuth.async_request_soft` | Authenticated POST that returns the envelope even when `result_code` is not success (needed for probing unknown paths). |
| `user_control_probe` | Experimental (non-stable) candidate list + probe helpers. Not re-exported as public API. |
| Live tests (`@pytest.mark.live`) | Login → plant → device → probe reads; optional gated write. |
| Unit tests | Soft-request re-login behaviour; probe body builders; summary classification. |

## Candidate endpoints (protocol path names)

Probe both **OpenAPI-shaped** paths (negative control: user token unlikely) and **app `/v1/`** naming guesses consistent with existing user-service path style:

- `/openapi/platform/paramSettingCheck`
- `/openapi/platform/paramSetting`
- `/openapi/platform/getParamSettingTask`
- `/v1/devService/paramSettingCheck`
- `/v1/devService/paramSetting`
- `/v1/devService/getParamSettingTask`
- `/v1/devService/setDeviceParam`
- `/v1/devService/getDeviceParam`
- `/v1/deviceService/paramSetting`
- `/v1/deviceService/paramSettingCheck`

Payloads for read probes mirror OpenAPI Appendix 10 shape where applicable:

- check: `set_type=2` (read) or `0` (update), `uuid`
- paramSetting read: `set_type=2`, `uuid`, `param_list=[{param_code: "10003", set_value: ""}]`, `task_name`, `expire_second`

## Safety

- Default: **no writes**
- Write requires `SUNGROW_USER_WRITE_OK=1`
- Write is a single idempotent re-apply of energy management mode (or skip if no successful read path)
- Never log password/token
- No charge/discharge power changes

## Outcome criteria

| Result | Criteria |
| --- | --- |
| Supported | Param read returns 10003 (or equivalent) with plausible value; gated write accepted |
| Read-only | Read works; write rejected |
| Unsupported | No working control endpoint for user session |
| Inconclusive | Ambiguous errors only |

## Deliverables

1. Probe module + soft request + unit/live tests in pysolarcloud
2. Comment on #271 with go/no-go table
3. No HA wiring unless result is Supported (Phase 5b)

## Live results (EU plant, 2026-07-18)

Plant devices: inverter (type 1), communication module (22), meter (7) — **no ESS**.

| Path | Result |
| --- | --- |
| `/openapi/platform/paramSetting*` | HTTP 401 Unauthorized (user token ≠ OpenAPI) |
| `/v1/devService/paramSettingCheck` (set_type 0 and 2) | **OK** — `check_result=1` |
| `/v1/devService/paramSetting` (OpenAPI-shaped body, many param codes) | Envelope success but **`result_data.code=4`**, no `task_id` — logical failure |
| `/v1/devService/queryParamSettingTask` | Exists (requires `task_id`); empty for task_id `0` |
| Other guessed paths (`setDeviceParam`, `deviceService/*`, …) | Mostly HTTP 404 |

**Classification: `partial`** — control *capability check* works over the user API; actual param read/write task submission is not working with the OpenAPI payload shape (or is blocked for this plant/role).

**Implication for Phase 5:** Do **not** wire HA dispatch yet. Next spike step is resolve `paramSetting` task body / role requirements (preferably on a hybrid/ESS plant) before implementing `UserControl`.

## Licensing

Clean-room only. Path names and field names are protocol facts. MIT attribution already in `NOTICE` for the user login envelope.
