# Requirements Document

## Introduction

This spec adds an explicit transport-mode selector to the Sungrow iSolarCloud config flow so that users manually setting up the integration via the UI can choose between Cloud Only, Cloud + Modbus (hybrid), or Modbus Only connectivity. This is a **breaking change** (`feat!`) that bumps the config-entry VERSION from 2 to 3 and introduces a migration step to back-fill `transport: cloud_only` on legacy cloud entries that currently have no `CONF_TRANSPORT` field.

The actual hybrid polling/merge logic for the "Cloud + Modbus" data path is out of scope (see #217). This spec covers only the setup-flow UI, config-entry data shape, migration, options-flow switching, reconfigure adaptation, and translation strings.

Reference: [GitHub Issue #216](https://github.com/KRoperUK/sungrow-hass/issues/216)

## Glossary

- **Config_Flow**: The Home Assistant config flow (`SungrowConfigFlow`) that guides a user through integration setup.
- **Options_Flow**: The Home Assistant options flow (`SungrowOptionsFlow`) that lets a user change runtime settings after setup.
- **Reconfigure_Flow**: The Home Assistant reconfigure flow that lets a user change entry credentials or connection details.
- **Config_Entry**: A Home Assistant `ConfigEntry` object persisting the integration's configuration data.
- **Transport_Mode**: A string field (`CONF_TRANSPORT`) on the Config_Entry's `data` dict indicating how the integration communicates with the inverter/plant. One of: `cloud_only`, `cloud_modbus`, `modbus_only`.
- **Cloud_Only**: Transport mode where data is fetched exclusively via the iSolarCloud REST API.
- **Cloud_Modbus**: Transport mode where cloud is used for plant aggregates/tariffs and Modbus TCP is preferred for fast electrical points.
- **Modbus_Only**: Transport mode where data is fetched exclusively via local Modbus TCP with no cloud credentials.
- **WiNet_S**: The Sungrow WiNet-S communication dongle that exposes a Modbus TCP interface on the local network.
- **Migration**: The `async_migrate_entry` function that upgrades old Config_Entry schemas to the current VERSION.
- **Zeroconf_Entry**: A Config_Entry created automatically via mDNS discovery of a WiNet_S dongle.
- **Transport_Step**: The new config-flow step presenting the three transport-mode choices to the user.
- **Host_Reachability_Test**: A TCP connection attempt to the user-supplied WiNet_S host on the Modbus port (502) to confirm the device is accessible before committing the entry.
- **TRANSPORT_CLOUD_ONLY**: The constant string `"cloud_only"` stored in `entry.data[CONF_TRANSPORT]`.
- **TRANSPORT_CLOUD_MODBUS**: The constant string `"cloud_modbus"` stored in `entry.data[CONF_TRANSPORT]`.
- **TRANSPORT_MODBUS_ONLY**: The existing constant string `"modbus_only"` stored in `entry.data[CONF_TRANSPORT]`.

## Requirements

### Requirement 1: Transport Mode Constants

**User Story:** As a developer, I want well-defined transport-mode constants, so that the codebase uses a single source of truth for valid transport values.

#### Acceptance Criteria

1. THE Config_Flow SHALL define `TRANSPORT_CLOUD_ONLY` as the string `"cloud_only"` in `const.py`.
2. THE Config_Flow SHALL define `TRANSPORT_CLOUD_MODBUS` as the string `"cloud_modbus"` in `const.py`.
3. THE Config_Flow SHALL continue to use the existing `TRANSPORT_MODBUS_ONLY` constant (`"modbus_only"`) unchanged.

### Requirement 2: Transport Step in the User Config Flow

**User Story:** As a user setting up the integration manually, I want to choose my transport mode before entering credentials, so that the flow only asks for information relevant to my chosen mode.

#### Acceptance Criteria

1. WHEN the user initiates a manual setup via `async_step_user`, THE Config_Flow SHALL present the Transport_Step as the first step with a selector offering three choices: Cloud Only, Cloud + Modbus, and Modbus Only.
2. WHEN the user selects Cloud Only on the Transport_Step, THE Config_Flow SHALL proceed to the existing cloud credentials step (app key, app secret, app ID, gateway, redirect URI) and store `TRANSPORT_CLOUD_ONLY` in the resulting Config_Entry data.
3. WHEN the user selects Cloud + Modbus on the Transport_Step, THE Config_Flow SHALL proceed to the cloud credentials step followed by a Modbus host step, and store `TRANSPORT_CLOUD_MODBUS` along with `CONF_MODBUS_HOST` in the resulting Config_Entry data.
4. WHEN the user selects Modbus Only on the Transport_Step, THE Config_Flow SHALL proceed to a local-setup step collecting the WiNet_S host, inverter serial, and model (bypassing all cloud credential steps) and store `TRANSPORT_MODBUS_ONLY` in the resulting Config_Entry data.
5. THE Config_Flow SHALL display the Transport_Step choices using localised strings from `strings.json` / `translations/en.json`.

### Requirement 3: Cloud + Modbus Host Collection

**User Story:** As a user choosing hybrid mode, I want to provide the WiNet-S IP address (or have it auto-detected) so that the integration can reach the local Modbus interface.

#### Acceptance Criteria

1. WHEN the user selects Cloud + Modbus, THE Config_Flow SHALL show a Modbus host step after the cloud credentials step, with a text field for the WiNet_S IP or hostname.
2. WHERE zeroconf has already discovered a WiNet_S on the same subnet, THE Config_Flow SHALL pre-fill the host field with the discovered address.
3. WHEN the user submits the Modbus host step, THE Config_Flow SHALL perform a Host_Reachability_Test by attempting a TCP connection to the supplied host on port 502 with a 5-second timeout.
4. IF the Host_Reachability_Test fails, THEN THE Config_Flow SHALL display an error on the Modbus host step indicating the host is unreachable and allow the user to correct the address or go back.
5. WHEN the Host_Reachability_Test succeeds, THE Config_Flow SHALL store the host in `entry.data[CONF_MODBUS_HOST]` and proceed to the OAuth authorization step.

### Requirement 4: Config Entry Data Shape

**User Story:** As a developer, I want a well-defined data shape per transport mode, so that downstream components can reliably branch on transport type.

#### Acceptance Criteria

1. WHEN a Config_Entry has `transport` set to `cloud_only`, THE Config_Entry data SHALL contain `app_key`, `app_secret`, `app_id`, `gateway`, `redirect_uri`, `tokens`, and `transport` fields but SHALL NOT contain `modbus_host`.
2. WHEN a Config_Entry has `transport` set to `cloud_modbus`, THE Config_Entry data SHALL contain `app_key`, `app_secret`, `app_id`, `gateway`, `redirect_uri`, `tokens`, `transport`, and `modbus_host` fields.
3. WHEN a Config_Entry has `transport` set to `modbus_only`, THE Config_Entry data SHALL contain `transport`, `serial`, `model`, and `modbus_host` fields but SHALL NOT contain `app_key`, `app_secret`, `app_id`, `gateway`, `redirect_uri`, or `tokens`.

### Requirement 5: Config Entry Migration v2 to v3

**User Story:** As an existing user upgrading the integration, I want my config entries to migrate seamlessly so that the integration continues to function without manual re-setup.

#### Acceptance Criteria

1. THE Config_Flow SHALL set `VERSION = 3` on the `SungrowConfigFlow` class.
2. WHEN `async_migrate_entry` is called with a Config_Entry at version 2, THE Migration SHALL check whether `CONF_TRANSPORT` is present in `entry.data`.
3. WHEN `CONF_TRANSPORT` is absent from a version-2 Config_Entry, THE Migration SHALL set `entry.data[CONF_TRANSPORT]` to `TRANSPORT_CLOUD_ONLY` and update the entry version to 3.
4. WHEN `CONF_TRANSPORT` is already `TRANSPORT_MODBUS_ONLY` on a version-2 Config_Entry, THE Migration SHALL leave the transport field unchanged and update the entry version to 3.
5. THE Migration SHALL preserve the existing v1-to-v2 scan_interval conversion logic unchanged.
6. WHEN a version-1 Config_Entry is migrated, THE Migration SHALL apply both v1-to-v2 and v2-to-v3 transformations sequentially.

### Requirement 6: Options Flow Transport Mode Switching

**User Story:** As a user who initially set up Cloud Only, I want to upgrade to Cloud + Modbus later from the options flow, so that I can add local Modbus without re-creating the entry.

#### Acceptance Criteria

1. WHILE a Config_Entry has `transport` set to `cloud_only`, THE Options_Flow SHALL show an optional field to enter a WiNet_S Modbus host address.
2. WHEN the user provides a Modbus host in the Options_Flow for a `cloud_only` entry, THE Options_Flow SHALL perform a Host_Reachability_Test on the supplied host.
3. IF the Host_Reachability_Test succeeds, THEN THE Options_Flow SHALL update `entry.data[CONF_TRANSPORT]` to `TRANSPORT_CLOUD_MODBUS`, add `CONF_MODBUS_HOST` to `entry.data`, and trigger a reload.
4. WHILE a Config_Entry has `transport` set to `cloud_modbus`, THE Options_Flow SHALL show the current Modbus host with an option to clear it.
5. WHEN the user clears the Modbus host field on a `cloud_modbus` entry, THE Options_Flow SHALL update `entry.data[CONF_TRANSPORT]` to `TRANSPORT_CLOUD_ONLY`, remove `CONF_MODBUS_HOST` from `entry.data`, and trigger a reload.
6. WHILE a Config_Entry has `transport` set to `modbus_only`, THE Options_Flow SHALL NOT offer transport-mode switching (no cloud credentials exist to fall back on).
7. IF the Host_Reachability_Test fails during Options_Flow switching, THEN THE Options_Flow SHALL display an error and retain the current transport mode unchanged.

### Requirement 7: Zeroconf Entries Unaffected

**User Story:** As a user with auto-discovered WiNet-S entries, I want the transport-mode selector to have no impact on my existing Modbus-only entries, so that local-only setups continue to work without changes.

#### Acceptance Criteria

1. WHEN a WiNet_S is discovered via zeroconf, THE Config_Flow SHALL continue to create the entry with `TRANSPORT_MODBUS_ONLY` as today.
2. THE Config_Flow SHALL NOT present the Transport_Step during zeroconf or import flows.
3. WHEN a zeroconf-created entry is loaded after the v2-to-v3 migration, THE Migration SHALL leave its `transport` field as `modbus_only`.

### Requirement 8: Reconfigure Flow Adaptation

**User Story:** As a user reconfiguring an existing entry, I want the reconfigure flow to adapt to my entry's transport mode, so that I can change the relevant connection details without ambiguity.

#### Acceptance Criteria

1. WHILE the Config_Entry has `transport` set to `cloud_only`, THE Reconfigure_Flow SHALL show the cloud credentials form (app key, app secret, gateway, redirect URI) as today.
2. WHILE the Config_Entry has `transport` set to `cloud_modbus`, THE Reconfigure_Flow SHALL show the cloud credentials form followed by a Modbus host step allowing the user to update the WiNet_S address.
3. WHILE the Config_Entry has `transport` set to `modbus_only`, THE Reconfigure_Flow SHALL show only the Modbus host form as today.
4. WHEN the user updates the Modbus host during reconfigure of a `cloud_modbus` entry, THE Reconfigure_Flow SHALL perform a Host_Reachability_Test before committing the change.
5. IF the Host_Reachability_Test fails during reconfigure, THEN THE Reconfigure_Flow SHALL display an error and allow the user to correct the address.

### Requirement 9: Breaking Change Semantics

**User Story:** As a maintainer, I want the commit and release to follow conventional-commit `feat!` semantics, so that release-please generates a MAJOR version bump and the CHANGELOG clearly communicates the breaking change.

#### Acceptance Criteria

1. THE commit introducing the VERSION bump to 3 SHALL use the `feat!:` prefix in its conventional commit message.
2. THE commit message footer SHALL include `BREAKING CHANGE: Config entry VERSION bumped from 2 to 3; existing cloud entries are migrated to include an explicit transport field.`
3. THE CHANGELOG entry generated by release-please SHALL appear under a "BREAKING CHANGES" heading.

### Requirement 10: Translation Strings

**User Story:** As a user in any supported locale, I want the new transport-mode steps and selectors to have proper translation strings, so that the UI is fully localised.

#### Acceptance Criteria

1. THE Config_Flow SHALL add translation keys for the Transport_Step title, description, and each selector option (Cloud Only, Cloud + Modbus, Modbus Only) in `strings.json`.
2. THE Config_Flow SHALL add translation keys for the Modbus host step title, description, field labels, and error messages (host unreachable) in `strings.json`.
3. THE Options_Flow SHALL add translation keys for the new Modbus host field and the transport-switch confirmation in `strings.json`.
4. THE `translations/en.json` file SHALL mirror all keys added to `strings.json` with English text.

### Requirement 11: Runtime Branching on Transport Mode

**User Story:** As a developer, I want `async_setup_entry` to branch correctly on all three transport modes, so that the right coordinator behaviour is activated for each entry type.

#### Acceptance Criteria

1. WHEN `async_setup_entry` loads a Config_Entry with `transport` set to `cloud_only`, THE integration SHALL set up the cloud coordinator without a Modbus client (existing behaviour).
2. WHEN `async_setup_entry` loads a Config_Entry with `transport` set to `cloud_modbus`, THE integration SHALL set up the cloud coordinator and note `CONF_MODBUS_HOST` is available for the future hybrid data path (actual Modbus client wiring is deferred to #217).
3. WHEN `async_setup_entry` loads a Config_Entry with `transport` set to `modbus_only`, THE integration SHALL set up the Modbus-only coordinator (existing behaviour).
4. IF `CONF_TRANSPORT` is missing from a loaded Config_Entry (should not happen after migration), THEN THE integration SHALL default to `cloud_only` behaviour and log a warning.

### Requirement 12: Host Reachability Test Specification

**User Story:** As a user, I want setup to verify the WiNet-S is reachable before committing, so that I do not end up with a broken hybrid entry pointing at a dead host.

#### Acceptance Criteria

1. THE Host_Reachability_Test SHALL attempt a TCP socket connection to the user-supplied host on port 502.
2. THE Host_Reachability_Test SHALL use a timeout of 5 seconds.
3. IF the TCP connection succeeds, THEN THE Host_Reachability_Test SHALL close the socket and report success.
4. IF the TCP connection times out or is refused, THEN THE Host_Reachability_Test SHALL report failure without raising an unhandled exception.
5. THE Host_Reachability_Test SHALL be used by the Config_Flow, Options_Flow, and Reconfigure_Flow wherever a Modbus host is accepted.
