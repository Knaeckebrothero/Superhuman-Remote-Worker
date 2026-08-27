"""Pure ordinary-HTTP security policy for Dynamic Canvas live preview."""

from __future__ import annotations

import pytest

from services.canvas_proxy_policy import (
    CanvasProxyError,
    CanvasProxyLimits,
    CanvasPublicOrigin,
    canvas_proxy_limits_from_env,
    rewrite_canvas_location,
    sanitize_canvas_response_headers,
    validate_canvas_request,
)

ORIGIN = CanvasPublicOrigin("11111111-aaaa-4aaa-8aaa-111111111111.canvas.test")
COCKPIT_ORIGINS = ("https://cockpit.test",)


def _headers(*extra: tuple[bytes, bytes]) -> list[tuple[bytes, bytes]]:
    return [(b"host", ORIGIN.authority.encode("ascii")), *extra]


def _validate(
    *,
    method: str = "GET",
    path: bytes | None = b"/",
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
):
    return validate_canvas_request(
        method=method,
        raw_path=path,
        raw_query=query,
        headers=headers or _headers(),
        public_origin=ORIGIN,
    )


def test_request_canonicalizes_raw_path_query_and_drops_control_keys() -> None:
    request = _validate(
        path=b"/caf%C3%A9/",
        query=b"a=one+two&a=&_Canvas_Token=secret&blank",
    )

    assert request.target == b"/caf%C3%A9/?a=one+two&a=&blank"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (None, "canvas_raw_path_unavailable"),
        (b"//double", "canvas_path_invalid"),
        (b"/a//b", "canvas_path_invalid"),
        (b"/%252e%252e", "canvas_path_invalid"),
        (b"/_CaNvAs/bootstrap", "canvas_path_invalid"),
    ],
)
def test_request_requires_preserved_canonical_raw_path(
    path: bytes | None, code: str
) -> None:
    with pytest.raises(CanvasProxyError) as caught:
        _validate(path=path)
    assert caught.value.code == code


def test_request_requires_exact_host_and_safe_method_origin() -> None:
    with pytest.raises(CanvasProxyError) as host_error:
        _validate(headers=[(b"host", b"other.canvas.test")])
    assert host_error.value.status_code == 421

    with pytest.raises(CanvasProxyError) as method_error:
        _validate(method="TRACE")
    assert method_error.value.status_code == 405

    with pytest.raises(CanvasProxyError) as missing_origin:
        _validate(method="POST")
    assert missing_origin.value.code == "canvas_origin_required"

    accepted = _validate(
        method="POST",
        headers=_headers(
            (b"origin", ORIGIN.origin.encode("ascii")),
            (b"content-length", b"0"),
        ),
    )
    assert accepted.prepare_upstream(0).method == b"POST"

    with pytest.raises(CanvasProxyError) as cross_origin:
        _validate(
            method="POST",
            headers=_headers((b"origin", b"https://attacker.test")),
        )
    assert cross_origin.value.status_code == 403


@pytest.mark.parametrize(
    "framing",
    [
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        [(b"transfer-encoding", b"gzip")],
        [(b"trailer", b"digest")],
    ],
)
def test_request_rejects_ambiguous_framing(
    framing: list[tuple[bytes, bytes]],
) -> None:
    with pytest.raises(CanvasProxyError) as caught:
        _validate(headers=_headers(*framing))
    assert caught.value.code == "canvas_request_framing_invalid"


def test_request_accepts_decoded_chunked_input_then_reframes() -> None:
    request = _validate(headers=_headers((b"transfer-encoding", b"chunked")))
    upstream = request.prepare_upstream(5)

    assert (b"transfer-encoding", b"chunked") not in upstream.headers
    assert upstream.headers.count((b"content-length", b"5")) == 1


