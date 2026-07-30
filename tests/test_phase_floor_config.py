"""min_todos flows from yaml config to the live TodoManager gate.

Annex-C regression (officer_blind_reads_and_worker_bureaucracy.md): the
``phase_settings.min_todos`` yaml key used to dead-end in an unread
parameter while every production TodoManager construction omitted it — the
live floor was silently the constructor default (5). These tests pin the
chain: yaml -> PhaseSettings -> construction sites. The gate itself is
covered in test_managers_todo.py::TestTodoManagerFloor.

Also pins the act-ratio tripwire limits parsing (same change).
"""

import re
import sys
from pathlib import Path

import yaml

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.core.loader import load_agent_config_from_dict  # noqa: E402

_BASE = {"agent_id": "a", "display_name": "A"}


class TestPhaseSettingsParsing:
    def test_min_todos_parses_from_dict(self):
        cfg = load_agent_config_from_dict({**_BASE, "phase_settings": {"min_todos": 2}})
        assert cfg.phase_settings.min_todos == 2
        assert cfg.phase_settings.max_todos == 20

    def test_defaults_when_unset(self):
        cfg = load_agent_config_from_dict(dict(_BASE))
        assert cfg.phase_settings.min_todos == 5
        assert cfg.phase_settings.max_todos == 20

    def test_worker_base_sets_floor_of_two(self):
        data = yaml.safe_load(
            (project_root / "config" / "worker_base.yaml").read_text()
        )
        assert data["phase_settings"]["min_todos"] == 2


class TestConstructionWiring:
    def test_agent_passes_min_todos_at_every_construction_site(self):
        """Every TodoManager(...) in src/agent.py must wire the config floor.

        This is the exact historical bug shape: the yaml key parsed fine but
        no construction site passed it, so the dataclass default silently won.
        """
        source = (project_root / "src" / "agent.py").read_text()
        sites = re.findall(r"TodoManager\((?:[^()]|\([^()]*\))*\)", source)
        assert sites, "expected TodoManager construction sites in src/agent.py"
        for site in sites:
            assert "min_todos=" in site, f"TodoManager call missing min_todos: {site}"


class TestActRatioLimitsParsing:
    def test_knobs_parse(self):
        cfg = load_agent_config_from_dict(
            {
                **_BASE,
                "limits": {
                    "act_ratio_nudge_threshold": 4,
                    "process_artifact_patterns": ["todos.yaml", "notes/*"],
                },
            }
        )
        assert cfg.limits.act_ratio_nudge_threshold == 4
        assert cfg.limits.process_artifact_patterns == ["todos.yaml", "notes/*"]

    def test_defaults_when_unset(self):
        cfg = load_agent_config_from_dict(dict(_BASE))
        assert cfg.limits.act_ratio_nudge_threshold == 6
        assert cfg.limits.process_artifact_patterns == [
            "todos.yaml",
            "plan.md",
            "archive/*",
            "*retrospective*",
        ]

    def test_non_list_patterns_fall_back_to_default(self):
        cfg = load_agent_config_from_dict(
            {**_BASE, "limits": {"process_artifact_patterns": "todos.yaml"}}
        )
        assert cfg.limits.process_artifact_patterns == [
            "todos.yaml",
            "plan.md",
            "archive/*",
            "*retrospective*",
        ]
