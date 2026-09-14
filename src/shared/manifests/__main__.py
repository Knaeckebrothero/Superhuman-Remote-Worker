"""Local conformance utility: python -m shared.manifests preview FILE..."""

import argparse
import json
from pathlib import Path
import sys

from . import (
    ManifestError,
    export_documents,
    parse_documents,
    preview_documents,
    validate_documents,
)
from .errors import fail
from .validation import MAX_SOURCE_BYTES


STDIN_SENTINEL = "-"


def _load_path(path: Path) -> tuple[str, str]:
    """Read one file path and return ``(text, format)``.

    The format follows the existing suffix convention: ``.json`` selects JSON,
    every other suffix selects YAML.
    """
    text = path.read_text(encoding="utf-8")
    chosen = "json" if path.suffix.lower() == ".json" else "yaml"
    return text, chosen


def _read_stdin() -> str:
    """Read stdin once, capped at the parser's source-byte budget.

    Raises the existing ``InputLimitExceeded`` directly when the pipe exceeds
    the budget so the caller sees the same code/message other inputs produce.
    Empty stdin reads return an empty string; the parser handles that.
    """
    raw = sys.stdin.buffer.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        fail("InputLimitExceeded", "Manifest text exceeds the 1 MiB limit.")
    return raw.decode("utf-8")


def _sniff_format(text: str) -> str:
    """Pick the stdin parser format from the first non-whitespace byte.

    JSON bundles exported by this CLI always start with ``[`` (array) or
    ``{`` (object); YAML documents never begin with those bytes. Falling
    through to YAML lets the existing parser accept JSON-shaped single
    documents too.
    """
    for ch in text:
        if ch.isspace():
            continue
        if ch in "[{":
            return "json"
        return "yaml"
    return "yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "preview", "export"))
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--scope-kind", choices=("Account", "Project", "Catalog"))
    parser.add_argument("--scope-name")
    parser.add_argument("--output-format", choices=("json", "yaml"), default="yaml")
    args = parser.parse_args()
    if bool(args.scope_kind) != bool(args.scope_name):
        parser.error("--scope-kind and --scope-name must be supplied together")
    scope = (
        {"kind": args.scope_kind, "name": args.scope_name} if args.scope_name else None
    )

    # Reject multiple ``-`` arguments BEFORE opening stdin: reading stdin twice
    # would either block forever on a closed pipe or silently drop the tail of
    # a populated one — neither is what the caller asked for.
    if sum(1 for path in args.files if str(path) == STDIN_SENTINEL) > 1:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "InvalidArguments",
                        "message": (
                            "Pass '-' at most once; read stdin once and supply "
                            "one or more documents."
                        ),
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1

    stdin_text: str | None = None
    stdin_format: str | None = None
    try:
        # Collect file inputs in argument order. Stdin (if requested) is read
        # lazily on first encounter so a non-stdin invocation never opens the
        # pipe, and a populated pipe can be consumed in its declared position
        # alongside any file arguments.
        documents = []
        for path in args.files:
            if str(path) == STDIN_SENTINEL:
                if stdin_text is None:
                    stdin_text = _read_stdin()
                    stdin_format = _sniff_format(stdin_text)
                documents.extend(parse_documents(stdin_text, format=stdin_format))
            else:
                text, chosen = _load_path(path)
                documents.extend(parse_documents(text, format=chosen))
        documents = validate_documents(documents)
        if args.operation == "export":
            print(
                export_documents(
                    documents, default_scope=scope, format=args.output_format
                ),
                end="",
            )
        else:
            result = (
                {"valid": True, "documents": len(documents)}
                if args.operation == "validate"
                else preview_documents(documents, default_scope=scope)
            )
            print(json.dumps(result, indent=2, allow_nan=False))
    except ManifestError as exc:
        print(json.dumps({"error": exc.as_dict()}), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        # Stay value-free: never echo the offending bytes back to the caller.
        print("Unable to read a manifest file as UTF-8.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
