import pytest

from src.core.backends.overlay import EntryMeta, VirtualOverlayBackend, VirtualPathError
from src.core.workspace_backend import SEARCH_RESULT_HARD_CAP
from tests._fs_backend import FilesystemTestBackend


class FakeProvider:
    """Read-only directory provider serving two fixed files."""

    prefix = "tools"
    is_dir = True
    writable = False

    def __init__(self, docs=None):
        self.docs = (
            docs
            if docs is not None
            else {
                "README.md": "# Available Tools\n\n- read_file\n",
                "read_file.md": "# read_file\n\nReads a file.\n",
            }
        )
        self.read_calls = 0

    def entries(self):
        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self.docs.items()
        }

    def read(self, name):
        self.read_calls += 1
        return self.docs.get(name)


@pytest.fixture
def overlay(tmp_path):
    inner = FilesystemTestBackend(tmp_path)
    ov = VirtualOverlayBackend(inner)
    ov.register(FakeProvider())
    return ov


def test_reads_virtual_file(overlay):
    assert "Reads a file." in overlay.read_file("tools/read_file.md")


def test_unknown_name_under_prefix_is_not_found_not_fallthrough(overlay, tmp_path):
    # A stale real file must NEVER be served once the prefix is virtual.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "removed_tool.md").write_text("STALE")
    with pytest.raises(FileNotFoundError):
        overlay.read_file("tools/removed_tool.md")


def test_real_paths_delegate(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("real")
    assert overlay.read_file("notes.md") == "real"


def test_nested_prefix_is_not_virtual(overlay, tmp_path):
    # `main/tools/` inside a cloned repo must stay real.
    (tmp_path / "main" / "tools").mkdir(parents=True)
    (tmp_path / "main" / "tools" / "x.md").write_text("repo file")
    assert overlay.read_file("main/tools/x.md") == "repo file"


def test_path_forms_normalize(overlay):
    for form in ("tools/README.md", "./tools/README.md", "/tools/README.md"):
        assert "Available Tools" in overlay.read_file(form)


def test_binary_read_returns_bytes(overlay):
    assert overlay.read_file("tools/README.md", binary=True).startswith(b"# Available")


def test_existence_and_type_predicates(overlay):
    assert overlay.exists("tools") and overlay.is_dir("tools")
    assert overlay.exists("tools/README.md") and overlay.is_file("tools/README.md")
    assert not overlay.is_dir("tools/README.md")
    assert not overlay.exists("tools/nope.md")


def test_stat_reports_rendered_size(overlay):
    assert overlay.stat("tools/README.md") == len(
        "# Available Tools\n\n- read_file\n".encode("utf-8")
    )


def test_stat_returns_zero_for_a_missing_virtual_entry(overlay):
    # WorkspaceBackend.stat contract (workspace_backend.py:448): "0 if path
    # doesn't exist". RemoteBackend and FilesystemTestBackend both honour it;
    # a virtual path must not diverge.
    assert overlay.stat("tools/nope.md") == 0


def test_provider_failure_surfaces_readable_error(tmp_path):
    class Broken(FakeProvider):
        def read(self, name):
            raise RuntimeError("upstream down")

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Broken())
    with pytest.raises(ValueError, match="temporarily unavailable"):
        ov.read_file("tools/README.md")


def test_unknown_attributes_delegate_to_inner(overlay):
    assert overlay.root == overlay.inner.root


