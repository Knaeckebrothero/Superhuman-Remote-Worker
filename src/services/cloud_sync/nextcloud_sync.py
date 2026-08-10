"""Nextcloud workspace sync — WebDAV + HTTP basic auth."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

from .base import WorkspaceSyncBase, _normalize_dav_listing

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

# Minimal prop set for the one-shot tree listing — exactly what the sync
# algorithm consumes. Requesting allprop makes Nextcloud compute permissions/
# share props per node, for nothing.
_TREE_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:getetag/><d:getcontentlength/><d:resourcetype/></d:prop>"
    "</d:propfind>"
)


class NextcloudWorkspaceSync(WorkspaceSyncBase):
    """Nextcloud sync client — agent-service user + password, classic WebDAV."""

    def __init__(
        self,
        workspace_path: Path,
        *,
        webdav_url: str,
        webdav_user: str,
        webdav_password: str,
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
        mount_subdir: str = "",
    ) -> None:
        super().__init__(
            workspace_path,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
            mount_subdir=mount_subdir,
        )
        self._webdav_url = webdav_url
        self._webdav_user = webdav_user
        self._webdav_password = webdav_password
        self._client = None
        # client.list(get_info=True) returns absolute server paths
        # (e.g. /remote.php/dav/files/user/folder/file.txt) — we strip
        # this prefix to normalize to paths relative to the session folder.
        self._webdav_base_path = urlparse(webdav_url).path.rstrip("/") + "/"
        # One-shot tree listing (Depth: infinity PROPFIND). Nextcloud gates it
        # behind `dav.propfind.depth_infinity`; a refusal is remembered so the
        # per-dir walk isn't preceded by a doomed probe on every pull.
        self._http: Optional[Any] = None
        self._depth_infinity_unsupported = False

    def _get_client(self):
        if self._client is None:
            from webdav3.client import Client

            self._client = Client(
                {
                    "webdav_hostname": self._webdav_url.rstrip("/"),
                    "webdav_login": self._webdav_user,
                    "webdav_password": self._webdav_password,
                }
            )
        return self._client

    async def _ensure_ready(self) -> None:
        self._get_client()

    async def _ensure_remote_dir(
        self,
        rel_dir: str,
        *,
        before_write: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        if before_write is not None:
            await before_write()
        client = self._get_client()
        await asyncio.to_thread(client.mkdir, rel_dir)

    async def _upload_file(
        self,
        rel_path: str,
        local_path: str,
        *,
        before_write: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        if before_write is not None:
            await before_write()
        client = self._get_client()
        await asyncio.to_thread(
            client.upload_sync,
            remote_path=rel_path,
            local_path=local_path,
        )

    async def _delete_remote_file(
        self,
        rel_path: str,
        *,
        before_write: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        if before_write is not None:
            await before_write()
        client = self._get_client()
        try:
            await asyncio.to_thread(client.clean, rel_path)
        except Exception as exc:
            if not self._marker_missing(exc):
                raise

    async def _list_remote_files(self, rel_dir: str = "") -> list[dict]:
        client = self._get_client()
        raw = await asyncio.to_thread(client.list, rel_dir or "/", get_info=True)
        return _normalize_dav_listing(raw, self._webdav_base_path)

    async def _list_remote_tree_fast(self) -> Optional[list[dict]]:
        """One ``Depth: infinity`` PROPFIND for the whole mount subtree.

        Measured on the k3d cluster (2026-08-08): the webdav3 per-directory
        walk costs ~2.5s/dir (its ``list()`` runs a ``check()`` PROPFIND
        before the real one, on a server where each PROPFIND is ~0.6s of
        PHP); the same tree comes back from a single infinity PROPFIND in
        ~1s. Nextcloud gates the feature behind
        ``dav.propfind.depth_infinity`` — any non-207 answer flips the
        per-instance capability flag and the walk takes over for good.
        """
        if self._depth_infinity_unsupported:
            return None
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(
                auth=(self._webdav_user, self._webdav_password),
                timeout=httpx.Timeout(30.0, read=60.0),
            )
        try:
            resp = await self._http.request(
                "PROPFIND",
                self._webdav_url.rstrip("/") + "/",
                headers={"Depth": "infinity", "Content-Type": "application/xml"},
                content=_TREE_PROPFIND_BODY,
            )
        except Exception as e:
            # Transport trouble is not a capability verdict — fall back for
            # THIS pull only (the walk sees the same network and reports the
            # real error under the caller's strict rules).
            logger.debug("Depth-infinity PROPFIND failed (%s); using walk", e)
            return None
        if resp.status_code != 207:
            self._depth_infinity_unsupported = True
            logger.info(
                "Depth-infinity PROPFIND unsupported here (HTTP %d) — "
                "per-directory walk from now on",
                resp.status_code,
            )
            return None
        try:
            return self._parse_multistatus(resp.text)
        except Exception as e:
            logger.warning("Depth-infinity PROPFIND parse failed (%s); using walk", e)
            return None

    def _parse_multistatus(self, xml_text: str) -> list[dict]:
        """Shape a DAV multistatus into the sync algorithm's listing dicts.

        Only the 200-status propstat of each response carries live props
        (404 propstats enumerate the props a node lacks — e.g. no
        getcontentlength on collections). Hrefs are percent-encoded and
        server-absolute; both sides of the base-path strip are unquoted so
        encoding differences can't break the prefix match.
        """
        root = ET.fromstring(xml_text)
        base = unquote(self._webdav_base_path)
        out: list[dict] = []
        for resp in root.iter("{DAV:}response"):
            href_el = resp.find("{DAV:}href")
            if href_el is None or not href_el.text:
                continue
            href = unquote(href_el.text)
            prop = None
            for ps in resp.findall("{DAV:}propstat"):
                status_el = ps.find("{DAV:}status")
                if status_el is not None and " 200 " in f" {status_el.text or ''} ":
                    prop = ps.find("{DAV:}prop")
                    break
            if prop is None:
                continue
            rtype = prop.find("{DAV:}resourcetype")
            isdir = rtype is not None and rtype.find("{DAV:}collection") is not None
            etag_el = prop.find("{DAV:}getetag")
            etag = (etag_el.text or "") if etag_el is not None else ""
            size_el = prop.find("{DAV:}getcontentlength")
            try:
                size: Optional[int] = (
                    int(size_el.text)
                    if size_el is not None and size_el.text not in (None, "")
                    else None
                )
            except (TypeError, ValueError):
                size = None
            rel = href[len(base) :] if href.startswith(base) else href.strip("/")
            rel = rel.strip("/")
            if not rel:
                continue  # the mount collection itself
            out.append({"path": rel, "etag": etag, "isdir": isdir, "size": size})
        return out

    async def _download_file(self, rel_path: str, local_path: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(
            client.download_sync,
            remote_path=rel_path,
            local_path=local_path,
        )

    async def aclose(self) -> None:
        # webdav3 client has no explicit close; drop the reference so GC reclaims.
        self._client = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
