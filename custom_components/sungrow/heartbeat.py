"""EMS heartbeat lifecycle management for the Sungrow integration.

The heartbeat keeps the inverter in External-EMS mode while a forced
charge/discharge is active. Extracted from ``__init__.py`` (#289).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from . import SungrowConfigEntry

_LOGGER = logging.getLogger(__name__)

# How long to wait for a heartbeat loop to observe its stop event and exit
# before force-cancelling it.
HEARTBEAT_STOP_TIMEOUT = 10

# Repair raised when the EMS heartbeat loop stops unexpectedly while a forced
# charge/discharge is active (#231/#254).
_HEARTBEAT_STOPPED_ISSUE = "heartbeat_stopped"
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"


async def _stop_heartbeat(heartbeat: tuple[asyncio.Event, asyncio.Task[None]]) -> None:
    """Signal a heartbeat loop to stop and wait for it to actually exit."""
    stop_event, task = heartbeat
    stop_event.set()
    try:
        async with asyncio.timeout(HEARTBEAT_STOP_TIMEOUT):
            await task
    except TimeoutError:
        _LOGGER.warning("Heartbeat loop did not stop within %ss; cancelling", HEARTBEAT_STOP_TIMEOUT)
        task.cancel()
    except asyncio.CancelledError:
        pass
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Heartbeat loop raised while stopping")


def _plant_name(entry: SungrowConfigEntry, plant_id: str) -> str:
    """Return the plant's display name for a Repair message, falling back to its id."""
    data = getattr(entry, "runtime_data", None)
    for coordinator in getattr(data, "coordinators", []) or []:
        if coordinator.plant_id == plant_id:
            return str(coordinator.plant_name)
    return plant_id


@callback
def _on_heartbeat_done(
    hass: HomeAssistant,
    entry: SungrowConfigEntry,
    plant_id: str,
    stop_event: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    """Detect an EMS heartbeat loop that stopped unexpectedly and raise a Repair (#254).

    The heartbeat keeps the inverter in External-EMS mode while a forced charge/discharge
    is active. If the loop raises and exits on its own — as seen in #231, where it died
    silently for ~1h48m — the inverter times out of forced mode and the command quietly
    stops being applied. A *requested* stop (``stop_event`` set) or a cancellation (entry
    unload / HA shutdown) is expected and ignored; anything else surfaces an actionable
    Repair so the user knows dispatch is no longer being kept alive.
    """
    if stop_event.is_set() or task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:  # pragma: no cover - guarded by task.cancelled() above
        return
    _LOGGER.error(
        "EMS heartbeat loop for plant %s stopped unexpectedly; a forced charge/discharge "
        "is no longer being kept alive on the inverter: %s",
        plant_id,
        exc,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_HEARTBEAT_STOPPED_ISSUE,
        translation_placeholders={"plant": _plant_name(entry, plant_id)},
        learn_more_url=_REPAIR_LEARN_MORE,
    )


async def _heartbeat_loop(control: object, device_uuid: str, interval: int, stop_event: asyncio.Event) -> None:
    """Run an EMS heartbeat loop on either OAuth ``Control`` or user ``UserControl``.

    OAuth ``Control`` exposes ``heartbeat_loop``; ``UserControl`` (0.14+) shares the same
    param write surface, so we drive param 10017 via ``async_update_parameters`` (#271).
    """
    loop = getattr(control, "heartbeat_loop", None)
    if callable(loop):
        await loop(device_uuid, interval, stop_event)
        return

    # UserControl (and any duck-typed client with async_update_parameters).
    from pysolarcloud.control import Control

    update = getattr(control, "async_update_parameters", None)
    if not callable(update):
        raise TypeError(f"control client has no heartbeat_loop or async_update_parameters: {type(control)!r}")

    if not 1 <= interval <= 1000:
        raise ValueError("heartbeat interval must be between 1 and 1000 seconds")
    wire = Control.encode_parameter("external_ems_heartbeat", interval)
    while not stop_event.is_set():
        try:
            await update(device_uuid, {"external_ems_heartbeat": wire})
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("EMS heartbeat failed for %s: %s", device_uuid, err)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
        return


async def async_start_heartbeat(
    hass: HomeAssistant, entry: SungrowConfigEntry, plant_id: str, device_uuid: str, interval: int
) -> None:
    """Start (or restart) the EMS heartbeat loop for a plant/device."""
    data = entry.runtime_data
    heartbeats = data.heartbeats

    # A fresh keepalive is starting, so clear any stale "heartbeat stopped" Repair (#254).
    ir.async_delete_issue(hass, DOMAIN, f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}")

    stop_event = asyncio.Event()
    control = data.control
    assert control is not None  # the heartbeat is only ever started on a cloud dispatch entry
    # Tracked by the config entry so HA cancels it automatically on unload.
    task = entry.async_create_background_task(
        hass,
        _heartbeat_loop(control, device_uuid, interval, stop_event),
        name=f"sungrow-heartbeat-{plant_id}",
    )
    # Surface an unexpected exit (the #231 silent-death bug) as a Repair. A requested
    # stop or a cancellation is ignored by the callback.
    task.add_done_callback(lambda finished: _on_heartbeat_done(hass, entry, plant_id, stop_event, finished))
    # Publish the new loop into the map BEFORE awaiting the old one's stop, so two
    # concurrent starts can't interleave across that await and orphan a task (a task
    # left running but no longer in `heartbeats`). Whichever start runs last owns the
    # map; every task it displaces is stopped by the displacing call.
    existing = heartbeats.get(plant_id)
    heartbeats[plant_id] = (stop_event, task)
    if existing is not None:
        await _stop_heartbeat(existing)


async def async_stop_heartbeat(hass: HomeAssistant, entry: SungrowConfigEntry, plant_id: str) -> None:
    """Stop the EMS heartbeat loop for a plant."""
    # An intentional stop means a dead-heartbeat Repair (if any) no longer applies (#254).
    ir.async_delete_issue(hass, DOMAIN, f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}")
    heartbeats = entry.runtime_data.heartbeats
    heartbeat = heartbeats.pop(plant_id, None)
    if heartbeat is not None:
        await _stop_heartbeat(heartbeat)
