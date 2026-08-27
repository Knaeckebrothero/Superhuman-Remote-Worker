"""Canonical inventory of connector types shipped by SRW.

The public product calls these resources connectors; ``datasource`` remains
the internal API and database term. Keep this module descriptive and static:
runtime availability, user grants, and attachment state belong to the later
capability resolver rather than this build-level inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DatasourceRuntimeKind = Literal[
    "generic_env",
    "repository",
    "knowledge",
    "managed_tools",
    "email_tools",
    "mcp_tools",
    "credential_file",
]


@dataclass(frozen=True, slots=True)
class DatasourceTypeDefinition:
    """One supported internal datasource type and its guide coverage decision."""

    type_id: str
    title: str
    guide_topic: str
    runtime_kind: DatasourceRuntimeKind


DATASOURCE_TYPE_CATALOG: tuple[DatasourceTypeDefinition, ...] = (
    DatasourceTypeDefinition("generic", "Generic", "datasources", "generic_env"),
    DatasourceTypeDefinition("repository", "Repository", "datasources", "repository"),
    DatasourceTypeDefinition(
        "kb",
        "OKF Knowledge Base",
        "datasources-okf",
        "knowledge",
    ),
    DatasourceTypeDefinition(
        "postgresql",
        "PostgreSQL",
        "datasources",
        "managed_tools",
    ),
    DatasourceTypeDefinition("neo4j", "Neo4j", "datasources", "managed_tools"),
    DatasourceTypeDefinition("mongodb", "MongoDB", "datasources", "managed_tools"),
    DatasourceTypeDefinition("webdav", "WebDAV", "datasources", "managed_tools"),
    DatasourceTypeDefinition("email", "Email", "datasources-email", "email_tools"),
    DatasourceTypeDefinition("mcp", "MCP Server", "datasources", "mcp_tools"),
    DatasourceTypeDefinition(
        "kubeconfig",
        "Kubeconfig",
        "datasources",
        "credential_file",
    ),
    DatasourceTypeDefinition("ssh_key", "SSH Key", "datasources", "credential_file"),
    DatasourceTypeDefinition(
        "generic_file",
        "Generic file",
        "datasources",
        "credential_file",
    ),
)

DATASOURCE_TYPE_IDS: tuple[str, ...] = tuple(
    definition.type_id for definition in DATASOURCE_TYPE_CATALOG
)
DATASOURCE_TYPES: frozenset[str] = frozenset(DATASOURCE_TYPE_IDS)

if len(DATASOURCE_TYPE_IDS) != len(DATASOURCE_TYPES):
    raise RuntimeError("Duplicate type_id in DATASOURCE_TYPE_CATALOG")


__all__ = [
    "DATASOURCE_TYPE_CATALOG",
    "DATASOURCE_TYPE_IDS",
    "DATASOURCE_TYPES",
    "DatasourceRuntimeKind",
    "DatasourceTypeDefinition",
]
