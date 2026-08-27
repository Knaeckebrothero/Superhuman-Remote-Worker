from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        (" 1 ", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("off", False),
    ],
)
def test_protected_cloud_mode_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("PROTECTED_CLOUD_MODE_ENABLED", value)
    main = importlib.import_module("orchestrator.main")
    assert main._is_protected_cloud_mode_enabled() is expected


def test_protected_cloud_mode_flag_absent_defaults_false(monkeypatch):
    monkeypatch.delenv("PROTECTED_CLOUD_MODE_ENABLED", raising=False)
    main = importlib.import_module("orchestrator.main")
    assert main._is_protected_cloud_mode_enabled() is False
