"""Tests for the C2 verifiable-capture primitives on ``SnapshotService``:

- ``verify_snapshot`` — the fail-safe gate a later task (C4) calls before the
  irreversible ``delete_workspace_pvc``. Anything unverifiable (no manifest,
  missing object, size mismatch, deep-verify with no stored checksum, hash
  mismatch) must return ``(False, reason)``; only an all-good result returns
  ``(True, "ok")``. A buggy ``True`` here would let a caller destroy the only
  remaining copy of unrecoverable data, so every negative branch is exercised
  explicitly rather than inferred from the happy path.
- ``_streaming_sha256_from_s3`` — the O(1)-memory re-hash helper backing the
  deep check. Never buffers the whole (possibly multi-GB) object.
- The post-upload size check in ``upload_snapshot`` — a truncated/partial
  multipart upload must never advertise ``"available"``.

See docs/features/workspace_durability_tiering.md §C2.
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from orchestrator.services.snapshot_service import SnapshotService

# A payload bigger than one 1 MB read-chunk, so a correct streaming
# implementation is forced to call .read() more than once — a single
# `body.read()` (whole object) would silently "work" on a small fixture and
# only blow up in production on a multi-GB archive.
_BIG_BYTES = b"x" * (1024 * 1024) + b"tail-bytes-not-a-full-chunk"
_BIG_SHA = hashlib.sha256(_BIG_BYTES).hexdigest()

GOOD_MANIFEST = {
    "checksum_sha256": _BIG_SHA,
    "size_compressed_bytes": len(_BIG_BYTES),
}


def _not_found(op_name: str = "HeadObject") -> ClientError:
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op_name)


class _FakeS3Body:
    """Stand-in for boto3's ``StreamingBody``.

    ``read(n)`` is mandatory (no default) — a regression to an unbounded
    ``body.read()`` call (no size arg) raises ``TypeError`` here, failing
    loudly instead of silently "working" against a tiny test fixture. Each
    call returns at most ``n`` bytes and the stream naturally empties over
    successive calls, exactly like the real S3 object body contract that
    ``_streaming_sha256_from_s3``'s chunked-read loop depends on.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.calls: list[int] = []

    def read(self, n: int) -> bytes:
        self.calls.append(n)
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


@pytest.fixture
def svc() -> SnapshotService:
    s = SnapshotService()
    s._available = True
    s._bucket = "test-bucket"
    s._s3 = MagicMock()
    return s


@pytest.fixture(autouse=True)
def _clean_verify_deep_env(monkeypatch):
    """Every test either passes ``deep=`` explicitly or opts into the env
    default deliberately — never let an ambient ``SNAPSHOT_VERIFY_DEEP``
    leak in from the outer environment.
    """
    monkeypatch.delenv("SNAPSHOT_VERIFY_DEEP", raising=False)


# =============================================================================
# _streaming_sha256_from_s3
# =============================================================================


class TestStreamingSha256FromS3:
    def test_returns_correct_hash_for_known_blob(self, svc):
        body = _FakeS3Body(_BIG_BYTES)
        svc._s3.get_object = MagicMock(return_value={"Body": body})

        got = svc._streaming_sha256_from_s3("jobs/j1/env.tar.zst")

        assert got == _BIG_SHA
        svc._s3.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="jobs/j1/env.tar.zst"
        )

    def test_reads_body_in_bounded_chunks_not_all_at_once(self, svc):
        """O(1)-memory guard: repeated bounded reads, never a single
        unbounded read() of the whole object.
        """
        body = _FakeS3Body(_BIG_BYTES)
        svc._s3.get_object = MagicMock(return_value={"Body": body})

        svc._streaming_sha256_from_s3("jobs/j1/env.tar.zst")

        assert len(body.calls) >= 2, "expected multiple chunked reads"
        assert all(n == 1024 * 1024 for n in body.calls), (
            f"each read() must request a bounded 1 MB chunk, got {body.calls}"
        )


# =============================================================================
# verify_snapshot — fail-safe truth table
# =============================================================================


