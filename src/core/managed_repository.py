"""Materialize server-owned managed-repository credentials into a workspace.

The payload is an internal orchestrator→runtime transport object. Callers pop
it from request/session metadata before invoking this module; this function
never logs or returns private material. Git remotes contain only an opaque SSH
host alias. The exact repo-scoped deploy key is loaded over the trusted SSH
transport into a dedicated ``ssh-agent`` and is never written into the
workspace, command line, environment, tmux scrollback, or Git configuration.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Iterable, Mapping, MutableMapping
from urllib.parse import urlparse
from uuid import UUID

from ..utils.ssh_key import normalize_private_key

_VERSION = 1
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ALIAS = re.compile(r"^srw-repo-[a-f0-9]{32}$")


class ManagedRepositoryMaterializationError(RuntimeError):
    """Credential delivery is malformed or cannot be proven in the workspace."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _validated_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if int(payload.get("version")) != _VERSION:
            raise ValueError
        authority_id = str(UUID(str(payload["authority_id"])))
        generation = int(payload["generation"])
        access_mode = str(payload["access_mode"])
        repo_name = str(payload["repo_name"])
        owner = str(payload["repository_owner"])
        alias = str(payload["alias"])
        host = str(payload["ssh_host"])
        port = int(payload["ssh_port"])
        clone_url = str(payload["clone_url"])
        fingerprint = str(payload["public_key_fingerprint"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedRepositoryMaterializationError(
            "managed_repository_credential_invalid"
        ) from exc

    parsed = urlparse(clone_url)
    expected_path = f"/{owner}/{repo_name}.git"
    if (
        generation < 1
        or access_mode not in {"read", "write"}
        or not _NAME.fullmatch(repo_name)
        or not _NAME.fullmatch(owner)
        or not _ALIAS.fullmatch(alias)
        or alias != f"srw-repo-{authority_id.replace('-', '')}"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host)
        or not 1 <= port <= 65535
        or parsed.scheme != "ssh"
        or parsed.hostname != alias
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != expected_path
        or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint)
    ):
        raise ManagedRepositoryMaterializationError(
            "managed_repository_credential_invalid"
        )
    try:
        normalized_private_key = normalize_private_key(str(payload["private_key"]))
        private_key = bytearray(normalized_private_key.encode("utf-8"))
        del normalized_private_key
    except (KeyError, TypeError, ValueError) as exc:
        raise ManagedRepositoryMaterializationError(
            "managed_repository_credential_invalid"
        ) from exc
    return {
        "authority_id": authority_id,
        "generation": generation,
        "access_mode": access_mode,
        "repo_name": repo_name,
        "repository_owner": owner,
        "alias": alias,
        "ssh_host": host,
        "ssh_port": port,
        "clone_url": clone_url,
        "private_key": private_key,
        "public_key_fingerprint": fingerprint,
    }


def repository_url_has_credentials(value: Any) -> bool:
    """Detect URI userinfo and scp-style Git identities."""

    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme:
        return parsed.username is not None or parsed.password is not None
    return bool(re.match(r"^[^/\\\s]+@[^:]+:", text))


def _wipe_validated_private_keys(validated: Iterable[dict[str, Any]]) -> None:
    """Best-effort zero every key that has not yet reached ``ssh-add``."""

    for item in validated:
        private_key = item.pop("private_key", None)
        if isinstance(private_key, bytearray):
            private_key[:] = b"\x00" * len(private_key)


