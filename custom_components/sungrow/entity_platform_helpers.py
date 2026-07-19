"""Shared helpers for the four Sungrow entity platforms.

``number.py``, ``select.py``, ``binary_sensor.py``, and ``sensor.py`` all follow
the same three-step pattern in their ``async_setup_entry``:

1. Track a per-entry ``set`` of already-seen ``unique_id`` values so a repeated
   coordinator update doesn't re-yield the same entity.
2. Consult the entity registry via
   :func:`~custom_components.sungrow.device_helpers.unique_id_owned_by_other_entry`
   so multiple Sungrow entries on the same plant don't produce the noisy
   ``Platform sungrow does not generate unique IDs`` ERROR from HA core
   (see #346, addressed in #347).
3. Register a coordinator listener that re-runs the adder when the coordinator's
   device set changes (dynamic-devices).

This module extracts the boilerplate into :func:`create_entity_adder` so each
platform's ``async_setup_entry`` shrinks from ~15 lines to ~5, and any future
platform we add can't accidentally omit the collision skip added in #347.

Closes #351.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .device_helpers import unique_id_owned_by_other_entry

if TYPE_CHECKING:
    from . import SungrowConfigEntry
    from .coordinator import SungrowPlantCoordinator

_LOGGER = logging.getLogger(__name__)


def create_entity_adder[EntityT: Entity](
    hass: HomeAssistant,
    entry: SungrowConfigEntry,
    platform: str,
    coordinators: Iterable[SungrowPlantCoordinator],
    build: Callable[[SungrowPlantCoordinator], Iterable[EntityT]],
    async_add_entities: AddEntitiesCallback,
) -> Callable[[], None]:
    """Return an ``_add_new_entities`` closure with dedup + cross-entry collision skip.

    Encapsulates the per-entry ``known_unique_ids`` cache, the cross-entry
    collision check from :func:`~custom_components.sungrow.device_helpers.unique_id_owned_by_other_entry`
    (#347), and the INFO-level log line so no platform can regress the fix by
    forgetting one of the steps.

    Args:
        hass: The Home Assistant instance the entry belongs to.
        entry: The typed Sungrow config entry being set up.
        platform: The HA platform name (``"number"`` / ``"select"`` /
            ``"binary_sensor"`` / ``"sensor"``) used both in the collision-check
            registry lookup and in the skip-log line.
        coordinators: The plant coordinators to build entities from. The returned
            closure iterates this on every call, so new coordinators added to the
            entry's runtime data after setup are picked up automatically.
        build: Callable that takes a coordinator and yields the entities it should
            contribute. Called per-coordinator on every adder invocation, so any
            per-poll device-set changes surface as new entities. Return an empty
            iterable when a coordinator has nothing to contribute.
        async_add_entities: The platform's ``async_add_entities`` callback, forwarded
            verbatim.

    Returns:
        A ``@callback``-marked function that consumers should call once immediately
        (to add the initial set) and then register as a listener on each
        coordinator via ``entry.async_on_unload(coordinator.async_add_listener(...))``.

    Behaviour on repeated calls:
        - Entities with ``unique_id in known_unique_ids`` are silently skipped
          (per-entry dedup — a coordinator update that re-yields the same entities
          shouldn't produce duplicates).
        - Entities whose ``unique_id`` is already registered against a different
          Sungrow config entry are skipped with a single INFO-level log line, then
          added to ``known_unique_ids`` so the log doesn't repeat per coordinator
          tick (see #346 for the noise class this prevents).
        - Entities with ``unique_id is None`` are skipped (HA rejects them anyway).
    """
    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[EntityT] = []
        for coordinator in coordinators:
            for entity in build(coordinator):
                uid = entity.unique_id
                if uid is None or uid in known_unique_ids:
                    continue
                if unique_id_owned_by_other_entry(hass, platform, uid, entry.entry_id):
                    _LOGGER.info(
                        "Skipping %s %s: already owned by another Sungrow entry",
                        platform,
                        uid,
                    )
                    known_unique_ids.add(uid)
                    continue
                known_unique_ids.add(uid)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    return _add_new_entities
