"""Multi-plant selection step for accounts serving more than one plant (#358).

Both cloud transports (OAuth and user-account) fan out to multiple plants when
the authenticated account has more than one — installers, multi-site owners.
Before #358 the entry silently set up every plant; the user could not skip one
they did not want the integration to touch.

This step is inserted by :class:`CloudOAuthMixin` after a successful token
exchange and by :class:`CloudUserMixin` after a successful login, whenever the
API returns more than one plant. For single-plant accounts the step is skipped
so the flow shape is unchanged for the common case.

Selection is stored as :data:`CONF_PLANT_IDS`, a list of ``ps_id`` strings, on
the config entry. Setup filters ``plant_list`` by this list; an absent /
missing key means "serve every plant returned" (the pre-#358 behaviour), so
legacy entries need no migration.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import CONF_PLANT_IDS, CONF_TRANSPORT, TRANSPORT_CLOUD_USER
from ._base import _SungrowFlowBase

_LOGGER = logging.getLogger(__name__)


class PlantSelectionMixin(_SungrowFlowBase):
    """Adds ``async_step_plant_selection`` to the config flow.

    The step reads ``self._pending_plant_list`` (list of ``{"ps_id", "ps_name"}``
    dicts, as returned by the library) and ``self._pending_entry_data`` (the
    entry ``data`` dict the caller was about to hand to
    ``async_create_entry``). It presents a multi-select picker of the plants
    and, on submit, merges the chosen ``CONF_PLANT_IDS`` into the entry data
    and dispatches back to the caller's transport-specific finaliser.

    Dispatch is keyed off ``entry_data[CONF_TRANSPORT]`` rather than a shared
    ``_finalise_plant_selection`` override — the mixin classes are combined
    via multiple inheritance and Python's MRO would otherwise pick just one
    class's override, sending every transport through the same code path.
    """

    _pending_plant_list: list[dict[str, Any]] | None = None
    _pending_entry_data: dict[str, Any] | None = None

    async def async_step_plant_selection(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Present a multi-select picker of the discovered plants (#358).

        Every plant is checked by default, so a user who just wants "all of them"
        can press Submit without changing anything — the flow is a strict superset
        of the pre-#358 behaviour for that case. Unchecking a plant excludes it
        from :data:`CONF_PLANT_IDS`; setup skips excluded plants entirely.
        """
        plant_list = self._pending_plant_list or []
        entry_data = self._pending_entry_data or {}

        if not plant_list:
            # No plants somehow — nothing to pick. Let the caller finalise with an
            # empty selection; setup will surface a clearer error than a dead step.
            return await self._dispatch_plant_selection_finalise(entry_data)

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = list(user_input.get(CONF_PLANT_IDS) or [])
            if not selected:
                errors[CONF_PLANT_IDS] = "no_plants_selected"
            else:
                merged = {**entry_data, CONF_PLANT_IDS: selected}
                return await self._dispatch_plant_selection_finalise(merged)

        all_ids = [str(p["ps_id"]) for p in plant_list]
        options = [
            SelectOptionDict(value=str(p["ps_id"]), label=str(p.get("ps_name") or f"Plant {p['ps_id']}"))
            for p in plant_list
        ]
        return self.async_show_form(
            step_id="plant_selection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PLANT_IDS, default=all_ids): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def _dispatch_plant_selection_finalise(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Route to the transport-specific finaliser based on the entry data.

        Each cloud mixin exposes its own uniquely-named finaliser
        (:meth:`CloudOAuthMixin._finalise_cloud_oauth_entry`,
        :meth:`CloudUserMixin._finalise_cloud_user_entry`) so MRO can't collapse
        them into a single call — this dispatcher picks the right one by
        transport.
        """
        transport = entry_data.get(CONF_TRANSPORT)
        if transport == TRANSPORT_CLOUD_USER:
            return await self._finalise_cloud_user_entry(entry_data)  # type: ignore[attr-defined,no-any-return]
        # Default: OAuth path (TRANSPORT_CLOUD_ONLY or missing).
        return await self._finalise_cloud_oauth_entry(entry_data)  # type: ignore[attr-defined,no-any-return]
