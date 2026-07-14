"""Config flow for Sungrow iSolarCloud integration."""

import asyncio
import logging
import secrets
from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_GATEWAY,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_REDIRECT_URI,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GATEWAYS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

# Try to import pysolarcloud, handle if missing gracefully for development
try:
    from pysolarcloud import Auth
except ImportError:
    # Optional import for local dev; production always has it via requirements.
    Auth = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

# How long to wait for the OAuth redirect before offering (or, on the manual step,
# giving up on) automatic completion. Generous, since iSolarCloud's approval page
# can be slow — a redirect that lands within this window completes the flow
# automatically even if the user has already reached the manual-entry form.
CALLBACK_WAIT_TIMEOUT = 300


def _parse_winet_properties(props: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``(inverter_serial, model)`` from a WiNet-S mDNS TXT-record dict.

    The dongle advertises ``inverter=1;<type_code>;<serial>;1;<x>;<model>;...``. TXT
    values may arrive as ``bytes``; a missing/short field yields ``None``.
    """
    raw: Any = props.get("inverter")
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if not raw:
        return None, None
    parts = str(raw).split(";")
    serial = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    model = parts[5].strip() if len(parts) > 5 and parts[5].strip() else None
    return serial, model


class SungrowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow iSolarCloud."""

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.init_info: dict[str, Any] = {}
        self.auth_client: Any = None
        self._reauth_entry: ConfigEntry | None = None
        self._is_reconfigure = False
        # OAuth flow state (kept on the instance, which persists across steps and the
        # callback background task, rather than in the typed ConfigFlowContext).
        self._code: str | None = None
        self._callback_timeout = False
        # OAuth ``state`` token that correlates the callback redirect back to this
        # exact flow (registered in hass.data so the callback view can look it up).
        self._state: str | None = None
        # Zeroconf-discovered WiNet-S local Modbus host, carried into the confirm step
        # that creates the cloud-free Modbus-only entry (#159).
        self._discovered_modbus_host: str | None = None
        # Transport mode selected in the transport step (#216).
        self._transport: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SungrowOptionsFlow":
        """Return the options flow handler."""
        return SungrowOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when the stored tokens are no longer valid."""
        self._reauth_entry = self._get_reauth_entry()
        # Reuse the credentials already stored on the entry; only the tokens are stale.
        self.init_info = {k: v for k, v in entry_data.items() if k != "tokens"}
        # If the entry is missing the App ID (legacy/corrupted), we cannot proceed
        # with auth — the user must reconfigure to supply the missing credential (#245).
        if not self.init_info.get(CONF_APP_ID):
            return self.async_abort(reason="missing_app_id")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm re-authentication by re-running the authorization step."""
        return await self.async_step_auth(user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the gateway / credentials of an existing entry, then re-authorize.

        The App ID is the entry's identity (unique_id) so it stays fixed; everything
        else — region, keys, redirect URI — is editable. Because new credentials or a
        new region invalidate the stored tokens, submitting always re-runs the OAuth
        authorization and updates the entry in place (no delete & re-add).

        For entries migrated from older versions that are missing the App ID, the
        reconfigure form requests it so the user can supply the value without having
        to delete and recreate the entry (#245).
        """
        entry = self._get_reconfigure_entry()
        transport = entry.data.get(CONF_TRANSPORT)
        # A cloud-free Modbus-only entry has no credentials to change; reconfigure just
        # updates the WiNet-S host (#159), not the cloud app key/secret/gateway.
        if transport == TRANSPORT_MODBUS_ONLY:
            return await self.async_step_reconfigure_modbus(user_input)
        self._reauth_entry = entry
        self._is_reconfigure = True
        self._transport = transport

        if user_input is not None:
            # Preserve the App ID (identity) and drop stale tokens — we re-authorize.
            self.init_info = {**entry.data, **user_input}
            self.init_info.pop("tokens", None)
            # Start a fresh Auth client for the (possibly changed) credentials.
            self.auth_client = None
            # For cloud_modbus: collect an updated modbus host before re-authorizing.
            if transport == TRANSPORT_CLOUD_MODBUS:
                return await self.async_step_reconfigure_modbus_host()
            return await self.async_step_auth()

        current = entry.data
        # Build the schema dynamically: include App ID when the entry is missing it
        # (legacy entries upgraded from older versions may lack this field).
        schema_fields: dict[Any, Any] = {}
        if not current.get(CONF_APP_ID):
            schema_fields[vol.Required(CONF_APP_ID, default="")] = str
        schema_fields[vol.Required(CONF_APP_KEY, default=current.get(CONF_APP_KEY, ""))] = str
        schema_fields[vol.Required(CONF_APP_SECRET, default=current.get(CONF_APP_SECRET, ""))] = str
        schema_fields[vol.Required(CONF_GATEWAY, default=current.get(CONF_GATEWAY, "Europe"))] = vol.In(
            list(GATEWAYS.keys())
        )
        schema_fields[vol.Required(CONF_REDIRECT_URI, default=current.get(CONF_REDIRECT_URI, ""))] = str

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(schema_fields),
        )

    async def async_step_reconfigure_modbus_host(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect updated Modbus host during reconfigure of a cloud_modbus entry (#216)."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            from .helpers import async_test_modbus_host

            if await async_test_modbus_host(host):
                # Store host in init_info so the auth step includes it in the updated entry.
                self.init_info[CONF_MODBUS_HOST] = host
                return await self.async_step_auth()
            errors["base"] = "host_unreachable"

        current_host = entry.data.get(CONF_MODBUS_HOST, "")
        return self.async_show_form(
            step_id="reconfigure_modbus_host",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=current_host): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_modbus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure a cloud-free Modbus-only entry: update the WiNet-S host (#159).

        No credentials are involved — the only thing worth changing is the local IP, in
        case the WiNet-S moved to a new DHCP lease and discovery did not re-announce.
        """
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # Blank means "leave unchanged" so reconfigure can never accidentally clear
            # the host (which would take the entry offline).
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip() or entry.data.get(CONF_MODBUS_HOST)
            return self.async_update_reload_and_abort(
                entry,
                data={**entry.data, CONF_MODBUS_HOST: host},
                reason="reconfigure_successful",
            )
        return self.async_show_form(
            step_id="reconfigure_modbus",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=entry.data.get(CONF_MODBUS_HOST, "")): str,
                }
            ),
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Discover a WiNet-S dongle via mDNS and offer a cloud-free local Modbus setup (#159).

        The dongle advertises ``WiNet-WebServer`` (``_http._tcp``) with TXT records that
        carry the inverter's serial and model, so we can identify it and pick the register
        map without connecting or needing any cloud credentials.
        """
        host = str(discovery_info.ip_address)
        serial, model = _parse_winet_properties(discovery_info.properties)
        if not serial:
            return self.async_abort(reason="not_sungrow_device")
        await self.async_set_unique_id(f"modbus_{serial}")
        # Already set up? Update the host in case the WiNet-S's IP changed, then stop.
        self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
        self._discovered_modbus_host = host
        self.init_info = {CONF_SERIAL: serial, CONF_MODEL: model or "Inverter"}
        self.context["title_placeholders"] = {"name": f"Sungrow {model or 'inverter'}"}
        # Always a standalone local entry — never mash Modbus into the cloud entry.
        # If a cloud plant already owns this serial, setup nests the local inverter under
        # that plant via device registry (soft link only).
        return await self.async_step_zeroconf_confirm()

    async def async_step_import(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Import a Modbus-only entry (legacy hybrid split / programmatic setup)."""
        serial = str(user_input.get(CONF_SERIAL) or "").strip()
        host = str(user_input.get(CONF_MODBUS_HOST) or "").strip()
        if not serial or not host:
            return self.async_abort(reason="not_sungrow_device")
        model = str(user_input.get(CONF_MODEL) or "Inverter")
        await self.async_set_unique_id(f"modbus_{serial}")
        self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
        options: dict[str, Any] = {
            CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)),
        }
        if user_input.get(CONF_MODBUS_DEBUG_DAILY_YIELD):
            options[CONF_MODBUS_DEBUG_DAILY_YIELD] = True
        return self.async_create_entry(
            title=f"Sungrow {model} (local)",
            data={
                CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                CONF_SERIAL: serial,
                CONF_MODEL: model,
                CONF_MODBUS_HOST: host,
            },
            options=options,
        )

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setting up the discovered WiNet-S as a local (Modbus-only) integration."""
        model = self.init_info.get(CONF_MODEL, "Inverter")
        if user_input is not None:
            return self.async_create_entry(
                title=f"Sungrow {model} (local)",
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                    CONF_SERIAL: self.init_info[CONF_SERIAL],
                    CONF_MODEL: model,
                    CONF_MODBUS_HOST: self._discovered_modbus_host,
                },
                options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"model": model, "host": self._discovered_modbus_host or ""},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Present the transport-mode selector as the first step (#216)."""
        if user_input is not None:
            transport = user_input[CONF_TRANSPORT]
            self._transport = transport
            if transport == TRANSPORT_MODBUS_ONLY:
                return await self.async_step_local_setup()
            # cloud_only or cloud_modbus → cloud credentials
            return await self.async_step_cloud_credentials()

        from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

        transport_options = [
            SelectOptionDict(value=TRANSPORT_CLOUD_ONLY, label="Cloud Only"),
            SelectOptionDict(value=TRANSPORT_CLOUD_MODBUS, label="Cloud + Modbus"),
            SelectOptionDict(value=TRANSPORT_MODBUS_ONLY, label="Modbus Only"),
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSPORT, default=TRANSPORT_CLOUD_ONLY): SelectSelector(
                        SelectSelectorConfig(options=transport_options, translation_key="transport")
                    ),
                }
            ),
        )

    async def async_step_cloud_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect iSolarCloud API credentials (formerly async_step_user)."""
        _LOGGER.debug("async_step_cloud_credentials called (user_input provided: %s)", user_input is not None)
        errors: dict[str, str] = {}

        if user_input is not None:
            self.init_info = user_input
            await self.async_set_unique_id(str(user_input[CONF_APP_ID]))
            self._abort_if_unique_id_configured()

            # If hybrid mode, collect the Modbus host next.
            if self._transport == TRANSPORT_CLOUD_MODBUS:
                return await self.async_step_modbus_host()

            # Create the hub immediately, before authorizing. Setting up this
            # token-less entry runs async_setup — which registers the OAuth
            # callback view — and then raises ConfigEntryAuthFailed, so Home
            # Assistant starts a reauth flow to finish authorization.
            data = {**user_input, CONF_TRANSPORT: self._transport or TRANSPORT_CLOUD_ONLY}
            return self.async_create_entry(
                title=f"Sungrow {user_input[CONF_APP_ID]}",
                data=data,
            )

        # Attempt to automatically detect the callback URL
        try:
            base_url = get_url(self.hass, allow_internal=False, allow_external=True)
        except Exception:
            base_url = "http://homeassistant.local:8123"  # Fallback

        default_redirect = f"{base_url}/api/sungrow_hass/callback"

        self.context["title_placeholders"] = {"app_id": "YourAppID"}

        description_placeholders = {
            "url": "https://developer-api.isolarcloud.com/#/application",
            "app_id_url": "https://developer-api.isolarcloud.com/#/editApplication?id=1234",
        }

        return self.async_show_form(
            step_id="cloud_credentials",
            description_placeholders=description_placeholders,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_KEY): str,
                    vol.Required(CONF_APP_SECRET): str,
                    vol.Required(CONF_APP_ID, default=""): str,
                    vol.Required(CONF_GATEWAY, default="Europe"): vol.In(list(GATEWAYS.keys())),
                    vol.Required(CONF_REDIRECT_URI, default=default_redirect): str,
                }
            ),
            errors=errors,
        )

    async def async_step_modbus_host(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the WiNet-S Modbus host for hybrid (Cloud + Modbus) mode (#216)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            from .helpers import async_test_modbus_host

            if await async_test_modbus_host(host):
                # Create the tokenless entry with the modbus host included.
                data = {
                    **self.init_info,
                    CONF_TRANSPORT: TRANSPORT_CLOUD_MODBUS,
                    CONF_MODBUS_HOST: host,
                }
                return self.async_create_entry(
                    title=f"Sungrow {self.init_info[CONF_APP_ID]}",
                    data=data,
                )
            errors["base"] = "host_unreachable"

        # Pre-fill from zeroconf discovery if available.
        default_host = self._discovered_modbus_host or ""
        return self.async_show_form(
            step_id="modbus_host",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=default_host): str,
                }
            ),
            errors=errors,
        )

    async def async_step_local_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect host, serial, and model for a fully local Modbus Only entry (#216)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            serial = (user_input.get(CONF_SERIAL) or "").strip()
            model = (user_input.get(CONF_MODEL) or "Inverter").strip()

            from .helpers import async_test_modbus_host

            if await async_test_modbus_host(host):
                await self.async_set_unique_id(f"modbus_{serial}")
                self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
                return self.async_create_entry(
                    title=f"Sungrow {model} (local)",
                    data={
                        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                        CONF_SERIAL: serial,
                        CONF_MODEL: model,
                        CONF_MODBUS_HOST: host,
                    },
                    options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
                )
            errors["base"] = "host_unreachable"

        return self.async_show_form(
            step_id="local_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST): str,
                    vol.Required(CONF_SERIAL): str,
                    vol.Required(CONF_MODEL, default="Inverter"): str,
                }
            ),
            errors=errors,
        )

    def _ensure_auth_client(self) -> bool:
        """Initialize the pysolarcloud Auth client if needed.

        Returns True on success, False if the library is missing.
        """
        if self.auth_client:
            return True

        session = async_get_clientsession(self.hass)
        gateway_url = GATEWAYS[self.init_info[CONF_GATEWAY]]

        if Auth is None:
            return False

        self.auth_client = Auth(
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
        from . import _ensure_callback_view_registered

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

    @callback
    def async_remove(self) -> None:
        """Prune this flow's pending OAuth future and state when the flow is removed.

        HA calls this on flow completion or abort. Dropping the future (cancelling it
        if still pending) and its ``state`` mapping stops stale correlators from
        lingering for up to ``CALLBACK_WAIT_TIMEOUT`` — which is what let a later,
        legitimate redirect land on an abandoned flow or trip ``len(flows) != 1`` and
        400 a valid setup (#116).
        """
        domain_data = self.hass.data.get(DOMAIN, {})
        flows = domain_data.get("flows")
        if isinstance(flows, dict):
            future = flows.pop(self.flow_id, None)
            if isinstance(future, asyncio.Future) and not future.done():
                future.cancel()
        states = domain_data.get("states")
        if isinstance(states, dict) and self._state is not None:
            states.pop(self._state, None)

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
                return self.async_update_reload_and_abort(self._reauth_entry, data=data, reason=reason)

            await self.async_set_unique_id(str(self.init_info[CONF_APP_ID]))
            self._abort_if_unique_id_configured()
            # Cloud and local stay separate: if a Modbus-only entry already exists it is
            # left alone (soft-linked by serial at device setup, never merged).
            return self.async_create_entry(title=f"Sungrow {self.init_info[CONF_APP_ID]}", data=data)

        except data_entry_flow.AbortFlow:
            raise
        except ClientError as e:
            _LOGGER.warning("Client connection error in async_step_finish: %s", e)
            return self._finish_error_result("cannot_connect")
        except Exception as e:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception in async_step_finish: %s", e)
            return self._finish_error_result("unknown")


def _parse_extra_measure_points(raw: str | None) -> dict[str, str]:
    """Parse a comma-separated 'point_id=code' list into a mapping.

    Whitespace around entries is ignored; duplicate point_ids keep the last value.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise vol.Invalid(f"Extra measure point '{entry}' must be in the form point_id=code")
        point_id, code = entry.split("=", 1)
        point_id = point_id.strip()
        code = code.strip()
        if not point_id or not code:
            raise vol.Invalid("point_id and code must not be empty")
        if not point_id.isdigit():
            raise vol.Invalid(f"point_id must be numeric, got '{point_id}'")
        out[point_id] = code
    return out


class SungrowOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle Sungrow integration options (e.g. polling interval).

    Subclasses ``OptionsFlowWithReload`` so an options change reloads the entry
    automatically. This replaces the old manual ``add_update_listener`` — which
    also fired on every token rotation (a plain ``entry.data`` write) and reloaded
    the whole integration on each refresh (#110). Because ``OptionsFlowWithReload``
    forbids config-entry update listeners, none are registered in ``__init__``.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the integration options."""
        transport = self.config_entry.data.get(CONF_TRANSPORT)

        # A cloud-free Modbus-only entry has none of the cloud settings (API quota,
        # extra measure points, per-device fetch); show only the local poll interval (#159).
        if transport == TRANSPORT_MODBUS_ONLY:
            return await self.async_step_modbus_options(user_input)

        errors: dict[str, str] = {}
        if user_input is not None:
            # Normalise the free-text mapping into a dict before storing.
            try:
                extras = _parse_extra_measure_points(user_input.get(CONF_EXTRA_MEASURE_POINTS))
            except vol.Invalid as exc:
                errors["base"] = "invalid_extra_measure_points"
                _LOGGER.warning("Invalid extra measure points input: %s", exc)
            else:
                modbus_host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
                data = {**user_input, CONF_EXTRA_MEASURE_POINTS: extras}
                data.pop(CONF_MODBUS_HOST, None)
                data.pop(CONF_MODBUS_DEBUG_DAILY_YIELD, None)

                # Transport switching: cloud_only + host → cloud_modbus
                if transport == TRANSPORT_CLOUD_ONLY and modbus_host:
                    from .helpers import async_test_modbus_host

                    if await async_test_modbus_host(modbus_host):
                        new_data = {
                            **self.config_entry.data,
                            CONF_TRANSPORT: TRANSPORT_CLOUD_MODBUS,
                            CONF_MODBUS_HOST: modbus_host,
                        }
                        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                    else:
                        errors["base"] = "host_unreachable"

                # Transport switching: cloud_modbus + cleared host → cloud_only
                elif transport == TRANSPORT_CLOUD_MODBUS and not modbus_host:
                    new_data = {k: v for k, v in self.config_entry.data.items() if k != CONF_MODBUS_HOST}
                    new_data[CONF_TRANSPORT] = TRANSPORT_CLOUD_ONLY
                    self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

                if not errors:
                    return self.async_create_entry(title="", data=data)

        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        current_extras = self.config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {})
        current_device_sensors = self.config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False)
        extras_str = ",".join(f"{pid}={code}" for pid, code in current_extras.items())

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
            vol.Optional(
                CONF_EXTRA_MEASURE_POINTS,
                default=extras_str,
                description={"suggested_value": extras_str},
            ): str,
            vol.Optional(CONF_ENABLE_DEVICE_SENSORS, default=current_device_sensors): bool,
        }

        # For cloud_only: show optional modbus_host field to allow switching to hybrid.
        # For cloud_modbus: show current host (user can clear to switch back to cloud_only).
        if transport == TRANSPORT_CLOUD_ONLY:
            schema_fields[vol.Optional(CONF_MODBUS_HOST, default="")] = str
        elif transport == TRANSPORT_CLOUD_MODBUS:
            current_host = self.config_entry.data.get(CONF_MODBUS_HOST, "")
            schema_fields[vol.Optional(CONF_MODBUS_HOST, default=current_host)] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def async_step_modbus_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Options for a cloud-free Modbus-only entry: just the local poll interval (#159).

        The cloud settings (API quota, extra measure points, per-device fetch, the
        optional-Modbus-host toggle) are all meaningless here, so none are shown. The
        WiNet-S host is managed by discovery, not the options flow.
        """
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    CONF_MODBUS_DEBUG_DAILY_YIELD: bool(user_input.get(CONF_MODBUS_DEBUG_DAILY_YIELD, False)),
                },
            )
        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)
        current_debug_daily = bool(self.config_entry.options.get(CONF_MODBUS_DEBUG_DAILY_YIELD, False))
        return self.async_show_form(
            step_id="modbus_options",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                    vol.Optional(CONF_MODBUS_DEBUG_DAILY_YIELD, default=current_debug_daily): bool,
                }
            ),
        )
