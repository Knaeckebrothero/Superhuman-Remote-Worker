"""Manage SRW resources over the public API.

Run ``python -m orchestrator.operator_cli.manifest_resources --help``. Set
SRW_API_URL and SRW_TOKEN, or read a bearer token using --token-stdin. This CLI
does not load orchestrator startup/database settings or inherit internal MCP
credentials. Local, offline conformance remains ``python -m shared.manifests``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urlsplit

import httpx

from shared.manifests import ManifestError, parse_documents
from shared.manifests.validation import MAX_SOURCE_BYTES
from shared.orch_surface.client import AsyncCockpitClient, MutationOutcomeUnknown


_KINDS = ("Expert", "WorkspaceTemplate", "Connector", "Project", "Job")
_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=os.environ.get("SRW_API_URL")
        or os.environ.get("COCKPIT_API_URL", "http://localhost:8085"),
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read a bearer token from one stdin line instead of SRW_TOKEN.",
    )
    parser.add_argument(
        "--ca-file",
        type=Path,
        help="PEM trust bundle; default uses the system trust store.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    def scope(command):
        command.add_argument("--scope-kind", choices=("Account", "Project", "Catalog"))
        command.add_argument("--scope-name")

    for operation in ("validate", "preview", "apply"):
        command = commands.add_parser(operation)
        command.add_argument(
            "-f",
            "--file",
            dest="files",
            action="append",
            required=True,
            help="Manifest file; repeat to combine files, or use - for stdin.",
        )
        command.add_argument(
            "--format",
            choices=("json", "yaml"),
            help="Defaults to JSON for .json files, YAML otherwise.",
        )
        if operation != "validate":
            scope(command)
        if operation == "preview":
            command.add_argument(
                "--resolution", choices=("stored", "bundle"), default="stored"
            )
        if operation == "apply":
            reviewed = command.add_mutually_exclusive_group()
            reviewed.add_argument(
                "--plan",
                type=Path,
                help="JSON output of a stored preview for these same files and scope.",
            )
            reviewed.add_argument(
                "--plan-revision", help="A reviewed server planRevision."
            )
            command.add_argument(
                "--expected-versions",
                type=Path,
                help="JSON map of canonical resource identity to resourceVersion.",
            )
            command.add_argument(
                "--expected-version",
                action="append",
                default=[],
                metavar="IDENTITY=VERSION",
                help="Explicit version for one resource; repeat for multiple resources.",
            )
            command.add_argument(
                "--idempotency-key",
                help="Reuse with the identical request to recover a lost apply response.",
            )
    command = commands.add_parser(
        "get", help="Read a resource by UID, or list an authorized scope."
    )
    command.add_argument("resource_id", nargs="?")
    command.add_argument("--kind", choices=_KINDS)
    scope(command)
    command = commands.add_parser(
        "export", help="Export a stored resource's authored manifest."
    )
    command.add_argument("resource_id")
    command.add_argument(
        "-o", "--output-format", choices=("yaml", "json"), default="yaml"
    )
    command = commands.add_parser(
        "delete", help="Delete a resource at the observed resourceVersion."
    )
    command.add_argument("resource_id")
    command.add_argument("--expected-version", required=True, type=int)
    return parser


def _scope(args):
    kind, name = getattr(args, "scope_kind", None), getattr(args, "scope_name", None)
    if bool(kind) != bool(name):
        raise ValueError("--scope-kind and --scope-name must be supplied together.")
    return {"kind": kind, "name": name} if kind else None


def _read_text(path):
    if str(path) == "-":
        text = sys.stdin.read(MAX_SOURCE_BYTES + 1)
    else:
        with Path(path).open(encoding="utf-8") as stream:
            text = stream.read(MAX_SOURCE_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Input exceeds the 1 MiB limit.")
    return text


def _source(args):
    if args.files.count("-") > 1:
        raise ValueError("Read manifest stdin only once.")
    documents = []
    for path in args.files:
        format = args.format or (
            "json" if Path(path).suffix.lower() == ".json" else "yaml"
        )
        documents.extend(parse_documents(_read_text(path), format=format))
    if len(documents) > 100:
        raise ValueError("A request may contain at most 100 resources.")
    source = json.dumps(documents, ensure_ascii=False, allow_nan=False)
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Combined manifests exceed the 1 MiB request limit.")
    return source


def _bookkeeping(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Plan/version files must not contain duplicate keys.")
            result[key] = value
        return result

    value = json.loads(_read_text(path), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("A plan/version file must contain a JSON object.")
    return value


def _apply_options(args):
    versions = _bookkeeping(args.expected_versions) if args.expected_versions else {}
    for item in args.expected_version:
        identity, separator, value = item.rpartition("=")
        if not separator or not identity or identity in versions:
            raise ValueError("Supply each expected version once as IDENTITY=VERSION.")
        versions[identity] = int(value)
    if len(versions) > 100 or any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        for key, value in versions.items()
    ):
        raise ValueError(
            "Expected versions must be positive integers for at most 100 resources."
        )
    revision = args.plan_revision
    if args.plan:
        reviewed = _bookkeeping(args.plan)
        if (
            reviewed.get("operation") != "preview"
            or reviewed.get("resolution") != "stored"
        ):
            raise ValueError(
                "--plan requires the JSON result of a stored server preview."
            )
        revision = reviewed.get("planRevision")
    if revision is not None and (
        not isinstance(revision, str) or not _REVISION.fullmatch(revision)
    ):
        raise ValueError("A plan revision must be a server sha256 content revision.")
    if args.plan and not revision:
        raise ValueError("The preview does not contain a planRevision.")
    if args.idempotency_key is not None and not 1 <= len(args.idempotency_key) <= 128:
        raise ValueError("Idempotency keys must contain 1 to 128 characters.")
    return {
        "expected_versions": versions,
        "plan_revision": revision,
        "idempotency_key": args.idempotency_key,
    }


async def _run(args, token):
    scope = _scope(args)
    source = _source(args) if hasattr(args, "files") else None
    options = _apply_options(args) if args.operation == "apply" else {}
    if args.operation == "get" and args.resource_id and (scope or args.kind):
        raise ValueError("A UID lookup cannot also specify scope or kind filters.")
    parsed_url = urlsplit(args.server)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError(
            "--server must be an HTTP(S) API URL without credentials, query or fragment."
        )
    verify = ssl.create_default_context(cafile=args.ca_file)
    async with AsyncCockpitClient(
        args.server, bearer_token=token, verify=verify, trust_env=False
    ) as client:
        if args.operation == "validate":
            return await client.manifest_validate(source, format="json")
        if args.operation == "preview":
            return await client.manifest_preview(
                source, format="json", default_scope=scope, resolution=args.resolution
            )
        if args.operation == "apply":
            return await client.manifest_apply(
                source, format="json", default_scope=scope, **options
            )
        if args.operation == "get":
            if args.resource_id:
                return await client.get_manifest_resource(args.resource_id)
            scope = scope or {"kind": "Account", "name": "me"}
            return await client.list_manifest_resources(
                scope_kind=scope["kind"], scope_name=scope["name"], kind=args.kind
            )
        if args.operation == "export":
            return await client.export_manifest_resource(
                args.resource_id, output_format=args.output_format
            )
        return await client.delete_manifest_resource(
            args.resource_id, expected_version=args.expected_version
        )


def _http_error(error):
    response = error.response
    result = {
        "code": "HttpError",
        "status": response.status_code,
        "message": "The server rejected the operation.",
    }
    try:
        detail = response.json().get("detail")
        if isinstance(detail, str):
            result["message"] = detail
        elif isinstance(detail, dict):
            for key in ("code", "message", "path"):
                if isinstance(detail.get(key), str):
                    result[key] = detail[key]
    except (ValueError, AttributeError):
        pass
    return result


def main(argv=None):
    args = _parser().parse_args(argv)
    token = ""
    try:
        if args.token_stdin and "-" in getattr(args, "files", []):
            raise ValueError("Token stdin and manifest stdin cannot be used together.")
        token = (
            sys.stdin.readline(65537).rstrip("\r\n")
            if args.token_stdin
            else os.environ.get("SRW_TOKEN", "")
        )
        if not token:
            raise ValueError(
                "Set SRW_TOKEN or supply --token-stdin with an approved-user bearer credential."
            )
        result = asyncio.run(_run(args, token))
        if args.operation == "export":
            print(result["source"], end="" if result["source"].endswith("\n") else "\n")
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except ManifestError as error:
        result = error.as_dict()
    except MutationOutcomeUnknown:
        result = {
            "code": "OutcomeUnknown",
            "message": "The server may have applied the operation. It was not retried. Read current resources before retrying; an identical apply may reuse its idempotency key.",
        }
    except httpx.HTTPStatusError as error:
        result = _http_error(error)
    except (httpx.RequestError, ssl.SSLError):
        result = {
            "code": "ConnectionFailed",
            "message": "The API request could not be completed. Check the server address, trust bundle and connectivity.",
        }
    except (OSError, UnicodeError):
        result = {
            "code": "InputUnavailable",
            "message": "Unable to read an input file or trust bundle.",
        }
    except (ValueError, TypeError, KeyError) as error:
        result = {"code": "InvalidInput", "message": str(error)}
    diagnostic = json.dumps({"error": result}, ensure_ascii=False)
    # Diagnostics may include a server message; a reflected credential must
    # never appear in logs. Successful authored exports retain opaque values.
    if token:
        diagnostic = diagnostic.replace(token, "[redacted]")
    print(diagnostic, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
