"""Tests for the credentials.files[] validator.

The validator lives at ``orchestrator/security/credential_files.py`` and is
invoked from ``orchestrator/main.py`` at the create_datasource /
update_datasource endpoints. It enforces:

- per-file size cap (64 KB) and per-datasource count cap (5)
- target_path safety (must resolve under writable mounts, not under system
  roots, not the agent-managed files like ``~/workspace.md``)
- file ``mode`` well-formedness (4-digit octal)
- ``env_var`` well-formedness (POSIX identifier)
- type-specific defaults for ``kubeconfig`` and ``ssh_key``

The validator's output is what eventually gets encrypted into the
``datasources.credentials`` JSONB column and shipped to the agent.
"""

from __future__ import annotations

import pytest

from security.credential_files import (
    AGENT_HOME,
    CREDENTIAL_FILE_TYPES,
    CredentialFileValidationError,
    MAX_FILES_PER_DATASOURCE,
    MAX_FILE_BYTES,
    normalize_credential_files,
    slugify_datasource_name,
)


# =============================================================================
# slugify_datasource_name
# =============================================================================


class TestSlugify:
    def test_simple(self):
        assert slugify_datasource_name("Prod EU Cluster") == "prod-eu-cluster"

    def test_collapse_punctuation(self):
        assert slugify_datasource_name("k3d (local dev)") == "k3d-local-dev"

    def test_unicode_stripped(self):
        # Non-[a-z0-9] sequences collapse to one hyphen.
        assert slugify_datasource_name("über/cluster") == "ber-cluster"

    def test_empty_falls_back(self):
        assert slugify_datasource_name("") == "unnamed"
        assert slugify_datasource_name("   ") == "unnamed"
        assert slugify_datasource_name("---") == "unnamed"


# =============================================================================
# Pass-through for non-credential-file types
# =============================================================================


class TestPassThrough:
    def test_generic_type_untouched(self):
        creds = {"env_vars": {"PGHOST": "db.example"}}
        assert normalize_credential_files("generic", "any", creds) is creds

    def test_repository_type_untouched(self):
        creds = {"ssh_key": "----PRIVATE----", "auth_method": "ssh"}
        out = normalize_credential_files("repository", "Github", creds)
        assert out is creds

    def test_none_passes_through_for_non_credential_types(self):
        assert normalize_credential_files("postgresql", "PG", None) is None


# =============================================================================
# kubeconfig defaults
# =============================================================================


class TestKubeconfig:
    def _ok(self):
        return {"files": [{"contents": "apiVersion: v1\nkind: Config\n"}]}

    def test_minimal_fills_defaults(self):
        out = normalize_credential_files("kubeconfig", "Prod EU", self._ok())
        assert out is not None
        files = out["files"]
        assert len(files) == 1
        f = files[0]
        assert f["target_path"] == f"{AGENT_HOME}/.kube/configs/prod-eu.yaml"
        assert f["mode"] == "0600"
        assert "env_var" not in f  # KUBECONFIG is set on the merged file, not per-ds

    def test_must_have_exactly_one_file(self):
        with pytest.raises(CredentialFileValidationError, match="exactly one file"):
            normalize_credential_files(
                "kubeconfig",
                "x",
                {"files": [{"contents": "a"}, {"contents": "b"}]},
            )

    def test_empty_files_rejected(self):
        with pytest.raises(CredentialFileValidationError, match="non-empty list"):
            normalize_credential_files("kubeconfig", "x", {"files": []})

    def test_missing_credentials_rejected(self):
        with pytest.raises(CredentialFileValidationError, match="required"):
            normalize_credential_files("kubeconfig", "x", None)

    def test_user_override_target_path_accepted(self):
        out = normalize_credential_files(
            "kubeconfig",
            "x",
            {
                "files": [
                    {
                        "contents": "a",
                        "target_path": "~/.kube/custom-config.yaml",
                    }
                ]
            },
        )
        assert out["files"][0]["target_path"] == f"{AGENT_HOME}/.kube/custom-config.yaml"


# =============================================================================
# ssh_key defaults
# =============================================================================


