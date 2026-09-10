#!/usr/bin/env python3
"""Convert bundled reference-harness leaves in place, retaining their comments.

No database or cluster access. Run with --write to change assets, or --check to
verify that all bundled experts already use the resource envelope. The original
private fragment is moved verbatim beneath runtime.config.config.
"""

import argparse
from pathlib import Path

import yaml

from shared.runtime.core.srw_manifest_config import (
    BUNDLED_SRW_IMAGE,
    SRW_HARNESS_ADAPTER,
    srw_private_config,
)


def converted(path: Path, *, image: str | None) -> str | None:
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"Expected an object: {path}")
    if raw.get("kind") == "Expert":
        private = srw_private_config(raw)
        updated = text
        if image is None and raw["spec"]["runtime"].get("image") == BUNDLED_SRW_IMAGE:
            updated = updated.replace(f"    image: {BUNDLED_SRW_IMAGE}\n", "", 1)
        if "asset_name" not in private:
            selector = private["config_name"]
            base = private.get("config", {}).get("$extends", "worker_base")
            updated = updated.replace(
                f"      config_name: {selector}\n",
                f"      config_name: {base}\n      asset_name: {selector}\n",
                1,
            )
        return updated if updated != text else None
    library = path.parent.parent.name == "subagents"
    name = path.parent.name
    role = "session" if raw.get("$extends") == "session_base" else "worker"
    tags = list(raw.get("tags") or [])
    role_tag = "subagent" if library else role
    if role_tag not in tags:
        tags.append(role_tag)
    metadata = {
        "name": ("subagent-" if library else "") + name,
        "scope": {"kind": "Catalog", "name": "shared"},
        "annotations": {
            "srw.io/display-name": str(
                raw.get("display_name") or name.replace("-", " ").title()
            ),
            "srw.io/description": str(raw.get("description") or "").strip(),
            "srw.io/icon": str(raw.get("icon") or "psychology"),
            "srw.io/color": str(raw.get("color") or "#cba6f7"),
            "srw.io/expert-type": role,
            "srw.io/bundled-selector": f"subagents/{name}" if library else name,
        },
        "tags": tags,
    }
    envelope = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Expert",
        "metadata": metadata,
        "spec": {
            "runtime": {
                **({"image": image} if image is not None else {}),
                "adapter": SRW_HARNESS_ADAPTER,
                "config": {
                    "config_name": raw.get("$extends", "worker_base"),
                    "asset_name": f"subagents/{name}" if library else name,
                },
            }
        },
    }
    header = yaml.safe_dump(envelope, sort_keys=False, allow_unicode=True)
    # Preserve explanatory comments and private scalars exactly. Only the old
    # language-server schema hint described the old document envelope.
    body = "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("# yaml-language-server:")
    )
    return (
        header
        + "      config:\n"
        + "\n".join("        " + line if line else "" for line in body.splitlines())
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config",
    )
    parser.add_argument(
        "--image", help="Constrain newly converted definitions to an explicit image"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    pending = []
    for group in ("experts", "subagents"):
        for path in sorted((args.config_dir / group).glob("*/config.yaml")):
            replacement = converted(path, image=args.image)
            if replacement is not None:
                pending.append(path)
                if args.write:
                    path.write_text(replacement, encoding="utf-8")
    print(
        f"Bundled Expert manifests: {len(pending)} {'converted' if args.write else 'pending conversion'}"
    )
    return 1 if args.check and pending else 0


if __name__ == "__main__":
    raise SystemExit(main())
