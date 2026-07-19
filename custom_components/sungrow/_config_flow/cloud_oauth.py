"""OAuth cloud transport steps + helpers (#354).

Covers the developer-portal OAuth handshake:

- ``async_step_auth`` — start authorization (armed callback wait)
- ``async_step_auth_callback`` — progress screen while the redirect is awaited
- ``async_step_auth_manual`` — fallback code-entry form when the redirect misses
- ``async_step_finish`` — token exchange and entry creation / reauth completion

Plus the OAuth-specific helper methods used only by these steps
(``_auth_url``, ``_arm_callback_wait`` etc.).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlowResult

from ..const import CONF_APP_ID, CONF_APP_KEY, CONF_APP_SECRET, CONF_GATEWAY, CONF_REDIRECT_URI, DOMAIN, GATEWAYS
from . import _base
from ._base import _SungrowFlowBase
from ._helpers import CALLBACK_WAIT_TIMEOUT
from .plant_selection import PlantSelectionMixin

_LOGGER = logging.getLogger(__name__)


class CloudOAuthMixin(PlantSelectionMixin, _SungrowFlowBase):
    """OAuth cloud transport steps for :class:`SungrowConfigFlow`."""

    # ---- OAuth-specific helpers -----------------------------------------

    def _ensure_auth_client(self) -> bool:
        """Initialize the pysolarcloud Auth client if needed.

        Returns True on success, False if the library is missing.
        """
        if self.auth_client:
            return True

        session = _base.async_get_clientsession(self.hass)
        gateway_url = GATEWAYS[self.init_info[CONF_GATEWAY]]

        if _base.Auth is None:
            return False

        self.auth_client = _base.Auth(
            host=gateway_url,
            appkey=self.init_info[CONF_APP_KEY],
            access_key=self.init_info[CONF_APP_SECRET],
            app_id=self.init_info[CONF_APP_ID],
            websession=session,
        )
        # Debug, not info (routine setup), and never log the client repr — it
        # holds the app key/secret.
        _LOGGER.debug("Initialized Auth client for Sungrow iSolarCloud")
        return True

    def _redirect_uri(self) -> str:
        """Canonical redirect URI used for BOTH the auth request and token exchange.

        iSolarCloud validates that the ``redirect_uri`` sent to the token endpoint
        matches the one from the authorization request, so they must be byte-for-byte
        identical — hence a single source here (trailing slash normalised).

        We deliberately do NOT append a ``flow_id``: iSolarCloud strips extra query
        params from the redirect, so it never round-tripped anyway (the callback view
        correlates the pending flow via the OAuth ``state`` param, falling back to the
        sole pending flow only when no correlator survives). Appending anything here
        also broke the token exchange, which uses the bare URI, with "invalid
        authentication".
        """
        # init_info is a dict[str, Any], so the redirect URI is typed as Any.
        return cast(str, self.init_info[CONF_REDIRECT_URI]).rstrip("/")

    def _ensure_state(self) -> str:
        """Return this flow's OAuth ``state`` token, (re)registering it for correlation.

        Generated once per flow and stored in ``hass.data[DOMAIN]["states"]`` keyed to
        the flow_id, so the callback view can map an incoming ``state`` back to the
        exact flow instead of guessing the single pending one (#116). Re-registered on
        every render so a re-armed flow always has a live mapping.
        """
        if self._state is None:
            self._state = secrets.token_urlsafe(16)
        states = self.hass.data.setdefault(DOMAIN, {}).setdefault("states", {})
        states[self._state] = self.flow_id
        return self._state

    def _auth_url(self) -> str:
        """Build the iSolarCloud authorization URL for the canonical redirect URI.

        Always called at render time (never cached), so every screen that shows the
        link — the progress wait, the manual-entry form, and the error-retry form —
        displays a freshly generated, current URL. The URL carries an OAuth ``state``
        token (not a ``flow_id`` on the redirect, which iSolarCloud strips) so a
        redirect that preserves ``state`` correlates unambiguously to this flow even
        when several setups are in flight.
        """
        # auth_client is the untyped pysolarcloud Auth, so auth_url returns Any.
        base = cast(str, self.auth_client.auth_url(self._redirect_uri()))
        state = self._ensure_state()
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}state={state}"

    def _arm_callback_wait(self, *, fall_back_to_manual: bool) -> asyncio.Task[None]:
        """Register a callback future and spawn a task that resumes the flow.

        Used by both the automatic progress step and the manual-entry form, so a
        redirect that arrives late (after the auto-wait timed out and the user
        reached the manual form) still completes the flow automatically. Reuses the
        registered future when one is still pending so the callback view always has
        a live target.
        """
        flows = self.hass.data.setdefault(DOMAIN, {}).setdefault("flows", {})
        future = flows.get(self.flow_id)
        if not isinstance(future, asyncio.Future) or future.done():
            future = asyncio.Future()
            flows[self.flow_id] = future

        async def _wait_for_callback() -> None:
            try:
                code = await asyncio.wait_for(future, timeout=CALLBACK_WAIT_TIMEOUT)
            except TimeoutError:
                if fall_back_to_manual:
                    _LOGGER.warning("OAuth callback not received within timeout for flow %s", self.flow_id)
                    self._callback_timeout = True
                    await self._resume_flow(user_input={})
                return
            except asyncio.CancelledError:
                return
            else:
                await self._resume_flow(user_input={"code": code})
            finally:
                # Drop the future once it has been consumed / expired so a stale one
                # isn't reused; leave a newer future (e.g. re-armed for manual) alone.
                if flows.get(self.flow_id) is future:
                    flows.pop(self.flow_id, None)

        return self.hass.async_create_background_task(_wait_for_callback(), name="sungrow-oauth-callback")

    async def _resume_flow(self, *, user_input: dict[str, Any]) -> None:
        """Re-enter the config flow from the OAuth callback background task."""
        try:
            await self.hass.config_entries.flow.async_configure(flow_id=self.flow_id, user_input=user_input)
        except Exception:  # pylint: disable=broad-except
            # The flow may already have progressed (e.g. the user finished manually);
            # a late resume is harmless.
            _LOGGER.debug("Could not resume config flow %s from OAuth callback", self.flow_id)

    def _drop_callback_future(self) -> None:
        """Remove this flow's pending callback future (it has been consumed)."""
        flows = self.hass.data.get(DOMAIN, {}).get("flows", {})
        if isinstance(flows, dict):
            flows.pop(self.flow_id, None)

    def _finish_error_result(self, error_key: str) -> ConfigFlowResult:
        """Return the manual code-entry form with an error so the user can retry.

        Both the automatic and manual paths land here on failure; showing the
        manual form lets the user paste a fresh code or full redirect URL.
        """
        auth_url = self._auth_url()
        return self.async_show_form(
            step_id="auth_manual",
            description_placeholders={"auth_url": auth_url},
            data_schema=vol.Schema({vol.Optional("code"): str}),
            errors={"base": error_key},
        )

    # ---- OAuth step methods ---------------------------------------------

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Begin authorization by automatically waiting for the OAuth redirect.

        No menu is shown: the callback listener starts immediately so the user
        only has to approve the app in their browser. If the redirect never
        arrives (e.g. the callback endpoint is unreachable), the flow falls back
        to manual entry so the user can paste the code or full redirect URL.
        """
        if not self._ensure_auth_client():
            return self.async_abort(reason="library_missing")
        # Register the callback view now: on a first-time install async_setup has
        # not run yet, so without this the OAuth redirect would 404.
        from .. import _ensure_callback_view_registered

        _ensure_callback_view_registered(self.hass)
        return await self.async_step_auth_callback()

    async def async_step_auth_callback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Wait for iSolarCloud to redirect back to the callback endpoint.

        The callback view extracts the authorization code and calls
        async_configure, which re-enters this step with user_input={"code": ...}.
        If the callback does not arrive within the timeout, the flow falls back
        to the manual code-entry form instead of aborting.
        """
        if self._callback_timeout:
            # The redirect never arrived — hand off to manual code entry so the
            # user can paste the authorization code or full redirect URL.
            return self.async_show_progress_done(next_step_id="auth_manual")
        if user_input is not None and user_input.get("code"):
            self._code = user_input["code"]
            self._drop_callback_future()
            return self.async_show_progress_done(next_step_id="finish")

        auth_url = self._auth_url()
        # Fall back to the manual form if the redirect doesn't arrive in time.
        task = self._arm_callback_wait(fall_back_to_manual=True)
        return self.async_show_progress(
            step_id="auth_callback",
            progress_action="wait_for_callback",
            progress_task=task,
            description_placeholders={"auth_url": auth_url},
        )

    async def async_step_auth_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual entry of the authorization code or full redirect URL.

        Reached as a fallback when the automatic redirect does not complete, or
        when the user wants to paste the code themselves.
        """
        errors = {}

        if user_input is not None and user_input.get("code"):
            try:
                code_input = user_input["code"].strip()
                if code_input.startswith("http"):
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(code_input)
                    query = parse_qs(parsed.query)
                    if "code" in query:
                        code = query["code"][0]
                    else:
                        query = parse_qs(parsed.fragment.split("?")[-1] if "?" in parsed.fragment else "")
                        if "code" in query:
                            code = query["code"][0]
                        else:
                            errors["base"] = "invalid_auth"
                            raise ValueError("Could not find code in URL")
                else:
                    code = code_input

                self._code = code
                self._drop_callback_future()
                return self.async_show_progress_done(next_step_id="finish")

            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in async_step_auth_manual: %s", e)
                errors["base"] = "unknown"

        # Keep a callback waiter armed while the manual form is shown, so a redirect
        # that lands late (after the auto-wait timed out) still completes the flow
        # automatically instead of stranding a "successful" callback with no effect.
        # Re-arm only when no live waiter exists: a consumed/expired future is
        # replaced (so a subsequent redirect isn't stranded — the old one-shot flag
        # never re-armed), while a still-pending one is left alone to avoid spawning
        # duplicate waiter tasks on every form re-render.
        flows = self.hass.data.get(DOMAIN, {}).get("flows", {})
        existing = flows.get(self.flow_id)
        if not isinstance(existing, asyncio.Future) or existing.done():
            self._arm_callback_wait(fall_back_to_manual=False)

        auth_url = self._auth_url()
        return self.async_show_form(
            step_id="auth_manual",
            description_placeholders={"auth_url": auth_url},
            data_schema=vol.Schema({vol.Optional("code"): str}),
            errors=errors,
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Exchange the authorization code for tokens and create the config entry."""

        if not self._ensure_auth_client():
            return self.async_abort(reason="library_missing")

        code = self._code
        if not code:
            _LOGGER.error("Finish step reached without an authorization code")
            return self.async_abort(reason="missing_code")

        try:
            redirect_uri_clean = self._redirect_uri()
            # Never log the authorization code or tokens — they are credentials.
            _LOGGER.debug("Authorizing with redirect_uri: %s", redirect_uri_clean)
            await self.auth_client.async_authorize(code, redirect_uri_clean)

            tokens = self.auth_client.tokens
            _LOGGER.debug(
                "Authorization succeeded (access token received: %s)", bool(tokens and tokens.get("access_token"))
            )

            if not tokens or not tokens.get("access_token"):
                _LOGGER.error("Failed to retrieve tokens")
                return self._finish_error_result("invalid_auth")

            data = {**self.init_info, "tokens": tokens}

            if self._reauth_entry is not None:
                # Guard against re-authorizing/reconfiguring onto a different account:
                # the App ID is the entry's identity, so it must not change.
                await self.async_set_unique_id(str(self.init_info[CONF_APP_ID]))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                reason = "reconfigure_successful" if self._is_reconfigure else "reauth_successful"
                # Reauth/reconfigure preserves the entry's existing plant selection
                # (#358); the picker is only shown on initial setup below.
                return self.async_update_reload_and_abort(self._reauth_entry, data=data, reason=reason)

            await self.async_set_unique_id(str(self.init_info[CONF_APP_ID]))
            self._abort_if_unique_id_configured()

            # Initial setup: fetch the account's plants using the freshly-obtained
            # tokens. Multi-plant accounts route through ``async_step_plant_selection``
            # (#358); single-plant accounts finalise directly with no extra step so
            # the flow shape is unchanged for the common case.
            plant_list = await self._fetch_plants()
            if len(plant_list) > 1:
                self._pending_plant_list = plant_list
                self._pending_entry_data = data
                return await self.async_step_plant_selection()
            return await self._finalise_cloud_oauth_entry(data)

        except data_entry_flow.AbortFlow:
            raise
        except ClientError as e:
            _LOGGER.warning("Client connection error in async_step_finish: %s", e)
            return self._finish_error_result("cannot_connect")
        except _base.PySolarCloudException as e:
            # ``invalid_grant`` means iSolarCloud rejected the authorization code —
            # typically because it's already been used (double-click, browser retry)
            # or has expired. Guide the user back to the manual form with a clear
            # message instead of surfacing a generic "unknown" error. The exposed
            # ``.error`` attribute carries the machine-readable code from the API
            # response envelope (``str(e)`` only returns the description text).
            if getattr(e, "error", None) == "invalid_grant":
                _LOGGER.warning(
                    "iSolarCloud rejected the authorization code as invalid_grant; "
                    "showing the manual code-entry step so the user can retry."
                )
                # Clear the used code so the next submission gets a fresh one.
                self._code = None
                return self._finish_error_result("invalid_auth_code")
            _LOGGER.warning("iSolarCloud error in async_step_finish: %s", e)
            return self._finish_error_result("invalid_auth")
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception in async_step_finish: %s", e)
            return self._finish_error_result("unknown")

    async def _fetch_plants(self) -> list[dict[str, Any]]:
        """Fetch the account's plants using the freshly-authorized ``auth_client`` (#358).

        Best-effort: any failure returns ``[]`` and the caller falls back to
        finalising the entry without a plant picker. Setup then re-fetches the
        list itself and serves whatever it discovers (legacy shape). The catch
        is deliberately broad — the picker is a UX nicety, not a correctness
        gate, so a probe error (typed or otherwise) must never block the entry
        from being created.
        """
        from pysolarcloud.plants import Plants

        try:
            plants_service = Plants(self.auth_client)
            async with asyncio.timeout(30):
                return list(await plants_service.async_get_plants() or [])
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Plant list fetch failed after OAuth authorize; skipping picker: %s", err)
            return []

    async def _finalise_cloud_oauth_entry(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the OAuth entry with the plant selection merged in.

        Called by :class:`PlantSelectionMixin._dispatch_plant_selection_finalise`
        after the user submits the picker, and by :meth:`async_step_finish`
        directly for single-plant accounts that skip the picker entirely.
        """
        return self.async_create_entry(title=f"Sungrow {self.init_info[CONF_APP_ID]}", data=entry_data)
