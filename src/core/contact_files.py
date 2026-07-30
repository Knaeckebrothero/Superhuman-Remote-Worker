"""Render project contacts as workspace files (docs/features/contacts_registry.md).

DB is the source of truth; these files are a read-only projection written at
job/session start (same lever as skills/). Frontmatter scalars are emitted via
json.dumps — JSON is valid YAML flow syntax, so this needs no yaml dependency
(this module is imported by the orchestrator image; keep it stdlib-only).

Currently dormant: its boot-time caller was reverted in favour of a
virtual-directory overlay (docs/features/virtual_directories.md). This module
is retained as the designated renderer for that design's ContactsProvider,
which will call it from a live, TTL-cached read path instead.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List

CONTACTS_DIR = "contacts"


def contact_slug(display_name: str, taken: set) -> str:
    base = unicodedata.normalize("NFKD", display_name or "")
    base = base.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "contact"
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def render_contact_md(contact: Dict[str, Any]) -> str:
    lines = [
        "---",
        f"name: {json.dumps(contact['_slug'])}",
        f"display_name: {json.dumps(contact.get('display_name', ''))}",
    ]
    addresses = contact.get("addresses") or []
    if addresses:
        lines.append("addresses:")
        for a in addresses:
            entry = {"channel": a.get("channel"), "address": a.get("address")}
            if a.get("is_primary"):
                entry["primary"] = True
            if a.get("opt_in_status") and a["opt_in_status"] != "opted_in":
                entry["opt_in"] = a["opt_in_status"]
            lines.append(f"  - {json.dumps(entry)}")
    projects = [p.get("name", "?") for p in (contact.get("projects") or [])]
    if projects:
        lines.append(f"projects: {json.dumps(projects)}")
    lines.append("---")
    body = (contact.get("notes") or "").strip()
    return "\n".join(lines) + ("\n\n" + body + "\n" if body else "\n")


def contacts_to_workspace_files(contacts: List[Dict[str, Any]]) -> Dict[str, str]:
    taken: set = set()
    out: Dict[str, str] = {}
    for c in contacts:
        slug = contact_slug(c.get("display_name", ""), taken)
        taken.add(slug)
        out[f"{CONTACTS_DIR}/{slug}.md"] = render_contact_md({**c, "_slug": slug})
    return out
