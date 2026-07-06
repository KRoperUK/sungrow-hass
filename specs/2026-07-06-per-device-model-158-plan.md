# #158 Per-Device Model — Implementation Plan

> **For agentic workers:** Implement task-by-task, TDD. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Re-home flat plant-level realtime sensors onto their physical device (inverter / battery / meter) via a curated point-code→device-type map, keeping entity unique_ids (non-breaking), and register the plant device explicitly so it anchors the `via_device` tree.

**Architecture:** A `POINT_DEVICE_TYPE` map (const.py) + `resolve_point_device()` (\_\_init\_\_.py) decide, per point, the single owning device — only when exactly one device of a mapped type exists (singular rule). `SungrowSensor` picks plant vs device `DeviceInfo` from the resolver; `unique_id` is unchanged so HA just re-parents the entity. The plant "service" device is registered explicitly at setup.

**Tech Stack:** Home Assistant custom integration, pysolarcloud, pytest + pytest-homeassistant-custom-component.

## Global Constraints

- Python 3.13; ruff line length 120; `mypy` must pass; coverage `fail_under` must stay green.
- Every behaviour change needs tests. Mirror existing test conventions (`tests/conftest.py`, MagicMock coordinators).
- Conventional Commits. Branch `feat/158-per-device-model` (already created off `main`).
- **Non-breaking:** never change a `SungrowSensor` `unique_id` (`{plant_id}_{point_code}`).
- Firmware is out of scope (device list exposes no firmware field — confirmed live).

---

### Task 1: Point→device-type map + resolver

**Files:**
- Modify: `custom_components/sungrow/const.py` (add map + type-set constants)
- Modify: `custom_components/sungrow/__init__.py` (add `resolve_point_device`)
- Test: `tests/test_init.py`

**Interfaces:**
- Produces: `POINT_DEVICE_TYPE: dict[str, frozenset[DeviceType]]` in const;
  `resolve_point_device(point_code: str, devices: list[dict]) -> dict | None` in the package root.

- [ ] **Step 1: Write the failing test** in `tests/test_init.py`:

```python
from pysolarcloud.plants import DeviceType
from custom_components.sungrow import resolve_point_device


def _dev(uuid, dtype):
    return {"uuid": uuid, "device_type": dtype}


def test_resolve_point_device_singular_rehomes():
    inv = _dev("inv-1", DeviceType.INVERTER)
    meter = _dev("m-1", DeviceType.METER)
    devices = [inv, meter]
    # PV point -> the single inverter; meter point -> the single meter.
    assert resolve_point_device("inverter_ac_power", devices) is inv
    assert resolve_point_device("grid_active_power", devices) is meter


def test_resolve_point_device_zero_or_multiple_stays_plant():
    two_inv = [_dev("inv-1", DeviceType.INVERTER), _dev("inv-2", DeviceType.INVERTER)]
    # 2 inverters -> aggregate stays on plant (None).
    assert resolve_point_device("inverter_ac_power", two_inv) is None
    # No battery device -> battery point stays on plant.
    assert resolve_point_device("battery_soc", [_dev("inv-1", DeviceType.INVERTER)]) is None


def test_resolve_point_device_unmapped_and_hybrid():
    # Unmapped code -> plant.
    assert resolve_point_device("load_power", [_dev("inv-1", DeviceType.INVERTER)]) is None
    # A hybrid ESS device satisfies both PV and battery points.
    ess = _dev("ess-1", DeviceType.ENERGY_STORAGE_SYSTEM)
    assert resolve_point_device("inverter_ac_power", [ess]) is ess
    assert resolve_point_device("battery_soc", [ess]) is ess


def test_resolve_point_device_ignores_uuidless():
    assert resolve_point_device("inverter_ac_power", [{"device_type": DeviceType.INVERTER}]) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_init.py -k resolve_point_device -q`
Expected: FAIL (ImportError: cannot import name 'resolve_point_device').

- [ ] **Step 3: Add the map to `const.py`** (append after the existing point maps):

