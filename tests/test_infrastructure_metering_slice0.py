from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from orchestrator.services.infrastructure_metering.capabilities import (
    REQUIRED_APP_INDEXES,
    REQUIRED_APP_TABLES,
    REQUIRED_APP_TRIGGER_RELATIONS,
    REQUIRED_APP_TRIGGERS,
    REQUIRED_AUDIT_COLUMNS,
    REQUIRED_AUDIT_CONSTRAINTS,
    REQUIRED_AUDIT_INDEXES,
    REQUIRED_AUDIT_TABLES,
    MeteringSchemaCapabilities,
    probe_schema_capabilities,
)
from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.queries import (
    UsageV2QueryService,
    UsageVisibility,
)
from orchestrator.services.infrastructure_metering.types import (
    UsageCoverageV2,
    UsageLedgerCostV2,
    UsageRowV2,
    UsageWindowV2,
    decimal_text,
    ledger_cost,
)
from orchestrator.services.usage_ledger import UsageLedger, UsageRates


def _read_capabilities() -> MeteringSchemaCapabilities:
    return MeteringSchemaCapabilities(
        audit_tables=REQUIRED_AUDIT_TABLES,
        audit_columns=REQUIRED_AUDIT_COLUMNS,
        audit_constraints=REQUIRED_AUDIT_CONSTRAINTS,
        audit_indexes=REQUIRED_AUDIT_INDEXES,
        half_even_function=True,
        dirty_day_trigger=True,
        append_only_trigger=True,
    )


class _CapabilityPool:
    def __init__(self, *, app: bool, append_mode: str = "O"):
        self.app = app
        self.append_mode = append_mode

    async def fetch(self, sql, *params):
        wanted = set(params[0]) if params else set()
        if "information_schema.tables" in sql:
            present = REQUIRED_APP_TABLES if self.app else REQUIRED_AUDIT_TABLES
            return [{"table_name": name} for name in wanted & present]
        if "pg_indexes" in sql:
            present = REQUIRED_APP_INDEXES if self.app else REQUIRED_AUDIT_INDEXES
            return [{"indexname": name} for name in wanted & present]
        if "information_schema.columns" in sql:
            return [{"column_name": name} for name in wanted & REQUIRED_AUDIT_COLUMNS]
        if "pg_constraint" in sql:
            return [{"conname": name} for name in wanted & REQUIRED_AUDIT_CONSTRAINTS]
        if "FROM pg_trigger" in sql:
            if self.app:
                return [
                    {
                        "tgname": name,
                        "enabled": "O",
                        "relname": REQUIRED_APP_TRIGGER_RELATIONS[name],
                    }
                    for name in wanted & REQUIRED_APP_TRIGGERS
                ]
            return [
                {
                    "tgname": "usage_events_rollup_dirty_days",
                    "enabled": "O",
                    "relname": "usage_events",
                },
                {
                    "tgname": "usage_events_append_only_v2",
                    "enabled": self.append_mode,
                    "relname": "usage_events",
                },
            ]
        raise AssertionError(f"unexpected capability fetch: {sql}")

    async def fetchval(self, sql, *_params):
        if "infra_metering_control" in sql:
            return True
        if "to_regprocedure" in sql or "WITH wanted AS" in sql:
            return True
        raise AssertionError(f"unexpected capability fetchval: {sql}")


@pytest.mark.asyncio
async def test_capability_probe_requires_normal_write_triggers_and_seed_rows():
    app = _CapabilityPool(app=True)
    disabled_append = _CapabilityPool(app=False, append_mode="D")
    capabilities = await probe_schema_capabilities(app, disabled_append)  # type: ignore[arg-type]
    assert not capabilities.append_only_trigger
    assert not capabilities.v2_reads_ready
    assert not capabilities.slice0_ready

    capabilities = await probe_schema_capabilities(
        app,
        _CapabilityPool(app=False, append_mode="A"),  # type: ignore[arg-type]
    )
    assert capabilities.append_only_trigger
    assert capabilities.app_seed_rows_ready
    assert capabilities.slice0_ready


