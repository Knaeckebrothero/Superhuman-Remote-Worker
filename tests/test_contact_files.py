"""contacts/<slug>.md rendering — pure functions, no I/O."""

from agent.core.contact_files import (
    contact_slug,
    contacts_to_workspace_files,
    render_contact_md,
)


def _anna():
    return {
        "display_name": "Anna Weber",
        "notes": "Head of Operations. Prefers short messages, CET.",
        "addresses": [
            {
                "channel": "email",
                "address": "anna@acme.de",
                "is_primary": True,
                "opt_in_status": "opted_in",
            },
            {
                "channel": "whatsapp",
                "address": "+4917055501",
                "is_primary": True,
                "opt_in_status": "pending",
            },
        ],
        "projects": [{"id": "p1", "name": "Acme Website"}],
    }


def test_slug_kebab_sanitize_collide():
    taken: set[str] = set()
    assert contact_slug("Anna Weber", taken) == "anna-weber"
    taken.add("anna-weber")
    assert contact_slug("Anna Weber", taken) == "anna-weber-2"
    assert contact_slug("Ümläut / Sla$h!", set()) == "umlaut-sla-h"
    assert contact_slug("", set()) == "contact"


def test_render_frontmatter_and_body():
    md = render_contact_md({**_anna(), "_slug": "anna-weber"})
    assert md.startswith("---\n")
    assert 'name: "anna-weber"' in md
    assert 'display_name: "Anna Weber"' in md
    assert '"channel": "whatsapp"' in md and '"opt_in": "pending"' in md
    # email is opted_in → no opt_in noise on that line
    assert md.count('"opt_in"') == 1
    assert md.rstrip().endswith("Prefers short messages, CET.")


def test_files_dict_paths_and_collisions():
    files = contacts_to_workspace_files([_anna(), {**_anna(), "notes": None}])
    assert set(files) == {"contacts/anna-weber.md", "contacts/anna-weber-2.md"}
    assert files["contacts/anna-weber-2.md"].rstrip().endswith("---")  # empty body ok