```python
from pysolarcloud.plants import DeviceType

# Physical-device modelling (#158): map a plant realtime point code to the device
# type(s) that own it. A sensor re-homes onto that device only when the plant has
# exactly one matching device (see resolve_point_device); otherwise it stays on the
# plant. Grounded in the 74 real codes from a live plant + docs/SENSORS.md. Unmapped
# codes (load, plant aggregates, ratios, forecasts) intentionally stay on the plant.
_PV_TYPES = frozenset({DeviceType.INVERTER, DeviceType.MICROINVERTER,
                       DeviceType.ENERGY_STORAGE_SYSTEM, DeviceType.ENERGY_STORAGE_SYSTEM_2})
_BATTERY_TYPES = frozenset({DeviceType.ENERGY_STORAGE_SYSTEM, DeviceType.ENERGY_STORAGE_SYSTEM_2,
                            DeviceType.BATTERY})
_METER_TYPES = frozenset({DeviceType.METER, DeviceType.GRID_CONNECTION_POINT})

POINT_DEVICE_TYPE: dict[str, frozenset[DeviceType]] = {
    # PV / inverter
    "total_active_power": _PV_TYPES, "total_active_power_of_pv": _PV_TYPES,
    "inverter_ac_power": _PV_TYPES, "inverter_ac_power_normalization": _PV_TYPES,
    "inverter_daily_yield": _PV_TYPES, "inverter_total_yield": _PV_TYPES,
    "inverter_pr": _PV_TYPES, "daily_yield": _PV_TYPES, "total_yield": _PV_TYPES,
    "total_pv_yield": _PV_TYPES, "daily_pv_yield_ems": _PV_TYPES,
    "pv_active_power_ems": _PV_TYPES, "total_dc_power": _PV_TYPES,
    "daily_equivalent_hours_of_inverter": _PV_TYPES,
    # Battery / ESS
    "battery_level_soc": _BATTERY_TYPES, "battery_soc": _BATTERY_TYPES,
    "total_field_soc": _BATTERY_TYPES, "energy_storage_soc_ems": _BATTERY_TYPES,
    "total_field_energy_storage_active_power": _BATTERY_TYPES,
    "total_field_energy_storage_maximum_reactive_power": _BATTERY_TYPES,
    "total_field_maximum_rechargeable_power": _BATTERY_TYPES,
    "total_field_maximum_dischargeable_power": _BATTERY_TYPES,
    "total_field_chargeable_energy": _BATTERY_TYPES,
    "total_field_dischargeable_energy": _BATTERY_TYPES,
    "total_field_charge_capacity": _BATTERY_TYPES,
    "total_field_discharge_capacity": _BATTERY_TYPES,
    "daily_field_charge_capacity": _BATTERY_TYPES,
    "daily_field_discharge_capacity": _BATTERY_TYPES,
    "total_field_power_factor": _BATTERY_TYPES,
    "total_field_reactive_power": _BATTERY_TYPES,
    "total_number_of_charge_discharge": _BATTERY_TYPES,
    "energy_storage_active_power_ems": _BATTERY_TYPES,
    "energy_storage_cumulative_charge": _BATTERY_TYPES,
    "energy_storage_remaining_charge": _BATTERY_TYPES,
    "energy_storage_remaining_charge_ems": _BATTERY_TYPES,
    "ess_daily_charge_ems": _BATTERY_TYPES, "ess_daily_discharge_ems": _BATTERY_TYPES,
    "cumulative_discharge": _BATTERY_TYPES,
    "planned_charging_power": _BATTERY_TYPES, "planned_discharging_power": _BATTERY_TYPES,
    "planned_es_charging_discharging_power": _BATTERY_TYPES, "planned_es_soc": _BATTERY_TYPES,
    "battery_charge_power": _BATTERY_TYPES, "battery_discharge_power": _BATTERY_TYPES,
    "battery_level": _BATTERY_TYPES, "battery_soh": _BATTERY_TYPES,
    "battery_voltage": _BATTERY_TYPES, "battery_current": _BATTERY_TYPES,
    "battery_temperature": _BATTERY_TYPES,
    "battery_total_charge_energy": _BATTERY_TYPES,
    "battery_total_discharge_energy": _BATTERY_TYPES,
    # Meter / grid
    "grid_active_power": _METER_TYPES, "grid_active_power_ems": _METER_TYPES,
    "meter_ac_power": _METER_TYPES, "meter_active_power": _METER_TYPES,
    "meter_daily_yield": _METER_TYPES, "meter_total_yield": _METER_TYPES,
    "meter_e_daily_consumption": _METER_TYPES,
    "accumulative_power_consumption_by_meter": _METER_TYPES,
    "feed_in_energy_today": _METER_TYPES, "feed_in_energy_total": _METER_TYPES,
    "daily_feed_in_energy_pv": _METER_TYPES, "energy_purchased_today": _METER_TYPES,
    "total_purchased_energy": _METER_TYPES,
    "meter_forward_active_energy": _METER_TYPES, "meter_reverse_active_energy": _METER_TYPES,
    "meter_daily_forward_active_energy": _METER_TYPES,
    "meter_daily_reverse_active_energy": _METER_TYPES,
    "meter_apparent_power": _METER_TYPES, "meter_frequency": _METER_TYPES,
}
```

- [ ] **Step 4: Add `resolve_point_device` to `__init__.py`** (after `_has_battery_device`, near the other device helpers). Import `POINT_DEVICE_TYPE` from const:

