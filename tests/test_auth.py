"""Tests for the token-persisting SungrowAuth wrapper."""

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
