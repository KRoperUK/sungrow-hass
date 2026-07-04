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
from typing import Any

from pysolarcloud import Auth

_LOGGER = logging.getLogger(__name__)

# Errors raised by pysolarcloud that indicate the stored credentials/tokens are no
# longer usable and the user must re-authorize (as opposed to a transient outage).
# These are the errors pysolarcloud does NOT surface as a typed ``AuthError``:
# "token_refresh_failed" (its ``TokenRefreshError``, raised when a refresh returns no
# access token) and the OAuth token-exchange failures ("invalid_grant"/"invalid_token")
# and "auth_not_initialised". ``is_auth_error`` matches these by string.
#
# The documented dead-credential *result codes* (E00003, E900, E912, E914) are NOT listed
# here: pysolarcloud >=0.9.0 raises them as a typed ``AuthError`` (KRoperUK/pysolarcloud#23),
# which ``is_auth_error`` catches via ``isinstance`` — so the integration no longer
# duplicates the library's code list and picks up any new auth codes automatically.
#
# The whitelist rejections E918 / E919 are handled separately (``WHITELIST_ERRORS`` in
# coordinator.py): a whitelist rejection is a Developer-Portal config issue that reauth
# cannot fix, so it must keep retrying — even though 0.9.0 types E919 as ``AuthError``.
AUTH_ERRORS = frozenset(
    {
        "auth_not_initialised",
        "invalid_grant",
        "invalid_token",
        "token_refresh_failed",
    }
)


class SungrowAuth(Auth):
    """``pysolarcloud.Auth`` that persists rotated tokens via a callback.

    ``token_updater`` is invoked with the new ``tokens`` dict whenever the upstream
    client refreshes them, allowing the caller to write them back to the config
    entry so they survive a restart.
    """

    def __init__(
        self,
        *args: Any,
        token_updater: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the auth client with an optional token-persistence callback."""
        super().__init__(*args, **kwargs)
        self._token_updater = token_updater
        # The last refresh-token value we persisted, used to persist each rotation
        # exactly once. Captured lazily on first use because the caller assigns
        # ``tokens`` after construction (see __init__.py), so it isn't known here yet.
        self._baseline_captured = False
        self._last_refresh_token: Any = None

    async def async_get_access_token(self) -> str:
        """Return a valid access token, persisting any rotated refresh token.

        A single ``Auth`` instance is shared across every plant coordinator plus the
        control and heartbeat paths, so overlapping calls could each try to spend the
        same single-use refresh token and provoke a spurious reauth. pysolarcloud >=0.8.0
        serializes refresh internally (its own lock with a double-checked expiry), so the
        first waiter refreshes while the rest see the freshly stored token and skip — this
        wrapper no longer needs a lock of its own (removed in #121; the historical
        unavailable-after-restart bug was #14/#15/#20/#21).

        All this wrapper adds is persistence. Rotation is detected by the refresh-token
        *value* (robust whether pysolarcloud reassigns the ``tokens`` dict or mutates it
        in place) and tracked on the instance, so overlapping callers that each observe
        the same rotation persist it exactly once, and a still-valid token (no rotation)
        never triggers a redundant write.
        """
        # Capture the loaded refresh token as the baseline; synchronous (no await) so
        # concurrent callers race-free agree on it before the first refresh.
        if not self._baseline_captured:
            self._baseline_captured = True
            self._last_refresh_token = self.tokens.get("refresh_token") if self.tokens else None

        token = await super().async_get_access_token()

        # Check-and-set with no await in between, so overlapping callers can't both persist.
        current = self.tokens
        if self._token_updater is not None and current is not None:
            refresh_token = current.get("refresh_token")
            if refresh_token != self._last_refresh_token:
                self._last_refresh_token = refresh_token
                _LOGGER.debug("Refresh token rotated; persisting updated tokens")
                self._token_updater(current)
        return token
