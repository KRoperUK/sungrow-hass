"""Config flow for Sungrow iSolarCloud integration."""

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GATEWAYS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

# Try to import pysolarcloud, handle if missing gracefully for development
try:
    from pysolarcloud import Auth
except ImportError:
    Auth = None
    # For local development if pysolarcloud is not installed but in path or similar
    # In a real environment, it should be installed via requirements.

_LOGGER = logging.getLogger(__name__)


class SungrowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow iSolarCloud."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self.init_info = {}
        self.auth_client = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SungrowOptionsFlow":
        """Return the options flow handler."""
        return SungrowOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle re-authentication when the stored tokens are no longer valid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        # Reuse the credentials already stored on the entry; only the tokens are stale.
        self.init_info = {k: v for k, v in entry_data.items() if k != "tokens"}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm re-authentication by re-running the authorization step."""
        return await self.async_step_auth(user_input)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        _LOGGER.debug(f"async_step_user called with user_input: {user_input}")
        errors = {}

        if user_input is not None:
            self.init_info = user_input
            return await self.async_step_auth()

        # Attempt to automatically detect the callback URL
        try:
            base_url = get_url(self.hass, allow_internal=False, allow_external=True)
        except Exception:
            base_url = "http://homeassistant.local:8123"  # Fallback

        default_redirect = f"{base_url}/api/sungrow_hass/callback"

        self.context["title_placeholders"] = {
            "app_id": user_input.get(CONF_APP_ID, "YourAppID") if user_input else "YourAppID"
        }

        description_placeholders = {
            "url": "https://developer-api.isolarcloud.com/#/application",
            "app_id_url": "https://developer-api.isolarcloud.com/#/editApplication?id=1234",
        }

        return self.async_show_form(
            step_id="user",
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
        _LOGGER.info("Initialized Auth client for Sungrow iSolarCloud")
        _LOGGER.debug("Auth client details: %s", self.auth_client)
        return True

    def _auth_url_with_flow_id(self) -> str:
        """Build the iSolarCloud authorization URL including this flow's ID.

        The flow_id lets the callback view resume the correct config flow when
        iSolarCloud redirects back to /api/sungrow_hass/callback.
        """
        redirect_uri = self.init_info[CONF_REDIRECT_URI].rstrip("/")
        # Preserve any existing query params on the configured redirect URI.
        separator = "&" if "?" in redirect_uri else "?"
        callback_redirect = f"{redirect_uri}{separator}flow_id={self.flow_id}"
        return self.auth_client.auth_url(callback_redirect)

    async def async_step_auth(self, user_input: dict[str, Any] | None = None):
        """Choose how to complete the OAuth authorization."""
        if not self._ensure_auth_client():
            return self.async_abort(reason="library_missing")

        if user_input is not None:
            # Menu selections arrive as {"next_step_id": "..."}
            next_step = user_input.get("next_step_id")
            method = "manual" if next_step == "auth_manual" else "callback"
            self.context["method"] = method
            if method == "manual":
                return await self.async_step_auth_manual()
            return await self.async_step_auth_callback()

        auth_url = self._auth_url_with_flow_id()
        return self.async_show_menu(
            step_id="auth",
            menu_options=["auth_callback", "auth_manual"],
            description_placeholders={"auth_url": auth_url},
        )

    async def async_step_auth_callback(self, user_input: dict[str, Any] | None = None):
        """Wait for iSolarCloud to redirect back to the callback endpoint.

        The callback view extracts the authorization code and calls
        async_configure, which re-enters this step with user_input={"code": ...}.
        """
        if user_input is not None and user_input.get("code"):
            self.context["code"] = user_input["code"]
            return self.async_show_progress_done(next_step_id="finish")

        # Register this flow so the callback view can resume it.
        flows = self.hass.data.setdefault(DOMAIN, {}).setdefault("flows", {})
        future: asyncio.Future[str] = asyncio.Future()
        flows[self.flow_id] = future

        auth_url = self._auth_url_with_flow_id()

        async def _wait_for_callback() -> None:
            """Resume the flow once the OAuth callback delivers a code."""
            try:
                code = await future
            except asyncio.CancelledError:
                return
            finally:
                flows.pop(self.flow_id, None)
            try:
                await self.hass.config_entries.flow.async_configure(flow_id=self.flow_id, user_input={"code": code})
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Failed to resume config flow from OAuth callback")

        task = self.hass.async_create_background_task(_wait_for_callback(), name="sungrow-oauth-callback")

        return self.async_show_progress(
            step_id="auth_callback",
            progress_action="wait_for_callback",
            progress_task=task,
            description_placeholders={"auth_url": auth_url},
        )

    async def async_step_auth_manual(self, user_input: dict[str, Any] | None = None):
        """Handle manual entry of the authorization code or full redirect URL."""
        self.context["method"] = "manual"
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

                self.context["code"] = code
                return self.async_show_progress_done(next_step_id="finish")

            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in async_step_auth_manual: %s", e)
                errors["base"] = "unknown"

        auth_url = self._auth_url_with_flow_id()
        return self.async_show_form(
            step_id="auth_manual",
            description_placeholders={"auth_url": auth_url},
            data_schema=vol.Schema({vol.Optional("code"): str}),
            errors=errors,
        )

    def _finish_error_result(self, error_key: str):
        """Return a user-facing result for an authorization failure.

        Manual-entry users are sent back to the code form so they can retry;
        automatic-callback users get an abort because there is no form to return to.
        """
        if self.context.get("method") == "manual":
            auth_url = self._auth_url_with_flow_id()
            return self.async_show_form(
                step_id="auth_manual",
                description_placeholders={"auth_url": auth_url},
                data_schema=vol.Schema({vol.Optional("code"): str}),
                errors={"base": error_key},
            )
        return self.async_abort(reason=error_key)

    async def async_step_finish(self, user_input: dict[str, Any] | None = None):
        """Exchange the authorization code for tokens and create the config entry."""

        if not self._ensure_auth_client():
            return self.async_abort(reason="library_missing")

        code = self.context.get("code")
        if not code:
            _LOGGER.error("Finish step reached without an authorization code")
            return self.async_abort(reason="missing_code")

        try:
            redirect_uri_clean = self.init_info[CONF_REDIRECT_URI]
            _LOGGER.info("Authorizing with code: %s and redirect_uri: %s", code, redirect_uri_clean)
            await self.auth_client.async_authorize(code, redirect_uri_clean)

            tokens = self.auth_client.tokens
            _LOGGER.debug("Received tokens: %s", tokens)

            if not tokens or not tokens.get("access_token"):
                _LOGGER.error("Failed to retrieve tokens")
                return self._finish_error_result("invalid_auth")

            data = {**self.init_info, "tokens": tokens}

            if self._reauth_entry is not None:
                return self.async_update_reload_and_abort(self._reauth_entry, data=data)

            await self.async_set_unique_id(str(self.init_info[CONF_APP_ID]))
            self._abort_if_unique_id_configured()
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


class SungrowOptionsFlow(config_entries.OptionsFlow):
    """Handle Sungrow integration options (e.g. polling interval)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage the integration options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Normalise the free-text mapping into a dict before storing.
            try:
                extras = _parse_extra_measure_points(user_input.get(CONF_EXTRA_MEASURE_POINTS))
            except vol.Invalid as exc:
                errors["base"] = "invalid_extra_measure_points"
                _LOGGER.warning("Invalid extra measure points input: %s", exc)
            else:
                data = {**user_input, CONF_EXTRA_MEASURE_POINTS: extras}
                return self.async_create_entry(title="", data=data)

        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        current_extras = self.config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {})
        extras_str = ",".join(f"{pid}={code}" for pid, code in current_extras.items())
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                    vol.Optional(
                        CONF_EXTRA_MEASURE_POINTS,
                        default=extras_str,
                        description={"suggested_value": extras_str},
                    ): str,
                }
            ),
            errors=errors,
        )
