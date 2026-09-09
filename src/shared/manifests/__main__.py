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
    try:
        documents = []
        for path in args.files:
            documents.extend(
                parse_documents(
                    path.read_text(encoding="utf-8"),
                    format="json" if path.suffix.lower() == ".json" else "yaml",
                )
            )
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
        print("Unable to read a manifest file as UTF-8.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
