"""Wire contract for the extracted media proxy.

This endpoint fetches an attacker-influenced URL on the server's behalf, so
every part of its boundary is a security ruling rather than a convenience:

* the approved-user (and CSRF) gate runs **before** any outbound work — a
  signed-out caller must not be able to make the orchestrator issue a request;
* the response is served inert: no store, no sniffing, no cross-origin read,
  an empty CSP with ``sandbox``, no referrer, and an ``inline`` disposition
  whose filename is derived from the validated media type, never from the URL;
* ``RemoteImageError`` maps to its own status with a ``{code, message}`` detail
  — the fetcher's errors are deliberately URL-free;
* neither the URL nor the image bytes are logged or persisted.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orchestrator.services import remote_image as subject
from tests._mounted_router import mount_router


URL = "https://images.example/secret-path/a.png"
USER = {"id": "00000000-0000-0000-0000-0000000000c1"}
PNG = b"\x89PNG\r\n\x1a\n-not-a-real-file-but-distinctive"


def _wire(*, approved_gate=None):
    from orchestrator.routers.media import MediaDependencies as RouteDeps
    from orchestrator.routers.media import router

    db = SimpleNamespace()

    async def approved(request, _store):
        if approved_gate is not None:
            return await approved_gate(request, _store)
        return USER

    deps = RouteDeps(store=db, require_approved_user=approved)
    app = mount_router(router, factories={"media_dependencies_factory": lambda: deps})
    return SimpleNamespace(client=TestClient(app), store=db)


def _image(media_type="image/png", content=PNG):
    return subject.RemoteImage(
        content=content, media_type=media_type, width=2, height=3
    )


def test_the_gate_runs_before_any_outbound_work():
    async def deny(_request, _store):
        raise HTTPException(status_code=401, detail="signed out")

    fetch = AsyncMock()
    wired = _wire(approved_gate=deny)

    with patch("orchestrator.routers.media.fetch_remote_image", fetch):
        response = wired.client.post("/api/media/remote-image", json={"url": URL})

    assert response.status_code == 401
    fetch.assert_not_awaited()


def test_an_approved_caller_gets_the_bytes_with_the_inert_header_set():
    wired = _wire()

    with patch(
        "orchestrator.routers.media.fetch_remote_image",
        AsyncMock(return_value=_image()),
    ):
        response = wired.client.post("/api/media/remote-image", json={"url": URL})

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"] == (
        'inline; filename="remote-image.png"'
    )
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "media_type,extension",
    [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/gif", "gif"),
        ("image/webp", "webp"),
    ],
)
def test_the_filename_extension_comes_from_the_validated_media_type(
    media_type, extension
):
    """Never from the URL — a ``.php`` path cannot become the served filename."""
    wired = _wire()

    with patch(
        "orchestrator.routers.media.fetch_remote_image",
        AsyncMock(return_value=_image(media_type=media_type)),
    ):
        response = wired.client.post(
            "/api/media/remote-image",
            json={"url": "https://images.example/evil.php?x=1"},
        )

    assert response.headers["content-disposition"] == (
        f'inline; filename="remote-image.{extension}"'
    )


@pytest.mark.parametrize(
    "status_code,code,message",
    [
        (400, "invalid-url", "Only http(s) URLs are supported"),
        (403, "blocked-address", "Address is not public"),
        (413, "too-large", "Image exceeds the size limit"),
        (415, "unsupported-type", "Not a supported raster image"),
        (502, "fetch-failed", "Upstream did not answer"),
    ],
)
def test_a_fetcher_error_maps_to_its_status_and_code_message_detail(
    status_code, code, message
):
    wired = _wire()
    error = subject.RemoteImageError(status_code, code, message)

    with patch(
        "orchestrator.routers.media.fetch_remote_image", AsyncMock(side_effect=error)
    ):
        response = wired.client.post("/api/media/remote-image", json={"url": URL})

    assert response.status_code == status_code
    assert response.json()["detail"] == {"code": code, "message": message}


def test_neither_the_url_nor_the_bytes_are_logged(caplog):
    wired = _wire()

    with caplog.at_level(logging.DEBUG):
        with patch(
            "orchestrator.routers.media.fetch_remote_image",
            AsyncMock(return_value=_image()),
        ):
            ok = wired.client.post("/api/media/remote-image", json={"url": URL})
        with patch(
            "orchestrator.routers.media.fetch_remote_image",
            AsyncMock(
                side_effect=subject.RemoteImageError(403, "blocked-address", "no")
            ),
        ):
            wired.client.post("/api/media/remote-image", json={"url": URL})

    assert ok.status_code == 200
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-path" not in logged
    assert "images.example" not in logged
    assert "PNG" not in logged


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": ""},
        {"url": "x" * (subject.MAX_REMOTE_IMAGE_URL_CHARS + 1)},
    ],
)
def test_the_url_field_is_bounded_before_the_handler_runs(body):
    fetch = AsyncMock()
    wired = _wire()

    with patch("orchestrator.routers.media.fetch_remote_image", fetch):
        response = wired.client.post("/api/media/remote-image", json=body)

    assert response.status_code == 422
    fetch.assert_not_awaited()


def test_a_url_at_the_length_limit_is_accepted():
    wired = _wire()
    url = "https://images.example/" + "a" * (
        subject.MAX_REMOTE_IMAGE_URL_CHARS - len("https://images.example/")
    )

    with patch(
        "orchestrator.routers.media.fetch_remote_image",
        AsyncMock(return_value=_image()),
    ):
        response = wired.client.post("/api/media/remote-image", json={"url": url})

    assert response.status_code == 200
