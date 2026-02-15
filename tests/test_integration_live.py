"""Live integration tests against iSolarCloud API.

Run with:  pytest -m live
Requires SUNGROW_APPKEY, SUNGROW_APPSECRET, SUNGROW_APP_ID environment variables
(or a populated .env file).
"""
import pytest

# Guard import so the test module itself loads even when pysolarcloud
# is not installed in the test environment.
pysolarcloud = pytest.importorskip("pysolarcloud")
from pysolarcloud import Auth


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
