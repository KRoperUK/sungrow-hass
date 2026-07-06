"""Tests validating strings.json / translations consistency."""

import json
from pathlib import Path

import pytest

STRINGS_PATH = Path(__file__).parent.parent / "custom_components" / "sungrow" / "strings.json"
TRANSLATIONS_DIR = Path(__file__).parent.parent / "custom_components" / "sungrow" / "translations"


@pytest.fixture
def strings_data() -> dict:
    """Load and return the parsed strings.json."""
    assert STRINGS_PATH.exists(), f"strings.json not found at {STRINGS_PATH}"
    return json.loads(STRINGS_PATH.read_text())


# ---------------------------------------------------------------------------
# Structure validation
# ---------------------------------------------------------------------------


def test_strings_has_config_section(strings_data):
    """Test strings.json contains a 'config' section."""
    assert "config" in strings_data


def test_strings_has_required_steps(strings_data):
    """Test config flow steps match what the code defines."""
    steps = strings_data["config"]["step"]
    assert "user" in steps, "Missing 'user' step"
    assert "auth_manual" in steps, "Missing 'auth_manual' step"
    assert "auth_callback" in steps, "Missing 'auth_callback' step"


def test_strings_user_step_has_required_fields(strings_data):
    """Test the user step defines all form fields used by the config flow."""
    user_data = strings_data["config"]["step"]["user"]["data"]
    required_fields = {"gateway", "app_key", "app_secret", "app_id", "redirect_uri"}
    assert required_fields.issubset(set(user_data.keys())), (
        f"Missing form fields: {required_fields - set(user_data.keys())}"
    )


def test_strings_auth_manual_step_has_code_field(strings_data):
    """Test the auth_manual step defines the 'code' field."""
    auth_data = strings_data["config"]["step"]["auth_manual"]["data"]
    assert "code" in auth_data


def test_strings_has_required_error_keys(strings_data):
    """Test that all error keys used by the config flow are defined."""
    errors = strings_data["config"]["error"]
    # These error keys are used in config_flow.py
    required_errors = {"cannot_connect", "invalid_auth", "unknown"}
    assert required_errors.issubset(set(errors.keys())), f"Missing error keys: {required_errors - set(errors.keys())}"


def test_strings_error_messages_not_empty(strings_data):
    """Test no error message is empty."""
    for key, message in strings_data["config"]["error"].items():
        assert message.strip(), f"Error key '{key}' has an empty message"


def test_strings_step_titles_not_empty(strings_data):
    """Test each step has a non-empty title."""
    for step_id, step_data in strings_data["config"]["step"].items():
        assert step_data.get("title", "").strip(), f"Step '{step_id}' has no title"


# ---------------------------------------------------------------------------
# Translation file consistency
# ---------------------------------------------------------------------------


def test_translations_en_matches_strings(strings_data):
    """Test that translations/en.json matches strings.json structure."""
    en_path = TRANSLATIONS_DIR / "en.json"
    if not en_path.exists():
        pytest.skip("translations/en.json does not exist")

    en_data = json.loads(en_path.read_text())

    # Both should define the same step IDs
    strings_steps = set(strings_data["config"]["step"].keys())
    en_steps = set(en_data.get("config", {}).get("step", {}).keys())
    assert strings_steps == en_steps, f"Step mismatch: strings.json={strings_steps}, en.json={en_steps}"

    # Both should define the same error keys
    strings_errors = set(strings_data["config"]["error"].keys())
    en_errors = set(en_data.get("config", {}).get("error", {}).keys())
    assert strings_errors == en_errors, f"Error key mismatch: strings.json={strings_errors}, en.json={en_errors}"


def _flatten_keys(data: dict, prefix: str = "") -> set[str]:
    """Return the set of dotted leaf-key paths in a nested dict."""
    keys: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        keys |= _flatten_keys(value, path) if isinstance(value, dict) else {path}
    return keys


@pytest.mark.parametrize("lang_file", sorted(TRANSLATIONS_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_translation_keys_match_strings(strings_data, lang_file):
    """Every translation file must define exactly the keys in strings.json (no drift).

    Guards against the historical drift where new strings (entity names, repair
    issues) were added to strings.json/en.json but never propagated to the other
    languages, leaving them silently untranslated.
    """
    lang_data = json.loads(lang_file.read_text())
    base = _flatten_keys(strings_data)
    have = _flatten_keys(lang_data)
    assert not base - have, f"{lang_file.name} is missing keys: {sorted(base - have)}"
    assert not have - base, f"{lang_file.name} has unexpected keys: {sorted(have - base)}"
