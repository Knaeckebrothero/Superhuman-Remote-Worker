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
JSON_BUNDLE = json.dumps(
    [
        {
            "apiVersion": "srw/v1alpha1",
            "kind": "Expert",
            "metadata": {
                "name": "bundle-one",
                "scope": {"kind": "Account", "name": "personal"},
            },
            "spec": {"runtime": {"image": "example/worker:1"}},
        },
        {
            "apiVersion": "srw/v1alpha1",
            "kind": "Expert",
            "metadata": {
                "name": "bundle-two",
                "scope": {"kind": "Account", "name": "personal"},
            },
            "spec": {"runtime": {"image": "example/worker:2"}},
        },
    ]
)


def _run(*args, stdin_bytes: bytes | None = None, timeout: float = 30.0):
    """Invoke the installed ``shared.manifests`` module through the venv Python.

    ``PYTHONSAFEPATH=1`` keeps the subprocess off the implicit source directory
    so the test exercises the editable install, not the checkout directly.
    ``cwd`` is the repo root so relative examples resolve as documented.
    """
    env = os.environ.copy()
    env["PYTHONSAFEPATH"] = "1"
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


def test_preview_pipe_yaml_happy_path():
    result = _run(
        "preview",
        "-",
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        stdin_bytes=YAML_DOC.encode("utf-8"),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["operation"] == "preview"
    assert payload["admissionReady"] is False
    assert payload["documents"][0]["metadata"]["name"] == "pipe-yaml"


def test_json_pipe_single_object_validates():
    result = _run("validate", "-", stdin_bytes=JSON_DOC.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 1}


def test_json_bundle_pipe_validates():
    # JSON arrays emitted by the existing ``export --output-format json`` must
    # round-trip through ``validate -`` without re-formatting.
    result = _run("validate", "-", stdin_bytes=JSON_BUNDLE.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 2}


def test_json_export_round_trip_through_stdin():
    # Export the JSON bundle back to JSON via stdin. The exported JSON must
    # reparse as the same two-document bundle, preserving authored names.
    export_result = _run(
        "export",
        "-",
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        "--output-format",
        "json",
        stdin_bytes=JSON_BUNDLE.encode("utf-8"),
    )
    assert export_result.returncode == 0, export_result.stderr.decode(
        "utf-8", "replace"
    )
    exported = export_result.stdout.decode("utf-8")
    parsed = json.loads(exported)
    assert isinstance(parsed, list), "JSON output must remain a list"
    assert [doc["metadata"]["name"] for doc in parsed] == ["bundle-one", "bundle-two"]

    # And re-validating that exported JSON via stdin must still be accepted.
    revalidate = _run("validate", "-", stdin_bytes=exported.encode("utf-8"))
    assert revalidate.returncode == 0, revalidate.stderr.decode("utf-8", "replace")
    payload = json.loads(revalidate.stdout)
    assert payload == {"valid": True, "documents": 2}


def test_mixed_file_then_stdin_exact_order():
    # File then stdin: the names from the file must appear before the stdin
    # name in exact argument order. ``resources.yaml`` ships three documents;
    # only the stdin document's name is uniquely authored here.
    file_arg = EXAMPLES / "resources.yaml"
    export_result = _run(
        "export",
        str(file_arg),
        "-",
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        "--output-format",
        "json",
        stdin_bytes=YAML_DOC.encode("utf-8"),
    )
    assert export_result.returncode == 0, export_result.stderr.decode(
        "utf-8", "replace"
    )
    parsed = json.loads(export_result.stdout.decode("utf-8"))
    assert isinstance(parsed, list)
    names = [doc["metadata"]["name"] for doc in parsed]
    assert names[-1] == "pipe-yaml"
    assert names.index("pipe-yaml") == len(names) - 1


def test_mixed_stdin_then_file_exact_order():
    # Stdin then file: the stdin YAML must come first, then the file's
    # documents in their existing YAML order.
    file_arg = EXAMPLES / "inline-job.yaml"
    export_result = _run(
        "export",
        "-",
        str(file_arg),
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        "--output-format",
        "json",
        stdin_bytes=YAML_DOC.encode("utf-8"),
    )
    assert export_result.returncode == 0, export_result.stderr.decode(
        "utf-8", "replace"
    )
    parsed = json.loads(export_result.stdout.decode("utf-8"))
    assert isinstance(parsed, list)
    assert parsed[0]["metadata"]["name"] == "pipe-yaml"
    assert parsed[1]["metadata"]["name"] == "zero-integration-example"


def test_duplicate_stdin_rejected_without_reading():
    # Open the stdin pipe and hold it open without writing any sentinel byte.
    # If the implementation read stdin first the empty pipe would surface
    # ``b""`` immediately, masking the no-read contract. A duplicate ``-``
    # argument must exit 1 with the InvalidArguments envelope BEFORE any
    # byte of the pipe is consumed.
    parent, child = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "shared.manifests", "validate", "-", "-"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONSAFEPATH": "1"},
            stdin=parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(parent)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 1
        err = json.loads(stderr.decode("utf-8"))
        assert err["error"]["code"] == "InvalidArguments"
    finally:
        os.close(child)


def test_oversized_stdin_rejected_without_hang():
    # Stream 1 MiB + 16 bytes through a pipe kept open. The CLI must exit 1
    # with InputLimitExceeded — never a generic parse error, never a hang.
    parent, child = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "shared.manifests", "validate", "-"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONSAFEPATH": "1"},
            stdin=parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(parent)
        # Write the oversized payload in one chunk and leave the writer end
        # open so the implementation cannot block waiting for EOF.
        os.write(child, b"a" * (1024 * 1024 + 16))
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 1
        err = json.loads(stderr.decode("utf-8"))
        assert err["error"]["code"] == "InputLimitExceeded"
    finally:
        os.close(child)


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
    result = _run(
        "validate",
        "-",
        stdin_bytes=b'{"apiVersion": "srw/v1alpha1", "kind":',
        timeout=10,
    )
    assert result.returncode == 1
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr
    assert "srw/v1alpha1" not in stderr
    err = json.loads(stderr)
    assert err["error"]["code"] == "InvalidSyntax"


def test_invalid_utf8_rejected_without_disclosure():
    bad = b"\xff\xfe\xfd not utf-8"
    result = _run("validate", "-", stdin_bytes=bad, timeout=10)
    assert result.returncode == 1
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr
    # The bad bytes must not appear in any output channel; the replacement
    # ``\ufffd`` would leak the original range, so assert it is absent too.
    assert "\ufffd" not in stderr
    # Either the JSON envelope from the parser or the value-free UTF-8
    # message from the CLI is acceptable — both refuse the input without
    # disclosing the offending bytes.
    if stderr.startswith("{"):
        envelope = json.loads(stderr)
        assert envelope["error"]["code"] in {"InvalidSource", "InputLimitExceeded"}
    else:
        assert stderr.strip() == "Unable to read a manifest file as UTF-8."


def test_existing_file_arguments_still_work():
    result = _run("validate", str(EXAMPLES / "inline-job.yaml"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["documents"] >= 1
