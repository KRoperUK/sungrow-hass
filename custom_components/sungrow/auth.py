"""Authentication helpers for the Sungrow iSolarCloud integration.

The upstream :class:`pysolarcloud.Auth` client refreshes the access token when it
expires and, in doing so, *rotates* the refresh token (iSolarCloud invalidates the
previous one and returns a brand new ``tokens`` dict held only in memory).

The integration must persist those rotated tokens back to the config entry,
otherwise after a Home Assistant restart it would reload the now-invalid refresh
token, the refresh would fail, and every entity would become unavailable until the
user deleted and re-added the integration. This module wires a callback into the
refresh path so the latest tokens are always saved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pysolarcloud import Auth

_LOGGER = logging.getLogger(__name__)

# Errors raised by pysolarcloud that indicate the stored credentials/tokens are no
# longer usable and the user must re-authorize (as opposed to a transient outage).
# "token_refresh_failed" is the code on pysolarcloud>=0.6.0's TokenRefreshError
# (a PySolarCloudException), raised when a refresh returns no access token.
AUTH_ERRORS = frozenset({"auth_not_initialised", "invalid_grant", "invalid_token", "token_refresh_failed"})


class SungrowAuth(Auth):
    """``pysolarcloud.Auth`` that persists rotated tokens via a callback.

    ``token_updater`` is invoked with the new ``tokens`` dict whenever the upstream
    client refreshes them, allowing the caller to write them back to the config
    entry so they survive a restart.
    """

    def __init__(
        self,
        *args,
        token_updater: Callable[[dict], None] | None = None,
        **kwargs,
    ) -> None:
        """Initialize the auth client with an optional token-persistence callback."""
        super().__init__(*args, **kwargs)
        self._token_updater = token_updater

    async def async_get_access_token(self) -> str:
        """Return a valid access token, persisting any refreshed tokens.

        pysolarcloud assigns a *new* ``tokens`` dict when it refreshes, so an
        identity comparison reliably detects a rotation without persisting on every
        call.
        """
        previous = self.tokens
        token = await super().async_get_access_token()
        if self._token_updater is not None and self.tokens is not previous:
            _LOGGER.debug("Access token refreshed; persisting rotated tokens")
            self._token_updater(self.tokens)
        return token
