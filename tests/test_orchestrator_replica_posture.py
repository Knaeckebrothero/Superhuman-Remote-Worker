"""What replica count the chart ships, and what the PDB does about it.

The chart default optimises for a single-node self-host: `orchestrator.replicas: 1`
and, derived from it, a PodDisruptionBudget that permits eviction. Multi-node
deployments raise the count and the PDB follows.

Both halves have bitten before. `minAvailable: 1` at `replicas: 1` blocks every
voluntary eviction and hangs `kubectl drain` forever -- the 2026-05-16 node-pull
incident that started the HA work. It was a values comment for months, and the
comment did not prevent it; it is derived now, and pinned here.

Spec: knowledge-base/knowledge/superpowers/specs/2026-08-21-cnpg-data-tier-ha-design.md
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm"
PDB = "templates/orchestrator/pdb.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="Helm is not installed"
)


def _render(values: Path, *settings: str, show_only: str | None = None) -> list[dict]:
    command = ["helm", "template", "posture-test", str(CHART), "-f", str(values)]
    if show_only:
        command.extend(["--show-only", show_only])
    for setting in settings:
        command.extend(["--set", setting])
    rendered = subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _ci(*settings: str, show_only: str | None = None) -> list[dict]:
    return _render(CHART / "ci/test-values.yaml", *settings, show_only=show_only)


def _min_available(documents: list[dict]) -> int:
    """The orchestrator's PDB specifically -- the agent ships one too, at 0,
    and it sorts first in a whole-chart render."""
    pdbs = [
        d
        for d in documents
        if d.get("kind") == "PodDisruptionBudget"
        and d["metadata"]["labels"].get("app.kubernetes.io/component") == "orchestrator"
    ]
    assert len(pdbs) == 1
    return pdbs[0]["spec"]["minAvailable"]


def _replicas(documents: list[dict]) -> int:
    deployments = [
        d
        for d in documents
        if d.get("kind") == "Deployment"
        and d["metadata"]["labels"].get("app.kubernetes.io/component") == "orchestrator"
    ]
    assert len(deployments) == 1
    return deployments[0]["spec"]["replicas"]


def test_chart_default_is_a_single_replica():
    """The mini-PC operator is the least likely to edit values.yaml, so the
    bare default is the one that has to suit them."""
    assert (
        yaml.safe_load((CHART / "values.yaml").read_text())["orchestrator"]["replicas"]
        == 1
    )
    assert _replicas(_ci()) == 1


def test_default_pdb_permits_eviction():
    """At replicas:1, minAvailable:1 would block every voluntary eviction and
    hang kubectl drain forever. Shipping the default at 1 without this would
    have made the 2026-05-16 incident the out-of-box experience."""
    assert _min_available(_ci(show_only=PDB)) == 0


def test_raising_replicas_protects_a_leader():
    assert _min_available(_ci("orchestrator.replicas=2", show_only=PDB)) == 1
    assert _min_available(_ci("orchestrator.replicas=3", show_only=PDB)) == 1


def test_minavailable_is_not_configurable():
    """It was, and the warning attached to it was a comment. Setting it now
    must be inert rather than quietly re-arming the incident."""
    assert _min_available(_ci("orchestrator.pdb.minAvailable=1", show_only=PDB)) == 0


# --- the shipped overlay ---------------------------------------------------
#
# deployment/values-local.yaml is gitignored; its committed template is the
# thing to pin. It needs `global.domain` and the licence, which the template
# deliberately leaves to the operator. (The dev instance's replicas:2 posture
# moved to the HomeLab values ConfigMap with the HelmOp cutover and is
# guarded there, not here.)
OVERLAY_SETTINGS = ("global.domain=posture.example.com", "license.acceptTerms=true")


def test_local_overlay_template_stays_at_one():
    documents = _render(
        ROOT / "deployment/values-local.yaml.example", *OVERLAY_SETTINGS
    )
    assert _replicas(documents) == 1
