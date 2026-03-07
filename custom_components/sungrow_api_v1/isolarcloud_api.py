"""iSolarCloud API v1 client (non-OAuth, username/password authentication)."""

from __future__ import annotations

import logging
import random
import string
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Common headers for all requests
_SYS_CODE = "901"


def _generate_nonce(length: int = 32) -> str:
    """Generate a random nonce string."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def _timestamp_ms() -> str:
    """Return current timestamp in milliseconds as string."""
    return str(int(time.time() * 1000))


class ISolarCloudAPI:
    """Client for iSolarCloud API v1 (non-OAuth)."""

    def __init__(
        self,
        host: str,
        appkey: str,
        access_key: str,
        user_account: str,
        user_password: str,
        websession: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        self.host = host.rstrip("/")
        self.appkey = appkey
        self.access_key = access_key
        self.user_account = user_account
        self.user_password = user_password
        self._session = websession
        self.token: str | None = None
        self.user_id: str | None = None

    def _base_headers(self) -> dict[str, str]:
        """Return base headers for API requests."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Home Assistant",
            "x-access-key": self.access_key,
            "sys_code": _SYS_CODE,
        }
        if self.token:
            headers["token"] = self.token
        return headers

    def _base_payload(self) -> dict:
        """Return base payload fields for API requests."""
        return {
            "appkey": self.appkey,
            "api_key_param": {
                "nonce": _generate_nonce(),
                "timestamp": _timestamp_ms(),
            },
        }

    async def _post(self, endpoint: str, payload: dict) -> dict:
        """Make a POST request to the API."""
        url = f"{self.host}/openapi/{endpoint}"
        headers = self._base_headers()

        _LOGGER.debug("POST %s payload=%s", url, {k: v for k, v in payload.items() if k != "user_password"})

        if self._session is None:
            raise RuntimeError("No HTTP session available")

        async with self._session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

        _LOGGER.debug("Response from %s: result_code=%s", endpoint, data.get("result_code"))

        if str(data.get("result_code")) != "1":
            msg = data.get("result_msg", "Unknown API error")
            raise ISolarCloudError(f"API error ({endpoint}): {msg} (code={data.get('result_code')})")

        return data

    async def async_login(self) -> dict:
        """Login and obtain a token.

        Returns the full result_data dict on success.
        """
        payload = {
            **self._base_payload(),
            "user_account": self.user_account,
            "user_password": self.user_password,
            "login_type": "1",
        }

        data = await self._post("login", payload)
        result_data = data.get("result_data", {})

        token = result_data.get("token")
        if not token:
            raise ISolarCloudError("Login succeeded but no token in response")

        self.token = token
        self.user_id = str(result_data.get("user_id", ""))
        _LOGGER.info("Successfully logged in to iSolarCloud (user_id=%s)", self.user_id)
        return result_data

    async def async_get_plant_list(self) -> list[dict]:
        """Get the list of power stations (plants).

        Returns a list of plant dicts with at least ps_id and ps_name.
        """
        payload = {
            **self._base_payload(),
            "curPage": 1,
            "size": 100,
        }

        data = await self._post("getPowerStationList", payload)
        result_data = data.get("result_data", {})

        # The API may return plants under different keys
        plants = result_data.get("pageList", result_data.get("list", []))
        if not isinstance(plants, list):
            plants = []

        return plants

    async def async_get_device_list(self, ps_id: str | int) -> list[dict]:
        """Get devices for a specific plant.

        Returns a list of device dicts.
        """
        payload = {
            **self._base_payload(),
            "ps_id": str(ps_id),
            "curPage": 1,
            "size": 100,
        }

        data = await self._post("getDeviceList", payload)
        result_data = data.get("result_data", {})

        devices = result_data.get("pageList", result_data.get("list", []))
        if not isinstance(devices, list):
            devices = []

        return devices

    async def async_get_device_realtime_data(
        self,
        ps_key_list: list[str],
        point_id_list: list[str] | None = None,
        device_type: int = 11,
    ) -> dict:
        """Get real-time data for devices.

        Args:
            ps_key_list: List of ps_key identifiers (e.g. ["123456_11_0_0"]).
            point_id_list: Optional list of specific point IDs to retrieve.
            device_type: Device type (default 11 for inverter).

        Returns the result_data dict from the API.
        """
        payload = {
            **self._base_payload(),
            "ps_key_list": ps_key_list,
            "device_type": device_type,
        }
        if point_id_list:
            payload["point_id_list"] = point_id_list

        data = await self._post("getDeviceRealTimeData", payload)
        return data.get("result_data", {})

    async def async_get_plant_realtime_data(self, ps_id: str | int) -> dict:
        """Get real-time data for a plant, organized by data point.

        Fetches device list, then real-time data for all devices.
        Returns a dict keyed by point code with value/unit/name info.
        """
        devices = await self.async_get_device_list(ps_id)

        all_points = {}

        for device in devices:
            dev_ps_key = device.get("ps_key")
            dev_type = device.get("device_type", 11)

            if not dev_ps_key:
                # Build ps_key from components if not provided
                dev_id = device.get("id", device.get("device_id", "0"))
                dev_ps_key = f"{ps_id}_{dev_type}_{dev_id}_0"

            try:
                result = await self.async_get_device_realtime_data(
                    ps_key_list=[dev_ps_key],
                    device_type=int(dev_type),
                )
            except ISolarCloudError as err:
                _LOGGER.warning("Failed to get realtime data for device %s: %s", dev_ps_key, err)
                continue

            # Parse device_point_list from response
            device_point_list = result.get("device_point_list", [])
            for device_entry in device_point_list:
                points = device_entry.get("device_point", {})
                if isinstance(points, dict):
                    for point_key, point_val in points.items():
                        # point_key is like "p83022"
                        code = point_key.lstrip("p") if point_key.startswith("p") else point_key
                        if isinstance(point_val, dict):
                            all_points[point_key] = {
                                "code": point_key,
                                "value": point_val.get("value", point_val.get("data_value", "")),
                                "unit": point_val.get("unit", point_val.get("data_unit", "")),
                                "name": point_val.get("name", point_val.get("data_name", point_key)),
                            }
                        else:
                            # Simple key-value pair
                            all_points[point_key] = {
                                "code": point_key,
                                "value": str(point_val),
                                "unit": "",
                                "name": point_key,
                            }

        return all_points


class ISolarCloudError(Exception):
    """Exception for iSolarCloud API errors."""
