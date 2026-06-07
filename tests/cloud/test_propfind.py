"""Unit tests for the shared WebDAV PROPFIND parser.

``parse_propfind_entries`` turns a ``Depth: 1`` multistatus body into
``ProjectFolderEntry`` rows for both the OpenCloud and Nextcloud backends.
The body below is modeled on a real sabre/dav response captured from a live
Nextcloud Group Folder: entity-encoded etags, server-absolute URL-encoded
hrefs, and ``<d:resourcetype/>`` (file) vs. ``<d:collection/>`` (directory).
"""

from __future__ import annotations

from orchestrator.services.cloud._propfind import parse_propfind_entries

# A mountpoint with a space → the href percent-encodes it; the prefix we
# pass is the encoded request path.
PREFIX = "/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/"

BODY = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" '
    'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
    # root collection — must be dropped (empty path after stripping prefix)
    "<d:response>"
    "<d:href>/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/</d:href>"
    "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
    "<d:getetag>&quot;root123&quot;</d:getetag></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    # a subdirectory
    "<d:response>"
    "<d:href>/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/Documents/</d:href>"
    "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
    "<d:getetag>&quot;dir456&quot;</d:getetag></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    # a file with a space in its name
    "<d:response>"
    "<d:href>/remote.php/dav/groupfolders/agent-service/NC%20Validation%20Project/"
    "Nextcloud%20Manual.pdf</d:href>"
    "<d:propstat><d:prop><d:getcontentlength>15839120</d:getcontentlength>"
    "<d:resourcetype/><d:getetag>&quot;cf35&quot;</d:getetag>"
    "<d:getcontenttype>application/pdf</d:getcontenttype></d:prop>"
    "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "</d:multistatus>"
)


def test_parses_dir_and_file_entries():
    entries = parse_propfind_entries(BODY, href_prefix=PREFIX)
    by_path = {e.path: e for e in entries}
    # Root collection is dropped; only descendants remain.
    assert set(by_path) == {"Documents", "Nextcloud Manual.pdf"}


def test_directory_flagged():
    docs = next(
        e
        for e in parse_propfind_entries(BODY, href_prefix=PREFIX)
        if e.path == "Documents"
    )
    assert docs.is_dir is True
    assert docs.size == 0


def test_file_metadata_parsed():
    pdf = next(
        e
        for e in parse_propfind_entries(BODY, href_prefix=PREFIX)
        if e.path == "Nextcloud Manual.pdf"
    )
    assert pdf.is_dir is False
    assert pdf.size == 15839120
    assert pdf.content_type == "application/pdf"
    assert pdf.etag  # captured (sabre entity-encodes; value is non-empty)


def test_href_decoded_against_encoded_prefix():
    # href percent-encodes the space; the prefix is the encoded request
    # path. The parser unquotes both sides, so paths come back
    # human-readable rather than "Nextcloud%20Manual.pdf".
    paths = {e.path for e in parse_propfind_entries(BODY, href_prefix=PREFIX)}
    assert "Nextcloud Manual.pdf" in paths
    assert "Nextcloud%20Manual.pdf" not in paths


def test_empty_multistatus_yields_nothing():
    body = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'
    assert parse_propfind_entries(body, href_prefix=PREFIX) == []