def test_settings_are_off_by_default_and_publication_fails_closed():
    assert InfrastructureMeteringSettings.from_env({}) == (
        InfrastructureMeteringSettings()
    )

    with pytest.raises(ValueError, match="requires collector, shadow"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "true"}
        )

    with pytest.raises(ValueError, match="shadow mode requires"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_SHADOW_ENABLED": "yes"}
        )


def test_settings_accept_only_explicit_boolean_values():
    with pytest.raises(ValueError, match="must be a boolean"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_V2_READS_ENABLED": "sometimes"}
        )

    settings = InfrastructureMeteringSettings.from_env(
        {
            "INFRASTRUCTURE_METERING_COLLECTOR_ENABLED": "1",
            "INFRASTRUCTURE_METERING_SHADOW_ENABLED": "true",
            "INFRASTRUCTURE_METERING_PUBLICATION_ENABLED": "yes",
            "INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": " dev-cluster ",
        }
    )
    assert settings.publication_enabled
    assert settings.stable_cluster_id == "dev-cluster"

    with pytest.raises(ValueError, match="stable cluster id"):
        InfrastructureMeteringSettings.from_env(
            {"INFRASTRUCTURE_METERING_STABLE_CLUSTER_ID": "not a cluster/id"}
        )


def test_decimal_wire_values_are_exact_and_non_exponent():
    assert decimal_text(Decimal("12.500000")) == "12.5"
    assert decimal_text(Decimal("1E-18")) == "0.000000000000000001"
    assert decimal_text(Decimal("-0.000")) == "0"
    assert decimal_text(Decimal("0.0000000000000000005")) == "0"
    assert decimal_text(Decimal("1.0000000000000000015")) == "1.000000000000000002"
    assert (
        decimal_text(Decimal("99999999999999999999.999999999999999999"))
        == "99999999999999999999.999999999999999999"
    )

    with pytest.raises(ValueError, match=r"NUMERIC\(38,18\)"):
        decimal_text(Decimal("100000000000000000000"))


@pytest.mark.parametrize("value", [0.1, True, "", "NaN", "Infinity"])
def test_decimal_wire_values_reject_inexact_or_non_finite_inputs(value):
    with pytest.raises(ValueError):
        decimal_text(value)


def test_cost_coverage_distinguishes_free_unpriced_and_partial():
    free = ledger_cost(amount=0, priced_quantity="4", unpriced_quantity="0")
    assert free.status == "priced"
    assert free.amount == "0"

    unpriced = ledger_cost(amount=None, priced_quantity="0", unpriced_quantity="4")
    assert unpriced.status == "unpriced"
    assert unpriced.amount is None

    partial = ledger_cost(amount="1.25", priced_quantity="2", unpriced_quantity="2")
    assert partial.status == "partially-priced"
    assert partial.amount == "1.25"

    zero_quantity_unpriced = ledger_cost(
        amount=None,
        priced_quantity="0",
        unpriced_quantity="0",
        priced_events=0,
        unpriced_events=1,
    )
    assert zero_quantity_unpriced.status == "unpriced"

    corrected = ledger_cost(
        amount="1",
        priced_quantity="4",
        unpriced_quantity="0",
        priced_events=1,
        unpriced_events=2,
    )
    assert corrected.status == "priced"

    empty = ledger_cost(amount=None, priced_quantity="0", unpriced_quantity="0")
    assert empty.status == "priced"
    assert empty.amount == "0"

    with pytest.raises(ValidationError, match="requires an amount"):
        ledger_cost(amount=None, priced_quantity="1", unpriced_quantity="0")


def test_typed_contracts_reject_impossible_cross_field_states():
    with pytest.raises(ValidationError, match="requires an amount"):
        UsageLedgerCostV2(
            status="priced",
            amount=None,
            priced_quantity="1",
            unpriced_quantity="0",
        )

    with pytest.raises(ValidationError, match="ledger quantity buckets"):
        UsageRowV2(
            category="compute",
            measurement_basis="scheduler-request",
            cost_domain="workload-allocation",
            resource_class="kubernetes-pod",
            measurement_algorithm="fixture-v1",
            resource="workspace_pod",
            unit="vcpu-hour",
            attribution_scope="customer",
            quantity="2",
            finalized_quantity="1",
            confirmed_provisional_quantity="1",
            ledger_cost=ledger_cost(
                amount=None, priced_quantity="0", unpriced_quantity="1"
            ),
            events=1,
        )

    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="end must be after start"):
        UsageWindowV2(start=now, end=now, as_of=now, data_through=None)
    with pytest.raises(ValidationError, match="cannot exceed"):
        UsageCoverageV2(
            status="partial",
            includes_provisional=False,
            required_sources_ok=2,
            required_sources_total=1,
        )


