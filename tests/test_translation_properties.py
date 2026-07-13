"""Property-based test for translation key parity (#216).

Feature: transport-mode-selector
Property 4: Translation key parity
Validates: Requirements 10.4
"""

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

STRINGS_PATH = Path(__file__).parent.parent / "custom_components" / "sungrow" / "strings.json"
EN_PATH = Path(__file__).parent.parent / "custom_components" / "sungrow" / "translations" / "en.json"


def _flatten_keys(data: dict, prefix: str = "") -> list[str]:
    """Return all dotted leaf-key paths in a nested dict."""
    keys: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.extend(_flatten_keys(value, path))
        else:
            keys.append(path)
    return keys


def _get_nested(data: dict, dotted_path: str):
    """Retrieve a nested value by dotted key path."""
    parts = dotted_path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


@pytest.fixture(scope="module")
def strings_keys():
    """All config+options key paths from strings.json."""
    strings = json.loads(STRINGS_PATH.read_text())
    config_keys = _flatten_keys(strings.get("config", {}), "config")
    options_keys = _flatten_keys(strings.get("options", {}), "options")
    return config_keys + options_keys


@pytest.fixture(scope="module")
def en_data():
    """Loaded en.json data."""
    return json.loads(EN_PATH.read_text())


@settings(max_examples=100)
@given(data=st.data())
def test_translation_key_parity(data, strings_keys, en_data):
    """Property 4: Every config/options key in strings.json exists in en.json with a non-empty string."""
    if not strings_keys:
        return
    key_path = data.draw(st.sampled_from(strings_keys))
    value = _get_nested(en_data, key_path)
    assert value is not None, f"Key '{key_path}' missing from en.json"
    assert isinstance(value, str), f"Key '{key_path}' is not a string in en.json"
    assert value.strip(), f"Key '{key_path}' is empty in en.json"