def test_request_consumes_viewer_cookie_and_strips_platform_identity() -> None:
    request = _validate(
        headers=_headers(
            (b"cookie", b"__Host-canvas_session=viewer-secret; srw_session=platform"),
            (b"authorization", b"Bearer platform"),
            (b"proxy-authorization", b"Basic platform"),
            (b"x-internal-key", b"internal"),
            (b"x-mcp-user-id", b"user"),
            (b"x-forwarded-for", b"203.0.113.10"),
            (b"x-real-ip", b"203.0.113.10"),
            (b"cf-connecting-ip", b"203.0.113.10"),
            (b"x-envoy-external-address", b"203.0.113.10"),
            (b"referer", b"https://cockpit.test/private/thread"),
            (b"connection", b"keep-alive, x-remove"),
            (b"x-remove", b"private"),
            (b"user-agent", b"browser"),
        )
    )
    upstream = request.prepare_upstream(0)
    names = [name for name, _ in upstream.headers]

    assert request.viewer_cookie == "viewer-secret"
    assert b"cookie" not in names
    assert b"authorization" not in names
    assert b"proxy-authorization" not in names
    assert b"x-internal-key" not in names
    assert b"x-mcp-user-id" not in names
    assert b"x-forwarded-for" not in names
    assert b"x-real-ip" not in names
    assert b"cf-connecting-ip" not in names
    assert b"x-envoy-external-address" not in names
    assert b"referer" not in names
    assert b"x-remove" not in names
    assert (b"user-agent", b"browser") in upstream.headers
    assert (
        b"forwarded",
        f'proto=https;host="{ORIGIN.authority}"'.encode(),
    ) in upstream.headers
    assert (b"connection", b"close") in upstream.headers


def test_request_rejects_duplicate_reserved_cookie_and_websocket() -> None:
    with pytest.raises(CanvasProxyError) as cookie_error:
        _validate(
            headers=_headers(
                (
                    b"cookie",
                    b"__Host-canvas_session=one; __Host-canvas_session=two",
                )
            )
        )
    assert cookie_error.value.code == "canvas_cookie_invalid"

    with pytest.raises(CanvasProxyError) as websocket_error:
        _validate(
            headers=_headers(
                (b"connection", b"Upgrade"),
                (b"upgrade", b"websocket"),
                (b"sec-websocket-key", b"test"),
            )
        )
    assert websocket_error.value.status_code == 426


def test_response_replaces_security_cache_cors_and_cookie_headers() -> None:
    response = sanitize_canvas_response_headers(
        status_code=200,
        headers=[
            (b"content-type", b"text/html; charset=utf-8"),
            (b"set-cookie", b"app=secret; Domain=.canvas.test"),
            (b"content-security-policy", b"default-src *"),
            (b"access-control-allow-origin", b"*"),
            (b"cache-control", b"public, max-age=86400"),
            (b"www-authenticate", b"Basic realm=workspace"),
            (b"x-internal-key", b"workspace-echo"),
        ],
        request_method="GET",
        request_path="/",
        public_origin=ORIGIN,
        cockpit_origins=COCKPIT_ORIGINS,
        entry_port=8501,
    )
    values = dict(response.headers)

    assert values[b"content-type"] == b"text/html; charset=utf-8"
    assert b"set-cookie" not in values
    assert b"access-control-allow-origin" not in values
    assert b"www-authenticate" not in values
    assert b"x-internal-key" not in values
    assert values[b"cache-control"] == b"private, no-store"
    assert (
        b"frame-ancestors 'self' https://cockpit.test"
        in values[b"content-security-policy"]
    )
    assert b"worker-src 'none'" in values[b"content-security-policy"]
    assert (
        b"sandbox allow-scripts allow-same-origin allow-forms"
        in values[b"content-security-policy"]
    )
    assert values[b"referrer-policy"] == b"no-referrer"
    assert values[b"x-content-type-options"] == b"nosniff"


def test_csp_lets_the_app_frame_its_own_pages_without_widening_who_frames_it() -> None:
    """The gallery shape: a shell page that iframes its own subpages.

    Two directives decide this and both have to admit it — the shell's
    ``frame-src`` for the nested load, then the subpage's own
    ``frame-ancestors``, because the app origin now sits in that subpage's
    ancestor chain. Admitting only the first leaves the nested document
    refused. The trusted Cockpit origin stays required either way: the browser
    checks every ancestor, so the outermost frame still cannot be anyone else.
    """

    response = sanitize_canvas_response_headers(
        status_code=200,
        headers=[(b"content-type", b"text/html; charset=utf-8")],
        request_method="GET",
        request_path="/pages/arrivals_queue.html",
        public_origin=ORIGIN,
        cockpit_origins=COCKPIT_ORIGINS,
        entry_port=8501,
    )
    policy = dict(response.headers)[b"content-security-policy"]

    assert b"frame-src 'self' blob:" in policy
    assert b"frame-src 'none'" not in policy
    assert b"frame-ancestors 'self' https://cockpit.test" in policy
    # Only the app's own origin is added. Plugins, workers, and every other
    # framing source stay closed.
    assert b"object-src 'none'" in policy
    assert b"worker-src 'none'" in policy