class TestSshKey:
    def test_single_file_is_private_key(self):
        out = normalize_credential_files(
            "ssh_key",
            "Github Deploy",
            {"files": [{"contents": "----PRIVATE----"}]},
        )
        f = out["files"][0]
        assert f["target_path"] == f"{AGENT_HOME}/.ssh/github-deploy"
        assert f["mode"] == "0600"

    def test_two_files_private_then_public(self):
        out = normalize_credential_files(
            "ssh_key",
            "Github",
            {
                "files": [
                    {"contents": "----PRIVATE----"},
                    {"contents": "ssh-ed25519 AAA..."},
                ]
            },
        )
        priv, pub = out["files"]
        assert priv["target_path"] == f"{AGENT_HOME}/.ssh/github"
        assert priv["mode"] == "0600"
        assert pub["target_path"] == f"{AGENT_HOME}/.ssh/github.pub"
        assert pub["mode"] == "0644"

    def test_three_files_rejected(self):
        with pytest.raises(CredentialFileValidationError, match="at most two files"):
            normalize_credential_files(
                "ssh_key",
                "x",
                {"files": [{"contents": c} for c in ("a", "b", "c")]},
            )


# =============================================================================
# generic_file
# =============================================================================


class TestGenericFile:
    def test_requires_target_path_from_user(self):
        with pytest.raises(CredentialFileValidationError, match="target_path"):
            normalize_credential_files(
                "generic_file",
                "x",
                {"files": [{"contents": "data"}]},
            )

    def test_minimal_user_payload(self):
        out = normalize_credential_files(
            "generic_file",
            "GCloud Creds",
            {
                "files": [
                    {
                        "contents": "{\"type\":\"service_account\"}",
                        "target_path": "~/.config/gcloud/creds.json",
                    }
                ]
            },
        )
        f = out["files"][0]
        # ~ resolved
        assert f["target_path"] == f"{AGENT_HOME}/.config/gcloud/creds.json"
        # default mode applied
        assert f["mode"] == "0600"

    def test_env_var_normalized(self):
        out = normalize_credential_files(
            "generic_file",
            "x",
            {
                "files": [
                    {
                        "contents": "abc",
                        "target_path": "/tmp/foo",
                        "env_var": "MY_TOKEN_FILE",
                    }
                ]
            },
        )
        assert out["files"][0]["env_var"] == "MY_TOKEN_FILE"


# =============================================================================
# Size / count caps
# =============================================================================


class TestCaps:
    def test_too_many_files(self):
        files = [
            {"contents": "x", "target_path": f"/tmp/{i}"}
            for i in range(MAX_FILES_PER_DATASOURCE + 1)
        ]
        with pytest.raises(CredentialFileValidationError, match="At most"):
            normalize_credential_files("generic_file", "x", {"files": files})

    def test_max_files_accepted(self):
        files = [
            {"contents": "x", "target_path": f"/tmp/{i}"}
            for i in range(MAX_FILES_PER_DATASOURCE)
        ]
        out = normalize_credential_files("generic_file", "x", {"files": files})
        assert len(out["files"]) == MAX_FILES_PER_DATASOURCE

    def test_contents_over_size_cap_rejected(self):
        too_big = "x" * (MAX_FILE_BYTES + 1)
        with pytest.raises(CredentialFileValidationError, match="exceed"):
            normalize_credential_files(
                "generic_file",
                "x",
                {"files": [{"contents": too_big, "target_path": "/tmp/big"}]},
            )

    def test_contents_must_be_string(self):
        with pytest.raises(CredentialFileValidationError, match="UTF-8 string"):
            normalize_credential_files(
                "generic_file",
                "x",
                {"files": [{"contents": 123, "target_path": "/tmp/x"}]},
            )


# =============================================================================
# Target path safety
# =============================================================================


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "/etc/shadow",
        "/proc/self/environ",
        "/sys/kernel",
        "/dev/null",
        "/var/log/messages",
        "/usr/bin/anything",
        # Traversal collapses via normpath, then fails the writable-root check.
        "/tmp/../etc/passwd",
        # ~ expands to /home/srw, then the relative .. crosses into /home.
        "~/../etc/passwd",
        # Outside any writable root.
        "/opt/something",
        "/root/anything",
        # Relative path.
        "relative/path",
    ],
)
def test_blocked_paths(bad_path):
    with pytest.raises(CredentialFileValidationError):
        normalize_credential_files(
            "generic_file",
            "x",
            {"files": [{"contents": "x", "target_path": bad_path}]},
        )