def _materialize_validated_credentials(
    validated: list[dict[str, Any]], backend: Any
) -> dict[str, str]:
    """Materialize already-validated payloads; the caller owns key wiping."""

    if len({item["repo_name"] for item in validated}) != len(validated):
        raise ManagedRepositoryMaterializationError(
            "managed_repository_credential_duplicate"
        )
    if len({item["authority_id"] for item in validated}) != len(validated):
        raise ManagedRepositoryMaterializationError(
            "managed_repository_credential_duplicate"
        )

    try:
        setup_ok = backend.execute_with_secret_stdin(
            "mkdir -p ~/.ssh/srw-managed/config.d ~/.ssh/srw-managed/sockets "
            "&& chmod 700 ~/.ssh ~/.ssh/srw-managed "
            "~/.ssh/srw-managed/config.d ~/.ssh/srw-managed/sockets "
            "&& touch ~/.ssh/config "
            "&& (grep -qxF 'Include ~/.ssh/srw-managed/config.d/*.conf' ~/.ssh/config "
            "|| printf '\\nInclude ~/.ssh/srw-managed/config.d/*.conf\\n' "
            ">> ~/.ssh/config) "
            "&& chmod 600 ~/.ssh/config",
            timeout=15,
            secret=b"",
        )
    except (NotImplementedError, OSError):
        setup_ok = False
    if not setup_ok:
        raise ManagedRepositoryMaterializationError(
            "managed_repository_materialization_failed"
        )

    result: dict[str, str] = {}
    for item in validated:
        authority_slug = item["authority_id"].replace("-", "")
        rel_config = f".ssh/srw-managed/config.d/{authority_slug}.conf"
        socket_path = backend.resolve_home_path(
            f".ssh/srw-managed/sockets/{authority_slug}.sock"
        )
        known_hosts = backend.resolve_home_path(".ssh/srw-managed/known_hosts")
        config = (
            f"Host {item['alias']}\n"
            f"  HostName {item['ssh_host']}\n"
            f"  Port {item['ssh_port']}\n"
            "  User git\n"
            f"  IdentityAgent {socket_path}\n"
            # This is a dedicated per-authority agent containing exactly one
            # key. ``IdentitiesOnly yes`` would suppress agent-only keys unless
            # a matching IdentityFile also existed, defeating the deliberate
            # no-private-key-file design.
            "  BatchMode yes\n"
            "  StrictHostKeyChecking accept-new\n"
            f"  UserKnownHostsFile {known_hosts}\n"
        )
        backend.write_home_file(rel_config, config)
        protected = backend.execute_with_secret_stdin(
            f"chmod 600 {shlex.quote(backend.resolve_home_path(rel_config))}",
            b"",
            timeout=10,
        )
        if not protected:
            raise ManagedRepositoryMaterializationError(
                "managed_repository_materialization_failed"
            )
        # Unlinking a prior socket makes an old agent process unreachable
        # without killing model-visible processes. ssh-add reads the key from
        # the private Paramiko channel stdin; no pathname or environment ever
        # contains the bearer.
        load_command = (
            "set -eu; "
            f"if test -S {shlex.quote(socket_path)}; then "
            f"SSH_AUTH_SOCK={shlex.quote(socket_path)} "
            "ssh-add -D >/dev/null 2>&1 || true; fi; "
            f"rm -f {shlex.quote(socket_path)}; "
            f"ssh-agent -a {shlex.quote(socket_path)} -s >/dev/null; "
            f"SSH_AUTH_SOCK={shlex.quote(socket_path)} "
            "ssh-add - >/dev/null 2>&1"
        )
        private_key = item.pop("private_key")
        try:
            try:
                loaded = backend.execute_with_secret_stdin(
                    load_command, private_key, timeout=20
                )
            except (NotImplementedError, OSError):
                loaded = False
        finally:
            private_key[:] = b"\x00" * len(private_key)
            del private_key
        if not loaded:
            raise ManagedRepositoryMaterializationError(
                "managed_repository_materialization_failed"
            )
        fingerprint_check = backend.execute_with_secret_stdin(
            f"SSH_AUTH_SOCK={shlex.quote(socket_path)} ssh-add -l "
            f"| grep -F -- {shlex.quote(item['public_key_fingerprint'])} "
            ">/dev/null",
            b"",
            timeout=10,
        )
        if not fingerprint_check:
            raise ManagedRepositoryMaterializationError(
                "managed_repository_materialization_failed"
            )
        probe = backend.execute_with_secret_stdin(
            f"GIT_TERMINAL_PROMPT=0 git ls-remote "
            f"{shlex.quote(item['clone_url'])} HEAD >/dev/null",
            b"",
            timeout=30,
        )
        if not probe:
            raise ManagedRepositoryMaterializationError(
                "managed_repository_workspace_probe_failed"
            )
        result[item["repo_name"]] = item["clone_url"]
    return result


def materialize_managed_repository_credentials(
    payloads: Iterable[Mapping[str, Any]] | None,
    backend: Any,
) -> dict[str, str]:
    """Install exact repo keys and prove each clean remote from the workspace.

    Returns ``{repo_name: clone_url}``, a secret-free map used to replace the
    canonical HTTP URLs in the ephemeral runtime request.  Any malformed or
    unreachable authority fails before workspace initialization/provider work.
    """

    raw = list(payloads or [])
    if not raw:
        return {}
    if not getattr(backend, "supports_shell", False):
        for item in raw:
            if isinstance(item, MutableMapping):
                item.pop("private_key", None)
        raise ManagedRepositoryMaterializationError(
            "managed_repository_requires_workspace"
        )
    validated: list[dict[str, Any]] = []
    try:
        for item in raw:
            try:
                validated.append(_validated_payload(item))
            finally:
                if isinstance(item, MutableMapping):
                    item.pop("private_key", None)
        return _materialize_validated_credentials(validated, backend)
    finally:
        # Covers validation, duplicate detection, config injection, transfer,
        # fingerprint verification, and probe failures. Keys already sent to
        # ``ssh-add`` were popped and zeroed at the transfer boundary; every
        # not-yet-transferred bytearray is destroyed here.
        for item in raw:
            if isinstance(item, MutableMapping):
                item.pop("private_key", None)
        _wipe_validated_private_keys(validated)


__all__ = [
    "ManagedRepositoryMaterializationError",
    "materialize_managed_repository_credentials",
    "repository_url_has_credentials",
]
