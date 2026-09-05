"""Archived projects must not be offered as targets for new work.

Live incident 2026-08-15: "Better Resavio" was split, leaving "Better Resavio
(pre-split archive)" behind under a near-identical name. An officer was
commissioned onto the ARCHIVE and ran a full watch there — dispatching three
workers against a project that exists only as a historical record — because
the only signal distinguishing the two was a parenthesised ``(archived)``
among many rows of a project listing.

Two properties are pinned here and they pull against each other: the archive
must not appear where someone is choosing a target, and it must not vanish
so completely that a caller hunting a project they know exists concludes it
was deleted.

This file covers the PRESENTATION half only — it was the whole of the 08-15
mitigation, and the server went on accepting every write. The server half (the
commission endpoint refusing, the auto-pull tick standing down, and the other
paths that create work) is pinned in
``tests/test_archived_projects_refuse_new_work.py``.
"""

from shared.orch_surface.formatters import format_project_detail, format_projects

LIVE = {
    "id": "a572e4a0-d97a-4103-91fd-92a980d6717d",
    "name": "Better Resavio",
    "status": "active",
    "goal": "Build a receptionist product for Hotel Rheinland",
    "updated_at": "2026-08-15T19:00:00Z",
}
ARCHIVE = {
    "id": "68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a",
    "name": "Better Resavio (pre-split archive)",
    "status": "archived",
    "goal": "Build a receptionist product for Hotel Rheinland",
    "updated_at": "2026-08-13T12:00:00Z",
}


class TestListing:
    def test_archived_is_hidden_by_default(self):
        out = format_projects([LIVE, ARCHIVE])
        assert "pre-split archive" not in out
        assert "Better Resavio" in out
        assert "Found 1 project(s)" in out

    def test_hiding_is_reported_never_silent(self):
        # The failure this guards: a caller looking for a project they know
        # exists must learn it was withheld, not infer it was deleted.
        out = format_projects([LIVE, ARCHIVE])
        assert "1 archived project(s) hidden" in out
        assert "include_archived=true" in out

    def test_opt_in_shows_them_loudly_marked(self):
        out = format_projects([LIVE, ARCHIVE], include_archived=True)
        assert "pre-split archive" in out
        assert "[ARCHIVED]" in out
        assert "Found 2 project(s)" in out
        # And the live one is NOT marked.
        live_line = next(
            line
            for line in out.splitlines()
            if line.strip().startswith("Better Resavio")
        )
        assert "[ARCHIVED]" not in live_line

    def test_all_archived_explains_itself_rather_than_reading_as_empty(self):
        out = format_projects([ARCHIVE])
        assert "No active projects found" in out
        assert "1 archived" in out
        assert "include_archived=true" in out

    def test_no_projects_at_all_is_unchanged(self):
        assert format_projects([]) == "No projects found."

    def test_unknown_or_missing_status_is_treated_as_visible(self):
        # Fail toward showing: a project whose status the caller cannot read
        # must not silently disappear from every listing.
        out = format_projects([{"id": "x", "name": "Mystery"}])
        assert "Mystery" in out
        assert "hidden" not in out


class TestDetail:
    def test_archived_detail_leads_with_a_warning(self):
        out = format_project_detail(ARCHIVE)
        assert out.startswith("[ARCHIVED]")
        # The reader of this output is deciding whether to ACT on the project.
        assert "Do not dispatch work" in out
        assert "commission an officer" in out

    def test_active_detail_carries_no_warning(self):
        out = format_project_detail(LIVE)
        assert "[ARCHIVED]" not in out
        assert out.startswith("Project: Better Resavio")

    def test_case_is_not_load_bearing(self):
        out = format_project_detail({**ARCHIVE, "status": "Archived"})
        assert out.startswith("[ARCHIVED]")
