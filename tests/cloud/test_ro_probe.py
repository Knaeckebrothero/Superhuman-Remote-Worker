from __future__ import annotations
import pytest

from orchestrator.services.cloud.ro_probe import (
    probe_read_only, RoProbeResult, MUTATING_VERBS,
)


class _FakeResp:
    def __init__(self, status): self.status_code = status


class _FakeClient:
    """Returns a per-verb status from a dict; defaults to 403 (rejected)."""
    def __init__(self, statuses): self._s = statuses
    async def request(self, method, url, **kw):
        return _FakeResp(self._s.get(method, 403))


@pytest.mark.asyncio
async def test_all_verbs_rejected_is_ok():
    res = await probe_read_only(_FakeClient({}), "https://cloud/dav", "folder/")
    assert isinstance(res, RoProbeResult)
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_any_write_success_fails_closed():
    # PUT unexpectedly accepted (201) -> not read-only
    res = await probe_read_only(_FakeClient({"PUT": 201}), "https://cloud/dav", "f/")
    assert res.ok is False
    assert "PUT" in res.failures[0]


@pytest.mark.asyncio
async def test_side_channel_verbs_are_probed():
    # versions/trash restore CVE class must be in the verb set
    assert any("MOVE" == v[0] for v in MUTATING_VERBS)
    assert any("restore" in (v[1] or "").lower() for v in MUTATING_VERBS)
