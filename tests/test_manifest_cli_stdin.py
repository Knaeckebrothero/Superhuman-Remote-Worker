"""Subprocess coverage for python -m shared.manifests ... - stdin support."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import textwrap
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "manifests"
MAX_SOURCE_BYTES = 1024 * 1024
HELPER_DEADLINE_SECONDS = 10.0

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
YAML_FLOW_DOC = textwrap.dedent(
    """\
    apiVersion: srw/v1alpha1
    kind: Expert
    metadata: {name: pipe-flow, scope: {kind: Account, name: personal}}
    spec:
      runtime: {image: "example/worker:1"}
    """
)
YAML_FLOW_TOP_DOC = textwrap.dedent(
    """\
    {apiVersion: "srw/v1alpha1", kind: Expert, metadata: {name: "pipe-flow-top", scope: {kind: Account, name: "personal"}}, spec: {runtime: {image: "example/worker:1"}}}
    """
)
YAML_MULTI_DOC = textwrap.dedent(
    """\
    apiVersion: srw/v1alpha1
    kind: Expert
    metadata:
      name: pipe-multi-one
      scope: {kind: Account, name: personal}
    spec:
      runtime:
        image: example/worker:1
    ---
    apiVersion: srw/v1alpha1
    kind: Expert
    metadata:
      name: pipe-multi-two
      scope: {kind: Account, name: personal}
    spec:
      runtime:
        image: example/worker:2
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


def _write_with_deadline(
    fd: int, payload: bytes, deadline_monotonic: float, chunk: int = 64 * 1024
) -> int:
    """Write ``payload`` to ``fd`` without blocking past ``deadline_monotonic``.

    A plain ``os.write`` is blocking and on a sleeping child can wait past
    the helper's own ``communicate()`` timeout — leaving the test process
    hung and the reviewer waiting. Setting the fd nonblocking and using
    ``select`` with a remaining-time deadline makes the writer
    deterministic: either the bytes fit in the pipe buffer and we return
    the count, or the writer waits until the budget expires and raises
    ``TimeoutError`` (test = hang-detected), or the child closes its read
    end and ``BrokenPipeError`` propagates.
    """
    flags = os.get_blocking(fd)
    try:
        os.set_blocking(fd, False)
        written = 0
        view = memoryview(payload)
        while written < len(view):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"helper write deadline exceeded after {written} bytes"
                )
            _, ready, _ = select.select([], [fd], [], remaining)
            if fd not in ready:
                raise TimeoutError(
                    f"helper write deadline exceeded after {written} bytes"
                )
            try:
                n = os.write(fd, bytes(view[written : written + chunk]))
            except BlockingIOError:
                continue
            written += n
        return written
    finally:
        # Restore blocking semantics so the kernel does not leak an
        # O_NONBLOCK flag if this helper is reused elsewhere.
        os.set_blocking(fd, flags)


def _spawn_open_pipe(
    *argv,
    stdin_payload: bytes | None = None,
    keep_writer_open: bool = False,
    command: list[str] | None = None,
):
    """Run the CLI with bounded writes and optional suppression of pipe EOF.

    ``command`` permits a real stalled-reader probe. Failure cleanup closes
    every pipe and terminates the exact child started by this call.
    """
    parent = child = proc = None
    writer_closed = False
    try:
        parent, child = os.pipe()
        if command is None:
            argv_list = [sys.executable, "-m", "shared.manifests", *argv]
            popen_kwargs = {
                "cwd": str(REPO_ROOT),
                "env": {**os.environ, "PYTHONSAFEPATH": "1"},
            }
        else:
            argv_list = list(command)
            popen_kwargs = {}
        proc = subprocess.Popen(
            argv_list,
            stdin=parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        os.close(parent)
        parent = None
        if stdin_payload is not None:
            deadline = time.monotonic() + HELPER_DEADLINE_SECONDS
            _write_with_deadline(child, stdin_payload[: MAX_SOURCE_BYTES + 1], deadline)
        if not keep_writer_open:
            os.close(child)
            child = None
            writer_closed = True
        stdout, stderr = proc.communicate(timeout=HELPER_DEADLINE_SECONDS)
    finally:
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass
        if child is not None and not writer_closed:
            try:
                os.close(child)
            except OSError:
                pass
        if proc is not None:
            # Close the child's stdout/stderr pipes on the failure path so
            # the helper does not leak a second pair of fds when an
            # earlier step raised before ``communicate()`` consumed them.
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    return proc.returncode, stdout, stderr


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


def test_yaml_flow_mapping_inside_block_is_yaml_not_json():
    # Block-style document whose inner values are YAML flow mappings. The
    # sniff sees the leading ``a`` of ``apiVersion`` and routes to YAML;
    # the JSON path is not exercised here.
    result = _run("validate", "-", stdin_bytes=YAML_FLOW_DOC.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 1}


def test_yaml_flow_mapping_at_top_level_is_yaml_not_json():
    # A top-level YAML flow mapping starts with ``{`` — this is the case
    # where the FIRST non-whitespace byte is the ambiguous one. JSON
    # requires a quoted first key; YAML flow mappings do not. The
    # quote-sniff must route this to YAML, not JSON, otherwise the JSON
    # parser would reject the unquoted ``apiVersion`` key.
    result = _run("validate", "-", stdin_bytes=YAML_FLOW_TOP_DOC.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 1}


def test_yaml_multi_document_stream_validates():
    # Multi-document YAML starts with ``---`` (not ``[`` / ``{``); the sniff
    # falls through to YAML and the parser walks every document.
    result = _run("validate", "-", stdin_bytes=YAML_MULTI_DOC.encode("utf-8"))
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout)
    assert payload == {"valid": True, "documents": 2}


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


