"""Frozen bundle-helper behavior through both existing tool-module imports."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture(params=("catalog", "workflows"))
def adapter(request):
    return importlib.import_module(f"agent.tools.orchestrator.{request.param}")


@pytest.fixture
def workspace():
    return Mock(spec=("read_file", "write_file"))


def test_unicode_canonical_encoding_and_frozen_hash(adapter):
    payload = {"z": 1, "ä": [True, None, {"a": "\n"}]}
    expected = r'{"z":1,"\u00e4":[true,null,{"a":"\n"}]}'
    expected_hash = "261ef0c682fd6e919aa4eb5826c9596e33b5537e218db1776d3a9272e94c6bb7"
    assert adapter._canonical_json(payload) == expected
    assert adapter._bundle_hash(payload) == expected_hash
    assert adapter._bundle_hash({"ä": payload["ä"], "z": 1}) == expected_hash
    assert list(payload) == ["z", "ä"]


def test_pretty_json_preserves_insertion_order_and_write_adds_one_newline(
    adapter, workspace
):
    payload = {"z": 1, "a": "ü"}
    expected = '{\n  "z": 1,\n  "a": "\\u00fc"\n}'
    assert adapter._pretty_json(payload) == expected
    path = "bundles/catalog.json"
    assert (
        adapter._write_workspace_json(
            SimpleNamespace(workspace_manager=workspace), path, payload
        )
        == path
    )
    workspace.write_file.assert_called_once_with(path, expected + "\n")


@pytest.mark.parametrize("destination", (None, ""))
def test_absent_destination_does_not_require_or_touch_workspace(adapter, destination):
    assert adapter._write_workspace_json(object(), destination, {}) is None


def test_destination_requires_workspace(adapter):
    with pytest.raises(ValueError) as caught:
        adapter._write_workspace_json(
            SimpleNamespace(workspace_manager=None), "out.json", {}
        )
    assert str(caught.value) == "destination_path requires a workspace-backed session."


def test_inline_and_path_ambiguity_precedes_workspace_access_even_for_empty_bundle(
    adapter, workspace
):
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(
            SimpleNamespace(workspace_manager=workspace),
            bundle={},
            bundle_path="in.json",
        )
    assert str(caught.value) == "Provide either bundle or bundle_path, not both."
    workspace.read_file.assert_not_called()


@pytest.mark.parametrize("bundle_path", (None, ""))
def test_empty_inline_dict_returns_the_original_object(adapter, bundle_path):
    payload = {}
    assert (
        adapter._read_bundle_payload(object(), bundle=payload, bundle_path=bundle_path)
        is payload
    )


def test_read_delegates_exact_path_to_workspace(adapter, workspace):
    workspace.read_file.return_value = '{"bundle": {"name": "fixture"}}'
    assert adapter._read_bundle_payload(
        SimpleNamespace(workspace_manager=workspace),
        bundle=None,
        bundle_path="bundles/in.json",
    ) == {"bundle": {"name": "fixture"}}
    workspace.read_file.assert_called_once_with("bundles/in.json")


def test_path_requires_workspace(adapter):
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(
            SimpleNamespace(workspace_manager=None), bundle=None, bundle_path="in.json"
        )
    assert str(caught.value) == "bundle_path requires a workspace-backed session."


@pytest.mark.parametrize("bundle_path", (None, ""))
def test_missing_bundle_is_explicit(adapter, bundle_path):
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(object(), bundle=None, bundle_path=bundle_path)
    assert str(caught.value) == "bundle or bundle_path is required."


@pytest.mark.parametrize("payload", ([], "{}", False, 0))
def test_inline_non_object_is_rejected(adapter, payload):
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(object(), bundle=payload, bundle_path=None)
    assert str(caught.value) == "bundle must be a JSON object."


@pytest.mark.parametrize("payload", ("[]", "null", "false", '"text"'))
def test_file_json_must_be_an_object(adapter, workspace, payload):
    workspace.read_file.return_value = payload
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(
            SimpleNamespace(workspace_manager=workspace),
            bundle=None,
            bundle_path="in.json",
        )
    assert str(caught.value) == "Bundle JSON must be an object."


def test_malformed_json_retains_path_and_original_decode_cause(adapter, workspace):
    workspace.read_file.return_value = '{"name":'
    with pytest.raises(ValueError) as caught:
        adapter._read_bundle_payload(
            SimpleNamespace(workspace_manager=workspace),
            bundle=None,
            bundle_path="bundles/bad.json",
        )
    assert str(caught.value) == (
        "bundles/bad.json is not valid JSON: Expecting value: line 1 column 9 (char 8)"
    )
    cause = caught.value.__cause__
    assert isinstance(cause, json.JSONDecodeError)
    assert cause.doc == '{"name":' and cause.pos == 8


@pytest.mark.parametrize("operation", ("read", "write"))
def test_workspace_io_error_propagates_unchanged(adapter, workspace, operation):
    error = PermissionError("workspace denied fixture path")
    context = SimpleNamespace(workspace_manager=workspace)
    getattr(workspace, operation + "_file").side_effect = error
    with pytest.raises(PermissionError) as caught:
        if operation == "read":
            adapter._read_bundle_payload(
                context, bundle=None, bundle_path="fixture.json"
            )
        else:
            adapter._write_workspace_json(context, "fixture.json", {})
    assert caught.value is error


def test_unwrap_preserves_nested_empty_dict_identity(adapter):
    nested = {}
    assert adapter._unwrap_bundle({"bundle": nested, "kind": "fixture"}) is nested


@pytest.mark.parametrize("nested", (None, [], "{}", False))
def test_unwrap_non_object_retains_entire_payload_identity(adapter, nested):
    payload = {"bundle": nested}
    assert adapter._unwrap_bundle(payload) is payload


def http_error(*, body=None, unread=False):
    request = httpx.Request("GET", "https://fixture.invalid/bundle")
    kwargs = {"stream": httpx.ByteStream(b"unread body")} if unread else {"text": body}
    response = httpx.Response(503, request=request, **kwargs)
    return httpx.HTTPStatusError(
        "fixture HTTP error", request=request, response=response
    )


def test_http_error_handles_unread_response_body(adapter):
    error = http_error(unread=True)
    with pytest.raises(httpx.ResponseNotRead):
        _ = error.response.text
    assert adapter._http_error(error) == "HTTP 503"


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("", "HTTP 503"),
        (" \n ", "HTTP 503: N/A"),
        ("a" * 500, "HTTP 503: " + "a" * 500),
        ("  " + "a" * 501 + "\n", "HTTP 503: " + "a" * 497 + "..."),
    ),
)
def test_http_error_empty_and_bounded_detail(adapter, body, expected):
    assert adapter._http_error(http_error(body=body)) == expected


@pytest.mark.parametrize("value", (None, "invalid", [], {}))
def test_invalid_limit_returns_unclamped_default(adapter, value):
    assert adapter._safe_limit(value, default=42, maximum=7) == 42
    assert adapter._safe_limit(value, default=0, maximum=7) == 0


@pytest.mark.parametrize(
    ("value", "expected"), ((-4, 1), (0, 1), ("99", 7), (3.9, 3), (True, 1))
)
def test_coercible_limit_is_clamped(adapter, value, expected):
    assert adapter._safe_limit(value, default=42, maximum=7) == expected


def test_limit_overflow_is_not_swallowed(adapter):
    with pytest.raises(OverflowError):
        adapter._safe_limit(float("inf"), default=42, maximum=7)