def test_head_preserves_validated_representation_length_only() -> None:
    response = sanitize_canvas_response_headers(
        status_code=200,
        headers=[(b"content-length", b"123"), (b"accept-ranges", b"bytes")],
        request_method="HEAD",
        request_path="/asset",
        public_origin=ORIGIN,
        cockpit_origins=COCKPIT_ORIGINS,
        entry_port=8501,
    )

    assert (b"content-length", b"123") in response.headers
    assert (b"accept-ranges", b"bytes") in response.headers


def test_csp_rejects_noncanonical_or_injectable_frame_ancestor() -> None:
    with pytest.raises(ValueError):
        sanitize_canvas_response_headers(
            status_code=200,
            headers=[],
            request_method="GET",
            request_path="/",
            public_origin=ORIGIN,
            cockpit_origins=("https://cockpit.test; frame-src *",),
            entry_port=8501,
        )


@pytest.mark.parametrize(
    "media_type", [b"text/event-stream", b"multipart/x-mixed-replace"]
)
def test_response_rejects_deferred_streaming(media_type: bytes) -> None:
    with pytest.raises(CanvasProxyError) as caught:
        sanitize_canvas_response_headers(
            status_code=200,
            headers=[(b"content-type", media_type)],
            request_method="GET",
            request_path="/",
            public_origin=ORIGIN,
            cockpit_origins=COCKPIT_ORIGINS,
            entry_port=8501,
        )
    assert caught.value.code == "canvas_streaming_unsupported"


def test_redirect_normalizes_same_origin_relative_and_exact_loopback() -> None:
    assert (
        rewrite_canvas_location(
            b"next?tab=one+two",
            current_path="/app/index",
            public_origin=ORIGIN,
            entry_port=8501,
        )
        == (ORIGIN.origin + "/app/next?tab=one+two").encode()
    )
    assert (
        rewrite_canvas_location(
            b"http://127.0.0.1:8501/login",
            current_path="/",
            public_origin=ORIGIN,
            entry_port=8501,
        )
        == (ORIGIN.origin + "/login").encode()
    )

    with pytest.raises(CanvasProxyError) as external:
        rewrite_canvas_location(
            b"https://attacker.test/phish",
            current_path="/",
            public_origin=ORIGIN,
            entry_port=8501,
        )
    assert external.value.code == "canvas_navigation_blocked"


def test_request_header_and_body_limits_are_explicit() -> None:
    limits = CanvasProxyLimits(max_header_bytes=32, max_request_body_bytes=2)
    with pytest.raises(CanvasProxyError) as headers_error:
        validate_canvas_request(
            method="GET",
            raw_path=b"/",
            raw_query=b"",
            headers=_headers((b"x-long", b"x" * 64)),
            public_origin=ORIGIN,
            limits=limits,
        )
    assert headers_error.value.status_code == 431

    body_limits = CanvasProxyLimits(max_request_body_bytes=2)
    with pytest.raises(CanvasProxyError) as body_error:
        validate_canvas_request(
            method="POST",
            raw_path=b"/",
            raw_query=b"",
            headers=_headers(
                (b"origin", ORIGIN.origin.encode()), (b"content-length", b"3")
            ),
            public_origin=ORIGIN,
            limits=body_limits,
        )
    assert body_error.value.status_code == 413


def test_proxy_limit_environment_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANVAS_VIEWER_MAX_REQUEST_BODY_BYTES", "1234")
    monkeypatch.setenv("CANVAS_VIEWER_CONNECT_TIMEOUT_SECONDS", "2.5")
    limits = canvas_proxy_limits_from_env()
    assert limits.max_request_body_bytes == 1234
    assert limits.connect_timeout_seconds == 2.5

    monkeypatch.setenv("CANVAS_VIEWER_MAX_HEADER_FIELDS", "1")
    with pytest.raises(ValueError, match="safe range"):
        canvas_proxy_limits_from_env()

    with pytest.raises(ValueError, match="max_request_body_bytes must be positive"):
        CanvasProxyLimits(max_request_body_bytes=0)