class TestVerifySnapshot:
    @pytest.mark.asyncio
    async def test_s3_unavailable_is_unverifiable(self, svc):
        svc._available = False
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))

        result = await svc.verify_snapshot("job-1")

        assert result == (False, "s3 unavailable")
        svc.get_manifest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_manifest_is_unverifiable(self, svc):
        svc.get_manifest = AsyncMock(return_value=None)

        result = await svc.verify_snapshot("job-1")

        assert result == (False, "no manifest")

    @pytest.mark.asyncio
    async def test_get_manifest_raising_is_unverifiable_not_raised(self, svc):
        """``get_manifest`` wraps ``ClientError`` internally, but a
        non-ClientError (e.g. a corrupt-JSON decode error, or a transient
        connection failure) could still escape it. The gate must still
        return a verdict rather than propagate — a caller authorizing a
        PVC delete on this result must never have to handle a raise itself.
        """
        svc.get_manifest = AsyncMock(side_effect=ValueError("bad manifest json"))

        ok, reason = await svc.verify_snapshot("job-1")

        assert ok is False
        assert "verify error" in reason

    @pytest.mark.asyncio
    async def test_missing_object_is_unverifiable(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(side_effect=_not_found())

        result = await svc.verify_snapshot("job-1")

        assert result == (False, "object missing")

    @pytest.mark.asyncio
    async def test_size_mismatch_is_unverifiable(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        # Deliberately no "ETag" key at all: if the implementation ever
        # touched head["ETag"] for integrity (the multipart caveat this gate
        # must never regress on), this dict raises KeyError instead of
        # silently supplying a plausible-looking value.
        svc._s3.head_object = MagicMock(return_value={"ContentLength": 123})

        ok, reason = await svc.verify_snapshot("job-1")

        assert ok is False
        assert "size mismatch" in reason
        assert "123" in reason
        assert str(len(_BIG_BYTES)) in reason
        # Size check fails closed before any hash is attempted.
        svc._s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_deep_with_no_stored_checksum_is_unverifiable(self, svc):
        """Legacy manifest (pre-checksum) — deep verify must fail closed,
        never treat "nothing to compare" as "verified".
        """
        legacy_manifest = {"size_compressed_bytes": len(_BIG_BYTES)}
        svc.get_manifest = AsyncMock(return_value=legacy_manifest)
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})

        ok, reason = await svc.verify_snapshot("job-1", deep=True)

        assert ok is False
        assert "no checksum in manifest" in reason
        svc._s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_deep_hash_mismatch_is_unverifiable(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock(
            return_value={"Body": _FakeS3Body(b"corrupted-different-bytes")}
        )

        result = await svc.verify_snapshot("job-1", deep=True)

        assert result == (False, "sha256 mismatch")

    @pytest.mark.asyncio
    async def test_deep_verify_streaming_hash_client_error_is_unverifiable_not_raised(
        self, svc
    ):
        """TOCTOU guard: the object can vanish between the HEAD and the GET
        (e.g. a concurrent GC purge). The deep re-hash must fail closed —
        return ``(False, reason)`` — rather than let the ClientError escape
        the gate. A later task authorizes deleting a PVC on this verdict;
        an unhandled raise here would defer that safety decision to an
        unwritten caller.
        """
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._streaming_sha256_from_s3 = MagicMock(side_effect=_not_found("GetObject"))

        ok, reason = await svc.verify_snapshot("job-1", deep=True)

        assert ok is False
        assert "verify error" in reason

    @pytest.mark.asyncio
    async def test_deep_verify_streaming_hash_generic_exception_is_unverifiable_not_raised(
        self, svc
    ):
        """Same guard, non-ClientError case: any transient S3 5xx/timeout/
        connection-reset during the deep re-hash must also fail closed
        instead of raising out of the gate.
        """
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._streaming_sha256_from_s3 = MagicMock(
            side_effect=ConnectionResetError("connection reset by peer")
        )

        ok, reason = await svc.verify_snapshot("job-1", deep=True)

        assert ok is False
        assert "verify error" in reason

    @pytest.mark.asyncio
    async def test_all_good_deep_true_is_ok(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock(return_value={"Body": _FakeS3Body(_BIG_BYTES)})

        result = await svc.verify_snapshot("job-1", deep=True)

        assert result == (True, "ok")
        svc.get_manifest.assert_awaited_once_with("job-1", entity_type="jobs")
        svc._s3.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="jobs/job-1/env.tar.zst"
        )

    @pytest.mark.asyncio
    async def test_deep_false_skips_hash_and_passes_on_size_alone(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock()  # must never be reached

        result = await svc.verify_snapshot("job-1", deep=False)

        assert result == (True, "ok")
        svc._s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_size_in_manifest_skips_size_check(self, svc):
        """``size_compressed_bytes`` absent => nothing to compare against,
        so the size check is a no-op (mirrors the "when present" guard in
        ``upload_snapshot``'s own post-upload check) and deep verify
        proceeds straight to the hash.
        """
        manifest = {"checksum_sha256": _BIG_SHA}  # no size_compressed_bytes
        svc.get_manifest = AsyncMock(return_value=manifest)
        # Deliberately mismatched — must never be consulted since want_size
        # is falsy.
        svc._s3.head_object = MagicMock(return_value={"ContentLength": 999999})
        svc._s3.get_object = MagicMock(return_value={"Body": _FakeS3Body(_BIG_BYTES)})

        result = await svc.verify_snapshot("job-1", deep=True)

        assert result == (True, "ok")

    @pytest.mark.asyncio
    async def test_default_deep_reads_env_true(self, svc, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_VERIFY_DEEP", "true")
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock(return_value={"Body": _FakeS3Body(_BIG_BYTES)})

        result = await svc.verify_snapshot("job-1")  # deep omitted

        assert result == (True, "ok")
        svc._s3.get_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_deep_reads_env_false_skips_hash(self, svc, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_VERIFY_DEEP", "false")
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock()  # must never be reached

        result = await svc.verify_snapshot("job-1")  # deep omitted

        assert result == (True, "ok")
        svc._s3.get_object.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["1", "yes", "on", "YES", "On"])
    async def test_env_var_accepts_common_truthy_tokens(self, svc, monkeypatch, value):
        """An operator setting ``SNAPSHOT_VERIFY_DEEP=1`` (or ``yes``/``on``,
        any case) intends "enable" — treating it as falsy would silently
        disable deep verification and weaken a data-safety gate.
        """
        monkeypatch.setenv("SNAPSHOT_VERIFY_DEEP", value)
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock(return_value={"Body": _FakeS3Body(_BIG_BYTES)})

        result = await svc.verify_snapshot("job-1")  # deep omitted

        assert result == (True, "ok")
        svc._s3.get_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_entity_type_threads_changes_object_key(self, svc):
        svc.get_manifest = AsyncMock(return_value=dict(GOOD_MANIFEST))
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_BIG_BYTES)})
        svc._s3.get_object = MagicMock(return_value={"Body": _FakeS3Body(_BIG_BYTES)})

        result = await svc.verify_snapshot("thread-1", entity_type="threads", deep=True)

        assert result == (True, "ok")
        svc.get_manifest.assert_awaited_once_with("thread-1", entity_type="threads")
        svc._s3.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="threads/thread-1/env.tar.zst"
        )


