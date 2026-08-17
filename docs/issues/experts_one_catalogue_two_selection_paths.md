---
tags:
  - issue
  - experts
  - architecture
  - api
  - agents
status: fixed
priority: P1
created: 2026-08-17
aliases:
  - two expert systems
  - config_name vs expert_id
  - bundled experts are not selectable as experts
related:
  - "[[officer_per_job_model_choice_is_silently_discarded]]"
  - "[[officer_commission_can_silently_de_arm_its_workers]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
---

# Experts are one catalogue and two selection paths

**Status:** **FIXED on develop `916d54d4` (2026-08-17), not pushed, not deployed, and NOT
live-verified.** Diagnosed 2026-08-17 against live dev (`sha-50af6e9`).

One selector — `expert: str | None` — now spans the agent tool, both MCP tools and both REST
endpoints, backed by a single resolver (`src/shared/expert_reference.py::resolve_expert_selection`).
A bundled slug resolves to `(config_name=slug, expert_id=None)` and is its own base; a DB UUID
resolves to `(worker_base, uuid)`. `config_name`/`expert_id` survive as aliases: a repeated
reference is accepted, a contradicting one refused. An unknown `expert=` is now refused at
creation naming `list_experts`, rather than failing at dispatch. 28 new tests.

**Owed:** dispatch a real job with `expert="developer"` on dev and confirm the worker binds a
shell. Everything below the fix line is local-test evidence only.

An expert can be **discovered** uniformly and **inspected** uniformly, and then cannot be
**selected** uniformly. Callers must know which of two mutually-exclusive parameters an expert
belongs to, and nothing in the catalogue tells them. The result is that ten of the twelve
experts on this deployment — including every one that can run a shell — are effectively
unreachable to an agent that is choosing a worker.

## What is already unified (do not rebuild this)

This is narrower than it first looks. Three surfaces are already correct:

| surface | behaviour |
|---|---|
| `GET /api/experts` (`orchestrator/main.py:46703`) | returns bundled (disk) **and** DB rows in one list, each tagged `source` |
| `list_experts` agent tool (`src/tools/orchestrator/catalog.py:646`) | renders that list, and `_format_catalog_item` (`:499`) does print `Source:` |
| `get_expert` (`src/tools/orchestrator/catalog.py:681`) | documented as "Expert UUID **or** bundled expert id/name" — one lookup, either form |

Live proof: `list_experts` on dev returns **12** experts — 10 bundled
(`bughunter`, `centurion`, `critic`, `curator`, `designer`, `designer-interactive`,
`developer`, `product-qa`, `scholar`, `writer`) plus 2 DB rows (`Assistant`,
`General Worker`).

## Where it breaks: selection

`create_job` (`src/shared/orch_surface/jobs/control.py:135`) takes **two** parameters for one
concept, and they are mutually exclusive:

```python
config_name: str = "worker_base",   # :139 — a BUNDLED expert, addressed by slug
expert_id: str | None = None,       # :140 — a DB expert, addressed by UUID
```

```python
# :204-208
if expert_id and config_name and config_name != "worker_base":
    return ("Refusing to create job: expert_id cannot be combined with "
            f"config_name={config_name!r}. Pass expert_id alone (it selects "
            "a DB expert) or config_name alone (a bundled one).")
```

Four separate problems in that seam:

1. **Neither parameter is called `expert`.** The catalogue's vocabulary is "expert"; the
   selection vocabulary is "config_name" and "expert_id". A caller who has just read
   `--- Expert: developer ---` has no reason to think `config_name` is the field for it.
2. **The mapping is undocumented.** Nothing states `source: bundled → config_name`,
   `source: global|user → expert_id`. The caller must infer it from the *shape of the id*
   — slug vs UUID — which is exactly the kind of inference that fails silently.
3. **The docstring for `config_name` is a bare string with no discovery pointer**
   (`:162` — "Base agent config (default: \"worker_base\")"). Compare the parameter
   immediately below it, which *does* point at its catalogue: "Use the `list_models` tool to
   discover available model IDs." Models are discoverable; worker profiles are not.
4. **~~The refusal fires on the correct call.~~ — WRONG, corrected 2026-08-17.** An earlier
   revision claimed that naming a bundled slug collided with the auto-injected application
   default and tripped the refusal, making "specify nothing" the only safe call. It does not:
   `should_resolve_default` (`orchestrator/main.py:12596-12602`, pre-change) was already gated
   on `config_name == "worker_base"`, so an explicit slug suppressed the default and the
   refusal could only fire if the caller *also* passed `expert_id`. The barrier was therefore
   purely vocabulary and discoverability — the officer never avoided a collision, it simply
   never knew the parameter existed. Verified by reading `916d54d4^`.

