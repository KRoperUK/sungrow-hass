"""Live integration tests against iSolarCloud API v1.

Run with:  pytest -m live
Requires SUNGROW_APPKEY, SUNGROW_APPSECRET, SUNGROW_USERNAME, SUNGROW_PASSWORD
environment variables (or a populated .env file).
"""

import pytest

from custom_components.sungrow.isolarcloud_api import ISolarCloudAPI


@pytest.mark.live
async def test_api_login(live_credentials):
    """Test login with live credentials."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        api = ISolarCloudAPI(
            host=live_credentials["host"],
            appkey=live_credentials["app_key"],
            access_key=live_credentials["app_secret"],
            user_account=live_credentials["username"],
            user_password=live_credentials["password"],
            websession=session,
        )

        result = await api.async_login()
        assert api.token is not None
        assert len(api.token) > 0


@pytest.mark.live
async def test_api_get_plants(live_credentials):
    """Test fetching plant list with live credentials."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        api = ISolarCloudAPI(
            host=live_credentials["host"],
            appkey=live_credentials["app_key"],
            access_key=live_credentials["app_secret"],
            user_account=live_credentials["username"],
            user_password=live_credentials["password"],
            websession=session,
        )

        await api.async_login()
        plants = await api.async_get_plant_list()
        assert isinstance(plants, list)
