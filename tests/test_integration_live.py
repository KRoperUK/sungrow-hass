"""Live integration tests against iSolarCloud API.

Run with:  pytest -m live
Requires SUNGROW_APPKEY, SUNGROW_APPSECRET, SUNGROW_APP_ID environment variables
(or a populated .env file).
"""

import pytest

# Guard import so the test module itself loads even when pysolarcloud
# is not installed in the test environment.
pysolarcloud = pytest.importorskip("pysolarcloud")
Auth = pysolarcloud.Auth


@pytest.mark.live
async def test_api_auth_init(live_credentials):
    """Test Auth can be instantiated with live credentials without errors."""
    auth = Auth(
        host=live_credentials["host"],
        appkey=live_credentials["app_key"],
        access_key=live_credentials["app_secret"],
        app_id=live_credentials["app_id"],
    )
    # Just verify the auth object was created and has expected attributes
    assert auth is not None
    assert hasattr(auth, "auth_url")
    assert hasattr(auth, "tokens")


@pytest.mark.live
async def test_auth_url_generation(live_credentials):
    """Test that auth URL can be generated from live credentials."""
    auth = Auth(
        host=live_credentials["host"],
        appkey=live_credentials["app_key"],
        access_key=live_credentials["app_secret"],
        app_id=live_credentials["app_id"],
    )

    redirect_uri = "http://localhost:8123/api/sungrow_hass/callback"
    url = auth.auth_url(redirect_uri)
    assert isinstance(url, str)
    assert url.startswith("http")
    assert len(url) > 50  # Should be a full URL, not empty


@pytest.mark.live
async def test_live_list_plants_smoke(live_credentials, live_tokens):
    """Read-only smoke test against a real account: authorize and list plants (#257).

    Detects API-contract drift (e.g. the result_code handling behind pysolarcloud #9,
    the getDeviceRealTimeData 009 regression in #155) before users do. Read-only: it
    never writes dispatch parameters. Skips unless both app credentials and a stored
    token secret are present, so it only runs in the gated live-smoke workflow.
    """
    from pysolarcloud.plants import Plants

    auth = Auth(
        host=live_credentials["host"],
        appkey=live_credentials["app_key"],
        access_key=live_credentials["app_secret"],
        app_id=live_credentials["app_id"],
    )
    # Inject the stored tokens so the client can make authorized calls without an
    # interactive OAuth round-trip; the library refreshes them as needed.
    auth.tokens = live_tokens

    plants_service = Plants(auth)
    try:
        plants = await plants_service.async_get_plants()
    finally:
        await auth.async_close()

    assert isinstance(plants, list)
    # If the account has plants, each entry should carry the identifiers the
    # integration relies on for setup.
    for plant in plants:
        assert "ps_id" in plant
