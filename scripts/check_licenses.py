#!/usr/bin/env python3
"""Third-party license policy gate + THIRD_PARTY_LICENSES.md generator.

Single source of truth for which licenses are allowed in the *bundled* set —
the Python packages installed into our images and the npm production packages
bundled into the cockpit build. Server software the Helm chart pulls from public
registries (Neo4j, PostgreSQL, MongoDB) is NOT in scope here; we reference those
images, we don't convey them. Their *client drivers* (neo4j, psycopg, pymongo)
ARE bundled and so ARE covered.

Two responsibilities, one parsed inventory:

  --check    Classify every bundled dependency and FAIL (exit 1) if any carries
             a denied license (strong copyleft / source-available). UNKNOWN
             licenses warn by default; --strict escalates them to failures.
             This is the CI gate (runs inside the dependency-audit job).

  --write P  Additionally regenerate the inventory + NOTICE sections of the
             THIRD_PARTY_LICENSES.md at path P, injecting between the
             `<!-- BEGIN: id -->` / `<!-- END: id -->` markers. Curated prose
             outside those markers is preserved.

Inputs are produced by `pip-licenses` (Python) and `license-checker-rseidelsohn`
(npm). By default this script invokes those tools itself; pass --pip-json /
--npm-json to feed pre-captured JSON instead (used by the unit tests).

Policy is data, not code — tune ALLOW_TOKENS / WEAK_TOKENS / DENY_TOKENS and the
per-package OVERRIDES below. Mirrors the explicit, override-able style of the
pip-audit ignore list and the npm-audit gate in .github/workflows/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = REPO_ROOT / "THIRD_PARTY_LICENSES.md"
COCKPIT_DIR = REPO_ROOT / "cockpit"

# --- Policy -----------------------------------------------------------------
# Categories: ALLOW (permissive, ship freely), WEAK (weak copyleft — fine when
# used as an unmodified, replaceable library, which is our case for every one of
# these), DENY (strong copyleft or source-available — must NOT enter the bundled
# set: a GPL/AGPL Python lib pip-installed into an image WOULD reach our code).
#
# Substring match against the lowercased license string. Order in classify()
# matters: AGPL and LGPL both contain "gpl", so they are tested before GPL.

DENY_TOKENS = (
    "agpl", "affero",
    "sspl", "server side public",
    "business source", "busl", "bsl-1", "bsl 1",
    "commons clause",
    "elastic license", "elastic-2", "elastic 2",
    "prosperity", "polyform",
    "proprietary", "commercial",
    # Plain GPL is denied too, but checked after LGPL in classify().
)

WEAK_TOKENS = (
    "lgpl", "lesser general public",
    "mpl-2", "mpl 2", "mozilla public",
    "eclipse public", "epl-",
)

ALLOW_TOKENS = (
    "mit", "bsd", "apache", "isc",
    "python software foundation", "psf",
    "zlib", "0bsd", "unlicense", "wtfpl", "boost",
    "hpnd", "historical permission",
    "public domain", "cc0", "cc-0",
    "blueoak", "blue oak",
)

# Per-package decisions that override token classification. Keyed by lowercased
# package name. Use for dual-licensed packages reported under one banner, or
# packages whose metadata reports UNKNOWN but whose license is verified.
# Add a one-line justification for each entry.
OVERRIDES: dict[str, str] = {
    # Dual-licensed Apache-2.0 OR BSD; pip metadata sometimes reports just one.
    "cryptography": "ALLOW",
    # PSF/HPND-style; occasionally reported UNKNOWN by older metadata.
    "pillow": "ALLOW",
    # Example of an UNKNOWN-but-verified entry; remove once metadata improves:
    # "somepkg": "ALLOW",  # verified MIT at <url>
}

CATEGORY_DENY = "DENY"
CATEGORY_WEAK = "WEAK"
CATEGORY_ALLOW = "ALLOW"
CATEGORY_UNKNOWN = "UNKNOWN"


def classify(license_str: str, pkg_name: str) -> str:
    """Return the policy category for a (license, package) pair."""
    override = OVERRIDES.get(pkg_name.lower())
    if override:
        return override

    s = (license_str or "").lower()
    if not s or s in ("unknown", "unlicensed"):
        return CATEGORY_UNKNOWN

    # AGPL / LGPL contain "gpl" — test them before plain GPL.
    if "agpl" in s or "affero" in s:
        return CATEGORY_DENY
    if any(t in s for t in ("lgpl", "lesser general public")):
        return CATEGORY_WEAK
    if "gpl" in s or "general public license" in s:
        return CATEGORY_DENY
    if any(t in s for t in DENY_TOKENS):
        return CATEGORY_DENY
    if any(t in s for t in WEAK_TOKENS):
        return CATEGORY_WEAK
    if any(t in s for t in ALLOW_TOKENS):
        return CATEGORY_ALLOW
    return CATEGORY_UNKNOWN


# --- Dependency record ------------------------------------------------------


class Dep:
    __slots__ = ("name", "version", "license", "url", "notice", "ecosystem", "category")

    def __init__(self, name, version, license_, url="", notice="", ecosystem="py"):
        self.name = name
        self.version = version
        self.license = license_ or "UNKNOWN"
        self.url = url or ""
        self.notice = notice or ""
        self.ecosystem = ecosystem
        self.category = classify(self.license, name)


# --- Collectors -------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"::warning::`{' '.join(cmd)}` exited {proc.returncode}: "
            f"{proc.stderr.strip()[:500]}\n"
        )
    return proc.stdout


def collect_python(pip_json: str | None) -> list[Dep]:
    if pip_json is not None:
        raw = pip_json
    else:
        raw = _run([
            "pip-licenses", "--from=mixed", "--format=json",
            "--with-urls", "--with-notice-file", "--no-license-path",
        ])
    if not raw.strip():
        return []
    deps = []
    for row in json.loads(raw):
        deps.append(Dep(
            name=row.get("Name", "?"),
            version=row.get("Version", ""),
            license_=row.get("License", "UNKNOWN"),
            url=row.get("URL", ""),
            notice=row.get("NoticeText", "") or "",
            ecosystem="py",
        ))
    # Drop our own package and pip-licenses' "UNKNOWN" sentinel rows for noise.
    return [d for d in deps if d.name.lower() not in ("pip-licenses",)]


def collect_npm(npm_json: str | None) -> list[Dep]:
    if npm_json is not None:
        raw = npm_json
    elif (COCKPIT_DIR / "node_modules").is_dir():
        raw = _run([
            "npx", "--yes", "license-checker-rseidelsohn",
            "--json", "--production", "--start", str(COCKPIT_DIR),
        ])
    else:
        sys.stderr.write(
            "::warning::cockpit/node_modules absent; skipping npm license scan "
            "(run `npm ci` in cockpit/ to include it)\n"
        )
        return []
    if not raw.strip():
        return []
    deps = []
    for key, meta in json.loads(raw).items():
        # key is "name@version" (name may itself contain @scope/).
        at = key.rfind("@")
        name, version = (key[:at], key[at + 1:]) if at > 0 else (key, "")
        lic = meta.get("licenses", "UNKNOWN")
        if isinstance(lic, list):
            lic = " / ".join(lic)
        deps.append(Dep(
            name=name,
            version=version,
            license_=lic,
            url=meta.get("repository", ""),
            ecosystem="js",
        ))
    return [d for d in deps if d.name != "cockpit"]


# --- Reporting & gate -------------------------------------------------------


def report(deps: list[Dep], strict: bool) -> int:
    """Print a grouped report; return process exit code."""
    buckets: dict[str, list[Dep]] = {
        CATEGORY_DENY: [], CATEGORY_WEAK: [],
        CATEGORY_UNKNOWN: [], CATEGORY_ALLOW: [],
    }
    for d in deps:
        buckets[d.category].append(d)

    def line(d: Dep) -> str:
        return f"    {d.ecosystem}:{d.name}=={d.version}  [{d.license}]"

    print(f"Scanned {len(deps)} bundled dependencies "
          f"({len(buckets[CATEGORY_ALLOW])} allow, {len(buckets[CATEGORY_WEAK])} weak-copyleft, "
          f"{len(buckets[CATEGORY_UNKNOWN])} unknown, {len(buckets[CATEGORY_DENY])} denied).")

    if buckets[CATEGORY_WEAK]:
        print("\nWeak-copyleft (allowed — used as unmodified libraries):")
        for d in sorted(buckets[CATEGORY_WEAK], key=lambda x: x.name.lower()):
            print(line(d))

    if buckets[CATEGORY_UNKNOWN]:
        sev = "::error::" if strict else "::warning::"
        print(f"\n{sev}Unknown / unclassified licenses "
              f"({'failing — --strict' if strict else 'review and add an OVERRIDE'}):")
        for d in sorted(buckets[CATEGORY_UNKNOWN], key=lambda x: x.name.lower()):
            print(line(d))

    if buckets[CATEGORY_DENY]:
        print("\n::error::DENIED licenses found in the bundled set — these must not ship:")
        for d in sorted(buckets[CATEGORY_DENY], key=lambda x: x.name.lower()):
            print(line(d))

    failed = bool(buckets[CATEGORY_DENY]) or (strict and bool(buckets[CATEGORY_UNKNOWN]))
    if failed:
        print("\nLicense policy check FAILED. Resolve by removing the dependency, "
              "replacing it, or — if the classification is wrong — adding a justified "
              "entry to OVERRIDES in scripts/check_licenses.py.")
        return 1
    print("\nLicense policy check passed.")
    return 0


# --- Markdown generation ----------------------------------------------------


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _inventory_table(deps: list[Dep]) -> str:
    rows = ["| Package | Version | License | Category |",
            "|---|---|---|---|"]
    for d in sorted(deps, key=lambda x: (x.category != CATEGORY_DENY, x.name.lower())):
        url = d.url.strip()
        name = f"[{d.name}]({url})" if url.startswith("http") else d.name
        rows.append(f"| {name} | {_md_escape(d.version)} | "
                    f"{_md_escape(d.license)} | {d.category} |")
    return "\n".join(rows)


def _notices(deps: list[Dep]) -> str:
    out = []
    for d in sorted(deps, key=lambda x: x.name.lower()):
        text = d.notice.strip()
        if text and text.upper() != "UNKNOWN":
            out.append(f"### {d.name} {d.version}\n\n```\n{text}\n```")
    return "\n\n".join(out) if out else "_No bundled dependency ships a NOTICE file._"


def inject(doc_path: Path, section_id: str, body: str) -> None:
    """Replace content between `<!-- BEGIN: id -->` and `<!-- END: id -->`."""
    text = doc_path.read_text(encoding="utf-8")
    begin, end = f"<!-- BEGIN: {section_id} -->", f"<!-- END: {section_id} -->"
    pattern = re.compile(
        re.escape(begin) + r".*?" + re.escape(end), re.DOTALL
    )
    replacement = f"{begin}\n{body}\n{end}"
    if not pattern.search(text):
        raise SystemExit(f"marker '{section_id}' not found in {doc_path}")
    doc_path.write_text(pattern.sub(replacement, text), encoding="utf-8")


def write_doc(doc_path: Path, py: list[Dep], js: list[Dep]) -> None:
    inject(doc_path, "backend-inventory", _inventory_table(py))
    inject(doc_path, "backend-notices", _notices(py))
    inject(doc_path, "frontend-inventory", _inventory_table(js))
    print(f"Regenerated {doc_path.relative_to(REPO_ROOT)}")


# --- Entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="run the policy gate (default if no mode given)")
    ap.add_argument("--write", metavar="PATH", nargs="?", const=str(DEFAULT_DOC),
                    help="regenerate the THIRD_PARTY_LICENSES.md at PATH")
    ap.add_argument("--strict", action="store_true",
                    help="treat UNKNOWN licenses as failures, not warnings")
    ap.add_argument("--no-python", action="store_true", help="skip Python deps")
    ap.add_argument("--no-js", action="store_true", help="skip npm deps")
    ap.add_argument("--pip-json", help="read pip-licenses JSON from file (testing)")
    ap.add_argument("--npm-json", help="read license-checker JSON from file (testing)")
    args = ap.parse_args(argv)

    def _read(p):
        return Path(p).read_text(encoding="utf-8") if p else None

    py = [] if args.no_python else collect_python(_read(args.pip_json))
    js = [] if args.no_js else collect_npm(_read(args.npm_json))
    deps = py + js
    if not deps:
        sys.stderr.write("::warning::no dependencies collected — nothing to check\n")

    if args.write:
        write_doc(Path(args.write), py, js)

    # The gate runs whenever --check is given, or when no mode was specified.
    if args.check or not args.write:
        return report(deps, strict=args.strict)
    # --write-only: still surface a denied license as a failure.
    return report(deps, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