```python
def resolve_point_device(point_code: str, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single physical device a plant point belongs to, else None (=plant).

    Re-homes a flat plant sensor onto its device (#158) only when the plant has exactly
    one device of a mapped type (the "singular" rule); 0 or >1 matches keep the point on
    the plant device so genuine aggregates (e.g. total power on a 2-inverter plant) stay
    correct. Unmapped codes also stay on the plant.
    """
    types = POINT_DEVICE_TYPE.get(point_code)
    if not types:
        return None
    matches = [d for d in devices if d.get("uuid") and any(_matches_device_type(d, t) for t in types)]
    return matches[0] if len(matches) == 1 else None
```

Add `POINT_DEVICE_TYPE` to the `from .const import (...)` block.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_init.py -k resolve_point_device -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add custom_components/sungrow/const.py custom_components/sungrow/__init__.py tests/test_init.py
git commit -m "feat: map plant points to owning device type for #158 re-homing"
```

---

### Task 2: Re-home sensors + plant-device helper

**Files:**
- Modify: `custom_components/sungrow/__init__.py` (add `build_plant_device_info`)
- Modify: `custom_components/sungrow/sensor.py` (`SungrowSensor.__init__` device selection; drop unused imports)
- Test: `tests/test_sensor.py`

**Interfaces:**
- Consumes: `resolve_point_device` (Task 1), `build_device_info` (existing).
- Produces: `build_plant_device_info(plant_id: str, plant_name: str, console_url: str) -> DeviceInfo`.

- [ ] **Step 1: Add `build_plant_device_info` to `__init__.py`** (near `build_device_info`):

```python
def build_plant_device_info(plant_id: str, plant_name: str, console_url: str) -> DeviceInfo:
    """DeviceInfo for the plant 'service' device that anchors the via_device tree."""
    return DeviceInfo(
        identifiers={(DOMAIN, plant_id)},
        name=plant_name,
        manufacturer="Sungrow",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=console_url,
    )
```

- [ ] **Step 2: Write the failing test** in `tests/test_sensor.py` (add near the SungrowSensor tests). Note the coordinator now needs `.devices`:

```python
from pysolarcloud.plants import DeviceType
from custom_components.sungrow.const import DOMAIN


def _coord_with_devices(devices, data=None):
    c = MagicMock()
    c.data = data or {}
    c.devices = devices
    c.plant_name = "Plant"
    return c


def test_sensor_rehomes_to_singular_device():
    inv = {"uuid": "inv-1", "device_type": DeviceType.INVERTER,
           "device_model_code": "SG3.6RS", "device_sn": "A1", "factory_name": "SUNGROW"}
    coordinator = _coord_with_devices([inv])
    sensor = SungrowSensor(coordinator, "inverter_ac_power", "123", "Plant",
                           {"code": "inverter_ac_power", "value": "1", "unit": "W"})
    # Re-homed onto the inverter device, unique_id unchanged (non-breaking).
    assert (DOMAIN, "inv-1") in sensor._attr_device_info["identifiers"]
    assert sensor._attr_unique_id == "123_inverter_ac_power"


def test_sensor_stays_on_plant_when_unmapped_or_no_device():
    coordinator = _coord_with_devices([])
    sensor = SungrowSensor(coordinator, "total_active_power", "123", "Plant",
                           {"code": "total_active_power", "value": "1", "unit": "kW"})
    # No devices -> stays on the plant service device.
    assert sensor._attr_device_info["identifiers"] == {(DOMAIN, "123")}
    assert sensor._attr_device_info.get("entry_type") is not None
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sensor.py -k "rehomes or stays_on_plant" -q`
Expected: FAIL (device_info still always the plant device / iterating MagicMock.devices).

- [ ] **Step 4: Update `SungrowSensor.__init__`** in `sensor.py`. Replace the inline plant `DeviceInfo` block (lines ~157-163) with:

```python
        # Re-home the sensor onto its physical device when the plant has exactly one
        # device of the mapped type; otherwise keep it on the plant device (#158).
        device = resolve_point_device(point_code, getattr(coordinator, "devices", None) or [])
        if device is not None:
            self._attr_device_info = build_device_info(device, plant_id, fallback_name=plant_name)
        else:
            self._attr_device_info = build_plant_device_info(plant_id, plant_name, console_url)
```

Update the import at the top: `from . import build_device_info, build_plant_device_info, resolve_point_device`. Remove the now-unused `from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo` line (SungrowSensor no longer builds DeviceInfo inline; SungrowDeviceSensor uses build_device_info). Verify `DeviceInfo`/`DeviceEntryType` are not referenced elsewhere in `sensor.py` before removing.

- [ ] **Step 5: Update existing sensor-test helpers** so MagicMock coordinators expose an iterable `devices`. In `tests/test_sensor.py`, add `coordinator.devices = []` to `TestSungrowSensor._make_coordinator`:

```python
    def _make_coordinator(self, data=None):
        coordinator = MagicMock()
        coordinator.data = data or {}
        coordinator.devices = []          # #158: SungrowSensor reads this
        return coordinator
