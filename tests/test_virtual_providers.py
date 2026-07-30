import pytest

from src.core.virtual_dirs import (
    ContactsProvider,
    SingleFileProvider,
    ToolsProvider,
    build_instruction_providers,
)
from src.core.virtual_dirs import contacts_provider as contacts_provider_module
from src.tools.description_manager import generate_tool_index


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description


def test_tools_provider_lists_index_and_each_tool():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file.")])
    assert set(provider.entries()) == {"README.md", "read_file.md"}


def test_readme_matches_canonical_renderer():
    tools = [FakeTool("read_file", "Reads a file.")]
    provider = ToolsProvider(lambda: tools)
    assert provider.read("README.md") == generate_tool_index(["read_file"])


def test_tool_doc_contains_full_docstring():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "Reads a file fully.")])
    assert "Reads a file fully." in provider.read("read_file.md")


def test_unknown_tool_returns_none():
    provider = ToolsProvider(lambda: [FakeTool("read_file", "d")])
    assert provider.read("gone.md") is None


def test_tool_list_changes_are_reflected_without_reregistration():
    """The workspace-upgrade re-derive changes the tool list mid-lifecycle."""
    tools = [FakeTool("read_file", "d")]
    provider = ToolsProvider(lambda: tools)
    assert "run_command.md" not in provider.entries()
    tools.append(FakeTool("run_command", "Runs a command."))
    assert "run_command.md" in provider.entries()
    assert "Runs a command." in provider.read("run_command.md")


def test_provider_flags():
    provider = ToolsProvider(lambda: [])
    assert provider.prefix == "tools" and provider.is_dir and not provider.writable


def test_single_file_provider_serves_one_entry():
    provider = SingleFileProvider("instructions.md", lambda: "# Instructions\n")
    assert set(provider.entries()) == {"instructions.md"}
    assert provider.read("instructions.md") == "# Instructions\n"
    assert provider.read("other.md") is None
    assert not provider.is_dir and not provider.writable


def test_single_file_provider_renders_lazily():
    calls = []

    def render():
        calls.append(1)
        return "body"

    provider = SingleFileProvider("task_brief.md", render)
    provider.read("task_brief.md")
    provider.read("task_brief.md")
    assert len(calls) == 2  # always live, never cached


def _providers(uploaded=None, template="TEMPLATE", brief="# Task Brief\n"):
    return {
        p.prefix: p
        for p in build_instruction_providers(
            uploaded=lambda: uploaded,
            template=lambda: template,
            brief=lambda: brief,
        )
    }


def test_builds_both_instruction_files():
    assert set(_providers()) == {"instructions.md", "task_brief.md"}


def test_uploaded_instructions_beat_the_template():
    provider = _providers(uploaded="UPLOADED")["instructions.md"]
    assert provider.read("instructions.md") == "UPLOADED"


