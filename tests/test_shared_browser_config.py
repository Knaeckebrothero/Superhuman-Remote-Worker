"""Fail-closed configuration tests for the shared-browser broker."""

import pytest

from orchestrator.services.browser_stream_config import (
    BrowserStreamConfigurationError,
    browser_stream_config,
)

_ENV = (
    "CANVAS_SHARED_BROWSER_ENABLED",
    "CANVAS_BROWSER_STREAM_PORT",
    "CANVAS_BROWSER_MAX_VIEWERS",
    "CANVAS_BROWSER_CONNECT_TIMEOUT_SECONDS",
    "CANVAS_BROWSER_ACTIVITY_INTERVAL_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default():
    assert browser_stream_config().enabled is False


def test_enabled_with_defaults(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    config = browser_stream_config()
    assert config.enabled is True
    assert config.stream_port == 38801
    assert config.max_viewers == 3
    assert config.connect_timeout_seconds == 10
    assert config.activity_interval_seconds == 60


def test_bounded_int_rejects_out_of_range(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "1")
    monkeypatch.setenv("CANVAS_BROWSER_STREAM_PORT", "80")
    with pytest.raises(BrowserStreamConfigurationError):
        browser_stream_config()


def test_bounded_int_rejects_garbage(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "1")
    monkeypatch.setenv("CANVAS_BROWSER_MAX_VIEWERS", "lots")
    with pytest.raises(BrowserStreamConfigurationError):
        browser_stream_config()