```

- [ ] **Step 6: Run the full sensor suite**

Run: `.venv/bin/python -m pytest tests/test_sensor.py -q`
Expected: PASS (new tests + all existing).

- [ ] **Step 7: Commit**

```bash
git add custom_components/sungrow/__init__.py custom_components/sungrow/sensor.py tests/test_sensor.py
git commit -m "feat: re-home plant sensors onto their physical device (#158)"
```

---

### Task 3: Register the plant device explicitly (anchor)

**Files:**
- Modify: `custom_components/sungrow/__init__.py` (`async_setup_entry`; imports)
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `build_plant_device_info` (Task 2).

- [ ] **Step 1: Write the failing test** in `tests/test_init.py`:

```python
from homeassistant.helpers import device_registry as dr
from custom_components.sungrow.const import DOMAIN


async def test_plant_device_registered_and_sensor_rehomes(hass, mock_setup_auth, mock_plants_service):
    """The plant device is registered as an anchor even when a sensor re-homes to a device."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from .conftest import MOCK_CONFIG_DATA

    inv_uuid = 1000001
    mock_plants_service.async_get_plant_devices.return_value = [
        {"uuid": inv_uuid, "device_type": 1, "device_name": "Inv",
         "device_model_code": "SG3.6RS", "device_sn": "A1", "factory_name": "SUNGROW",
         "dev_fault_status": 4, "dev_status": "1", "ps_key": "57_1"}
    ]
    mock_plants_service.async_get_realtime_data.return_value = {
        "12345": {"inverter_ac_power": {"code": "inverter_ac_power", "value": "1", "unit": "W"}},
        "67890": {},
    }
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="Sungrow", unique_id="test_app_id")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    # Plant anchor device exists...
    assert registry.async_get_device(identifiers={(DOMAIN, "12345")}) is not None
    # ...and the physical inverter device exists too.
    assert registry.async_get_device(identifiers={(DOMAIN, str(inv_uuid))}) is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_init.py -k plant_device_registered -q`
Expected: FAIL (plant device only implicitly created; assertion on explicit anchor may still pass via sensors — if it passes, still add the explicit registration for the all-re-home case and keep the test as a guard).

- [ ] **Step 3: Add explicit registration** in `async_setup_entry`, immediately after `entry.runtime_data = SungrowData(...)` and before `async_forward_entry_setups`:

```python
    # Register the plant "service" device explicitly so it always anchors the
    # via_device tree, even when every plant sensor re-homes onto a physical
    # device (#158).
    console_url = GATEWAY_CONSOLE_URLS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_CONSOLE_URL)
    device_registry = dr.async_get(hass)
    for coordinator in coordinators:
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **build_plant_device_info(coordinator.plant_id, coordinator.plant_name, console_url),
        )
```

Add `DEFAULT_CONSOLE_URL, GATEWAY_CONSOLE_URLS` to the `from .const import (...)` block.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_init.py -k plant_device_registered -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/__init__.py tests/test_init.py
git commit -m "feat: register the plant device explicitly as via_device anchor (#158)"
```

---

### Task 4: Full gate + docs

**Files:**
- Modify: `docs/SENSORS.md`, `docs/configuration.md`, `README.md`, `docs/index.md` (describe the device tree)

- [ ] **Step 1: Run the full local CI gate**

Run:
```bash
.venv/bin/ruff check custom_components/ tests/
.venv/bin/ruff format --check custom_components/ tests/
.venv/bin/mypy
.venv/bin/python -m pytest tests/ -q
```
Expected: all pass; coverage `fail_under` green.

- [ ] **Step 2: Update docs** — in `docs/SENSORS.md` "Device-level" section and `docs/configuration.md` "Per-device sensors", note that **plant realtime sensors now appear under their physical device** (inverter/meter/battery) when the plant has a single device of that type, falling back to the plant device otherwise; entity IDs and history are unchanged. Add a matching bullet to `README.md`/`docs/index.md` features. Keep GitHub-friendly `>` blockquotes in SENSORS.md/README.md; `!!!` admonitions only in configuration.md/index.md.

- [ ] **Step 3: Commit**

```bash
git add docs/ README.md
git commit -m "docs: document per-device sensor grouping (#158)"
```

---

## Self-Review

- **Spec coverage:** map (Task 1) ✓, resolver singular rule (Task 1) ✓, non-breaking unique_id (Task 2) ✓, plant anchor (Task 3) ✓, firmware dropped ✓, docs (Task 4) ✓.
- **Placeholders:** none — full code in every step.
- **Type consistency:** `resolve_point_device(point_code, devices)->dict|None`, `build_plant_device_info(plant_id, plant_name, console_url)->DeviceInfo` used identically across tasks.