def test_query_adapter_derives_total_from_quantized_pricing_buckets():
    row = UsageV2QueryService._row(
        {
            "category": "compute",
            "measurement_basis": "scheduler-request",
            "cost_domain": "workload-allocation",
            "resource_class": "kubernetes-pod",
            "measurement_algorithm": "legacy-end-stamped-v1",
            "resource": "workspace_pod",
            "unit": "vcpu-hour",
            "attribution_scope": "customer",
            "quantity": Decimal("0.000000000000000001"),
            "cost_usd": Decimal("0"),
            "priced_quantity": Decimal("0.0000000000000000005"),
            "unpriced_quantity": Decimal("0.0000000000000000005"),
            "priced_events": 1,
            "unpriced_events": 1,
            "events": 2,
        }
    )

    assert row.quantity == "0"
    assert row.ledger_cost.priced_quantity == "0"
    assert row.ledger_cost.unpriced_quantity == "0"


def test_usage_row_rejects_an_unknown_typed_dimension():
    with pytest.raises(ValidationError):
        UsageRowV2(
            category="compute",
            measurement_basis="made-up",
            cost_domain="workload-allocation",
            resource_class="kubernetes-pod",
            measurement_algorithm="fixture-v1",
            resource="workspace_pod",
            unit="vcpu-hour",
            attribution_scope="customer",
            quantity="1",
            finalized_quantity="1",
            confirmed_provisional_quantity="0",
            ledger_cost=ledger_cost(
                amount=None, priced_quantity="0", unpriced_quantity="1"
            ),
            events=1,
        )


class _AuditPool:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return self.rows


class _LedgerPool:
    def __init__(self):
        self.calls = []

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def fetch(self, sql, *params):
                pool.calls.append((sql, params))
                return []

        return _Acquire()


@pytest.mark.asyncio
async def test_v2_summary_keeps_cpu_and_memory_as_separate_decimal_rows():
    audit = _AuditPool(
        [
            {
                "category": "compute",
                "measurement_basis": "scheduler-request",
                "cost_domain": "workload-allocation",
                "resource_class": "kubernetes-pod",
                "measurement_algorithm": "legacy-end-stamped-v1",
                "resource": "workspace_pod",
                "unit": "vcpu-hour",
                "attribution_scope": "customer",
                "quantity": Decimal("8.000000"),
                "cost_usd": None,
                "priced_quantity": Decimal("0"),
                "unpriced_quantity": Decimal("8"),
                "priced_events": 0,
                "unpriced_events": 1,
                "events": 1,
            },
            {
                "category": "compute",
                "measurement_basis": "scheduler-request",
                "cost_domain": "workload-allocation",
                "resource_class": "kubernetes-pod",
                "measurement_algorithm": "legacy-end-stamped-v1",
                "resource": "workspace_pod",
                "unit": "gib-hour",
                "attribution_scope": "customer",
                "quantity": Decimal("16.000000"),
                "cost_usd": Decimal("0"),
                "priced_quantity": Decimal("16"),
                "unpriced_quantity": Decimal("0"),
                "priced_events": 1,
                "unpriced_events": 0,
                "events": 1,
            },
        ]
    )
    service = UsageV2QueryService(audit, _read_capabilities())
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    user_id = str(uuid4())

    result = await service.summary(
        from_ts=start,
        to_ts=end,
        visibility=UsageVisibility(owner_user_id=user_id),
        as_of=end,
    )

    assert [(row.unit, row.quantity) for row in result.rows] == [
        ("vcpu-hour", "8"),
        ("gib-hour", "16"),
    ]
    assert result.rows[0].ledger_cost.status == "unpriced"
    assert result.rows[1].ledger_cost.status == "priced"
    assert result.rows[1].ledger_cost.amount == "0"
    assert result.coverage.status == "partial"
    assert "live-resource-inventory" in result.coverage.excluded_domains

    sql, params = audit.calls[0]
    assert "period_start IS NULL AND ts >= $1 AND ts < $2" in sql
    assert "EXTRACT(EPOCH" not in sql
    assert "attribution_scope = 'customer'" in sql
    assert params[:3] == (start, end, False)
    assert params[3].hex == user_id.replace("-", "")
    assert result.window.data_through is None
    assert "typed-infrastructure-intervals" in result.coverage.excluded_domains


