"""Native-KB metadata interpretation shared by two independent applications."""

import uuid
from collections import UserDict
from importlib import import_module

import pytest


PROJECT_ID = "1a387b4d-1111-2222-3333-444444444444"


@pytest.fixture(
    params=["agent.services.knowledge.bindings", "orchestrator.services.kb_datasources"]
)
def native_project_id(request):
    return import_module(request.param).native_kb_project_id


@pytest.mark.parametrize(
    "datasource, expected",
    [
        pytest.param(None, None, id="missing-row"),
        pytest.param({}, None, id="empty-row"),
        pytest.param({"type": "kb"}, None, id="missing-config"),
        pytest.param({"config": None}, None, id="null-config"),
        pytest.param({"config": {}}, None, id="empty-config"),
        pytest.param({"config": []}, None, id="empty-list-config"),
        pytest.param({"config": [PROJECT_ID]}, None, id="list-config"),
        pytest.param({"config": '{"native_project_id": "p"}'}, None, id="json-config"),
        pytest.param(
            {"config": UserDict(native_project_id=PROJECT_ID)},
            None,
            id="mapping-config",
        ),
        pytest.param({"config": {"project_id": PROJECT_ID}}, None, id="unrelated-key"),
        pytest.param({"config": {"native_project_id": None}}, None, id="null-marker"),
        pytest.param({"config": {"native_project_id": ""}}, None, id="empty-marker"),
        pytest.param({"config": {"native_project_id": False}}, None, id="false-marker"),
        pytest.param({"config": {"native_project_id": 0}}, None, id="zero-marker"),
        pytest.param(
            {"config": {"native_project_id": []}}, None, id="empty-list-marker"
        ),
        pytest.param(
            {"type": "kb", "config": {"native_project_id": PROJECT_ID}},
            PROJECT_ID,
            id="native-kb",
        ),
        pytest.param(
            {"type": "other", "config": {"native_project_id": PROJECT_ID}},
            PROJECT_ID,
            id="type-validation-belongs-to-caller",
        ),
        pytest.param(
            {"config": {"native_project_id": uuid.UUID(PROJECT_ID)}},
            PROJECT_ID,
            id="uuid-marker",
        ),
        pytest.param(
            {"config": {"native_project_id": "  "}}, "  ", id="untrimmed-marker"
        ),
        pytest.param(
            {"config": {"native_project_id": 123}}, "123", id="integer-marker"
        ),
        pytest.param({"config": {"native_project_id": True}}, "True", id="true-marker"),
        pytest.param(
            {"config": {"native_project_id": [PROJECT_ID]}},
            f"['{PROJECT_ID}']",
            id="truthy-marker-is-not-validated",
        ),
    ],
)
def test_native_marker_interpretation(native_project_id, datasource, expected):
    assert native_project_id(datasource) == expected


@pytest.mark.parametrize("datasource", ["not a row", [PROJECT_ID]])
def test_truthy_nonmapping_rows_keep_the_existing_error(native_project_id, datasource):
    with pytest.raises(AttributeError, match="get"):
        native_project_id(datasource)
