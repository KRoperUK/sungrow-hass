## [3.0.0](https://github.com/KRoperUK/sungrow-hass/compare/v2.0.0...v3.0.0) (2026-07-03)


### ⚠ BREAKING CHANGES

* audit remediation for 3.0.0 — classification, dispatch, auth, reliability (#109–#118)
* comprehensive measure-point mapping + classification (#105) (#107)

### Features

* add a reconfigure flow (change region/credentials without delete) ([#87](https://github.com/KRoperUK/sungrow-hass/issues/87)) ([d7b21f0](https://github.com/KRoperUK/sungrow-hass/commit/d7b21f00040b9f9616576cd13e3258db2cde4730)), closes [#80](https://github.com/KRoperUK/sungrow-hass/issues/80)
* add battery power-cap and second forced-charge target controls ([#100](https://github.com/KRoperUK/sungrow-hass/issues/100)) ([31db2cf](https://github.com/KRoperUK/sungrow-hass/commit/31db2cfc14a1fe3cc03c790a0ebc04e3dd0f1d69))
* add French (fr) translation ([#125](https://github.com/KRoperUK/sungrow-hass/issues/125)) ([090c64c](https://github.com/KRoperUK/sungrow-hass/commit/090c64c584287cbd7f2d43cedf28a23c188ca2e4))
* add German (de) translation ([#124](https://github.com/KRoperUK/sungrow-hass/issues/124)) ([93a6c3a](https://github.com/KRoperUK/sungrow-hass/commit/93a6c3a346113f7d0e0248cfa6a985e263b758e9))
* add Spanish (es) translation ([#126](https://github.com/KRoperUK/sungrow-hass/issues/126)) ([54a0589](https://github.com/KRoperUK/sungrow-hass/commit/54a05895502107a7f6b92b127eea8d21db1dfbc1))
* add Welsh (cy) translation ([#123](https://github.com/KRoperUK/sungrow-hass/issues/123)) ([170ea94](https://github.com/KRoperUK/sungrow-hass/commit/170ea9465542b9db48785b0d7d7f21e4d35df6c0))
* comprehensive measure-point mapping + classification ([#105](https://github.com/KRoperUK/sungrow-hass/issues/105)) ([#107](https://github.com/KRoperUK/sungrow-hass/issues/107)) ([70ce6ce](https://github.com/KRoperUK/sungrow-hass/commit/70ce6cebb231b951edf6c53b412517d315d354c6))
* derive dispatch power max from the device model code ([#90](https://github.com/KRoperUK/sungrow-hass/issues/90)) ([d56d260](https://github.com/KRoperUK/sungrow-hass/commit/d56d260fab75749440505419213946a06ab8c3d1)), closes [#81](https://github.com/KRoperUK/sungrow-hass/issues/81)
* document and alias common measuring points from the official docs ([#106](https://github.com/KRoperUK/sungrow-hass/issues/106)) ([ee33dc6](https://github.com/KRoperUK/sungrow-hass/commit/ee33dc60762efa66f66e27db70acdcd3bc550427))
* entity polish for Gold (icon + exception translations) ([#93](https://github.com/KRoperUK/sungrow-hass/issues/93)) ([2b51fc1](https://github.com/KRoperUK/sungrow-hass/commit/2b51fc1a8b7c5ee13b72d0dc3e123a1593ca7fcc))
* harden to Gold — address HA docs audit findings ([#97](https://github.com/KRoperUK/sungrow-hass/issues/97)) ([7a9bc10](https://github.com/KRoperUK/sungrow-hass/commit/7a9bc10740af918f7f89797e88a406bd2dc58795))
* opt-in per-device sensors (EV chargers, meters, extra batteries) ([#84](https://github.com/KRoperUK/sungrow-hass/issues/84)) ([9de8a6e](https://github.com/KRoperUK/sungrow-hass/commit/9de8a6e9204977548ebe667a647578a7ea92d8e2)), closes [#74](https://github.com/KRoperUK/sungrow-hass/issues/74) [#67](https://github.com/KRoperUK/sungrow-hass/issues/67)
* runtime device management for Gold (dynamic + stale devices) ([#95](https://github.com/KRoperUK/sungrow-hass/issues/95)) ([48a521a](https://github.com/KRoperUK/sungrow-hass/commit/48a521a94e42e77033c0efc36833e8711f9ebc74))
* strict typing (Platinum) + ship py.typed ([#96](https://github.com/KRoperUK/sungrow-hass/issues/96)) ([9a40471](https://github.com/KRoperUK/sungrow-hass/commit/9a40471159bd4b31599bdec221145fbf6c036e0f))
* verified dispatch controls + fix kW unit bug ([#101](https://github.com/KRoperUK/sungrow-hass/issues/101)) ([#102](https://github.com/KRoperUK/sungrow-hass/issues/102)) ([7319b98](https://github.com/KRoperUK/sungrow-hass/commit/7319b98217f0f9d0c58e1305cc0c14ab06d42529))


### Bug Fixes

* audit cleanups — sensor coercion, attributes, reauth helper ([#99](https://github.com/KRoperUK/sungrow-hass/issues/99)) ([02d1bc1](https://github.com/KRoperUK/sungrow-hass/commit/02d1bc1aa74e2896ba2f3317ad8180ceb5c12e5a)), closes [#98](https://github.com/KRoperUK/sungrow-hass/issues/98)
* audit remediation for 3.0.0 — classification, dispatch, auth, reliability ([#109](https://github.com/KRoperUK/sungrow-hass/issues/109)–[#118](https://github.com/KRoperUK/sungrow-hass/issues/118)) ([a51aec7](https://github.com/KRoperUK/sungrow-hass/commit/a51aec763271fc7d9df7c8dd31ea1bb8e2199222))
* complete the flow from a late OAuth callback on the manual step ([#86](https://github.com/KRoperUK/sungrow-hass/issues/86)) ([bb16479](https://github.com/KRoperUK/sungrow-hass/commit/bb16479c7b96c42fe6a15877199d6806765413a1)), closes [#75](https://github.com/KRoperUK/sungrow-hass/issues/75)
* correct dispatch parameter encodings per the official API docs ([#103](https://github.com/KRoperUK/sungrow-hass/issues/103)) ([cdb8d53](https://github.com/KRoperUK/sungrow-hass/commit/cdb8d53a6cacff318652baf99b98e19a14104ee2)), closes [#102](https://github.com/KRoperUK/sungrow-hass/issues/102)
* match the typed token-refresh error instead of a bare KeyError ([#91](https://github.com/KRoperUK/sungrow-hass/issues/91)) ([8ea3c48](https://github.com/KRoperUK/sungrow-hass/commit/8ea3c4813321a56c58a69c47cc79814db66e908f)), closes [#82](https://github.com/KRoperUK/sungrow-hass/issues/82) [KRoperUK/pysolarcloud#1](https://github.com/KRoperUK/pysolarcloud/issues/1)
* require pysolarcloud 0.6.0 and drop KeyError refresh fallback ([#92](https://github.com/KRoperUK/sungrow-hass/issues/92)) ([ca83f4b](https://github.com/KRoperUK/sungrow-hass/commit/ca83f4b00ee1d4e7cff792194a1104857a0b5ffc))

## [5.0.0](https://github.com/KRoperUK/sungrow-hass/compare/v4.1.0...v5.0.0) (2026-07-17)


### ⚠ BREAKING CHANGES

* select.*_charge_discharge_command is replaced by select.*_battery_mode with renamed options. Update automations or use sungrow.set_battery_mode.
* **dispatch:** select.*_charge_discharge_command is replaced by select.*_battery_mode with renamed options. Update automations or use sungrow.set_battery_mode.

### Features

* cloud_user realtime — map getPsDetail to measure points ([#269](https://github.com/KRoperUK/sungrow-hass/issues/269)) ([#276](https://github.com/KRoperUK/sungrow-hass/issues/276)) ([10a46e3](https://github.com/KRoperUK/sungrow-hass/commit/10a46e3bf6e7e2789e001e974b5e7f554590ad82))
* cloud_user transport (email/password) + config flow ([#268](https://github.com/KRoperUK/sungrow-hass/issues/268)) ([#275](https://github.com/KRoperUK/sungrow-hass/issues/275)) ([96d94f8](https://github.com/KRoperUK/sungrow-hass/commit/96d94f8d72292be08016941a83d027e3dddb73bc))
* **dispatch:** unified Battery Mode select + set_battery_mode service ([#284](https://github.com/KRoperUK/sungrow-hass/issues/284)) ([e5bd744](https://github.com/KRoperUK/sungrow-hass/commit/e5bd744ff3adb7c040709d1b59f35b8591ebf3c9))
* per-model measure-point capability map ([#251](https://github.com/KRoperUK/sungrow-hass/issues/251)) ([#259](https://github.com/KRoperUK/sungrow-hass/issues/259)) ([1f831cc](https://github.com/KRoperUK/sungrow-hass/commit/1f831ccd314e7717ce833cb9c402be023eeb42bc))
* point-discovery catalog in diagnostics ([#252](https://github.com/KRoperUK/sungrow-hass/issues/252)) ([#261](https://github.com/KRoperUK/sungrow-hass/issues/261)) ([61caa6f](https://github.com/KRoperUK/sungrow-hass/commit/61caa6fa7253defd28943ee510cfff47de99c7e7))
* surface silent EMS heartbeat death as a Repair ([#254](https://github.com/KRoperUK/sungrow-hass/issues/254)) ([#266](https://github.com/KRoperUK/sungrow-hass/issues/266)) ([763dc5d](https://github.com/KRoperUK/sungrow-hass/commit/763dc5dd937c65124a1aef1003ab0fa227651e68))
* verify dispatch actuation, retry, then Repair if unapplied ([#254](https://github.com/KRoperUK/sungrow-hass/issues/254)) ([#273](https://github.com/KRoperUK/sungrow-hass/issues/273)) ([9e42709](https://github.com/KRoperUK/sungrow-hass/commit/9e42709ed1461c73dd5a86f74f5c9111b8b85ee1))


### Bug Fixes

* **cloud_user:** catalog plant point 83123 as total feed-in ([#282](https://github.com/KRoperUK/sungrow-hass/issues/282)) ([efd2d74](https://github.com/KRoperUK/sungrow-hass/commit/efd2d74e952f56dab54626a6075c44d74fe51458)), closes [#281](https://github.com/KRoperUK/sungrow-hass/issues/281)
* **cloud_user:** resolve realtime point names via the catalog ([#269](https://github.com/KRoperUK/sungrow-hass/issues/269)) ([#278](https://github.com/KRoperUK/sungrow-hass/issues/278)) ([433c946](https://github.com/KRoperUK/sungrow-hass/commit/433c946dd3bf8481e97328a22b3f3bda87a7c569))
* **dispatch:** default Forced Dispatch Duration to 60 minutes ([#283](https://github.com/KRoperUK/sungrow-hass/issues/283)) ([8b1df3b](https://github.com/KRoperUK/sungrow-hass/commit/8b1df3b3ff7bbb1d34c3fd8fcf4becc6f1ddd559))
* normalize kW→W for cloud_user power sensors ([#285](https://github.com/KRoperUK/sungrow-hass/issues/285)) ([6c0d3fa](https://github.com/KRoperUK/sungrow-hass/commit/6c0d3fa18e67aaac53413f1c6c9d3984ba6e1dd0))

## [4.1.0](https://github.com/KRoperUK/sungrow-hass/compare/v4.0.2...v4.1.0) (2026-07-15)


### Features

* auto-request battery charge/discharge power for ESS devices ([#249](https://github.com/KRoperUK/sungrow-hass/issues/249)) ([83fd09b](https://github.com/KRoperUK/sungrow-hass/commit/83fd09b50e762ec4a30852ad3bb1c0899797547d))

## [4.0.2](https://github.com/KRoperUK/sungrow-hass/compare/v4.0.1...v4.0.2) (2026-07-14)


### Bug Fixes

* **config-flow:** back-fill app_id at setup time, not only migration ([#245](https://github.com/KRoperUK/sungrow-hass/issues/245)) ([#248](https://github.com/KRoperUK/sungrow-hass/issues/248)) ([474a954](https://github.com/KRoperUK/sungrow-hass/commit/474a954838cd74b8e4b246817d6bb66c33a5007f))
* **config-flow:** handle missing app_id in reconfigure/reauth ([#245](https://github.com/KRoperUK/sungrow-hass/issues/245)) ([#246](https://github.com/KRoperUK/sungrow-hass/issues/246)) ([5ec1318](https://github.com/KRoperUK/sungrow-hass/commit/5ec13184b266a71cca1fecdc96668b7fb73ca693))

## [4.0.1](https://github.com/KRoperUK/sungrow-hass/compare/v4.0.0...v4.0.1) (2026-07-13)


### Bug Fixes

* **ci:** upload HACS zip asset to releases ([#243](https://github.com/KRoperUK/sungrow-hass/issues/243)) ([420e070](https://github.com/KRoperUK/sungrow-hass/commit/420e0702357a49514a0344cd9146923b7264039b))

## [4.0.0](https://github.com/KRoperUK/sungrow-hass/compare/v3.4.1...v4.0.0) (2026-07-13)


### ⚠ BREAKING CHANGES

* Config entry VERSION bumped from 2 to 3; existing cloud entries are migrated to include an explicit transport field. The config flow now presents a transport-mode choice as the first step.
* Cloud and local Modbus connections are now configured as separate integration entries. Existing local-mode config entries must be re-added after upgrading. Cloud-only users are unaffected.

### Features

* add Modbus local polling support ([c2b35f9](https://github.com/KRoperUK/sungrow-hass/commit/c2b35f90d69bae66c602f1f1a77532708795bd81))
* discovery offers to add local Modbus to an existing cloud entry ([#218](https://github.com/KRoperUK/sungrow-hass/issues/218)) ([c3c99d8](https://github.com/KRoperUK/sungrow-hass/commit/c3c99d8690864a90f7596d8a34110baa002c92d3))
* discovery offers to add local Modbus to an existing cloud entry ([#218](https://github.com/KRoperUK/sungrow-hass/issues/218)) ([c3c99d8](https://github.com/KRoperUK/sungrow-hass/commit/c3c99d8690864a90f7596d8a34110baa002c92d3))
* discovery offers to add local Modbus to an existing cloud entry ([#218](https://github.com/KRoperUK/sungrow-hass/issues/218)) ([709d315](https://github.com/KRoperUK/sungrow-hass/commit/709d3159b5586a5078e6cea190572c8d817a4358))
* extend local Modbus support with SH-RT hybrid maps and family auto-detection ([5873f52](https://github.com/KRoperUK/sungrow-hass/commit/5873f52a4ec9b1d8dbc2f94fee20c84b1735c8fe))
* hybrid local+cloud polish for pre-release testing ([d77e429](https://github.com/KRoperUK/sungrow-hass/commit/d77e429bca831c78c87d5e05df9518d234c00522))
* local Modbus client + SG-RS register map ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159) phase 1) ([#213](https://github.com/KRoperUK/sungrow-hass/issues/213)) ([c37cb28](https://github.com/KRoperUK/sungrow-hass/commit/c37cb2824d595f8f9ede7aa8c26f19ad227c4bf2))
* Modbus connectivity sensor, diagnostics, and re-nest improvements ([a2efd0e](https://github.com/KRoperUK/sungrow-hass/commit/a2efd0e38831ae0874574e30783cf36b5abe256b))
* Modbus connectivity sensor, diagnostics, and re-nest improvements ([93063eb](https://github.com/KRoperUK/sungrow-hass/commit/93063ebd773304c86e85a71ff667d4327e4a0823))
* **modbus:** surface a daily_yield diagnostic dump on the sensor ([#223](https://github.com/KRoperUK/sungrow-hass/issues/223)) ([a7c2fb0](https://github.com/KRoperUK/sungrow-hass/commit/a7c2fb0d543605a4daab33056dbe61f6c7d122f0))
* **modbus:** surface a daily_yield diagnostic dump on the sensor ([#223](https://github.com/KRoperUK/sungrow-hass/issues/223)) ([a7c2fb0](https://github.com/KRoperUK/sungrow-hass/commit/a7c2fb0d543605a4daab33056dbe61f6c7d122f0))
* **modbus:** surface a daily_yield diagnostic dump on the sensor ([#223](https://github.com/KRoperUK/sungrow-hass/issues/223)) ([e3b9855](https://github.com/KRoperUK/sungrow-hass/commit/e3b9855de5edf0fe5fa54625358c78e14d73235a))
* opt-in local Modbus transport with cloud merge ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159) phase 2) ([22140f9](https://github.com/KRoperUK/sungrow-hass/commit/22140f9ff814b30784cf6a212855f4f7dbeec6ce))
* transport-mode selector, backfill statistics, hybrid MPPT fix ([#242](https://github.com/KRoperUK/sungrow-hass/issues/242)) ([f8b456b](https://github.com/KRoperUK/sungrow-hass/commit/f8b456be99919ab763cf5a90bb9a02e3016915a1))
* zeroconf discovery of WiNet-S for cloud-free local Modbus setup ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159) phase 3) ([93727f1](https://github.com/KRoperUK/sungrow-hass/commit/93727f18d6aa8836291d298ccd52778942de1184))


### Bug Fixes

* correct Modbus-only entry options + unload ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([6429b73](https://github.com/KRoperUK/sungrow-hass/commit/6429b73ed4a0b0136f7cc062d05ff211dba2f22b))
* correct Modbus-only entry options + unload ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([6429b73](https://github.com/KRoperUK/sungrow-hass/commit/6429b73ed4a0b0136f7cc062d05ff211dba2f22b))
* correct Modbus-only entry options + unload ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([79165c3](https://github.com/KRoperUK/sungrow-hass/commit/79165c37eab1933c492d793b5fe08913c1ab88df))
* default unitless battery SOC sensors to % ([062152f](https://github.com/KRoperUK/sungrow-hass/commit/062152f29c7335b54bd0f75ea6fcc38f269c1a23))
* default unitless battery SOC sensors to % ([4f7fb63](https://github.com/KRoperUK/sungrow-hass/commit/4f7fb63bd6825318cfe7f0e49d2001773defb506)), closes [#228](https://github.com/KRoperUK/sungrow-hass/issues/228)
* derive Modbus daily_yield from total_yield since local midnight ([5497cfc](https://github.com/KRoperUK/sungrow-hass/commit/5497cfcc817091dc1fc624d11f79993a004c06ac))
* hybrid local+cloud — derived daily, merge local-first, units & provenance ([8221653](https://github.com/KRoperUK/sungrow-hass/commit/8221653da4cd21040909f5e4da064b41699d8d34))
* Modbus-only entry reconfigure edits host, not cloud credentials ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([07b90ad](https://github.com/KRoperUK/sungrow-hass/commit/07b90adb0d068b4585aea0eca81671ac3cabea12))
* Modbus-only entry reconfigure edits host, not cloud credentials ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([07b90ad](https://github.com/KRoperUK/sungrow-hass/commit/07b90adb0d068b4585aea0eca81671ac3cabea12))
* Modbus-only entry reconfigure edits host, not cloud credentials ([#159](https://github.com/KRoperUK/sungrow-hass/issues/159)) ([d19138d](https://github.com/KRoperUK/sungrow-hass/commit/d19138dbc0215f47404909eb0ba54923f7782c2b))
* nest all local Modbus sensors under the inverter and clean up stale plant device ([160388d](https://github.com/KRoperUK/sungrow-hass/commit/160388d0d22b8e262d58bd96d41b4589606813dd))
* nest all local Modbus sensors under the inverter and clean up stale plant device ([7d9e1b5](https://github.com/KRoperUK/sungrow-hass/commit/7d9e1b50422ad575242612a069261961788350ec))
* never log the OAuth authorization code or raw callback params ([0a84e8e](https://github.com/KRoperUK/sungrow-hass/commit/0a84e8ec29316f03f78c5327e74a90081fbf6a93))
* redact plant/device names from diagnostics (leak a home address) ([#210](https://github.com/KRoperUK/sungrow-hass/issues/210)) ([02919cb](https://github.com/KRoperUK/sungrow-hass/commit/02919cb553c0df71e51aae1e614a5e6f0a46f303))
* remove speculative SG-RS high registers and skip unsupported Modbus blocks ([bf74900](https://github.com/KRoperUK/sungrow-hass/commit/bf749001fc99890e6e7e5c86bdff425eca750714))
* satisfy mypy on daily_yield unpack and optional config_entry ([6422192](https://github.com/KRoperUK/sungrow-hass/commit/6422192ffdef1e1599f23b2529e6e34f0af726df))
* skip unsupported Modbus blocks and remove speculative SG-RS high registers ([124ca5c](https://github.com/KRoperUK/sungrow-hass/commit/124ca5cf5bd1b4a8115017a7e34d7c81ebe14eed))
* switch Energy Management Mode when forcing charge/discharge ([0abc0d9](https://github.com/KRoperUK/sungrow-hass/commit/0abc0d9dbcdf962f5780bd524ed73ffcf3110ae1))
* switch Energy Management Mode when forcing charge/discharge ([5d8dc84](https://github.com/KRoperUK/sungrow-hass/commit/5d8dc84347357b1e3fd9b25a2bc5ff1cbd97ddd3)), closes [#231](https://github.com/KRoperUK/sungrow-hass/issues/231)
* translate new local-modbus strings and resolve mypy/ruff lint failures ([8158e5e](https://github.com/KRoperUK/sungrow-hass/commit/8158e5ea54c7fb2d0ebc544c42f73f3cffc4525b))
* translations, lint and extended Modbus maps for PR [#236](https://github.com/KRoperUK/sungrow-hass/issues/236) follow-up ([ab627da](https://github.com/KRoperUK/sungrow-hass/commit/ab627da00c8209684ea5d91bdfcd69728f867eeb))

## [3.4.1](https://github.com/KRoperUK/sungrow-hass/compare/v3.4.0...v3.4.1) (2026-07-06)


### Bug Fixes

* harden the EMS heartbeat lifecycle against reload/unload races ([#208](https://github.com/KRoperUK/sungrow-hass/issues/208)) ([f0c13ab](https://github.com/KRoperUK/sungrow-hass/commit/f0c13abae327ee92db2919cba0805b0416fe01da))
* keep rate-limit backoff on transient errors; fix ESS operating-status collision ([ed815ca](https://github.com/KRoperUK/sungrow-hass/commit/ed815ca7f67502d4fda86984b16e8d0c7411ec96))
* keep rate-limit backoff on transient errors; fix ESS operating-status collision ([eabc140](https://github.com/KRoperUK/sungrow-hass/commit/eabc140d51d296aa3596888bef81fcd40d4a549d))
* mark write-only dispatch controls as assumed_state ([9737157](https://github.com/KRoperUK/sungrow-hass/commit/9737157f8005509dde516f22ce92e837954368f8))
* mark write-only dispatch controls as assumed_state ([9737157](https://github.com/KRoperUK/sungrow-hass/commit/9737157f8005509dde516f22ce92e837954368f8))
* mark write-only dispatch controls as assumed_state ([69c541d](https://github.com/KRoperUK/sungrow-hass/commit/69c541db6cf9ffaa5fccbbdc6877261511ad6418))
* normalise the ℃ unit glyph to °C; record tariff sensors as statistics ([8d9297c](https://github.com/KRoperUK/sungrow-hass/commit/8d9297c97598f66a3cc9344ad7ed8ddc2a95013d))
* normalise the ℃ unit glyph to °C; record tariff sensors as statistics ([8d9297c](https://github.com/KRoperUK/sungrow-hass/commit/8d9297c97598f66a3cc9344ad7ed8ddc2a95013d))
* normalise the ℃ unit glyph to °C; record tariff sensors as statistics ([30db2c8](https://github.com/KRoperUK/sungrow-hass/commit/30db2c8f4d78ac5ba7e63c26786194c1c0c3a2eb))

## [3.4.0](https://github.com/KRoperUK/sungrow-hass/compare/v3.3.0...v3.4.0) (2026-07-06)


### Features

* acronym names, diagnostic icons & integer counts for device sensors ([bb1a01a](https://github.com/KRoperUK/sungrow-hass/commit/bb1a01a34437ea1fdb4a0c3ca572b982df259f1f))
* add battery cell/module health sensors ([#180](https://github.com/KRoperUK/sungrow-hass/issues/180)) ([77d976a](https://github.com/KRoperUK/sungrow-hass/commit/77d976ab8f5c70f97f4185f6d192a6fb546d2c69))
* add per-string DC voltage & current sensors ([#189](https://github.com/KRoperUK/sungrow-hass/issues/189)) ([#192](https://github.com/KRoperUK/sungrow-hass/issues/192)) ([2bf70c2](https://github.com/KRoperUK/sungrow-hass/commit/2bf70c2ad1d58889d7f0ae18e623cb4c7d7861f5))
* add reactive-power / power-factor dispatch controls ([#181](https://github.com/KRoperUK/sungrow-hass/issues/181)) ([#190](https://github.com/KRoperUK/sungrow-hass/issues/190)) ([3f5e399](https://github.com/KRoperUK/sungrow-hass/commit/3f5e399406554396e934c193f17b38b9c0dc06b4))
* expand per-device diagnostic sensors (inverter grid health, meter) ([#179](https://github.com/KRoperUK/sungrow-hass/issues/179)) ([6149850](https://github.com/KRoperUK/sungrow-hass/commit/61498500270a5eeb15d7a873081d64126bb1d1e6))
* icons for plant-detail sensors + power_fraction ([1667654](https://github.com/KRoperUK/sungrow-hass/commit/16676546797fd49463f967c2e5f359cd45690972))
* model each physical device as a nested HA device ([#158](https://github.com/KRoperUK/sungrow-hass/issues/158)) ([#184](https://github.com/KRoperUK/sungrow-hass/issues/184)) ([e9a0777](https://github.com/KRoperUK/sungrow-hass/commit/e9a07777326ba258f3c4e95d1507ef5843b462fb))
* signal-strength device class + connectivity icon ([ccf01b6](https://github.com/KRoperUK/sungrow-hass/commit/ccf01b6cd2994ff43cef31c77a6ed0fbd85eccf7))
* signal-strength device class + connectivity icon ([5f7e526](https://github.com/KRoperUK/sungrow-hass/commit/5f7e526ba9c45bf5b465005033748bf3e19534db))
* surface operating-status reason on the Fault binary sensor ([#182](https://github.com/KRoperUK/sungrow-hass/issues/182)) ([#201](https://github.com/KRoperUK/sungrow-hass/issues/201)) ([1a83a07](https://github.com/KRoperUK/sungrow-hass/commit/1a83a073f379a2b2e52da10c97a1fb4363ce72a1))
* surface plant-level diagnostics & tariffs from getPowerStationDetail ([#178](https://github.com/KRoperUK/sungrow-hass/issues/178)) ([#187](https://github.com/KRoperUK/sungrow-hass/issues/187)) ([99a0f61](https://github.com/KRoperUK/sungrow-hass/commit/99a0f612563c8fb75bfcfaa7d74f51c550a2d0b9))
* time-limited auto-revert for forced dispatch ([#157](https://github.com/KRoperUK/sungrow-hass/issues/157)) ([#191](https://github.com/KRoperUK/sungrow-hass/issues/191)) ([7b14f86](https://github.com/KRoperUK/sungrow-hass/commit/7b14f86cbadb44a8e5c0b37091a3585f3938009c))


### Bug Fixes

* bump pysolarcloud to 0.10.1 to restore per-device sensors ([#194](https://github.com/KRoperUK/sungrow-hass/issues/194)) ([aef9643](https://github.com/KRoperUK/sungrow-hass/commit/aef96439bfea342fecb2c22a74ff538f382269cc))
* bump pysolarcloud to 0.10.2 — restore inverter per-device sensors ([66f0728](https://github.com/KRoperUK/sungrow-hass/commit/66f0728d91ec68321bce91d36648987316084a77))
* bump pysolarcloud to 0.10.3 — chunk realtime point_id_list (100-point cap) ([1d32f1a](https://github.com/KRoperUK/sungrow-hass/commit/1d32f1ac90ca077e5be677222af30a7d19abca6e))
* bump pysolarcloud to 0.10.3 — chunk realtime point_id_list (100-point cap) ([1d32f1a](https://github.com/KRoperUK/sungrow-hass/commit/1d32f1ac90ca077e5be677222af30a7d19abca6e))
* bump pysolarcloud to 0.10.3 — chunk realtime point_id_list (100-point cap) ([6a17c64](https://github.com/KRoperUK/sungrow-hass/commit/6a17c64b60a80cec1abc1e631788125f730e1d28))

## [3.3.0](https://github.com/KRoperUK/sungrow-hass/compare/v3.2.0...v3.3.0) (2026-07-06)


### Features

* add a per-device fault/alarm binary sensor ([#151](https://github.com/KRoperUK/sungrow-hass/issues/151)) ([#166](https://github.com/KRoperUK/sungrow-hass/issues/166)) ([436dd88](https://github.com/KRoperUK/sungrow-hass/commit/436dd885bda443c07089094d4ea260ad32d33141))
* add device connectivity, commissioning date & WLAN signal strength ([#149](https://github.com/KRoperUK/sungrow-hass/issues/149)) ([1f2a775](https://github.com/KRoperUK/sungrow-hass/commit/1f2a775dccced032e80d995ba52c816825da3075))
* back off the poll interval when rate-limited ([#156](https://github.com/KRoperUK/sungrow-hass/issues/156)) ([948bcf0](https://github.com/KRoperUK/sungrow-hass/commit/948bcf0f0512f495846c98c24adee66e976ea76d))
* enrich device registry with model/serial/manufacturer ([#149](https://github.com/KRoperUK/sungrow-hass/issues/149)) ([#162](https://github.com/KRoperUK/sungrow-hass/issues/162)) ([b1430dc](https://github.com/KRoperUK/sungrow-hass/commit/b1430dcc76783057e07bc8bc5aa8173ae358c2e7))
* expose battery/ESS device-level sensors for hybrid users ([#154](https://github.com/KRoperUK/sungrow-hass/issues/154)) ([0d7edbf](https://github.com/KRoperUK/sungrow-hass/commit/0d7edbf13d9c52cf78551d52cc23ff36486f4263))
* expose inverter device-level diagnostic sensors ([#149](https://github.com/KRoperUK/sungrow-hass/issues/149)) ([#165](https://github.com/KRoperUK/sungrow-hass/issues/165)) ([5886c8d](https://github.com/KRoperUK/sungrow-hass/commit/5886c8d13853ba035a44cbc1f8d052d7273f361b))
* raise HA Repair issues for whitelist & rate-limit errors ([#153](https://github.com/KRoperUK/sungrow-hass/issues/153)) ([#171](https://github.com/KRoperUK/sungrow-hass/issues/171)) ([e065ee0](https://github.com/KRoperUK/sungrow-hass/commit/e065ee07a06745a9fbf1cc5da4f62fc7f7857607))


### Bug Fixes

* delete stale main RC prereleases for released versions ([#146](https://github.com/KRoperUK/sungrow-hass/issues/146)) ([c596524](https://github.com/KRoperUK/sungrow-hass/commit/c5965249776a057e1460fec77f0ba5aa853b3ea2))
* hide battery-only dispatch controls on PV-only plants ([#148](https://github.com/KRoperUK/sungrow-hass/issues/148)) ([#150](https://github.com/KRoperUK/sungrow-hass/issues/150)) ([f58d8aa](https://github.com/KRoperUK/sungrow-hass/commit/f58d8aa1757bf4f742b5aa4cd48d524a72886279))
* pass ps_key_list to device realtime; bump pysolarcloud to 0.9.1 ([#155](https://github.com/KRoperUK/sungrow-hass/issues/155)) ([#163](https://github.com/KRoperUK/sungrow-hass/issues/163)) ([cd8bbc7](https://github.com/KRoperUK/sungrow-hass/commit/cd8bbc7dcf519c5bb3363a4782abf3b8b1715a2a))
* ride out transient poll failures instead of flapping unavailable ([#152](https://github.com/KRoperUK/sungrow-hass/issues/152)) ([#167](https://github.com/KRoperUK/sungrow-hass/issues/167)) ([cd3014d](https://github.com/KRoperUK/sungrow-hass/commit/cd3014d1f8194d42f1f68b83cecb8b8120aeb86b))

## [3.2.0](https://github.com/KRoperUK/sungrow-hass/compare/v3.1.0...v3.2.0) (2026-07-04)


### Features

* present capacity-factor ratios as percentages ([#141](https://github.com/KRoperUK/sungrow-hass/issues/141)) ([c60000c](https://github.com/KRoperUK/sungrow-hass/commit/c60000c57c67cbfb1fd24fda515ffe8ef8d11a19))


### Bug Fixes

* stringify dispatch device uuid so the device isn't pruned ([#142](https://github.com/KRoperUK/sungrow-hass/issues/142)) ([11ce9d1](https://github.com/KRoperUK/sungrow-hass/commit/11ce9d119262068ccb41fd9e42add81888ab6276))

## [3.1.0](https://github.com/KRoperUK/sungrow-hass/compare/v3.0.0...v3.1.0) (2026-07-04)


### Features

* adopt pysolarcloud 0.9.0 typed exceptions for auth classification ([#135](https://github.com/KRoperUK/sungrow-hass/issues/135)) ([0b814eb](https://github.com/KRoperUK/sungrow-hass/commit/0b814eb4657e467f76b9254e1b383e3d6526402a)), closes [#131](https://github.com/KRoperUK/sungrow-hass/issues/131)
* clearer log messages for iSolarCloud whitelist rejections (E918/E919) ([#133](https://github.com/KRoperUK/sungrow-hass/issues/133)) ([7210c37](https://github.com/KRoperUK/sungrow-hass/commit/7210c37dc6ac3cf5d55cf4dfb1b5856e915e037e))


### Bug Fixes

* anonymise device uuid keys in diagnostics per-device realtime ([#128](https://github.com/KRoperUK/sungrow-hass/issues/128)) ([744555b](https://github.com/KRoperUK/sungrow-hass/commit/744555b66cfefbdea7938aaaa424325d87849618)), closes [#122](https://github.com/KRoperUK/sungrow-hass/issues/122)
* don't create sensors for points with no usable reading ([#132](https://github.com/KRoperUK/sungrow-hass/issues/132)) ([bd0ef0e](https://github.com/KRoperUK/sungrow-hass/commit/bd0ef0e13315da2bccddff5386c39025ec5a6d08))

## [2.0.0](https://github.com/KRoperUK/sungrow-hass/compare/v1.0.0...v2.0.0) (2026-07-02)


### ⚠ BREAKING CHANGES

* first-time setup now creates the hub immediately and completes
authorization via a follow-up reauth prompt, instead of authorizing inside the
initial config flow. Existing configured entries are unaffected.

Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Features

* auto-start OAuth redirect with manual code/URL fallback ([#46](https://github.com/KRoperUK/sungrow-hass/issues/46)) ([492fa4a](https://github.com/KRoperUK/sungrow-hass/commit/492fa4a5e85d47a2b1fb52922a9296d3a982bb6b))
* create the hub first, then authorize via reauth ([#71](https://github.com/KRoperUK/sungrow-hass/issues/71)) ([579479b](https://github.com/KRoperUK/sungrow-hass/commit/579479ba21a0e7801fd0036e26e0ef1c4027963a))
* **diagnostics:** capture all devices + per-device realtime for [#18](https://github.com/KRoperUK/sungrow-hass/issues/18) ([#67](https://github.com/KRoperUK/sungrow-hass/issues/67)) ([4d4dd54](https://github.com/KRoperUK/sungrow-hass/commit/4d4dd546ba6509ad9f96f5741ec7d8be57a6c690))
* harden OAuth callback, expand tests, add repo hygiene ([#38](https://github.com/KRoperUK/sungrow-hass/issues/38)) ([ebb9968](https://github.com/KRoperUK/sungrow-hass/commit/ebb9968e68416678eaca2a1081e3010f85334e7f)), closes [#18](https://github.com/KRoperUK/sungrow-hass/issues/18)
* Home Assistant quality-scale compliance (Batch B) ([#62](https://github.com/KRoperUK/sungrow-hass/issues/62)) ([c956194](https://github.com/KRoperUK/sungrow-hass/commit/c9561943f656448d0c9ceaff4a4b55bd3e365643)), closes [#50](https://github.com/KRoperUK/sungrow-hass/issues/50)
* sub-minute polling intervals (seconds, min 10 s) ([#43](https://github.com/KRoperUK/sungrow-hass/issues/43)) ([f180ad1](https://github.com/KRoperUK/sungrow-hass/commit/f180ad1fbf4105dc2dca8c4f58a97d7751186cae)), closes [#40](https://github.com/KRoperUK/sungrow-hass/issues/40)


### Bug Fixes

* correct icon and alias for battery SoC sensor ([#39](https://github.com/KRoperUK/sungrow-hass/issues/39)) ([#45](https://github.com/KRoperUK/sungrow-hass/issues/45)) ([077b49a](https://github.com/KRoperUK/sungrow-hass/commit/077b49aacc52ec9a05863fb1c3b7bfa94d6cfdb5))
* **dispatch:** select ESS device when device_type is a DeviceType enum ([#68](https://github.com/KRoperUK/sungrow-hass/issues/68)) ([6919bf3](https://github.com/KRoperUK/sungrow-hass/commit/6919bf3448fd88925a24d2e3f50e3f421e283df0))
* handle OAuth callback when iSolarCloud strips flow_id query param ([#41](https://github.com/KRoperUK/sungrow-hass/issues/41)) ([cf5ef83](https://github.com/KRoperUK/sungrow-hass/commit/cf5ef83de298370cd3689feb495c7b5bc03826e1))
* harden heartbeat lifecycle, entity availability, and secret logging ([#48](https://github.com/KRoperUK/sungrow-hass/issues/48)) ([51dee14](https://github.com/KRoperUK/sungrow-hass/commit/51dee1448cd866f110e41c5f6675bbdbc8274f9a))
* register OAuth callback view during the config flow (first-install 404) ([#69](https://github.com/KRoperUK/sungrow-hass/issues/69)) ([329a155](https://github.com/KRoperUK/sungrow-hass/commit/329a155750d9f4be83a85dc3b5e17e73d3dff474))
* use one bare redirect_uri for auth + token exchange (invalid auth) ([#73](https://github.com/KRoperUK/sungrow-hass/issues/73)) ([3b979c2](https://github.com/KRoperUK/sungrow-hass/commit/3b979c28ca92093d36bd7125d0fe33c07260cb56))

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