@pytest.mark.asyncio
async def test_v2_project_scope_narrows_the_identity_visibility_union():
    audit = _AuditPool([])
    service = UsageV2QueryService(
        audit,
        _read_capabilities(),
    )
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    owner_id = str(uuid4())
    visible_project_id = str(uuid4())
    scope_project_id = str(uuid4())

    await service.summary(
        from_ts=start,
        to_ts=end,
        visibility=UsageVisibility(
            owner_user_id=owner_id,
            visible_project_ids=(visible_project_id,),
            scope_project_id=scope_project_id,
        ),
        as_of=end,
    )

    sql, params = audit.calls[0]
    assert "user_id = $4" in sql
    assert "project_id = ANY($5::uuid[])" in sql
    assert "project_id = $6" in sql
    assert params[3] == UUID(owner_id)
    assert params[4] == [UUID(visible_project_id)]
    assert params[5] == UUID(scope_project_id)


@pytest.mark.asyncio
async def test_v1_queries_are_frozen_to_llm_and_workspace_cpu_memory():
    pool = _LedgerPool()
    ledger = UsageLedger(pool, UsageRates(None))
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)

    await ledger.query_usage(from_ts=start, to_ts=end)
    await ledger.query_grouped(from_ts=start, to_ts=end, group_by="model")

    usage_sql = pool.calls[0][0]
    model_sql = pool.calls[1][0]
    assert "category IN ('llm', 'tts', 'stt')" in usage_sql
    assert "resource = 'workspace_pod'" in usage_sql
    assert "unit IN ('vcpu-hour', 'gib-hour')" in usage_sql
    assert "category = 'llm'" in model_sql


@pytest.mark.asyncio
async def test_v1_project_scope_narrows_summary_and_strict_self_views():
    pool = _LedgerPool()
    ledger = UsageLedger(pool, UsageRates(None))
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    end = datetime(2026, 8, 6, tzinfo=timezone.utc)
    owner_id = str(uuid4())
    visible_project_id = str(uuid4())
    scope_project_id = str(uuid4())

    await ledger.query_usage(
        from_ts=start,
        to_ts=end,
        owner_user_id=owner_id,
        visible_project_ids=(visible_project_id,),
        scope_project_id=scope_project_id,
    )
    await ledger.query_grouped(
        from_ts=start,
        to_ts=end,
        group_by="user",
        owner_user_id=owner_id,
        scope_project_id=scope_project_id,
    )

    summary_sql, summary_params = pool.calls[0]
    breakdown_sql, breakdown_params = pool.calls[1]
    assert "user_id = $3" in summary_sql
    assert "project_id = ANY($4::uuid[])" in summary_sql
    assert "project_id = $5" in summary_sql
    assert summary_params[2] == UUID(owner_id)
    assert summary_params[3] == [UUID(visible_project_id)]
    assert summary_params[4] == UUID(scope_project_id)
    assert "user_id = $3" in breakdown_sql
    assert "project_id = $4" in breakdown_sql
    assert breakdown_params[2] == UUID(owner_id)
    assert breakdown_params[3] == UUID(scope_project_id)


@pytest.mark.asyncio
async def test_v2_service_refuses_reads_without_audit_capability():
    service = UsageV2QueryService(_AuditPool([]), MeteringSchemaCapabilities())
    now = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="schema is unavailable"):
        await service.summary(
            from_ts=now,
            to_ts=now.replace(year=now.year + 1),
            visibility=UsageVisibility(),
        )


