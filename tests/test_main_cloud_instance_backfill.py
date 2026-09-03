"""The pre-0186 instance-authority backfill — mostly its refusals.

Stamping a row with an installation UUID is a one-way act: from then on every
effect (member grants, shares, folder deletes) is dispatched against that
installation and nothing downstream questions it again. A wrong stamp is
therefore strictly worse than the fail-closed 500 it replaces, which is why
this endpoint is a re-attested, admin-gated operation rather than a SQL
migration — psql cannot obtain a live installation proof.

These tests exist to keep the refusals refusing. The happy path is one test;
the rest pin the conditions under which we must decline to guess.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException


PROOF = "025b48d99509423b4b10d0b8963270a2b0202f167345f1f506eb798f884c4768"
OTHER_PROOF = "f" * 64
ACTIVE_INSTANCE = "4e72e665-1f70-4b69-9804-d981b51416e6"


def _authority(backend_id="nextcloud", instance_id=ACTIVE_INSTANCE, proof=PROOF):
    return SimpleNamespace(
        backend_id=backend_id,
        backend_instance_id=instance_id,
        installation_proof_sha256=proof,
    )


def _db(*, projects=None, threads=None, registry=None, active=None):
    db = MagicMock()
    db.survey_unstamped_main_cloud_rows = AsyncMock(
        return_value={
            "projects": projects if projects is not None else [],
            "threads": threads if threads is not None else [],
        }
    )
    db.list_main_cloud_backend_instances = AsyncMock(
        return_value=registry if registry is not None else [_authority()]
    )
    db.get_active_main_cloud_backend_instance = AsyncMock(
        return_value=(
            active
            if active is not None
            else {"authority": _authority(), "activation_revision": 2}
        )
    )
    db.stamp_main_cloud_instance_authority = AsyncMock(
        return_value={"projects": 0, "threads": 0}
    )
    return db


def _legacy_project(name="Better Resavio", provider="nextcloud"):
    return {
        "id": uuid4(),
        "name": name,
        "status": "active",
        "main_cloud_backend": provider,
        "main_cloud_folder_handle": "nextcloud:12345",
    }


def _run(db, *, apply=False, reattest=True):
    """Call the endpoint with the admin gate and re-attestation stubbed."""
    import main

    return patch.multiple(
        main,
        _require_admin=AsyncMock(return_value={"id": "admin"}),
        postgres_db=db,
        reload_active_main_cloud_instance=AsyncMock(return_value=reattest),
    ), apply


class TestBackfillRefusals:
    @pytest.mark.asyncio
    async def test_two_distinct_installations_abort(self, fake_request):
        """Two *proofs* for one provider is the real ambiguity — refuse."""
        from main import backfill_main_cloud_instance_authority

        db = _db(
            projects=[_legacy_project()],
            registry=[
                _authority(proof=PROOF),
                _authority(instance_id=str(uuid4()), proof=OTHER_PROOF),
            ],
        )
        ctx, _ = _run(db)
        with ctx:
            with pytest.raises(HTTPException) as exc:
                await backfill_main_cloud_instance_authority(fake_request, apply=True)
        assert exc.value.status_code == 409
        assert "distinct" in exc.value.detail
        db.stamp_main_cloud_instance_authority.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_routing_snapshots_of_one_installation_are_not_ambiguous(
        self, fake_request
    ):
        """The dev-cluster shape: two registry rows, one installation.

        Routing snapshots multiply when a non-secret setting changes. They
        share an installation proof, and the folders live in the installation
        — not in the snapshot — so this must NOT abort.
        """
        from main import backfill_main_cloud_instance_authority

        db = _db(
            projects=[_legacy_project()],
            registry=[
                _authority(instance_id=str(uuid4()), proof=PROOF),
                _authority(instance_id=ACTIVE_INSTANCE, proof=PROOF),
            ],
        )
        db.stamp_main_cloud_instance_authority = AsyncMock(
            return_value={"projects": 1, "threads": 0}
        )
        ctx, _ = _run(db)
        with ctx:
            result = await backfill_main_cloud_instance_authority(
                fake_request, apply=True
            )
        assert result["status"] == "ok"
        assert result["projects"] == 1

    @pytest.mark.asyncio
    async def test_non_active_provider_refuses(self, fake_request):
        """A provider we cannot re-attest right now is never stamped."""
        from main import backfill_main_cloud_instance_authority

        db = _db(
            projects=[_legacy_project(provider="opencloud")],
            registry=[_authority(backend_id="opencloud", proof=PROOF)],
            active={"authority": _authority(backend_id="nextcloud")},
        )
        ctx, _ = _run(db)
        with ctx:
            with pytest.raises(HTTPException) as exc:
                await backfill_main_cloud_instance_authority(fake_request, apply=True)
        assert exc.value.status_code == 409
        assert "not the active backend" in exc.value.detail
        db.stamp_main_cloud_instance_authority.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_registry_proof_must_match_the_live_attestation(self, fake_request):
        """Registry says one installation, the live probe says another."""
        from main import backfill_main_cloud_instance_authority

        db = _db(
            projects=[_legacy_project()],
            registry=[_authority(proof=OTHER_PROOF)],
            active={"authority": _authority(proof=PROOF)},
        )
        ctx, _ = _run(db)
        with ctx:
            with pytest.raises(HTTPException) as exc:
                await backfill_main_cloud_instance_authority(fake_request, apply=True)
        assert exc.value.status_code == 409
        assert "does not match" in exc.value.detail
        db.stamp_main_cloud_instance_authority.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_reattestation_refuses(self, fake_request):
        """No live proof, no stamp — the proof is the whole safety argument."""
        from main import backfill_main_cloud_instance_authority

        db = _db(projects=[_legacy_project()])
        ctx, _ = _run(db, reattest=False)
        with ctx:
            with pytest.raises(HTTPException) as exc:
                await backfill_main_cloud_instance_authority(fake_request, apply=True)
        assert exc.value.status_code == 409
        db.stamp_main_cloud_instance_authority.assert_not_awaited()


class TestBackfillHappyPath:
    @pytest.mark.asyncio
    async def test_dry_run_is_the_default_and_writes_nothing(self, fake_request):
        from main import backfill_main_cloud_instance_authority

        db = _db(projects=[_legacy_project(), _legacy_project("Test")])
        ctx, _ = _run(db)
        with ctx:
            result = await backfill_main_cloud_instance_authority(fake_request)

        assert result["status"] == "dry_run"
        assert result["applied"] is False
        assert result["projects"] == 2
        assert result["plan"][0]["backend_instance_id"] == ACTIVE_INSTANCE
        db.stamp_main_cloud_instance_authority.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_stamps_and_reports(self, fake_request):
        from main import backfill_main_cloud_instance_authority

        db = _db(
            projects=[_legacy_project()],
            threads=[{"id": uuid4(), "main_cloud_backend": "nextcloud"}],
        )
        db.stamp_main_cloud_instance_authority = AsyncMock(
            return_value={"projects": 1, "threads": 1}
        )
        ctx, _ = _run(db)
        with ctx:
            result = await backfill_main_cloud_instance_authority(
                fake_request, apply=True
            )

        assert result["status"] == "ok"
        assert (result["projects"], result["threads"]) == (1, 1)
        db.stamp_main_cloud_instance_authority.assert_awaited_once_with(
            backend_id="nextcloud", backend_instance_id=ACTIVE_INSTANCE
        )

    @pytest.mark.asyncio
    async def test_nothing_to_do_is_a_noop_without_reattesting(self, fake_request):
        """An already-clean deployment must not be made to probe its cloud."""
        from main import backfill_main_cloud_instance_authority

        db = _db()
        ctx, _ = _run(db)
        with ctx:
            result = await backfill_main_cloud_instance_authority(
                fake_request, apply=True
            )
        assert result["status"] == "noop"
        assert result["applied"] is False
        db.get_active_main_cloud_backend_instance.assert_not_awaited()
        db.stamp_main_cloud_instance_authority.assert_not_awaited()
