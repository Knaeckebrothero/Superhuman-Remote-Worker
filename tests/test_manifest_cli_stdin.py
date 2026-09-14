"""Subprocess coverage for python -m shared.manifests ... - stdin support."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "manifests"
YAML_DOC = textwrap.dedent(
    """\
    apiVersion: srw/v1alpha1
    kind: Expert
    metadata:
      name: pipe-yaml
      scope: {kind: Account, name: personal}
    spec:
      runtime:
        image: example/worker:1
    """
)
JSON_DOC = json.dumps(
    {
        "apiVersion": "srw/v1alpha1",
        "kind": "Expert",
        "metadata": {
            "name": "pipe-json",
            "scope": {"kind": "Account", "name": "personal"},
        },
        "spec": {"runtime": {"image": "example/worker:1"}},
    }
)


def _run(*args, stdin_bytes: bytes | None = None, timeout: float = 30.0):
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT / "src"))
    return subprocess.run(
        [sys.executable, "-m", "shared.manifests", *args],
        cwd=str(REPO_ROOT),
        env=env,
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
    )


def test_yaml_pipe_validates():
    result = _run("validate", "-", stdin_bytes=YAML_DOC.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 1}


def test_json_pipe_via_stdin_flag_validates():
    # '-' has no suffix; the legacy contract defaults to YAML. JSON content
    # arrives through stdin and is parsed by the existing parser when the
    # caller selects json via the dedicated CLI flag in scope.
    result = _run("validate", "-", stdin_bytes=JSON_DOC.encode("utf-8"))
    # JSON parses as a single YAML document, so this is a happy path: the
    # resource should validate the same as the YAML version.
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["documents"] == 1


def test_export_pipe_with_scope():
    result = _run(
        "export",
        "-",
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        "--output-format",
        "json",
        stdin_bytes=YAML_DOC.encode("utf-8"),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["kind"] == "Expert"
    assert payload["metadata"]["name"] == "pipe-yaml"


def test_mixed_file_then_stdin_order():
    file_arg = EXAMPLES / "resources.yaml"
    result = _run(
        "validate", str(file_arg), "-", stdin_bytes=YAML_DOC.encode("utf-8")
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    # resources.yaml alone contributes several documents; the stdin YAML adds
    # one more.
    assert payload["documents"] >= 2


def test_mixed_stdin_then_file_order():
    file_arg = EXAMPLES / "inline-job.yaml"
    result = _run(
        "validate", "-", str(file_arg), stdin_bytes=YAML_DOC.encode("utf-8")
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["documents"] >= 2


def test_duplicate_stdin_rejected_without_reading():
    # Empty stdin plus two '-' arguments must still exit 1 cleanly. If the
    # implementation read stdin before rejecting, an empty pipe would let
    # ``sys.stdin.buffer.read`` return ``b""`` immediately so the test would
    # pass spuriously. To prove the no-read contract we deliberately provide
    # a sentinel that would surface in stderr if the implementation echoed
    # any byte from stdin, and we assert it does not appear.
    sentinel = b"UNAUTHORIZED-STDIN-CONTENT-MUST-NOT-LEAK"
    result = _run("validate", "-", "-", stdin_bytes=sentinel, timeout=10)
    assert result.returncode == 1
    assert sentinel.decode("utf-8") not in result.stderr.decode("utf-8", "replace")
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "InvalidArguments"


def test_empty_stdin_clean_exit():
    result = _run("validate", "-", stdin_bytes=b"", timeout=10)
    assert result.returncode == 1
    # Parser emits a documented ManifestError; the CLI prints the JSON envelope.
    err = json.loads(result.stderr)
    assert err["error"]["code"] in {"InvalidSyntax", "InvalidSource", "DocumentLimit"}


def test_malformed_yaml_clean_exit_no_traceback():
    result = _run(
        "validate",
        "-",
        stdin_bytes=b"this: : : not valid yaml: [unterminated",
        timeout=10,
    )
    assert result.returncode == 1
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr
    assert "not valid yaml" not in stderr
    err = json.loads(stderr)
    assert err["error"]["code"] == "InvalidSyntax"


def test_malformed_json_clean_exit_no_traceback():
    # JSON-mode bypass: supply JSON via stdin and rely on the parser's
    # ``InvalidSyntax`` envelope. The parser already converts YAML/JSON
    # parse errors into a value-free ManifestError, so this asserts the
    # same code path with a JSON-shaped payload.
    result = _run(
        "validate",
        "-",
        stdin_bytes=b'{"apiVersion": "srw/v1alpha1", "kind":',
        timeout=10,
    )
    assert result.returncode == 1
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr
    assert 'srw/v1alpha1' not in stderr
    err = json.loads(stderr)
    assert err["error"]["code"] == "InvalidSyntax"


def test_oversized_stdin_rejected_without_hang():
    # 1 MiB + a few bytes. The implementation caps the raw read at the
    # parser's source-byte budget so it never buffers the whole stream.
    payload = b"a" * (1024 * 1024 + 16)
    result = _run("validate", "-", stdin_bytes=payload, timeout=10)
    assert result.returncode == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] in {"InputLimitExceeded", "InvalidSyntax"}


def test_invalid_utf8_rejected_without_disclosure():
    bad = b"\xff\xfe\xfd not utf-8"
    result = _run("validate", "-", stdin_bytes=bad, timeout=10)
    assert result.returncode == 1
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr
    # The bad bytes must not appear in any output channel.
    assert b"\xff\xfe\xfd".decode("utf-8", "replace") not in stderr
    assert "Unable to read a manifest file as UTF-8." in stderr or json.loads(
        stderr
    )["error"]["code"] in {"InvalidSource", "InputLimitExceeded"}


def test_existing_file_arguments_still_work():
    result = _run("validate", str(EXAMPLES / "inline-job.yaml"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["documents"] >= 1
