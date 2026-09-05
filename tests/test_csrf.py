"""Shared HTTP/WS browser-origin authority tests."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from orchestrator.security.csrf import allowed_browser_origins, websocket_origin_allowed


def _headers(*origins: str) -> Headers:
    return Headers(raw=[(b"origin", value.encode("ascii")) for value in origins])


def test_allowed_origins_include_dev_and_normalized_environment(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "  https://Cockpit.Example.TEST:443 ,"
        "http://dev.example.test:8080,"
        "https://bad.example.test/path,"
        "https://user:pass@example.test,"
        "ftp://example.test,"
        "null,",
    )

    allowed = allowed_browser_origins()

    assert isinstance(allowed, frozenset)
    assert {
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
        "https://cockpit.example.test",
        "http://dev.example.test:8080",
    } <= allowed
    assert all("bad.example" not in origin for origin in allowed)
    assert all("user:pass" not in origin for origin in allowed)
    assert all(not origin.startswith("ftp:") for origin in allowed)


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "https://example.test/",
        "https://example.test/path",
        "https://example.test?query=1",
        "https://example.test#fragment",
        "https://user@example.test",
        "https://example.test\\@evil.test",
        "ftp://example.test",
        "https://example.test:bad",
        "https://example.test:\x00",
    ],
)
def test_websocket_origin_rejects_noncanonical_values(origin):
    assert websocket_origin_allowed(_headers(origin)) is False


def test_websocket_origin_requires_exactly_one_header(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://cockpit.example.test")

    assert websocket_origin_allowed(_headers()) is False
    assert (
        websocket_origin_allowed(
            _headers(
                "https://cockpit.example.test",
                "https://cockpit.example.test",
            )
        )
        is False
    )
    assert websocket_origin_allowed(_headers("https://elsewhere.example.test")) is False
    assert websocket_origin_allowed(_headers("https://cockpit.example.test")) is True


def test_websocket_origin_normalizes_scheme_host_and_default_port(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://cockpit.example.test")

    assert websocket_origin_allowed(_headers("HTTPS://COCKPIT.EXAMPLE.TEST:443"))
