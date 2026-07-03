"""HF-5 — per-store connection pool sizing.

The orchestrator's ``PostgresDB`` constructor sizes each store's asyncpg pool
from its OWN env prefix (``POSTGRES`` control plane, ``VECTOR_POSTGRES``,
``AUDIT_POSTGRES``) with a per-store baked-in default. Before this change the
vector and audit instances passed no min/max and silently inherited the
control-plane ``POSTGRES_MIN/MAX_CONNECTIONS`` — so tuning the control pool
resized them too. The defaults also guarantee we never fall through to asyncpg's
own ``create_pool`` default (min 10/max 10).

These assert construction-time state only (``_min_connections`` /
``_max_connections``) — no pool is opened, so no live Postgres is required.
"""

import pytest

from orchestrator.database.postgres import PostgresDB

# Never connected — the constructor only parses config; connect() is separate.
_DSN = "postgresql://t:t@localhost:5432/t"

_POOL_ENV = [
    "POSTGRES_MIN_CONNECTIONS",
    "POSTGRES_MAX_CONNECTIONS",
    "VECTOR_POSTGRES_MIN_CONNECTIONS",
    "VECTOR_POSTGRES_MAX_CONNECTIONS",
    "AUDIT_POSTGRES_MIN_CONNECTIONS",
    "AUDIT_POSTGRES_MAX_CONNECTIONS",
]


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    """Isolate from any ambient ``*_CONNECTIONS`` env so defaults are observable."""
    for name in _POOL_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


class TestDefaults:
    """Baked-in per-store defaults apply when the env vars are unset."""

    def test_control_plane_defaults_2_10(self):
        db = PostgresDB(connection_string=_DSN)
        assert (db._min_connections, db._max_connections) == (2, 10)

    def test_vector_defaults_1_5(self):
        db = PostgresDB(
            connection_string=_DSN,
            env_prefix="VECTOR_POSTGRES",
            default_min_connections=1,
            default_max_connections=5,
        )
        assert (db._min_connections, db._max_connections) == (1, 5)

    def test_audit_defaults_1_4(self):
        db = PostgresDB(
            connection_string=_DSN,
            env_prefix="AUDIT_POSTGRES",
            default_min_connections=1,
            default_max_connections=4,
        )
        assert (db._min_connections, db._max_connections) == (1, 4)


class TestEnvOverride:
    """The store's own env prefix overrides its default; an explicit arg wins."""

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("VECTOR_POSTGRES_MIN_CONNECTIONS", "3")
        monkeypatch.setenv("VECTOR_POSTGRES_MAX_CONNECTIONS", "9")
        db = PostgresDB(
            connection_string=_DSN,
            env_prefix="VECTOR_POSTGRES",
            default_min_connections=1,
            default_max_connections=5,
        )
        assert (db._min_connections, db._max_connections) == (3, 9)

    def test_explicit_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_MIN_CONNECTIONS", "7")
        monkeypatch.setenv("POSTGRES_MAX_CONNECTIONS", "8")
        db = PostgresDB(connection_string=_DSN, min_connections=2, max_connections=4)
        assert (db._min_connections, db._max_connections) == (2, 4)


class TestStoreIsolation:
    """Regression: vector/audit pools must NOT read the control-plane env.

    This is the actual bug HF-5 fixes — bumping ``POSTGRES_MAX_CONNECTIONS`` to
    tune the control plane used to balloon the vector and audit pools too.
    """

    def test_vector_ignores_control_plane_env(self, monkeypatch):
        # Someone bumps the control-plane pool...
        monkeypatch.setenv("POSTGRES_MIN_CONNECTIONS", "20")
        monkeypatch.setenv("POSTGRES_MAX_CONNECTIONS", "40")
        # ...the vector store keeps its own sizing (its env unset -> its default).
        db = PostgresDB(
            connection_string=_DSN,
            env_prefix="VECTOR_POSTGRES",
            default_min_connections=1,
            default_max_connections=5,
        )
        assert (db._min_connections, db._max_connections) == (1, 5)

    def test_audit_ignores_control_plane_env(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_MIN_CONNECTIONS", "20")
        monkeypatch.setenv("POSTGRES_MAX_CONNECTIONS", "40")
        db = PostgresDB(
            connection_string=_DSN,
            env_prefix="AUDIT_POSTGRES",
            default_min_connections=1,
            default_max_connections=4,
        )
        assert (db._min_connections, db._max_connections) == (1, 4)
