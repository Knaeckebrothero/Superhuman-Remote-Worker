---
tags:
  - issue
  - knowledge-base
  - okf
  - reindex
  - orchestrator
related:
  - "[[okf_knowledge_base]]"
  - "[[kb_reindex_duplicate_key_on_legacy_notes]]"
---

# Two note files sharing one frontmatter `id` collide on every reindex

**Filed:** 2026-07-29, split out of [[kb_reindex_duplicate_key_on_legacy_notes]] after
that issue's `.md`-shaped-directory corruption was cleaned up and this residual became
visible underneath it. Line numbers are develop @ 2026-07-29.

## Symptom

Four `uq_knowledge_project_note` duplicate-key errors per reindex on project
`68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a` ("Better Resavio"), after the 1811 errors from
the export corruption were resolved.

Unlike that issue, **the vault contains no duplicate files** — every note is at exactly
one path. The collision is on *identity*: `note_fields` (`kb_reindex.py:191`) takes
`note_id` from the frontmatter, not the filename —

```python
note_id = str(fm.get("id") or stem)[:_NOTE_ID_MAX]
```

— so two differently-named files carrying the same `id:` line both claim one
`(project_id, note_id)` key. One wins; the other fails its INSERT on every run.

## The four pairs

Each pair is a short `iter-4-*` filename and a slug-length filename, both carrying the
long id:

| frontmatter `id` | files |
| --- | --- |
| `hhv-gastecard-kurorte-netz-interop-pattern-f-chain-extension-of-kurkartewallet-k` | `knowledge/hhv-gastecard-…-kurkartewallet-k.md`<br>`knowledge/iter-4-proposal-001-hhv-gastecard-kurorte-netz-interop.md` |
| `pedelec-kurbeitrag-discount-pattern-f-chain-extension-of-evcharging-iter-24-for-` | `knowledge/pedelec-kurbeitrag-…-iter-24-for-.md`<br>`knowledge/iter-4-proposal-003-pedelec-kurbeitrag-discount.md` |
| `phase-75-blocker-diagnosis-ac-3ac-4-pdf-prefix-oracles-mathematically-impossible` | `knowledge/phase-75-blocker-diagnosis-…-impossible.md`<br>`knowledge/iter-4-phase-7-5-blocker-diagnosis.md` |
| `q64checkout-departure-meldung-mirror-extending-kurort-vertical-meldeschein-to-cl` | `knowledge/q64checkout-…-meldeschein-to-cl.md`<br>`knowledge/iter-4-proposal-002-q64-checkout-departure-meldung-mirror.md` |

In each case the long filename is the slugified title (`slugify`, 80-char cap) that
`_dual_write_note` would produce, and the short `iter-4-*` file is hand-named. All four
are from iteration 4, which suggests a single job wrote the short copies via
`write_file` while reusing frontmatter from the kb_write-authored note — **not traced,
stated as a pattern only.**

## Why it matters

Whichever file loses the key is never indexed: no chunk rows, no `blob_sha`, so it stays
in the next run's diff and re-fails forever. It is invisible to hybrid search while
looking perfectly healthy on disk. The loser is decided by sort order over the upsert
list, so it is stable but arbitrary.

## Detection

Already covered on both sides:

- **`kb_lint`** reports it as a `duplicate-id` ERROR finding (`gardener.py:325-335`,
  "id 'X' also used by …"). This vault has never had `kb_lint` run against it.
- **The reindexer** now logs the condition as one readable line naming the id, both
  paths, and which one the index holds (`_log_duplicate_note_id`, `a9d406b4`) instead of
  the raw Postgres error. **Not yet deployed to dev.**

## Fix

Per pair: decide which note is canonical, then either give the other a distinct `id:` or
delete it if it is a redundant copy. This is a **content judgement** — the two files in
each pair are not byte-identical, so it cannot be automated safely. Deliberately left
for a human.

Open design question worth deciding separately: should the reindexer *refuse* a second
file claiming a live id (skip + log, like the malformed-frontmatter path) rather than
attempting the INSERT and erroring? Skipping would make the run clean and the condition
purely a lint finding, at the cost of silently ignoring a file.

## Repro

```sh
# in a clone of the vault
for f in knowledge/*.md; do
  sed -n 's/^id: *//p;/^---$/q' "$f" | head -1 | sed "s|\$|\t$f|"
done | sort | awk -F'\t' '{ if ($1==p) print p"\n  "q"\n  "$2; p=$1; q=$2 }'
```

Or run `kb_lint` against the vault and read the `duplicate-id` findings.

## Verification

```sql
-- no note id may appear at two paths; and after a clean run this returns nothing
SELECT note_id, count(*) FROM knowledge_index
 WHERE project_id = '<project>' AND path IS NOT NULL
 GROUP BY note_id HAVING count(*) > 1;
```

Reindex error count for the project should reach **0** once the four pairs are resolved
(measured 2026-07-29: 4).