def test_template_is_used_when_no_upload():
    provider = _providers(uploaded=None)["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_blank_upload_falls_back_to_template():
    provider = _providers(uploaded="   ")["instructions.md"]
    assert provider.read("instructions.md") == "TEMPLATE"


def test_source_precedence_is_inline_then_upload_then_template():
    """Mirrors the agent's `_uploaded_instructions` closure contract."""

    def resolver(inline, upload):
        def _resolve():
            if inline and inline.strip():
                return inline
            return upload

        return _resolve

    def _read(inline, upload):
        providers = {
            p.prefix: p
            for p in build_instruction_providers(
                uploaded=resolver(inline, upload),
                template=lambda: "TEMPLATE",
                brief=lambda: "",
            )
        }
        return providers["instructions.md"].read("instructions.md")

    assert _read("INLINE", "UPLOAD") == "INLINE"
    assert _read(None, "UPLOAD") == "UPLOAD"
    assert _read(None, None) == "TEMPLATE"


def test_task_brief_is_served_from_the_callable():
    provider = _providers(brief="# Task Brief\n\nDo the thing.")["task_brief.md"]
    assert "Do the thing." in provider.read("task_brief.md")


def test_instruction_providers_are_read_only():
    for provider in _providers().values():
        assert not provider.writable and not provider.is_dir


CONTACTS = [
    {
        "display_name": "Anna Weber",
        "notes": "Head of Operations.",
        "addresses": [
            {"channel": "email", "address": "anna@acme.de", "is_primary": True}
        ],
        "projects": [{"name": "Acme Website"}],
    }
]


def test_contacts_entries_include_index_and_each_contact():
    provider = ContactsProvider(lambda: CONTACTS)
    assert set(provider.entries()) == {"README.md", "anna-weber.md"}


def test_contact_file_carries_name_and_notes():
    body = ContactsProvider(lambda: CONTACTS).read("anna-weber.md")
    assert "Anna Weber" in body and "Head of Operations." in body


def test_readme_lists_contacts():
    assert "Anna Weber" in ContactsProvider(lambda: CONTACTS).read("README.md")


def test_slug_collisions_are_deterministic():
    duplicates = [dict(CONTACTS[0]), dict(CONTACTS[0])]
    names = set(ContactsProvider(lambda: duplicates).entries())
    assert {"anna-weber.md", "anna-weber-2.md"} <= names


def test_empty_project_renders_an_empty_index():
    provider = ContactsProvider(lambda: [])
    assert set(provider.entries()) == {"README.md"}
    assert "no contacts" in provider.read("README.md").lower()


def test_fetch_happens_once_per_ttl_window():
    calls = []

    def fetch():
        calls.append(1)
        return CONTACTS

    provider = ContactsProvider(fetch, ttl_seconds=3600)
    provider.entries()
    provider.read("anna-weber.md")
    assert len(calls) == 1


def test_stale_cache_is_served_when_the_fetch_fails():
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("orchestrator down")
        return CONTACTS

    provider = ContactsProvider(fetch, ttl_seconds=0)
    assert provider.read("anna-weber.md")
    state["fail"] = True
    assert "Anna Weber" in provider.read("anna-weber.md")  # stale, not an error


def test_error_when_the_fetch_fails_with_a_cold_cache():
    def fetch():
        raise RuntimeError("orchestrator down")

    with pytest.raises(ValueError, match="temporarily unavailable"):
        ContactsProvider(fetch).entries()


def test_repeated_reads_during_an_outage_attempt_only_one_fetch_per_window(
    monkeypatch,
):
    """Once the TTL lapses during an outage, retries must be throttled to the
    same cadence as successes — not re-attempted on every single read.

    Regression: stamping ``_fetched_at`` only on success means a failed
    attempt never advances the clock, so every subsequent read keeps seeing
    a stale timestamp and keeps retrying (and paying the client timeout)
    until the fetch succeeds again.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(contacts_provider_module.time, "monotonic", lambda: clock["t"])

    state = {"fail": False}
    calls = []

    def fetch():
        calls.append(1)
        if state["fail"]:
            raise RuntimeError("orchestrator down")
        return CONTACTS

    provider = ContactsProvider(fetch, ttl_seconds=60)
    provider.read("anna-weber.md")  # warms the cache: 1 fetch so far
    state["fail"] = True
    clock["t"] = 61  # TTL has lapsed; the next read must retry exactly once

    for _ in range(5):
        assert "Anna Weber" in provider.read("anna-weber.md")  # stale-served

    assert len(calls) == 2  # the warm fetch + exactly one retry, not five


# ---------------------------------------------------------------------------
# Search cost, measured on the REAL providers (not a stand-in).
# ---------------------------------------------------------------------------


def _real_tool_names(count=10):
    from src.tools.registry import TOOL_REGISTRY

    return sorted(TOOL_REGISTRY)[:count]


_REAL_TOOL_NAMES = _real_tool_names()


def test_root_search_renders_each_tool_doc_once(tmp_path, monkeypatch):
    """Regression: a root search re-rendered every tool doc once per entry.

    ``ToolsProvider.read`` renders the WHOLE set to return one document, and the
    overlay called ``entries()`` (another full render) then ``read()`` per name.
    At the production tool count (~40) that is ~1,700 ``generate_tool_description``
    invocations for a single ``search_files`` on the agent's request path.
    """
    from src.core.backends.overlay import VirtualOverlayBackend
    from src.tools.description_manager import DescriptionManager
    from tests._fs_backend import FilesystemTestBackend

    rendered = []
    real = DescriptionManager.generate_tool_description

    def counting(self, name):
        rendered.append(name)
        return real(self, name)

    monkeypatch.setattr(DescriptionManager, "generate_tool_description", counting)

    # Real registry names: an unregistered name short-circuits to a stub and
    # would not exercise the rendering cost this test is about.
    tools = [FakeTool(name, "Does the needle thing.") for name in _REAL_TOOL_NAMES]
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(ToolsProvider(lambda: tools))

    hits = overlay.search_files("needle")

    assert len(hits) == len(tools)
    assert len(rendered) == len(tools)  # one pass, not one per entry


def test_root_search_reflects_a_tool_list_changed_since_the_last_search(tmp_path):
    """The one-pass optimization must not memoize across calls."""
    from src.core.backends.overlay import VirtualOverlayBackend
    from tests._fs_backend import FilesystemTestBackend

    first, second = _REAL_TOOL_NAMES[0], _REAL_TOOL_NAMES[1]
    tools = [FakeTool(first, "Does the needle thing.")]
    overlay = VirtualOverlayBackend(FilesystemTestBackend(tmp_path))
    overlay.register(ToolsProvider(lambda: tools))

    assert len(overlay.search_files("needle")) == 1
    tools.append(FakeTool(second, "Does the needle thing too."))
    assert len(overlay.search_files("needle")) == 2
