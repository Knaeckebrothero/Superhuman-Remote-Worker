"""Keep developer-only credentials out of production Docker build contexts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_secret_files_are_explicitly_excluded_from_docker_context() -> None:
    rules = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env" in rules
    assert "deployment/values-local.yaml" in rules
    assert "cockpit/test-results/" in rules
    assert "cockpit/playwright-report/" in rules
    assert "cockpit/blob-report/" in rules
    assert "!.env.example" in rules
    assert "deployment/values-local.yaml.example" not in rules


def test_gitignored_credentials_and_private_repositories_are_excluded() -> None:
    rules = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".dev/",
        ".local-ssh/",
        ".mcp.json",
        ".playwright-mcp/",
        "docker/certs/",
        "docker/vpn/config",
        "docker/vpn/cluster.conf",
        "docker/vpn/research.conf",
        "HomeLab/",
        "knowledge-base/",
        "knowledge-history/",
        "srw-cloud/",
        "KurortEngine/",
        "KurortEngine-salvage/",
        "KurortEngine-extracted/",
        "BetterResavio-KB/",
    } <= rules
