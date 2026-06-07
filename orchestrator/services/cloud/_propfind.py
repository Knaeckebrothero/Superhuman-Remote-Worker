"""Shared WebDAV PROPFIND multistatus parsing.

Both ``OpenCloudBackend`` and ``NextcloudBackend`` walk their project
folders with ``Depth: 1`` PROPFIND requests and parse the resulting
``<d:multistatus>`` body into ``ProjectFolderEntry`` rows. The two servers
are both sabre/dav-compatible (``xmlns:s="http://sabredav.org/ns"``,
``d:``-prefixed properties), so a single regex-based parser serves both.

Regex rather than a full XML parser: the PROPFIND output is structurally
regular enough that this is safe, and it keeps the cloud package free of an
XML-parser dependency. Each backend owns its own authenticated
``_propfind_depth_one`` (bearer vs. basic auth differ); only the pure
body→entries parsing lives here.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from .handles import ProjectFolderEntry

# PROPFIND response patterns — module-level so they compile once.
_PROPFIND_RESPONSE_RE = re.compile(
    r"<d:response[^>]*>(.*?)</d:response>", re.DOTALL | re.IGNORECASE
)
_PROPFIND_HREF_RE = re.compile(r"<d:href[^>]*>([^<]+)</d:href>", re.IGNORECASE)
_PROPFIND_COLLECTION_RE = re.compile(r"<d:collection\s*/>", re.IGNORECASE)
_PROPFIND_CONTENTLENGTH_RE = re.compile(
    r"<d:getcontentlength[^>]*>(\d+)</d:getcontentlength>", re.IGNORECASE
)
_PROPFIND_ETAG_RE = re.compile(r"<d:getetag[^>]*>([^<]+)</d:getetag>", re.IGNORECASE)
_PROPFIND_CONTENTTYPE_RE = re.compile(
    r"<d:getcontenttype[^>]*>([^<]+)</d:getcontenttype>", re.IGNORECASE
)


def parse_propfind_entries(xml: str, *, href_prefix: str) -> list[ProjectFolderEntry]:
    """Pull file + directory entries out of a PROPFIND multistatus body.

    ``href_prefix`` is the URL path prefix to strip so returned paths are
    relative to the folder root (no leading slash). Both ``href_prefix``
    and the ``<d:href>`` values are URL-decoded before comparison — servers
    may return a literal character in the href even when the request URL had
    it percent-encoded (e.g. OpenCloud's literal ``$``, Nextcloud's spaces),
    which would otherwise break a naive ``startswith`` check. Entries
    pointing at the root itself (empty path after stripping) are dropped.
    """
    decoded_prefix = unquote(href_prefix)
    entries: list[ProjectFolderEntry] = []
    for resp in _PROPFIND_RESPONSE_RE.finditer(xml):
        block = resp.group(1)
        href_match = _PROPFIND_HREF_RE.search(block)
        if not href_match:
            continue
        href = unquote(href_match.group(1).strip())
        if not href.startswith(decoded_prefix):
            continue
        rel_path = href[len(decoded_prefix) :].rstrip("/")
        if not rel_path:
            # The folder root itself shows up first; skip it.
            continue
        is_dir = bool(_PROPFIND_COLLECTION_RE.search(block))
        size_match = _PROPFIND_CONTENTLENGTH_RE.search(block)
        etag_match = _PROPFIND_ETAG_RE.search(block)
        ctype_match = _PROPFIND_CONTENTTYPE_RE.search(block)
        entries.append(
            ProjectFolderEntry(
                path=rel_path,
                is_dir=is_dir,
                size=int(size_match.group(1)) if size_match else 0,
                etag=etag_match.group(1).strip().strip('"') if etag_match else "",
                content_type=ctype_match.group(1).strip() if ctype_match else "",
            )
        )
    return entries