@pytest.mark.parametrize(
    "managed_path",
    [
        "~/.bashrc",
        "~/.bash_profile",
        "~/.profile",
        "~/workspace.md",
    ],
)
def test_blocked_managed_files(managed_path):
    with pytest.raises(CredentialFileValidationError, match="reserved"):
        normalize_credential_files(
            "generic_file",
            "x",
            {"files": [{"contents": "x", "target_path": managed_path}]},
        )


@pytest.mark.parametrize(
    "good_path",
    [
        "~/.kube/configs/foo.yaml",
        "~/.ssh/id_ed25519",
        "~/.config/gcloud/creds.json",
        "/tmp/something",
        "/run/secret.txt",
        "/workspace/.secrets/key",
    ],
)
def test_allowed_paths(good_path):
    out = normalize_credential_files(
        "generic_file",
        "x",
        {"files": [{"contents": "x", "target_path": good_path}]},
    )
    # No exception means the path resolved to a writable root.
    assert out["files"][0]["target_path"].startswith(("/home/srw", "/tmp", "/run", "/workspace"))


def test_etcd_not_treated_as_etc():
    """Regression: ``/etcd/x`` and ``/usr_data`` must not match the /etc/, /usr/ blocklist."""
    # Both are outside writable roots, so they fail — but with a "writable
    # root" error, not a "blocked system root" one. We assert the latter
    # message is NOT raised.
    for path in ("/etcd/x", "/usrlocal/x"):
        with pytest.raises(CredentialFileValidationError) as excinfo:
            normalize_credential_files(
                "generic_file",
                "x",
                {"files": [{"contents": "x", "target_path": path}]},
            )
        assert "blocked system root" not in str(excinfo.value)


# =============================================================================
# mode and env_var formats
# =============================================================================


@pytest.mark.parametrize("good_mode", ["0600", "0644", "0400", "0755"])
def test_mode_accepted(good_mode):
    out = normalize_credential_files(
        "generic_file",
        "x",
        {"files": [{"contents": "x", "target_path": "/tmp/x", "mode": good_mode}]},
    )
    assert out["files"][0]["mode"] == good_mode


@pytest.mark.parametrize("bad_mode", ["600", "8888", "rwxrwxrwx", "0999", 0o600])
def test_mode_rejected(bad_mode):
    with pytest.raises(CredentialFileValidationError, match="mode"):
        normalize_credential_files(
            "generic_file",
            "x",
            {"files": [{"contents": "x", "target_path": "/tmp/x", "mode": bad_mode}]},
        )


@pytest.mark.parametrize("good_env", ["KUBECONFIG", "MY_VAR", "_X", "AWS_PROFILE_2"])
def test_env_var_accepted(good_env):
    out = normalize_credential_files(
        "generic_file",
        "x",
        {
            "files": [
                {
                    "contents": "x",
                    "target_path": "/tmp/x",
                    "env_var": good_env,
                }
            ]
        },
    )
    assert out["files"][0]["env_var"] == good_env


@pytest.mark.parametrize(
    "bad_env",
    ["2_LEADING_DIGIT", "has space", "has-dash", "has.dot"],
)
def test_env_var_rejected(bad_env):
    with pytest.raises(CredentialFileValidationError, match="env_var"):
        normalize_credential_files(
            "generic_file",
            "x",
            {
                "files": [
                    {
                        "contents": "x",
                        "target_path": "/tmp/x",
                        "env_var": bad_env,
                    }
                ]
            },
        )


def test_empty_env_var_dropped():
    """Empty string is treated as "not set" — the env_var key is omitted from output."""
    out = normalize_credential_files(
        "generic_file",
        "x",
        {
            "files": [
                {"contents": "x", "target_path": "/tmp/x", "env_var": ""}
            ]
        },
    )
    assert "env_var" not in out["files"][0]


# =============================================================================
# Type allowlist contract
# =============================================================================


def test_credential_file_types_set():
    """The orchestrator endpoint depends on this exact set."""
    assert CREDENTIAL_FILE_TYPES == frozenset({"kubeconfig", "ssh_key", "generic_file"})
