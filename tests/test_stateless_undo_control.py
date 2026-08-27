"""Closed receipt contract for lane-free stateless workspace undo."""

from __future__ import annotations

from uuid import UUID

import pytest

from src.shared.thread_controls import control_receipt_result


REQUEST_ID = UUID("aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb")
CLIENT_REQUEST_ID = UUID("cccccccc-4444-4555-8666-dddddddddddd")
TARGET_SHA = "1" * 40
RESTORE_SHA = "2" * 40


def _result(**payload_overrides):
    payload = {
        "request_id": str(REQUEST_ID),
        "client_request_id": str(CLIENT_REQUEST_ID),
        "request_seq": 7,
        "method": "workspace.undo",
        "paths": ["notes.txt", "src/new.py"],
        "restored_to_sha": TARGET_SHA,
        "restore_commit_sha": RESTORE_SHA,
    }
    payload.update(payload_overrides)
    return control_receipt_result(
        request_id=REQUEST_ID,
        client_request_id=CLIENT_REQUEST_ID,
        request_seq=7,
        verb="workspace.undo",
        request_payload={},
        event_kind="files.restored",
        event_payload=payload,
    )


def test_workspace_undo_receipt_is_applied_non_scalar():
    assert _result() == ("applied", None, None)


@pytest.mark.parametrize(
    "override",
    [
        {"request_id": str(UUID(int=9))},
        {"method": "mode.set"},
        {"paths": ["../escape"]},
        {"paths": ["same", "same"]},
        {"paths": "notes.txt"},
        {"restored_to_sha": "short"},
        {"restore_commit_sha": "G" * 40},
    ],
)
def test_workspace_undo_receipt_rejects_malformed_effect(override):
    assert _result(**override) is None


def test_workspace_undo_rejection_receipt_remains_valid():
    assert control_receipt_result(
        request_id=REQUEST_ID,
        client_request_id=CLIENT_REQUEST_ID,
        request_seq=7,
        verb="workspace.undo",
        request_payload={},
        event_kind="control.rejected",
        event_payload={
            "request_id": str(REQUEST_ID),
            "client_request_id": str(CLIENT_REQUEST_ID),
            "request_seq": 7,
            "method": "workspace.undo",
            "error_code": "workspace_undo_unavailable",
        },
    ) == ("rejected", "workspace_undo_unavailable", None)