## Observed consequence

Project `a572e4a0` (Better Resavio), all **8** jobs ever dispatched by its officer:

| | value |
|---|---|
| `config_name` | `worker_base` — every job |
| `expert_id` | `6a3ba4b5…` (`general-worker`) — every job |
| `expert_selection.source` | `application` — i.e. **the officer named no expert at all** |
| `tools.shell` | `[]` — every job |

`general-worker` sets **no `tools` key and no `workspace` key**; it is a thin overlay over
`worker_base.yaml`, which is what actually ships `shell: []`. So every worker this officer has
ever commissioned could write files and read git, and could not run a command.

Meanwhile all six shell-capable bundled configs resolve **today, in the deployed image**
(verified via `resolve_config_path` inside the running agent pod):

```
developer    shell=['run_command', 'cancel_command', 'shell_read']
bughunter    shell=['run_command', 'cancel_command', 'shell_execute', 'shell_read']
critic       shell=['run_command', 'cancel_command']
scholar      shell=['run_command', 'cancel_command']
designer     shell=['run_command', 'cancel_command', 'shell_read']
product-qa   shell=['run_command', 'cancel_command', 'shell_read']
```

`create_job(config_name="developer")` would have worked on any of those eight dispatches. It
was never once attempted.

The cost is visible in the work: the loop produced themes, HTML mockups and research notes for
three days and never produced a runnable product, because its workers had no way to run
anything. Job `bc8b68df`, asked to verify a UI, recorded in its own completion notes that it
could only inspect the HTML as text.

## Why this is the upstream bug

Every other officer-effectiveness issue filed against this project is downstream of it. There
is no point tuning deliverable contracts, model pinning or backlog automation while the
officer's entire hiring pool is two writers.

It is also a public-API problem, not just an internal one: the same split is what a customer's
agent — or the cockpit — has to navigate.

## Direction

The invariant to restore: **an expert is one concept with one identifier and one selection
parameter, and the bundled/DB distinction is an implementation detail of resolution.**

Sketch, cheapest first:

1. **One selection parameter.** Add `expert: str | None` to `create_job`, accepting either a
   bundled slug or a DB UUID, and resolve it internally the way `get_expert` already does.
   Keep `config_name` and `expert_id` as deprecated aliases so nothing breaks.
2. **Resolution belongs in one place.** A single `resolve_expert(ref)` used by `create_job`,
   `get_expert`, the session-create path and the cockpit — returning the resolved config
   regardless of origin. Today `get_expert` knows how to do this and `create_job` does not.
3. **Make the catalogue state its own selector.** Emit a canonical, unambiguous `id` per entry
   (or an explicit `select_with` field) so a caller never infers from id shape. Reference the
   catalogue from the parameter docstring, as `list_models` is referenced.
4. **Decide what a DB expert IS.** Today bundled entries are complete agent definitions
   (tools, prompts, persona, model) and DB rows are thin overlays with no `tools` key. If both
   are "experts" to the consumer, one of them is mislabelled. Either DB experts gain the full
   shape, or they are renamed to what they are (overlays/profiles) and stop sharing a
   catalogue with complete definitions.

## Adjacent, deliberately out of scope here

`officer.slots` (`orchestrator/services/officer_slots.py:43`) allows exactly
`{count, model, backend, category, spend_ceiling_daily}` — there is **no key naming who staffs
the slot**. So even after selection is unified, an officer roster still cannot express "my
build slot is a developer". That is a separate change and should be a separate ticket; it is
listed here only so the two are not solved in ignorance of each other.

## Acceptance

- A caller can take any entry returned by `list_experts` and pass its id to one selection
  parameter, with no knowledge of whether it came from disk or the database.
- Passing a bundled expert no longer collides with an application-default DB expert.
- The parameter documentation names the tool that lists valid values.
- A regression test dispatches a job for one bundled and one DB expert through the same
  parameter and asserts both resolve.

## Traps for whoever takes this

- **Do not "fix" it by hiding bundled experts.** They are the only complete definitions on the
  system; hiding them removes every shell-capable profile.
- The mutual-exclusion refusal at `control.py:204` exists for a real reason — `expert_id`
  resolves *over* a base config. Unifying the parameter means deciding what the base is when
  the reference is a slug, not deleting the check.
- `application_expert_defaults` (2 rows) injects an expert when the caller names none, and
  stamps `expert_selection.source = "application"` (`orchestrator/main.py:33874`). A unified
  parameter must decide whether an explicit bundled slug suppresses that default — today the
  combination is exactly what the refusal rejects.
- `project_experts` is empty on dev (0 rows); do not assume project-scoped experts are an
  exercised path.
