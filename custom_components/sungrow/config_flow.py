"""Config flow for Sungrow iSolarCloud integration."""

import asyncio
import logging
from typing import Any, cast

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_ENABLE_DEVICE_SENSORS,
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

# How long to wait for the OAuth redirect before offering (or, on the manual step,
# giving up on) automatic completion. Generous, since iSolarCloud's approval page
# can be slow — a redirect that lands within this window completes the flow
# automatically even if the user has already reached the manual-entry form.
CALLBACK_WAIT_TIMEOUT = 300


class SungrowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow iSolarCloud."""

    VERSION = 2

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
        self._manual_waiter_armed = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SungrowOptionsFlow":
        """Return the options flow handler."""
        return SungrowOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when the stored tokens are no longer valid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        # Reuse the credentials already stored on the entry; only the tokens are stale.
        self.init_info = {k: v for k, v in entry_data.items() if k != "tokens"}
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
        """
        entry = self._get_reconfigure_entry()
        self._reauth_entry = entry
        self._is_reconfigure = True

        if user_input is not None:
            # Preserve the App ID (identity) and drop stale tokens — we re-authorize.
            self.init_info = {**entry.data, **user_input}
            self.init_info.pop("tokens", None)
            # Start a fresh Auth client for the (possibly changed) credentials.
            self.auth_client = None
            return await self.async_step_auth()

        current = entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_KEY, default=current.get(CONF_APP_KEY, "")): str,
                    vol.Required(CONF_APP_SECRET, default=current.get(CONF_APP_SECRET, "")): str,
                    vol.Required(CONF_GATEWAY, default=current.get(CONF_GATEWAY, "Europe")): vol.In(
                        list(GATEWAYS.keys())
                    ),
                    vol.Required(CONF_REDIRECT_URI, default=current.get(CONF_REDIRECT_URI, "")): str,
                }
            ),
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        # Do not log user_input — it contains the app secret.
        _LOGGER.debug("async_step_user called (user_input provided: %s)", user_input is not None)
        errors: dict[str, str] = {}

        if user_input is not None:
            self.init_info = user_input
            await self.async_set_unique_id(str(user_input[CONF_APP_ID]))
            self._abort_if_unique_id_configured()
            # Create the hub immediately, before authorizing. Setting up this
            # token-less entry runs async_setup — which registers the OAuth
            # callback view — and then raises ConfigEntryAuthFailed, so Home
            # Assistant starts a reauth flow to finish authorization. This
            # guarantees the callback endpoint exists before the OAuth redirect
            # (fixing the first-install 404), and lets the user complete auth
            # automatically via the redirect or manually by pasting the code/URL.
            return self.async_create_entry(
                title=f"Sungrow {user_input[CONF_APP_ID]}",
                data=user_input,
            )

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
        resolves the pending flow via its single-flow fallback). Appending it only
        broke the token exchange, which uses the bare URI, with "invalid
        authentication".
        """
        # init_info is a dict[str, Any], so the redirect URI is typed as Any.
        return cast(str, self.init_info[CONF_REDIRECT_URI]).rstrip("/")

    def _auth_url(self) -> str:
        """Build the iSolarCloud authorization URL for the canonical redirect URI.

        Always called at render time (never cached), so every screen that shows the
        link — the progress wait, the manual-entry form, and the error-retry form —
        displays a freshly generated, current URL. The URL no longer embeds a
        per-flow ``flow_id`` that could go stale, so it stays valid for the whole
        flow and each visit yields a fresh authorization code from iSolarCloud.
        """
        # auth_client is the untyped pysolarcloud Auth, so auth_url returns Any.
        return cast(str, self.auth_client.auth_url(self._redirect_uri()))

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
        # A context flag arms it exactly once (not on every form re-render).
        if not self._manual_waiter_armed:
            self._manual_waiter_armed = True
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

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
        current_device_sensors = self.config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False)
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
                    vol.Optional(CONF_ENABLE_DEVICE_SENSORS, default=current_device_sensors): bool,
                }
            ),
            errors=errors,
        )
