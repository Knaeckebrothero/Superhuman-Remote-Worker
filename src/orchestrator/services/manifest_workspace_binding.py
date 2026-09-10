"""Workspace binding shape validation for request schemas.

Kept apart from manifest_workspace_selection so request schemas can validate a
binding without importing the selection path's manifest authority, resolver and
store — that chain reaches application startup and the schema layer must not.
"""

from jsonschema import Draft202012Validator

from shared.manifests import load_schema
from shared.manifests.validation import check_json_value


def validate_workspace_selection(value: dict | None) -> dict | None:
    """Use the manifest binding schema for HTTP compatibility ingress too."""
    if value is None:
        return None
    check_json_value(value)
    schema = load_schema()
    validator = Draft202012Validator(
        {"$ref": "#/$defs/WorkspaceBinding", "$defs": schema["$defs"]}
    )
    if next(validator.iter_errors(value), None) is not None:
        raise ValueError(
            "workspace must be null or a manifest template/instanceRef binding"
        )
    return value
