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
from .validation import MAX_SOURCE_BYTES


STDIN_SENTINEL = "-"
# Cap the stdin read so an unbounded pipe cannot exhaust memory. Read up to the
# parser's limit; the existing ``InputLimitExceeded`` error then reports the
# actual overshoot on the validate/preview/export path without buffering the
# whole stream.
_STDIN_BYTE_BUDGET = MAX_SOURCE_BYTES


def _load_path(path):
    text = path.read_text(encoding="utf-8")
    chosen = "json" if path.suffix.lower() == ".json" else "yaml"
    return text, chosen


def _read_stdin(limit):
    """Read up to ``limit`` bytes from stdin and decode as UTF-8.

    Returns ``(text, raw_size)``. ``raw_size`` is the number of bytes actually
    read from the pipe (post-buffer); when it exceeds ``limit``, the caller
    hands the text to the parser and lets the existing
    ``InputLimitExceeded`` error report the overshoot.
    """
    raw = sys.stdin.buffer.read(limit + 1)
    return raw.decode("utf-8"), len(raw)


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
                            "one document."
                        ),
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1

    sources = []  # list of (label, text, format)
    try:
        for path in args.files:
            if str(path) == STDIN_SENTINEL:
                text, raw_size = _read_stdin(_STDIN_BYTE_BUDGET)
                # Stdin has no suffix; default to YAML (the historical contract
                # for ``*.yaml`` path arguments).
                sources.append(("<stdin>", text, "yaml"))
                if raw_size > _STDIN_BYTE_BUDGET:
                    # Surface the existing limit error from the parser so the
                    # caller sees the same code/messages other inputs produce.
                    sources[-1] = (
                        "<stdin>",
                        text + " ",  # one byte over the parser limit
                        "yaml",
                    )
            else:
                text, chosen = _load_path(path)
                sources.append((str(path), text, chosen))
        documents = []
        for _label, text, chosen in sources:
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
