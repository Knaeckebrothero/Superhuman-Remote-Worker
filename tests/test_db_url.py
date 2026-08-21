"""Contract tests for the duplicated Postgres DSN builder.

``build_postgres_url`` exists byte-identically in two trees so the orchestrator
image need not bundle the agent ``src/`` tree. These tests exercise both copies
and assert they have not drifted.

asyncpg's accepted ``sslmode`` values were verified directly against the
installed 0.31.0: ``disable, allow, prefer, require, verify-ca, verify-full``.
With no ``sslmode`` the driver defaults to ``prefer`` (verify_mode=0,
check_hostname=False); ``verify-full`` plus ``sslrootcert`` yields verify_mode=2
and check_hostname=True.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_COPY = ROOT / "orchestrator" / "utils" / "db_url.py"
AGENT_COPY = ROOT / "src" / "utils" / "db_url.py"

SSL_MODES = ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]


def _load(path: Path):
    """Exec one module's source in a bare namespace.

    Importing ``orchestrator.utils.db_url`` and ``src.utils.db_url`` in the same
    process would rely on two package roots resolving unambiguously, which the
    flattened orchestrator image deliberately breaks. The module has no
    intra-repo imports, so exec'ing the file is sufficient and isolated.
    """
    namespace: dict = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace["build_postgres_url"]


def _function_source(path: Path, name: str) -> str:
    source = path.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"{name} not found in {path}")


@pytest.fixture(params=[ORCHESTRATOR_COPY, AGENT_COPY], ids=["orchestrator", "agent"])
def build(request):
    return _load(request.param)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("POSTGRES_", "VECTOR_POSTGRES_", "AUDIT_POSTGRES_")) or key == "DATABASE_URL":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_USER", "srw")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("POSTGRES_HOST", "pg.example.com")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "srw")
    return monkeypatch


def test_no_ssl_env_leaves_the_dsn_unchanged(build):
    assert build("POSTGRES") == "postgresql://srw:pw@pg.example.com:5432/srw"


def test_sslmode_is_appended_as_a_query_parameter(build, clean_env):
    clean_env.setenv("POSTGRES_SSLMODE", "require")
    assert build("POSTGRES") == "postgresql://srw:pw@pg.example.com:5432/srw?sslmode=require"


def test_sslrootcert_is_appended_after_sslmode(build, clean_env):
    clean_env.setenv("POSTGRES_SSLMODE", "verify-full")
    clean_env.setenv("POSTGRES_SSLROOTCERT", "/etc/ssl/certs/pg-ca.crt")
    assert build("POSTGRES") == (
        "postgresql://srw:pw@pg.example.com:5432/srw"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Fssl%2Fcerts%2Fpg-ca.crt"
    )


def test_sslrootcert_alone_is_appended(build, clean_env):
    clean_env.setenv("POSTGRES_SSLROOTCERT", "/ca.crt")
    assert build("POSTGRES") == "postgresql://srw:pw@pg.example.com:5432/srw?sslrootcert=%2Fca.crt"


def test_ssl_env_is_not_appended_to_a_fallback_dsn(build, clean_env):
    clean_env.delenv("POSTGRES_USER")
    clean_env.delenv("POSTGRES_PASSWORD")
    clean_env.setenv("POSTGRES_SSLMODE", "require")
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d?application_name=srw")
    assert build("POSTGRES", fallback_env="DATABASE_URL") == (
        "postgresql://u:p@h:5432/d?application_name=srw"
    )


def test_ssl_env_is_scoped_to_its_prefix(build, clean_env):
    clean_env.setenv("VECTOR_POSTGRES_SSLMODE", "require")
    assert build("POSTGRES") == "postgresql://srw:pw@pg.example.com:5432/srw"


def test_credentials_are_still_quoted_alongside_ssl(build, clean_env):
    clean_env.setenv("POSTGRES_PASSWORD", "a/b@c:d")
    clean_env.setenv("POSTGRES_SSLMODE", "require")
    assert build("POSTGRES") == (
        "postgresql://srw:a%2Fb%40c%3Ad@pg.example.com:5432/srw?sslmode=require"
    )


@pytest.mark.parametrize("mode", SSL_MODES)
def test_asyncpg_accepts_every_sslmode_we_expose(build, clean_env, mode):
    """Prove the DSN is valid for the driver, not merely well-shaped.

    Connects to a closed port so the attempt fails fast. Any error is fine --
    what must NOT happen is a configuration error about sslmode itself.
    """
    asyncpg = pytest.importorskip("asyncpg")
    clean_env.setenv("POSTGRES_HOST", "127.0.0.1")
    clean_env.setenv("POSTGRES_PORT", "1")
    clean_env.setenv("POSTGRES_SSLMODE", mode)
    dsn = build("POSTGRES")
    with pytest.raises(Exception) as excinfo:
        asyncio.run(asyncpg.connect(dsn=dsn, timeout=0.5))
    assert "sslmode" not in str(excinfo.value).lower()


def test_both_trees_define_an_identical_build_postgres_url():
    assert _function_source(ORCHESTRATOR_COPY, "build_postgres_url") == _function_source(
        AGENT_COPY, "build_postgres_url"
    ), (
        "build_postgres_url has drifted between orchestrator/utils/db_url.py and "
        "src/utils/db_url.py. The duplication is deliberate; keep both in sync."
    )
