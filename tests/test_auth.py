"""Tests for the token-persisting SungrowAuth wrapper."""

import asyncio
from unittest.mock import MagicMock, patch

import pysolarcloud

from custom_components.sungrow.auth import SungrowAuth


def _make_auth(token_updater=None) -> SungrowAuth:
    """Create a SungrowAuth with a mocked websession (no real network)."""
    return SungrowAuth(
        host="https://gateway.isolarcloud.eu",
        appkey="key",
        access_key="secret",
        app_id="1234",
        websession=MagicMock(),
        token_updater=token_updater,
    )


async def test_token_updater_called_when_tokens_rotate():
    """When pysolarcloud refreshes (assigns a new tokens dict), the updater fires."""
    saved = []
    auth = _make_auth(token_updater=saved.append)
    auth.tokens = {"access_token": "old", "refresh_token": "r1", "expires_at": 0}

    new_tokens = {"access_token": "new", "refresh_token": "r2", "expires_at": 9999999999}

    async def fake_parent(self):
        self.tokens = new_tokens
        return "new"

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        token = await auth.async_get_access_token()

    assert token == "new"
    assert saved == [new_tokens]


async def test_token_updater_not_called_without_rotation():
    """If the token is still valid (no new dict assigned), nothing is persisted."""
    saved = []
    auth = _make_auth(token_updater=saved.append)
    auth.tokens = {"access_token": "tok", "refresh_token": "r1", "expires_at": 9999999999}

    async def fake_parent(self):
        # Token still valid — pysolarcloud returns it without reassigning self.tokens.
        return self.tokens["access_token"]

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        token = await auth.async_get_access_token()

    assert token == "tok"
    assert saved == []


async def test_no_updater_is_safe():
    """A missing token_updater must not raise even when tokens rotate."""
    auth = _make_auth(token_updater=None)
    auth.tokens = {"access_token": "old", "expires_at": 0}

    async def fake_parent(self):
        self.tokens = {"access_token": "new", "expires_at": 9999999999}
        return "new"

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        assert await auth.async_get_access_token() == "new"


async def test_concurrent_refresh_refreshes_and_persists_once():
    """Concurrent callers sharing one Auth must refresh + persist exactly once (issue #111).

    The shared Auth is used by every coordinator plus the control/heartbeat paths, so
    two overlapping calls could otherwise each spend the single-use refresh token and
    trigger a spurious reauth. The lock serializes the body: the first waiter refreshes
    and persists, later waiters see the now-fresh token and no-op.
    """
    saved = []
    auth = _make_auth(token_updater=saved.append)
    auth.tokens = {"access_token": "old", "refresh_token": "r1", "expires_at": 0}
    calls = {"refresh": 0}

    async def fake_parent(self):
        # Mimic pysolarcloud: only refresh when the current token is expired.
        if self.tokens["expires_at"] == 0:
            calls["refresh"] += 1
            await asyncio.sleep(0)  # yield so the other caller can interleave
            self.tokens = {"access_token": "new", "refresh_token": "r2", "expires_at": 9999999999}
        return self.tokens["access_token"]

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        tokens = await asyncio.gather(auth.async_get_access_token(), auth.async_get_access_token())

    assert tokens == ["new", "new"]
    assert calls["refresh"] == 1
    assert saved == [{"access_token": "new", "refresh_token": "r2", "expires_at": 9999999999}]


async def test_rotation_detected_by_refresh_token_value():
    """A changed refresh_token value triggers a persist (issue #111)."""
    saved = []
    auth = _make_auth(token_updater=saved.append)
    auth.tokens = {"access_token": "a1", "refresh_token": "r1", "expires_at": 0}
    new_tokens = {"access_token": "a2", "refresh_token": "r2", "expires_at": 9999999999}

    async def fake_parent(self):
        self.tokens = new_tokens
        return "a2"

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        await auth.async_get_access_token()

    assert saved == [new_tokens]


async def test_no_persist_when_refresh_token_value_unchanged():
    """A new tokens dict whose refresh_token is unchanged must NOT persist (issue #111).

    Rotation is detected by refresh-token value, not dict identity, so a future
    pysolarcloud that re-fetches only the access token (same refresh token) does not
    churn a persist — and, conversely, an in-place mutation would still be caught.
    """
    saved = []
    auth = _make_auth(token_updater=saved.append)
    auth.tokens = {"access_token": "a1", "refresh_token": "r1", "expires_at": 0}

    async def fake_parent(self):
        # Brand-new dict object, but the refresh token has NOT rotated.
        self.tokens = {"access_token": "a2", "refresh_token": "r1", "expires_at": 9999999999}
        return "a2"

    with patch.object(pysolarcloud.Auth, "async_get_access_token", fake_parent):
        await auth.async_get_access_token()

    assert saved == []
