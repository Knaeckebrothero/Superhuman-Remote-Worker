"""Per-workspace code-server credentials — the IDE proxy's recipient binding.

The property under test is narrow and load-bearing: two different owners must
never derive the same credential, because that value is the only thing standing
between a proxy that dialled a reused Pod IP and another tenant's workspace.
"""

from types import SimpleNamespace

import pytest

from orchestrator.services.ide_credentials import (
    IDE_CREDENTIAL_COOKIE,
    ide_credential,
    ide_credential_cookie_header,
    ide_credential_root,
)


ARGS = {
    "namespace": "srw",
    "owner_kind": "thread",
    "owner_id": "dfab9ef9-3bd9-4902-9eee-53785a0a916f",
    "pod_name": "ws-thread-dfab9ef9-3bd",
}


@pytest.fixture
def root_key(monkeypatch):
    monkeypatch.setenv("IDE_CREDENTIAL_KEY", "test-root-key")


class TestFailClosed:
    def test_no_root_key_yields_no_credential(self, monkeypatch):
        """Unset must mean "no IDE", never "no credential needed"."""
        monkeypatch.delenv("IDE_CREDENTIAL_KEY", raising=False)

        assert ide_credential_root() is None
        assert ide_credential(**ARGS) is None

    def test_blank_root_key_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("IDE_CREDENTIAL_KEY", "   ")

        assert ide_credential_root() is None
        assert ide_credential(**ARGS) is None

    @pytest.mark.parametrize(
        "field", ["namespace", "owner_kind", "owner_id", "pod_name"]
    )
    def test_missing_input_yields_no_credential(self, root_key, field):
        """A partially-known runtime must not produce a usable credential."""
        assert ide_credential(**{**ARGS, field: ""}) is None


class TestDerivation:
    def test_is_deterministic(self, root_key):
        assert ide_credential(**ARGS) == ide_credential(**ARGS)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("owner_id", "00000000-0000-0000-0000-000000000000"),
            ("owner_kind", "job"),
            ("namespace", "other"),
            ("pod_name", "ws-thread-000000000000"),
        ],
    )
    def test_any_identity_change_changes_the_credential(self, root_key, field, value):
        """A foreign owner — or a foreign namespace — cannot derive ours."""
        assert ide_credential(**{**ARGS, field: value}) != ide_credential(**ARGS)

    def test_rotating_the_root_key_changes_the_credential(self, monkeypatch):
        monkeypatch.setenv("IDE_CREDENTIAL_KEY", "key-a")
        first = ide_credential(**ARGS)
        monkeypatch.setenv("IDE_CREDENTIAL_KEY", "key-b")

        assert first != ide_credential(**ARGS)

    def test_fields_cannot_be_shifted_across_the_separator(self, root_key):
        """Concatenation must not be ambiguous between adjacent fields."""
        shifted = ide_credential(
            **{**ARGS, "owner_kind": "thread\x1fdfab9ef9", "owner_id": "rest"}
        )

        assert shifted != ide_credential(**ARGS)

    def test_takes_code_servers_sha256_branch(self, root_key):
        """`$argon` in the value would route code-server to argon2 verification.

        The credential is presented verbatim as the session cookie, which only
        works on the branch that does a constant-time compare against the
        configured value. A hex digest can never contain `$argon`, but assert
        it: a future change to a base64/urlsafe encoding could.
        """
        credential = ide_credential(**ARGS)

        assert "$argon" not in credential
        assert len(credential) == 64
        assert all(character in "0123456789abcdef" for character in credential)


class TestCookieHeader:
    def test_renders_the_name_code_server_checks(self, root_key):
        credential = ide_credential(**ARGS)

        assert ide_credential_cookie_header(credential) == (
            f"{IDE_CREDENTIAL_COOKIE}={credential}"
        )


