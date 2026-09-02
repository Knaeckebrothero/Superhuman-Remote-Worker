"""Tests for orchestrator/services/agent_pod_entrypoint.py.

Security audit 2026-08-27, finding #3: ``config_name`` — caller-controlled
via the job/thread creation APIs — was f-spliced unquoted into the agent
pod's ``sh -c`` entrypoint, in a pod carrying the platform Secret via
``envFrom``. The module under test is the one allow-list for the value and
the one builder of that entrypoint. The provisioner-boundary tests (rejected
before any Kubernetes call, no pod spec built) live next to each provisioner
in test_agent_provisioner.py and test_persistent_provisioner.py.
"""

import shlex
import shutil
import subprocess

import pytest

from orchestrator.services.agent_pod_entrypoint import (
    CONFIG_NAME_MAX_LENGTH,
    InvalidConfigNameError,
    agent_exec_command,
    validate_config_name,
)

# The payloads named in the audit brief, verbatim.
HOSTILE_CONFIG_NAMES = [
    "worker_base; touch /tmp/pwned",
    "$(id)",
    "`id`",
    "a b",
    "../x",
    "x" * 1000,
]


class TestValidateConfigName:
    @pytest.mark.parametrize("hostile", HOSTILE_CONFIG_NAMES)
    def test_rejects_the_audit_payloads(self, hostile):
        with pytest.raises(InvalidConfigNameError):
            validate_config_name(hostile)

    @pytest.mark.parametrize(
        "name",
        [
            "-x",  # argparse would read it as a flag
            "--config",
            "/etc/passwd",  # absolute path = empty leading segment
            "a//b",  # empty middle segment
            "a/",  # empty trailing segment
            "a/../b",  # traversal inside the path
            "..",
            "name\n",
            "name\ttab",
            "scholar'",
            'scholar"',
            "scholar\\",
            "schölar",
            "a|b",
            "a&b",
            "a>b",
            "a<b",
            "a;b",
            "a$b",
            "a*b",
            "a?b",
            "a~b",
            "x" * (CONFIG_NAME_MAX_LENGTH + 1),
        ],
    )
    def test_rejects_flags_shell_syntax_bad_paths_and_overlong_names(self, name):
        with pytest.raises(InvalidConfigNameError):
            validate_config_name(name)

    @pytest.mark.parametrize("value", [123, ["scholar"], b"scholar"])
    def test_rejects_non_strings(self, value):
        with pytest.raises(InvalidConfigNameError):
            validate_config_name(value)

    @pytest.mark.parametrize(
        "name",
        [
            "worker_base",
            "session_base",
            "scholar",
            "general-worker",
            "product-qa",
            "config/experts/scholar/config.yaml",
            "config/experts/scholar.yaml",
            # The compatibility aliases canonical_config_name() still maps —
            # the validator must let them through so the alias layer keeps
            # working; it never rewrites them itself.
            "default",
            "defaults",
            "persistent_default",
            "persistent_defaults",
            "experts/default.yaml",
            "a.b_c-d/e",
            "x" * CONFIG_NAME_MAX_LENGTH,
        ],
    )
    def test_accepts_bundled_config_selectors_unchanged(self, name):
        assert validate_config_name(name) == name

    @pytest.mark.parametrize("empty", [None, ""])
    def test_empty_passes_through_so_the_provisioner_default_applies(self, empty):
        assert validate_config_name(empty) == empty

    def test_is_a_value_error(self):
        # persistent_provisioner already raises ValueError for bad input
        # (PERSISTENT_AGENT_IMAGE_PULL_POLICY); callers need no new branch.
        assert issubclass(InvalidConfigNameError, ValueError)

    def test_error_names_the_rule_and_truncates_the_echoed_value(self):
        with pytest.raises(InvalidConfigNameError, match=r"'\.\.' segment"):
            validate_config_name("../x")
        with pytest.raises(InvalidConfigNameError, match="start with '-'"):
            validate_config_name("-x")
        with pytest.raises(InvalidConfigNameError, match="empty segment"):
            validate_config_name("/abs")
        overlong = "a" * 300
        with pytest.raises(InvalidConfigNameError) as info:
            validate_config_name(overlong)
        assert "300 characters" in str(info.value)
        assert overlong not in str(info.value)


class TestAgentExecCommand:
    def test_shape_is_sh_dash_c_exec_of_the_argv(self):
        cmd = agent_exec_command(["python", "agent.py", "--config", "scholar"])
        assert cmd == ["sh", "-c", "exec python agent.py --config scholar"]

    @pytest.mark.parametrize(
        "word",
        HOSTILE_CONFIG_NAMES + ["it's", 'say "hi"', "new\nline", "", "-x", "*"],
    )
    def test_every_word_survives_shlex_as_exactly_one_argument(self, word):
        argv = ["python", "agent.py", "--config", word, "--port", "8001"]
        cmd = agent_exec_command(argv)
        assert cmd[:2] == ["sh", "-c"]
        assert cmd[2].startswith("exec ")
        assert shlex.split(cmd[2]) == ["exec", *argv]

    def test_non_string_words_are_stringified(self):
        cmd = agent_exec_command(["nc", "-z", "host", 8085])
        assert shlex.split(cmd[2]) == ["exec", "nc", "-z", "host", "8085"]

    @pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
    @pytest.mark.parametrize("word", HOSTILE_CONFIG_NAMES)
    def test_a_real_sh_hands_hostile_words_to_the_program_verbatim(
        self, word, tmp_path
    ):
        """shlex's opinion is not the shell's; ask an actual ``sh``.

        The program is ``printf`` so the pod's ``exec`` shape is exercised
        end to end: if any word were interpreted, ``printf`` would print
        something else (or the ``touch`` marker would appear).
        """
        marker = tmp_path / "pwned"
        payload = word.replace("/tmp/pwned", str(marker))
        cmd = agent_exec_command(["printf", "%s\\n", payload])
        run = subprocess.run(cmd, capture_output=True, text=True, check=True)
        assert run.stdout == payload + "\n"
        assert not marker.exists()
