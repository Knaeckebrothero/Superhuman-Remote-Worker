"""Bind a job's deliverable contract to its authorized datasource selection.

Contract normalization remains in ``deliverable_contracts``. An Officer ticket's
external-repository refusal can deliberately persist a requirement through the
bound admission authority; this stage does not own that transaction or locks.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, Sequence

from fastapi import HTTPException

from orchestrator.services.deliverable_contracts import (
    DeliveryContractConflict,
    prepare_delivery_contract,
)
from orchestrator.services.officer_admission import (
    OfficerAdmissionConflict,
    OfficerAdmissionPreparation,
)

if TYPE_CHECKING:
    from orchestrator.schemas.job_create import JobCreate


class JobDeliveryStore(Protocol):
    async def resolve_datasources_for_thread(
        self, datasource_ids: list[str], project_ids: list[str]
    ) -> list[dict[str, Any]]: ...


class RecordRejectedTicketDeliveryRequirement(Protocol):
    async def __call__(
        self,
        *,
        preparation: OfficerAdmissionPreparation,
        ticket_note_id: str,
        ticket_ready_at: datetime | str,
        required_pr_repositories: Sequence[str],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class JobAdmissionDeliveryDependencies:
    store: JobDeliveryStore
    record_rejected_ticket_delivery_requirement: RecordRejectedTicketDeliveryRequirement


async def prepare_job_admission_delivery(
    *,
    command: "JobCreate",
    context: dict[str, Any],
    datasource_ids: list[str],
    target_project_ids: list[str],
    officer_preparation: OfficerAdmissionPreparation | None,
    ticket_ready_at: datetime | None,
    dependencies: JobAdmissionDeliveryDependencies,
) -> dict[str, Any] | None:
    # Repository binding is needed only for a declared contract. Keep
    # jobs without deliverables on the historical creation path (and
    # avoid turning an optional connector read into a new admission
    # dependency for every job).
    selected_datasources = []
    if command.required_deliverables:
        selected_datasources = await dependencies.store.resolve_datasources_for_thread(
            datasource_ids, target_project_ids
        )
    try:
        delivery_plan = prepare_delivery_contract(
            command.required_deliverables or [],
            datasources=selected_datasources,
        )
    except DeliveryContractConflict as exc:
        if (
            exc.code == "external_repository_requires_pr"
            and officer_preparation is not None
            and command.ticket
            and ticket_ready_at is not None
        ):
            try:
                await dependencies.record_rejected_ticket_delivery_requirement(
                    preparation=officer_preparation,
                    ticket_note_id=str(command.ticket),
                    ticket_ready_at=ticket_ready_at,
                    required_pr_repositories=list(
                        exc.fields.get("required_pr_repositories") or []
                    ),
                )
            except OfficerAdmissionConflict as admission_exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": admission_exc.code,
                        "message": admission_exc.detail,
                        **admission_exc.fields,
                    },
                ) from admission_exc
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, **exc.fields},
        ) from exc

    if delivery_plan.deliverables:
        context["required_deliverables"] = list(delivery_plan.deliverables)
        delivery_contract_record = delivery_plan.as_database_record()
    else:
        context.pop("required_deliverables", None)
        delivery_contract_record = None
    return delivery_contract_record