class TestWorkspacePodInjection:
    """The provisioner must hand the Pod the same value the proxy derives."""

    def _provisioner(self):
        """A ContainerProvisioner with only what _build_pod_manifest reads."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = object.__new__(ContainerProvisioner)
        provisioner._namespace = ARGS["namespace"]
        provisioner._fuse_enabled = False
        provisioner._fuse_privileged = False
        provisioner._workspace_image = "srw-workspace:test"
        provisioner._ssh_secret_name = "srw-workspace-ssh"
        return provisioner

    def _env(self, manifest):
        container = manifest["spec"]["containers"][0]
        return {entry["name"]: entry.get("value") for entry in container["env"]}

    def _manifest(self, provisioner, pod_name):
        from orchestrator.services.workspace_lifecycle import WorkspaceOwner

        return provisioner._build_pod_manifest(
            pod_name=pod_name,
            owner=WorkspaceOwner.session(ARGS["owner_id"]),
            image="srw-workspace:test",
            cpu="1",
            memory="1Gi",
            cpu_limit="2",
            memory_limit="2Gi",
        )

    def test_injected_value_matches_what_the_proxy_derives(self, root_key):
        """Both sides must derive from the same ``WorkspaceOwner``.

        Regression: the IDE proxy calls a thread's owner kind "thread" while
        ``WorkspaceOwner.session()`` reports "session". Deriving from the two
        vocabularies independently produced different credentials and 401'd
        every thread IDE.
        """
        from orchestrator.services.workspace_lifecycle import WorkspaceOwner

        owner = WorkspaceOwner.session(ARGS["owner_id"])
        pod_name = owner.pod_name
        env = self._env(self._manifest(self._provisioner(), pod_name))

        assert env["HASHED_PASSWORD"] == ide_credential(
            namespace=ARGS["namespace"],
            owner_kind=owner.kind,
            owner_id=owner.id,
            pod_name=pod_name,
        )

    def test_ide_session_pod_gets_its_own_credential(self, root_key):
        """Restored IDE Pods reuse this builder under a different name."""
        provisioner = self._provisioner()
        workspace = self._env(self._manifest(provisioner, "ws-thread-dfab9ef9-3bd"))
        ide_pod = self._env(self._manifest(provisioner, "ide-dfab9ef9-3bd"))

        assert ide_pod["HASHED_PASSWORD"] != workspace["HASHED_PASSWORD"]

    def test_no_root_key_injects_nothing(self, monkeypatch):
        """Fail closed: the entrypoint then refuses to start code-server."""
        monkeypatch.delenv("IDE_CREDENTIAL_KEY", raising=False)

        env = self._env(self._manifest(self._provisioner(), "ws-thread-dfab9ef9-3bd"))

        assert "HASHED_PASSWORD" not in env


class TestEnforcementIsReadBackNotAssumed:
    """Deriving a credential proves what we would send, not what is enforced."""

    def _pod(self, env_entries):
        return SimpleNamespace(
            spec=SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        name="workspace",
                        env=[
                            SimpleNamespace(name=name, value=value)
                            for name, value in env_entries
                        ],
                    )
                ]
            )
        )

    def _check(self, pod):
        from orchestrator.services.ide_proxy import IdeProxyService
        from orchestrator.services.workspace_lifecycle import WorkspaceOwner

        owner = WorkspaceOwner.session(ARGS["owner_id"])
        return IdeProxyService._enforced_credential(
            pod, owner, ARGS["namespace"], owner.pod_name
        )

    def _expected(self):
        from orchestrator.services.workspace_lifecycle import WorkspaceOwner

        owner = WorkspaceOwner.session(ARGS["owner_id"])
        return ide_credential(
            namespace=ARGS["namespace"],
            owner_kind=owner.kind,
            owner_id=owner.id,
            pod_name=owner.pod_name,
        )

    def test_pod_carrying_the_credential_is_bound(self, root_key):
        pod = self._pod([("HASHED_PASSWORD", self._expected())])

        assert self._check(pod) == self._expected()

    def test_pod_predating_the_credential_stays_contained(self, root_key):
        """The transition needs no flag day — an old Pod is simply refused.

        Pods created before this shipped run `auth: none` and would serve the
        workspace to anyone who reached them. Sending them a credential they
        ignore would be security theatre.
        """
        pod = self._pod([("SRW_WORKSPACE_OWNER_ID", ARGS["owner_id"])])

        assert self._check(pod) is None

    def test_pod_carrying_a_foreign_credential_is_refused(self, root_key):
        pod = self._pod([("HASHED_PASSWORD", "f" * 64)])

        assert self._check(pod) is None

    def test_no_root_key_means_no_binding_even_if_the_pod_has_one(
        self, monkeypatch, root_key
    ):
        expected = self._expected()
        monkeypatch.delenv("IDE_CREDENTIAL_KEY", raising=False)

        assert self._check(self._pod([("HASHED_PASSWORD", expected)])) is None

    def test_unreadable_pod_spec_is_refused(self, root_key):
        assert self._check(SimpleNamespace(spec=None)) is None
