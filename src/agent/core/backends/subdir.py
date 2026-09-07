"""SubdirBackend — a re-rooted VIEW over an existing workspace backend.

Wraps a parent `WorkspaceBackend` and re-roots every workspace-relative path
under a subdirectory (e.g. a light subagent's git worktree), delegating to the
parent so both share the parent's single connection (one SSH/SFTP transport, no
second login). Workspace-relative *input* paths are prefixed with the subdir;
root-relative *output* paths (`list_dir`/`search_files`/`walk`) have the prefix
stripped so results remain valid reader-relative paths.

HOME-relative operations (`write_home_file`/`resolve_home_path`) pass through to
the parent unchanged — they are not workspace-rooted. This is a duck-typed proxy
(not a `WorkspaceBackend` subclass): anything not overridden is delegated via
`__getattr__`. No isinstance checks target backend types in the codebase, so
this is safe.

Shell is the one shared resource that needs care: readers share the parent's
tmux session (same connection), so an optional ``shell_tab_prefix`` namespaces
every tab name — parallel readers never collide on the default ``"default"`` tab
— and shell commands default their working directory to the subdir. The proxy
deliberately does NOT expose ``shell_cleanup`` (which kills the whole shared
session); teardown uses ``close_reader_tabs()`` to close only this reader's tabs.

Used by the light-subagent reader environment so each reader's file/git/shell
tools operate inside its own worktree while sharing the parent's connection.
See knowledge-base/knowledge/issues/delegation_light_mode_missing.md (Phase 2).
"""

import logging
import posixpath
from typing import Any, List

logger = logging.getLogger(__name__)


