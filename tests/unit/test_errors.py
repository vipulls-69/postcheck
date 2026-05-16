"""Tests for ``postcheck.core.errors``."""
from __future__ import annotations

import json

import pytest

from postcheck.core.errors import (
    AdapterDetectionError,
    AnalysisError,
    CDPAttachError,
    ConfigError,
    PostcheckError,
    ScenarioExecutionError,
)


def test_postcheck_error_to_dict_round_trips_through_json():
    err = PostcheckError("boom", context={"k": 1})
    payload = err.to_dict()
    assert payload["code"] == "postcheck_error"
    assert payload["type"] == "PostcheckError"
    assert payload["message"] == "boom"
    assert payload["context"] == {"k": 1}
    # Must be JSON-serializable for structured logging
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    ("cls", "expected_code"),
    [
        (ConfigError, "config_error"),
        (AdapterDetectionError, "adapter_detection_error"),
        (AnalysisError, "analysis_error"),
        (ScenarioExecutionError, "scenario_execution_error"),
    ],
)
def test_subclass_codes(cls, expected_code):
    err = cls("nope")
    assert err.code == expected_code
    assert err.to_dict()["code"] == expected_code
    assert isinstance(err, PostcheckError)


def test_cdp_attach_error_message_contains_all_platform_commands():
    err = CDPAttachError(port=9222, cause="ECONNREFUSED")
    msg = str(err)
    # Each platform must be present with the chosen port and profile dir
    assert "macOS:" in msg
    assert "Linux:" in msg
    assert "Windows:" in msg
    assert "--remote-debugging-port=9222" in msg
    assert ".postcheck-chrome-profile" in msg
    # Platform-specific binary hints
    assert "Google Chrome.app" in msg
    assert "google-chrome" in msg
    assert "chrome.exe" in msg
    # Underlying cause must be mentioned
    assert "ECONNREFUSED" in msg


def test_cdp_attach_error_uses_supplied_port_and_profile():
    err = CDPAttachError(port=9333, profile_name=".my-profile")
    assert err.port == 9333
    assert ".my-profile" in str(err)
    assert "--remote-debugging-port=9333" in str(err)
    assert "9333" in err.endpoint


def test_cdp_attach_error_to_dict_carries_relaunch_commands():
    err = CDPAttachError(port=9222)
    payload = err.to_dict()
    assert payload["code"] == "cdp_attach_error"
    cmds = payload["context"]["relaunch_commands"]
    assert set(cmds) == {"macos", "linux", "windows"}
    for cmd in cmds.values():
        assert "9222" in cmd
    assert json.loads(json.dumps(payload)) == payload


def test_postcheck_error_is_raisable_and_catchable():
    with pytest.raises(PostcheckError) as exc_info:
        raise CDPAttachError(port=9222)
    assert isinstance(exc_info.value, CDPAttachError)