@pytest.mark.asyncio
async def test_usage_v2_route_is_hidden_while_its_gate_is_off(monkeypatch):
    import main as orchestrator_main

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(),
    )
    auth = AsyncMock()
    monkeypatch.setattr(orchestrator_main, "require_approved_user", auth)

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=30,
            from_date=None,
            to_date=None,
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 404
    auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_v2_restricts_non_customer_rows_to_fleet_admin(monkeypatch):
    import main as orchestrator_main

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": False, "scopes": []}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=30,
            from_date=None,
            to_date=None,
            ref_id=None,
            include_non_customer=True,
        )

    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_usage_v2_fleet_admin_passes_explicit_visibility(monkeypatch):
    import main as orchestrator_main

    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    response = UsageV2QueryService._row(
        {
            "category": "compute",
            "measurement_basis": "scheduler-request",
            "cost_domain": "workload-allocation",
            "resource_class": "kubernetes-pod",
            "measurement_algorithm": "fixture-v1",
            "resource": "workspace_pod",
            "unit": "vcpu-hour",
            "attribution_scope": "customer",
            "quantity": Decimal("1"),
            "cost_usd": None,
            "priced_quantity": Decimal("0"),
            "unpriced_quantity": Decimal("1"),
            "priced_events": 0,
            "unpriced_events": 1,
            "events": 1,
        }
    )
    summary = {
        "schema_version": 2,
        "window": {
            "start": now,
            "end": now.replace(day=7),
            "as_of": now,
            "data_through": now,
        },
        "rows": [response],
        "coverage": {
            "status": "partial",
            "includes_provisional": False,
            "required_sources_ok": 0,
            "required_sources_total": 0,
            "unknown_ranges": [],
            "excluded_domains": [],
        },
    }

    class _Service:
        is_available = True

        def __init__(self):
            self.kwargs = None

        async def summary(self, **kwargs):
            self.kwargs = kwargs
            return summary

    service = _Service()

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=True)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", service)
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_visibility_kwargs_for_stats",
        AsyncMock(return_value={}),
    )

    result = await orchestrator_main.get_usage_v2(
        MagicMock(),
        days=1,
        from_date="2026-08-05T00:00:00Z",
        to_date="2026-08-06T00:00:00Z",
        ref_id=None,
        include_non_customer=True,
    )

    assert result == summary
    assert service.kwargs["visibility"].include_non_customer is True


@pytest.mark.asyncio
async def test_usage_v2_refuses_reads_until_bootstrap_is_complete(monkeypatch):
    import main as orchestrator_main

    class _Service:
        is_available = True

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=False)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", _Service())
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=1,
            from_date="2026-08-05T00:00:00Z",
            to_date="2026-08-06T00:00:00Z",
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 503
    assert "bootstrap incomplete" in raised.value.detail


@pytest.mark.asyncio
async def test_usage_v2_does_not_echo_server_contract_failures_as_client_errors(
    monkeypatch,
):
    import main as orchestrator_main

    class _Service:
        is_available = True

        async def summary(self, **_kwargs):
            raise ValueError("sensitive-invalid-ledger-value")

    class _Rollup:
        async def bootstrap_state(self):
            return MagicMock(read_ready=True)

    monkeypatch.setattr(
        orchestrator_main,
        "infrastructure_metering_settings",
        orchestrator_main.InfrastructureMeteringSettings(v2_reads_enabled=True),
    )
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_v2", _Service())
    monkeypatch.setattr(orchestrator_main, "infrastructure_usage_rollup", _Rollup())
    monkeypatch.setattr(
        orchestrator_main,
        "require_approved_user",
        AsyncMock(return_value={"id": str(uuid4()), "is_admin": True, "scopes": []}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_visibility_kwargs_for_stats",
        AsyncMock(return_value={}),
    )

    with pytest.raises(HTTPException) as raised:
        await orchestrator_main.get_usage_v2(
            MagicMock(),
            days=1,
            from_date="2026-08-05T00:00:00Z",
            to_date="2026-08-06T00:00:00Z",
            ref_id=None,
            include_non_customer=False,
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == "Usage API v2 query failed"