def test_root_listing_merges_virtual_prefix(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("hi")
    listing = overlay.list_dir("")
    assert "notes.md" in listing
    assert "tools/" in listing


def test_root_listing_dedupes_real_leftover(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("x")
    assert overlay.list_dir("").count("tools/") == 1


def test_listing_inside_prefix_comes_from_provider(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("x")
    assert sorted(overlay.list_dir("tools")) == [
        "tools/README.md",
        "tools/read_file.md",
    ]


def test_listing_respects_pattern(overlay):
    assert overlay.list_dir("tools", "READ*") == ["tools/README.md"]


def test_nonroot_listing_gets_no_virtual_entries(overlay, tmp_path):
    (tmp_path / "sub").mkdir()
    assert overlay.list_dir("sub") == []


def test_search_inside_prefix_searches_provider_only(overlay, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "stale.md").write_text("Reads a file.")
    hits = overlay.search_files("Reads a file", path="tools")
    assert [h["path"] for h in hits] == ["tools/read_file.md"]
    assert hits[0]["line_number"] == 3


def test_root_search_merges_real_and_virtual(overlay, tmp_path):
    (tmp_path / "notes.md").write_text("read_file is handy\n")
    paths = {h["path"] for h in overlay.search_files("read_file")}
    assert "notes.md" in paths
    assert "tools/README.md" in paths


def test_search_is_case_insensitive_by_default(overlay):
    assert overlay.search_files("READS A FILE")
    assert not overlay.search_files("READS A FILE", case_sensitive=True)


def test_search_respects_hard_cap(tmp_path):
    body = "\n".join(["needle"] * (SEARCH_RESULT_HARD_CAP + 50))

    class Big(FakeProvider):
        def __init__(self):
            super().__init__({"big.md": body})

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Big())
    assert len(ov.search_files("needle")) == SEARCH_RESULT_HARD_CAP


def test_search_scoped_to_a_single_virtual_file(overlay):
    # Naming one entry must search THAT entry, never its siblings.
    hits = overlay.search_files("Reads a file", path="tools/read_file.md")
    assert [h["path"] for h in hits] == ["tools/read_file.md"]
    assert overlay.search_files("Available Tools", path="tools/read_file.md") == []


def test_listing_below_a_virtual_entry_is_empty(overlay):
    assert overlay.list_dir("tools/README.md") == []


@pytest.mark.parametrize(
    "op",
    [
        lambda ov: ov.write_file("tools/x.md", "nope"),
        lambda ov: ov.append_file("tools/README.md", "nope"),
        lambda ov: ov.mkdir("tools/sub"),
        lambda ov: ov.delete_file("tools/README.md"),
        lambda ov: ov.delete_directory("tools"),
        lambda ov: ov.move("tools/README.md", "copy.md"),
        lambda ov: ov.move("real.md", "tools/README.md"),
        lambda ov: ov.copy("real.md", "tools/x.md"),
    ],
)
def test_mutations_on_readonly_virtual_paths_are_rejected(overlay, tmp_path, op):
    (tmp_path / "real.md").write_text("real")
    with pytest.raises(VirtualPathError) as excinfo:
        op(overlay)
    assert "virtual" in str(excinfo.value).lower()


def test_copy_out_of_virtual_to_real_is_allowed(overlay, tmp_path):
    overlay.copy("tools/README.md", "my_tools.md")
    assert "Available Tools" in (tmp_path / "my_tools.md").read_text()


def test_real_to_real_mutations_still_delegate(overlay, tmp_path):
    overlay.write_file("scratch.md", "content")
    assert (tmp_path / "scratch.md").read_text() == "content"
    overlay.delete_file("scratch.md")
    assert not (tmp_path / "scratch.md").exists()


def test_writable_provider_receives_writes(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def __init__(self):
            super().__init__({"plan.md": "# Plan\n"})
            self.written = None

        def write(self, name, content):
            self.written = (name, content)
            self.docs[name] = content

    provider = Writable()
    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(provider)
    ov.write_file("plan.md", "# New Plan\n")
    assert provider.written == ("plan.md", "# New Plan\n")
    assert ov.read_file("plan.md") == "# New Plan\n"


def test_writable_provider_still_rejects_delete(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            self.docs[name] = content

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Writable({"plan.md": "x"}))
    with pytest.raises(VirtualPathError):
        ov.delete_file("plan.md")


def test_writable_provider_receives_appends(tmp_path):
    class Writable(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            self.docs[name] = content

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Writable({"plan.md": "# Plan\n"}))
    ov.append_file("plan.md", "- step one\n")
    assert ov.read_file("plan.md") == "# Plan\n- step one\n"


def test_writable_provider_write_failure_is_a_readable_error(tmp_path):
    class Exploding(FakeProvider):
        prefix = "plan.md"
        is_dir = False
        writable = True

        def write(self, name, content):
            raise RuntimeError("database down")

    ov = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    ov.register(Exploding({"plan.md": "# Plan\n"}))
    with pytest.raises(ValueError, match="could not be written"):
        ov.write_file("plan.md", "# New\n")


# ---------------------------------------------------------------------------
# Search cost: one render pass per search, not one per entry.
# ---------------------------------------------------------------------------


class _CountingToolsProvider:
    """ToolsProvider-shaped provider that counts per-document renders.

    Mirrors the real cost model: ToolsProvider.read() re-renders the WHOLE
    document set on every call, so entries()+read()-per-name is quadratic in
    the tool count on the agent's request path.
    """

    prefix = "tools"
    is_dir = True
    writable = False

    def __init__(self, names):
        self._names = list(names)
        self.renders = 0

    def _render_all(self):
        docs = {"README.md": "# Available Tools\n"}
        for name in self._names:
            self.renders += 1
            docs[f"{name}.md"] = f"# {name}\n\nneedle in {name}\n"
        return docs

    def entries(self):
        from src.core.backends.overlay import EntryMeta

        return {
            name: EntryMeta(size=len(body.encode("utf-8")))
            for name, body in self._render_all().items()
        }

    def read(self, name):
        return self._render_all().get(name)

    def read_all(self):
        return self._render_all()


def test_root_search_renders_each_document_once(tmp_path):
    """Regression: root search was O(N^2) markdown renders.

    _search_provider called entries() (a full render) and then read() per entry
    — and ToolsProvider.read() re-renders everything per call. With ~40 tools
    that is ~1,700 generate_tool_description invocations per root search, paid
    on the agent's request path.
    """
    from src.core.backends.overlay import VirtualOverlayBackend
    from tests._fs_backend import FilesystemTestBackend

    names = [f"tool_{i}" for i in range(10)]
    provider = _CountingToolsProvider(names)
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(provider)

    hits = overlay.search_files("needle")

    assert len(hits) == len(names)
    # One pass over the document set, not one pass per entry.
    assert provider.renders == len(names)


def test_search_still_reflects_the_current_document_set(tmp_path):
    """Freshness must survive the optimization — no cross-call memoization."""
    from src.core.backends.overlay import VirtualOverlayBackend
    from tests._fs_backend import FilesystemTestBackend

    provider = _CountingToolsProvider(["tool_a"])
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(provider)

    assert len(overlay.search_files("needle")) == 1
    provider._names.append("tool_b")
    assert len(overlay.search_files("needle")) == 2


def test_search_falls_back_when_a_provider_has_no_read_all(tmp_path):
    """read_all() is optional — providers without it must still be searchable."""
    from src.core.backends.overlay import EntryMeta, VirtualOverlayBackend
    from tests._fs_backend import FilesystemTestBackend

    class Minimal:
        prefix = "notes_v"
        is_dir = True
        writable = False

        def entries(self):
            return {"a.md": EntryMeta(size=6)}

        def read(self, name):
            return "needle" if name == "a.md" else None

    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(Minimal())

    hits = overlay.search_files("needle")
    assert [h["path"] for h in hits] == ["notes_v/a.md"]
