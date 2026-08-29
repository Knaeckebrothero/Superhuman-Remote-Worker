"""Task 9: every workspace trusts the ssh-gateway's user CA.

Ruling G45: the four brief tests below all grep file TEXT, so they would
pass on a match inside a comment. Acceptable for the Dockerfile and shell
heredocs -- there is no cheap alternative to exercising real sshd/bash --
but ``test_provisioner_projects_the_ca_public_key`` builds the actual
Kubernetes volume spec and asserts on its structure instead, per that
ruling.

Ruling G44: closing the "one cert works fleet-wide" gap needs the workspace
to scope certificate auth to its own principal (``AuthorizedPrincipalsFile``)
in addition to trusting the CA. Those tests live here too, alongside the
CA-trust ones the brief specified.
"""

import pathlib

from orchestrator.services.container_provisioner import ContainerProvisioner
from orchestrator.services.workspace_lifecycle import WorkspaceOwner

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_sshd_trusts_the_user_ca():
    body = (REPO / "docker" / "Dockerfile.workspace").read_text()
    assert "TrustedUserCAKeys /etc/ssh/srw_user_ca.pub" in body


def test_sshd_scopes_certificates_to_a_per_workspace_principals_file():
    """Trusting the CA alone (the assertion above) means every workspace
    honors every certificate the gateway ever mints, for that cert's whole
    validity window -- Ruling G3/G44's "fleet-wide reach" gap.
    ``AuthorizedPrincipalsFile`` is what makes a workspace check the
    certificate's principal against ITS OWN file rather than the (shared,
    baked-in) login username.
    """
    body = (REPO / "docker" / "Dockerfile.workspace").read_text()
    assert "AuthorizedPrincipalsFile /etc/ssh/principals/%u" in body


def test_entrypoint_installs_the_ca_public_key():
    body = (REPO / "docker" / "workspace-entrypoint.sh").read_text()
    assert "/tmp/ssh-pubkey/user-ca.pub" in body
    assert "/etc/ssh/srw_user_ca.pub" in body


def test_entrypoint_writes_the_workspace_owner_id_into_the_principals_file():
    """The file ``AuthorizedPrincipalsFile /etc/ssh/principals/%u`` reads for
    login user ``agent-host`` must contain THIS workspace's identity, or the
    principals-scoping test above is decorative -- every workspace would
    still accept every certificate because every principals file would be
    either absent or identical.
    """
    body = (REPO / "docker" / "workspace-entrypoint.sh").read_text()
    assert "/etc/ssh/principals/agent-host" in body
    assert "$SRW_WORKSPACE_OWNER_ID" in body


def test_sshd_still_refuses_passwords_and_root():
    body = (REPO / "docker" / "Dockerfile.workspace").read_text()
    assert "PasswordAuthentication no" in body
    assert "PermitRootLogin no" in body


def test_provisioner_projects_the_ca_public_key():
    """Builds the real volume spec (Ruling G45) rather than grepping
    container_provisioner.py's source, which is ~13k lines and would pass
    on a match inside a comment or a docstring.
    """
    provisioner = ContainerProvisioner()
    provisioner._ssh_secret_name = "test-ssh-secret"

    manifest = provisioner._build_pod_manifest(
        pod_name="workspace-abc123",
        owner=WorkspaceOwner.job("abc123"),
        image="test:latest",
        cpu="500m",
        memory="1Gi",
        cpu_limit="2000m",
        memory_limit="4Gi",
    )

    volumes = {v["name"]: v for v in manifest["spec"]["volumes"]}
    secret = volumes["ssh-pubkey"]["secret"]
    assert secret["secretName"] == "test-ssh-secret"

    items = {item["key"]: item for item in secret["items"]}

    # The pre-existing agent key projection must survive untouched --
    # there is project history on workspace SSH file modes
    # ([[dev_snapshot_ssh_0444_root_only]]) and 0600 is deliberate.
    assert items["ssh-publickey"]["path"] == "ssh-publickey"
    assert items["ssh-publickey"]["mode"] == 0o600

    assert items["user-ca.pub"]["path"] == "user-ca.pub"
    assert items["user-ca.pub"]["mode"] == 0o644

    # A deployment whose ssh-gateway secret predates this key must still
    # start: the projected CA key is additive, not required.
    assert secret["optional"] is True

    # defaultMode is untouched: only the per-item mode changed.
    assert secret["defaultMode"] == 0o600