class SubdirBackend:
    """A path-prefixing view of ``parent`` rooted at ``subdir``."""

    def __init__(self, parent: Any, subdir: str, *, shell_tab_prefix: str = ""):
        self._parent = parent
        self._subdir = subdir.strip("/")
        self._tab_prefix = shell_tab_prefix

    # --- path translation -------------------------------------------------

    def _p(self, path: str) -> str:
        """Workspace-relative (reader) path -> parent-relative path."""
        if not path or path == ".":
            return self._subdir
        return posixpath.join(self._subdir, path.lstrip("/"))

    def _strip(self, path: str) -> str:
        """Parent-relative (root) path -> reader-relative path.

        Preserves any trailing slash (directory marker). Defensive: a path that
        is not under the subdir is returned unchanged.
        """
        prefix = self._subdir + "/"
        if path == self._subdir or path == prefix:
            return ""
        if path.startswith(prefix):
            return path[len(prefix) :]
        return path

    # --- properties -------------------------------------------------------

    @property
    def root(self) -> str:
        return posixpath.join(self._parent.root, self._subdir)

    # --- path-taking file ops (prefix inputs) -----------------------------

    def read_file(self, path: str, binary: bool = False):
        return self._parent.read_file(self._p(path), binary=binary)

    def write_file(self, path: str, content) -> None:
        return self._parent.write_file(self._p(path), content)

    def append_file(self, path: str, content: str) -> None:
        return self._parent.append_file(self._p(path), content)

    def exists(self, path: str) -> bool:
        return self._parent.exists(self._p(path))

    def is_file(self, path: str) -> bool:
        return self._parent.is_file(self._p(path))

    def is_dir(self, path: str) -> bool:
        return self._parent.is_dir(self._p(path))

    def mkdir(self, path: str) -> None:
        return self._parent.mkdir(self._p(path))

    def delete_file(self, path: str) -> bool:
        return self._parent.delete_file(self._p(path))

    def delete_directory(self, path: str) -> bool:
        return self._parent.delete_directory(self._p(path))

    def stat(self, path: str) -> int:
        return self._parent.stat(self._p(path))

    def resolve_path(self, relative_path: str) -> str:
        # Returns an absolute path on the parent — no reader-relative stripping.
        return self._parent.resolve_path(self._p(relative_path))

    def move(self, src: str, dst: str) -> None:
        return self._parent.move(self._p(src), self._p(dst))

    def copy(self, src: str, dst: str) -> None:
        return self._parent.copy(self._p(src), self._p(dst))

    # --- listing ops (prefix inputs, strip outputs) -----------------------

    def list_dir(self, path: str = "", pattern: str = "*") -> List[str]:
        entries = self._parent.list_dir(self._p(path), pattern)
        return [self._strip(e) for e in entries]

    def search_files(
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: list[str] | None = None,
    ) -> List[dict]:
        results = self._parent.search_files(
            query, self._p(path), case_sensitive, exclude_dirs=exclude_dirs
        )
        out = []
        for r in results:
            r = dict(r)
            if "path" in r:
                r["path"] = self._strip(r["path"])
            out.append(r)
        return out

    def walk(self, path: str = "") -> List[str]:
        return sorted(self._strip(p) for p in self._parent.walk(self._p(path)))

    # --- shell (prefix working_dir + tab names; session is shared) --------

    def _tab(self, name: str) -> str:
        return f"{self._tab_prefix}{name}" if self._tab_prefix else name

    def shell_run(
        self,
        command: str,
        timeout=None,
        tab_name: str = "default",
        working_dir=None,
    ) -> str:
        # working_dir is workspace-relative → re-root it under the subdir so the
        # reader's shell runs inside its worktree by default.
        wd = self._p(working_dir) if working_dir else self._subdir
        return self._parent.shell_run(
            command, timeout=timeout, tab_name=self._tab(tab_name), working_dir=wd
        )

    def shell_ensure_tab(self, name: str) -> None:
        return self._parent.shell_ensure_tab(self._tab(name))

    def shell_open_tab(self, name: str, *args, **kwargs):
        return self._parent.shell_open_tab(self._tab(name), *args, **kwargs)

    def shell_send(
        self,
        name: str,
        text: str,
        enter: bool = True,
        working_dir=None,
        allow_busy: bool = False,
    ):
        wd = self._p(working_dir) if working_dir else None
        return self._parent.shell_send(
            self._tab(name),
            text,
            enter=enter,
            working_dir=wd,
            allow_busy=allow_busy,
        )

    def shell_read(self, name: str, *args, **kwargs):
        return self._parent.shell_read(self._tab(name), *args, **kwargs)

    def shell_read_with_offset(self, name: str, *args, **kwargs):
        return self._parent.shell_read_with_offset(self._tab(name), *args, **kwargs)

    def shell_close_tab(self, name: str) -> str:
        return self._parent.shell_close_tab(self._tab(name))

    def shell_cancel(self, name: str = "default", *args, **kwargs):
        return self._parent.shell_cancel(self._tab(name), *args, **kwargs)

    def shell_list_tabs(self) -> List[dict]:
        # Show only this reader's tabs, with the namespace prefix stripped.
        tabs = self._parent.shell_list_tabs()
        if not self._tab_prefix:
            return tabs
        out = []
        for t in tabs:
            name = t.get("name", "")
            if name.startswith(self._tab_prefix):
                t = dict(t)
                t["name"] = name[len(self._tab_prefix) :]
                out.append(t)
        return out

    def close_reader_tabs(self) -> None:
        """Close only this reader's namespaced tabs — never the shared session.

        The reader must NOT call ``shell_cleanup`` (that kills the whole tmux
        session shared with the parent and siblings). Best-effort; logged.
        """
        if not self._tab_prefix:
            return
        try:
            tabs = self._parent.shell_list_tabs()
        except Exception as e:
            logger.warning("close_reader_tabs: could not list tabs: %s", e)
            return
        for t in tabs:
            name = t.get("name", "")
            if name.startswith(self._tab_prefix):
                try:
                    self._parent.shell_close_tab(name)
                except Exception as e:
                    logger.warning("close_reader_tabs: failed to close %s: %s", name, e)

    # --- everything else delegates to the shared parent -------------------

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined above (host, supports_shell,
        # supports_file_tools, connect/disconnect/is_connected, exec_command,
        # write_home_file/resolve_home_path, etc.). Connection- or home-scoped,
        # not workspace-rooted, so they pass through unchanged. NOTE:
        # shell_cleanup passes through too — callers must use close_reader_tabs()
        # for reader teardown, never shell_cleanup (which kills the session).
        return getattr(self._parent, name)