# =============================================================================
# upload_snapshot — post-upload size check
# =============================================================================

_TAR_BYTES = b"fake-archive-bytes-for-testing-only"


class TestUploadSnapshotPostUploadSizeCheck:
    @pytest.fixture
    def tar_path(self, tmp_path) -> str:
        p = tmp_path / "env.tar.zst"
        p.write_bytes(_TAR_BYTES)
        return str(p)

    @pytest.fixture
    def svc(self) -> SnapshotService:
        s = SnapshotService()
        s._available = True
        s._bucket = "test-bucket"
        s._s3 = MagicMock()
        s._set_snapshot_context = AsyncMock()
        return s

    @staticmethod
    def _statuses(mock_set_snapshot_context) -> list:
        return [
            call.args[1].get("status")
            for call in mock_set_snapshot_context.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_size_mismatch_fails_closed_before_available(self, svc, tar_path):
        svc._s3.head_object = MagicMock(return_value={"ContentLength": 5})
        manifest = {"size_compressed_bytes": 999}  # != 5

        ok = await svc.upload_snapshot("job-1", tar_path, manifest)

        assert ok is False
        statuses = self._statuses(svc._set_snapshot_context)
        assert "capture_failed" in statuses
        assert "available" not in statuses
        svc._s3.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="jobs/job-1/env.tar.zst"
        )

    @pytest.mark.asyncio
    async def test_matching_size_proceeds_to_available(self, svc, tar_path):
        svc._s3.head_object = MagicMock(return_value={"ContentLength": len(_TAR_BYTES)})
        manifest = {"size_compressed_bytes": len(_TAR_BYTES)}

        ok = await svc.upload_snapshot("job-1", tar_path, manifest)

        assert ok is True
        statuses = self._statuses(svc._set_snapshot_context)
        assert "available" in statuses
        assert "capture_failed" not in statuses

    @pytest.mark.asyncio
    async def test_missing_size_in_manifest_skips_head_check(self, svc, tar_path):
        """No ``size_compressed_bytes`` in the manifest => nothing to
        compare against, so the post-upload check is a no-op and upload
        still succeeds (unchanged happy path for callers that don't set
        it).
        """
        svc._s3.head_object = MagicMock()
        manifest: dict = {}

        ok = await svc.upload_snapshot("job-1", tar_path, manifest)

        assert ok is True
        svc._s3.head_object.assert_not_called()
