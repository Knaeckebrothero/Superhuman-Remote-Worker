"""Tests for the neo4j import guard — the orchestrator-image import heisenbug.

The orchestrator image ships WITHOUT the ``neo4j`` package (graph features are
agent-side). ``src/tools/__init__`` eagerly builds the tool registry, which pulls
``knowledge_tools`` → ``knowledge_graph`` → ``src.database.neo4j_db`` →
``from neo4j import GraphDatabase`` — so the FIRST import of anything under
``src.tools`` (e.g. the PR2 chunker, and through it ``services.kb_reindex``)
raised ModuleNotFoundError on the orchestrator. Worse, the failure was
order-dependent: the failed attempt left ``src.tools.knowledge`` cached in
``sys.modules``, so every RETRY succeeded — the KB sweeper died silently at boot
while the post-merge trigger worked after the first failed merge "polluted" the
cache (live-diagnosed on dev 2026-07-05).

The guard defers the failure to Neo4jDB *construction* so the module chain
imports cleanly without neo4j. These tests block ``neo4j`` in a fresh subprocess
(sys.modules[name] = None makes ``import name`` raise ImportError) — which
reproduces the orchestrator image's dependency set even on a dev box that has
neo4j installed.
"""

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BLOCK_NEO4J = (
    "import sys; sys.modules['neo4j'] = None; sys.modules['neo4j.exceptions'] = None; "
)


def _run(code: str, extra_path: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + (os.pathsep + extra_path if extra_path else "")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )


class TestImportChainWithoutNeo4j:
    def test_chunker_imports_without_neo4j(self):
        """The PR2 chunker (under src.tools) must import on a neo4j-less image."""
        proc = _run(_BLOCK_NEO4J + "import src.tools.knowledge.chunker; print('OK')")
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_tools_package_imports_without_neo4j(self):
        """The eager registry chain in src/tools/__init__ must survive."""
        proc = _run(_BLOCK_NEO4J + "import src.tools; print('OK')")
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_kb_reindex_imports_without_neo4j(self):
        """services.kb_reindex (the sweeper's import) must be deterministic."""
        proc = _run(
            _BLOCK_NEO4J + "from services.kb_reindex import reindex_kb; print('OK')",
            extra_path=os.path.join(REPO_ROOT, "orchestrator"),
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_import_is_order_independent(self):
        """Regression for the heisenbug: first-import and retry must agree."""
        proc = _run(
            _BLOCK_NEO4J
            + (
                "first = None\n"
                "try:\n"
                "    import src.tools.knowledge.chunker\n"
                "    first = 'ok'\n"
                "except Exception as e:\n"
                "    first = type(e).__name__\n"
                "import src.tools.knowledge.chunker\n"
                "print('first=' + first + ' retry=ok')\n"
            )
        )
        assert proc.returncode == 0, proc.stderr
        assert "first=ok retry=ok" in proc.stdout


class TestNeo4jDbConstructionGuard:
    def test_construction_raises_without_driver(self, monkeypatch):
        """With the package absent the failure moves to Neo4jDB(...) — loudly."""
        import src.database.neo4j_db as neo4j_db

        monkeypatch.setattr(neo4j_db, "GraphDatabase", None)
        with pytest.raises(RuntimeError, match="neo4j"):
            neo4j_db.Neo4jDB(uri="bolt://x:7687", username="u", password="p")