def test_duplicate_yaml_key_in_pipe_still_rejected():
    # The pre-existing duplicate-key protection must not be quietly dropped
    # when stdin is used; a malformed YAML stream must still exit 1 with the
    # documented DuplicateKey envelope, not be laundered into a parse error.
    duplicate = textwrap.dedent(
        """\
        apiVersion: srw/v1alpha1
        kind: Expert
        kind: Job
        metadata:
          name: dup
          scope: {kind: Account, name: personal}
        spec:
          runtime: {image: example/worker:1}
        """
    )
    result = _run("validate", "-", stdin_bytes=duplicate.encode("utf-8"))
    assert result.returncode == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "DuplicateKey"


def test_oversized_json_pipe_still_rejected():
    # The 1 MiB / 100-document / schema protections apply on the JSON path
    # too — _sniff_format must not have weakened the parser to accept flow
    # mappings on the YAML side at the cost of any existing guard.
    huge = '{"a":' + '"x"' * (1024 * 1024) + "}"
    result = _run("validate", "-", stdin_bytes=huge.encode("utf-8"))
    assert result.returncode == 1
    err = json.loads(result.stderr)
    assert err["error"]["code"] == "InputLimitExceeded"


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
    # name in EXACT argument order. ``resources.yaml`` ships three documents
    # (cpp-developer, cpp-terraform-react, application-source); the stdin
    # YAML adds pipe-yaml at the END. Every name is asserted.
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
    assert [doc["metadata"]["name"] for doc in parsed] == [
        "cpp-developer",
        "cpp-terraform-react",
        "application-source",
        "pipe-yaml",
    ]


def test_mixed_stdin_then_file_exact_order():
    # Stdin then file: the stdin YAML must come first, then the file's
    # documents in their existing YAML order. Every name is asserted.
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
    assert [doc["metadata"]["name"] for doc in parsed] == [
        "pipe-yaml",
        "zero-integration-example",
    ]


def test_duplicate_stdin_rejected_without_reading():
    # A duplicate ``-`` argument must exit 1 with the InvalidArguments
    # envelope BEFORE any byte of the pipe is consumed. We hold the writer
    # end open with no bytes; sys.stdin.buffer.read(N) would block until
    # the writer produces N bytes or closes the pipe, so the duplicate
    # check must fire before the implementation ever tries to read.
    returncode, stdout, stderr = _spawn_open_pipe(
        "validate", "-", "-", stdin_payload=None, keep_writer_open=True
    )
    assert returncode == 1
    err = json.loads(stderr.decode("utf-8"))
    assert err["error"]["code"] == "InvalidArguments"
    assert "InvalidSyntax" not in stderr.decode("utf-8", "replace")


def test_oversized_stdin_rejected_without_hang():
    # Write ``MAX_SOURCE_BYTES + 1`` bytes through the pipe and close the
    # writer so the reader receives EOF and returns with the full payload.
    # The CLI must exit 1 with InputLimitExceeded, never a generic parse
    # error, never a hang. The deadline-bounded writer in the helper
    # caps the test-side wait, and the bounded ``MAX_SOURCE_BYTES + 1``
    # read in ``_read_stdin`` means the producer cannot make the CLI
    # wait for more data once the budget is exceeded.
    payload = b"a" * (MAX_SOURCE_BYTES + 16)
    returncode, stdout, stderr = _spawn_open_pipe(
        "validate", "-", stdin_payload=payload, keep_writer_open=False
    )
    assert returncode == 1
    err = json.loads(stderr.decode("utf-8"))
    assert err["error"]["code"] == "InputLimitExceeded"


def test_open_unread_stdin_pipe_ignored_for_file_only_invocation():
    # File-only invocation MUST ignore an open unread stdin pipe. The pipe
    # contains unrelated content; the implementation never opens it because
    # ``-`` is not in args. ``resources.yaml`` ships three documents;
    # the result is exactly those three, in YAML order.
    payload = b"this never gets read"
    returncode, stdout, stderr = _spawn_open_pipe(
        "export",
        str(EXAMPLES / "resources.yaml"),
        "--scope-kind",
        "Account",
        "--scope-name",
        "personal",
        "--output-format",
        "json",
        stdin_payload=payload,
        keep_writer_open=True,
    )
    assert returncode == 0, stderr.decode("utf-8", "replace")
    parsed = json.loads(stdout.decode("utf-8"))
    assert [doc["metadata"]["name"] for doc in parsed] == [
        "cpp-developer",
        "cpp-terraform-react",
        "application-source",
    ]


def test_stalled_reader_times_out_and_closes_child_and_pipes(monkeypatch):
    children = []
    real_popen = subprocess.Popen

    def capture_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", capture_child)
    monkeypatch.setitem(globals(), "HELPER_DEADLINE_SECONDS", 0.2)
    # More bytes than an ordinary pipe can buffer force the writer to wait.
    # The child also has a finite lifetime if deadline enforcement regresses.
    with pytest.raises(TimeoutError):
        _spawn_open_pipe(
            stdin_payload=b"x" * (MAX_SOURCE_BYTES + 1),
            keep_writer_open=True,
            command=[sys.executable, "-c", "import time; time.sleep(3)"],
        )
    assert len(children) == 1
    assert children[0].poll() is not None
    assert children[0].stdout.closed
    assert children[0].stderr.closed


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
