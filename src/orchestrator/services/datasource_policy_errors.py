"""Datasource policy failures shared by persistence and admission callers."""


class DatasourcePolicyError(ValueError):
    """Base class for datasource scope/default persistence failures."""


class DatasourcePolicyValidationError(DatasourcePolicyError):
    """A requested datasource policy is structurally invalid."""


class DatasourcePolicyConflictError(DatasourcePolicyError):
    """The caller edited a stale datasource policy revision."""


class DatasourceProjectAuthorizationError(DatasourcePolicyError):
    """A project-link addition lost its required owner authority."""


class DatasourceScopeAuthorizationError(DatasourcePolicyError):
    """A project-scoped principal attempted a cross-scope datasource mutation."""


class DatasourceMaterializationAuthorizationError(DatasourcePolicyError):
    """A work owner lost approval or target-project access before insert."""
