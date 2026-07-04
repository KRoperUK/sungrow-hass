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
