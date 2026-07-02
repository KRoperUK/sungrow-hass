## [1.1.0](https://github.com/KRoperUK/sungrow-hass/compare/v1.0.0...v1.1.0) (2026-07-02)

> [!IMPORTANT]
> **Upgrade note — dispatch (charge/discharge) users.** The dispatch number/select
> controls now correctly attach to your **battery / energy-storage device** instead
> of the first-listed device ([#68](https://github.com/KRoperUK/sungrow-hass/issues/68)).
> On systems with **both an inverter and a battery**, this can change those entities'
> unique IDs, so Home Assistant may **recreate the dispatch entities** — the old ones
> appear as *unavailable* and can be deleted, and any automations/dashboards that
> reference them should be re-checked after updating. **Sensors are unaffected**, and
> plants without dispatch controls (or with a single device) see no change.
>
> Polling intervals are migrated automatically (the setting moved from minutes to
> seconds — an existing 5 becomes 300s); no action needed.

### Features

* auto-start OAuth redirect with manual code/URL fallback ([#46](https://github.com/KRoperUK/sungrow-hass/issues/46)) ([492fa4a](https://github.com/KRoperUK/sungrow-hass/commit/492fa4a5e85d47a2b1fb52922a9296d3a982bb6b))
* **diagnostics:** capture all devices + per-device realtime for [#18](https://github.com/KRoperUK/sungrow-hass/issues/18) ([#67](https://github.com/KRoperUK/sungrow-hass/issues/67)) ([4d4dd54](https://github.com/KRoperUK/sungrow-hass/commit/4d4dd546ba6509ad9f96f5741ec7d8be57a6c690))
* harden OAuth callback, expand tests, add repo hygiene ([#38](https://github.com/KRoperUK/sungrow-hass/issues/38)) ([ebb9968](https://github.com/KRoperUK/sungrow-hass/commit/ebb9968e68416678eaca2a1081e3010f85334e7f)), closes [#18](https://github.com/KRoperUK/sungrow-hass/issues/18)
* Home Assistant quality-scale compliance (Batch B) ([#62](https://github.com/KRoperUK/sungrow-hass/issues/62)) ([c956194](https://github.com/KRoperUK/sungrow-hass/commit/c9561943f656448d0c9ceaff4a4b55bd3e365643)), closes [#50](https://github.com/KRoperUK/sungrow-hass/issues/50)
* sub-minute polling intervals (seconds, min 10 s) ([#43](https://github.com/KRoperUK/sungrow-hass/issues/43)) ([f180ad1](https://github.com/KRoperUK/sungrow-hass/commit/f180ad1fbf4105dc2dca8c4f58a97d7751186cae)), closes [#40](https://github.com/KRoperUK/sungrow-hass/issues/40)


### Bug Fixes

* correct icon and alias for battery SoC sensor ([#39](https://github.com/KRoperUK/sungrow-hass/issues/39)) ([#45](https://github.com/KRoperUK/sungrow-hass/issues/45)) ([077b49a](https://github.com/KRoperUK/sungrow-hass/commit/077b49aacc52ec9a05863fb1c3b7bfa94d6cfdb5))
* **dispatch:** select ESS device when device_type is a DeviceType enum ([#68](https://github.com/KRoperUK/sungrow-hass/issues/68)) ([6919bf3](https://github.com/KRoperUK/sungrow-hass/commit/6919bf3448fd88925a24d2e3f50e3f421e283df0))
* handle OAuth callback when iSolarCloud strips flow_id query param ([#41](https://github.com/KRoperUK/sungrow-hass/issues/41)) ([cf5ef83](https://github.com/KRoperUK/sungrow-hass/commit/cf5ef83de298370cd3689feb495c7b5bc03826e1))
* harden heartbeat lifecycle, entity availability, and secret logging ([#48](https://github.com/KRoperUK/sungrow-hass/issues/48)) ([51dee14](https://github.com/KRoperUK/sungrow-hass/commit/51dee1448cd866f110e41c5f6675bbdbc8274f9a))

## [1.0.0](https://github.com/KRoperUK/sungrow-hass/compare/v0.2.3...v1.0.0) (2026-07-02)


### ⚠ BREAKING CHANGES

* automatic OAuth callback handling (#35)

### Features

* automatic OAuth callback handling ([#35](https://github.com/KRoperUK/sungrow-hass/issues/35)) ([5b17efe](https://github.com/KRoperUK/sungrow-hass/commit/5b17efeeed3abbf1e3398833aeb07777b136625f)), closes [#34](https://github.com/KRoperUK/sungrow-hass/issues/34)
* dispatch control, extra measure points, sensor aliases, forked API client ([#33](https://github.com/KRoperUK/sungrow-hass/issues/33)) ([a30e4f2](https://github.com/KRoperUK/sungrow-hass/commit/a30e4f220aaf8dfeab420d61903854cb763a4f96)), closes [#7](https://github.com/KRoperUK/sungrow-hass/issues/7) [#17](https://github.com/KRoperUK/sungrow-hass/issues/17) [#31](https://github.com/KRoperUK/sungrow-hass/issues/31) [#18](https://github.com/KRoperUK/sungrow-hass/issues/18)

## [0.2.3](https://github.com/KRoperUK/sungrow-hass/compare/v0.2.2...v0.2.3) (2026-06-08)


### Bug Fixes

* persist refreshed tokens and harden setup ([#23](https://github.com/KRoperUK/sungrow-hass/issues/23)) ([2d579fd](https://github.com/KRoperUK/sungrow-hass/commit/2d579fd3af9ca2ba3836fe3ccaa2963683d69d7b)), closes [#14](https://github.com/KRoperUK/sungrow-hass/issues/14) [#15](https://github.com/KRoperUK/sungrow-hass/issues/15) [#20](https://github.com/KRoperUK/sungrow-hass/issues/20) [#21](https://github.com/KRoperUK/sungrow-hass/issues/21) [#21](https://github.com/KRoperUK/sungrow-hass/issues/21) [#19](https://github.com/KRoperUK/sungrow-hass/issues/19) [#14](https://github.com/KRoperUK/sungrow-hass/issues/14) [#15](https://github.com/KRoperUK/sungrow-hass/issues/15) [#19](https://github.com/KRoperUK/sungrow-hass/issues/19) [#20](https://github.com/KRoperUK/sungrow-hass/issues/20) [#21](https://github.com/KRoperUK/sungrow-hass/issues/21)
* resolved hassfest warnings ([3b65b4c](https://github.com/KRoperUK/sungrow-hass/commit/3b65b4cb486f5be3bbf66ea34b70b3786013e7a0))
* resolved hassfest warnings ([74dc412](https://github.com/KRoperUK/sungrow-hass/commit/74dc4123499d2dc1d3cebec0e4597e84488bb81c))
* resolved hassfest warnings ([44fe5dd](https://github.com/KRoperUK/sungrow-hass/commit/44fe5dd5ee630ed191f25ee63a3c56a648137800))
* resolved hassfest warnings ([990fce2](https://github.com/KRoperUK/sungrow-hass/commit/990fce2292a68b95790e12745d3ee3b0b29da69b))
* resolved hassfest warnings ([492d444](https://github.com/KRoperUK/sungrow-hass/commit/492d444aee493ec3485c22d623be6cc3a63827a2))
* resolved hassfest warnings ([fe118f3](https://github.com/KRoperUK/sungrow-hass/commit/fe118f39de2c4413bad1176558eec599a3f1d7bb))
* resolved hassfest warnings ([9b62243](https://github.com/KRoperUK/sungrow-hass/commit/9b622435faf635d17245d4aeac1d8d7fcc1006de))
* resolved hassfest warnings ([704e48c](https://github.com/KRoperUK/sungrow-hass/commit/704e48c9e59fe5b8c76cb164e870ac8179f2807b))

