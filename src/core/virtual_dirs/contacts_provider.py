"""ContactsProvider — contacts/ served live from the orchestrator.

The DB is the source of truth (knowledge-history/done/contacts_registry.md). The agent
sees a read-only projection with a short TTL, so a contact linked mid-session
becomes visible without a restart. Deliberately not the reverted boot-snapshot
approach (commit b8e48c10), which could only ever be as fresh as job start.
"""

import logging
import time
from typing import Callable, Dict, List, Optional

from ..backends.overlay import EntryMeta
from ..contact_files import contact_slug, render_contact_md

logger = logging.getLogger(__name__)


class ContactsProvider:
    prefix = "contacts"
    is_dir = True
    writable = False

    def __init__(self, fetch: Callable[[], List[dict]], ttl_seconds: float = 60.0):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._cache: Optional[Dict[str, str]] = None
        self._fetched_at = 0.0

    def _render(self, contacts: List[dict]) -> Dict[str, str]:
        docs: Dict[str, str] = {}
        taken: set = set()
        lines = ["# Contacts", ""]
        if not contacts:
            lines.append("No contacts are linked to this project.")
        for contact in contacts:
            slug = contact_slug(contact.get("display_name", "contact"), taken)
            taken.add(slug)
            docs[f"{slug}.md"] = render_contact_md({**contact, "_slug": slug})
            channels = sorted(
                {a.get("channel", "") for a in contact.get("addresses", []) if a}
            )
            suffix = f" — {', '.join(channels)}" if channels else ""
            lines.append(f"- [{contact.get('display_name', slug)}]({slug}.md){suffix}")
        lines.append("")
        docs["README.md"] = "\n".join(lines)
        return docs

    def _docs(self) -> Dict[str, str]:
        fresh = (
            self._cache is not None
            and (time.monotonic() - self._fetched_at) < self._ttl
        )
        if fresh:
            return self._cache
        # Stamp BEFORE the attempt so failures are throttled to the same TTL
        # cadence as successes. Stamping only on success means that during an
        # orchestrator outage every single read re-attempts the fetch and pays
        # the full client timeout before falling back to stale content.
        self._fetched_at = time.monotonic()
        try:
            self._cache = self._render(self._fetch() or [])
        except Exception as e:
            if self._cache is not None:
                logger.warning("contacts fetch failed; serving stale cache: %s", e)
                return self._cache
            logger.warning("contacts fetch failed with a cold cache: %s", e)
            raise ValueError(f"contacts are temporarily unavailable: {e}") from e
        return self._cache

    def entries(self) -> Dict[str, EntryMeta]:
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self._docs().items()
        }

    def read(self, name: str) -> Optional[str]:
        return self._docs().get(name)

    def read_all(self) -> Dict[str, str]:
        """One TTL-checked pass for the whole set (see ``_read_all``)."""
        return self._docs()
