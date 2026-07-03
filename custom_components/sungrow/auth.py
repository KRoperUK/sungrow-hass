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

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pysolarcloud import Auth

_LOGGER = logging.getLogger(__name__)

# Errors raised by pysolarcloud that indicate the stored credentials/tokens are no
# longer usable and the user must re-authorize (as opposed to a transient outage).
# "token_refresh_failed" is the code on pysolarcloud>=0.6.0's TokenRefreshError
# (a PySolarCloudException), raised when a refresh returns no access token.
#
# The "E*" codes are the documented iSolarCloud OpenAPI result codes for dead or
# unauthorized credentials (E00003 token invalid/expired, E900 unauthorized, E919
# de-whitelisted, E912/E914 bad or mismatched app key). These are surfaced once the
# pinned pysolarcloud raises PySolarCloudException carrying the API result_code (a
# paired library fix); the quota/throttle codes E998/E999 are deliberately excluded
# because they are transient and must keep retrying (UpdateFailed), not reauth.
AUTH_ERRORS = frozenset(
    {
        "auth_not_initialised",
        "invalid_grant",
        "invalid_token",
        "token_refresh_failed",
        "E00003",
        "E900",
        "E919",
        "E912",
        "E914",
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
        # Created lazily inside the coroutine so it binds to the running event loop.
        self._refresh_lock: asyncio.Lock | None = None

    async def async_get_access_token(self) -> str:
        """Return a valid access token, persisting any rotated refresh token.

        A single ``Auth`` instance is shared across every plant coordinator plus the
        control and heartbeat paths, so overlapping calls could each try to spend the
        same single-use refresh token and provoke a spurious reauth. Serializing the
        whole body with a lock means the first waiter refreshes and persists while
        later waiters find the token already fresh (``super()`` re-checks expiry) and
        no-op. The pinned pysolarcloud 0.7.0 has no lock of its own, so this shim is
        what protects us; it stays harmless if a future version adds one.

        Rotation is detected by comparing the *refresh-token value* rather than the
        ``tokens`` dict identity. Identity works only because 0.7.0 reassigns the dict;
        a future in-place mutation would silently stop persisting and leave an invalid
        refresh token on the next restart. Comparing the value is robust either way.
        """
        # Synchronous check-and-set (no await between) so it is race-free.
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        async with self._refresh_lock:
            previous = self.tokens
            token = await super().async_get_access_token()
            current = self.tokens
            rotated = previous is None or previous.get("refresh_token") != current.get("refresh_token")
            if self._token_updater is not None and current is not None and rotated:
                _LOGGER.debug("Refresh token rotated; persisting updated tokens")
                self._token_updater(current)
            return token
