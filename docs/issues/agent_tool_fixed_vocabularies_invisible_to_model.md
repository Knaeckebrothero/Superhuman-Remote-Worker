# Tools document their fixed vocabularies in prose, and `defer_to_workspace` strips the prose — so the model is asked to guess enum values it was never shown

**Status:** **DIAGNOSED 2026-07-15 from live evidence + code audit (3-agent sweep) · P0–P5 BUILT + verified 2026-07-15 · UNCOMMITTED.** The failure was *observed*, not hypothesised: thread `5cf60c83` burned 9 failed `edit_citation` calls and the bad data is still in the dev DB (citations 2090 / 2094, refs below). Fix verified three ways: 38 new unit tests in `tests/test_tool_vocabularies.py` (proven to fail when the `Literal` is reverted); the 6 real-Postgres `tests/citation_engine/` tests run green against k3d `srw_vector` (they normally skip); and a live reproduction against that same Postgres replaying all three values the model actually guessed — each now returns an error naming the five valid values, and the schema rejects them before execution. Full suite: 9776 passed, 2 pre-existing unrelated failures (§Verification). **Not yet exercised by a real LLM session** — see "left to do" below.
**Found:** 2026-07-15, user-reported — an `edit_citation` tool card showing `error: invalid input value for enum extraction_method: "Tavily extract from raw GitHub document"`. First read blamed the Tavily tool. **It is not Tavily.** Tavily's `web_search` is the *control group* that proves the citation tools are broken (§"The controlled experiment").
**Severity:** **Medium-High, and it is a class, not an instance.** As reported: 9 wasted tool calls + one permanently-failed citation carrying a fabricated quote + one duplicate citation, in a single session. The class includes **silent data corruption** — `tag_source(action="Remove")` silently *adds* the tags it was asked to remove and reports `"Added"` (§"Tier 1"). Loud enum errors are the *benign* end of this bug class.
**Component:** `src/tools/citation/sources.py` (11 of the 24 deferred tools) · `src/tools/description_manager.py:217-285` (`apply_overrides` / `_copy_with_description`) · `src/citation_engine/engine.py:756-763` vs `:936-1009` (the validation asymmetry) · `src/core/citation_feedback_injection.py:47` (routes the agent into the broken tool) · plus `knowledge/knowledge_tools.py`, `orchestrator/workflows.py`, `core/session_task_tools.py`, `workspace/files.py` (§"The class")
**Related:** [[tool_implementation]] — `docs/features/tool_implementation.md:44` already documents the exact mechanism ("deferral shortens the *description text* only… the full parameter schema is still serialized every call") but frames it purely as a **cost**; this doc draws the consequence nobody drew, and turns that "gap" into the fix. Its deferred-tool inventory at `:33` is **stale** — it lists ~12 and omits all 11 citation tools · [[citation_issues]] — `docs/citation_issues.md:48` *predicted this symptom* ("the agent often hallucinates *new* citations" during correction loops) but attributed it to attention decay; here the cause is mechanical · [[citation_engine_roadmap]] · [[tool_issues]]

## Summary

Agent tools declare parameters that accept a **closed set of values** — Postgres enums, `NOTE_TYPES`, `_AUTONOMY_VALUES`, mode switches. Those value sets are documented **in the docstring prose**, and the parameters are typed as bare `str`.

Two independent mechanisms then destroy that documentation before it reaches the model:

1. **`defer_to_workspace: True`** swaps the full docstring for a one-line `short_description` (`description_manager.py:236-244`, live via `agent.py:2809` and `persistent_session.py:1311`). For `edit_citation` the replacement is `"Edit citation fields (claim, quote, confidence, etc.)"` — which does not name `extraction_method` **at all**, let alone its five values.
2. **The bare `str` type** emits no JSON-schema `enum`, so the constraint isn't in the schema either.

The model is therefore handed *an undescribed, unconstrained free-text field named `extraction_method`* and asked to fill it. Writing `"Tavily extract from raw GitHub document"` into that box is a **correct reading of what it was shown**. This is not a model error.

Nothing upstream compensates: `grep -rni "extraction_method" config/` returns **zero hits** across every prompt, template, and expert config. The skill layer explicitly defers to the tool description (`config/skills/cite-as-you-write/SKILL.md:29-31`: *"See the tool description for the exact arguments"*) — pointing at the one thing that was stripped.

## The controlled experiment already in the repo

The repo contains a natural A/B test that settles both the diagnosis and the fix.

| | `web_search` (Tavily) | `edit_citation` |
|---|---|---|
| `defer_to_workspace: True` | **Yes** (`research/web.py:35`) | **Yes** (`citation/sources.py:75`) |
| Docstring stripped at bind time | **Yes** | **Yes** |
| Closed-vocab params | `search_depth`, `topic`, `time_range` | `extraction_method`, `confidence` |
| How the vocabulary is declared | **`Literal[...]` in the signature** | **prose in the stripped docstring** |
| Bad values ever observed | **None** | **8 in one session** |

```python
src/tools/research/web.py:179-188          # deferred — and has never emitted a bad value
    def web_search(
        query: str,
        max_results: int = 5,
        search_depth: Literal["basic", "advanced"] = "basic",
        topic: Literal["general", "news", "finance"] = "general",
        time_range: Optional[Literal["day", "week", "month", "year"]] = None,

src/tools/citation/sources.py:619          # deferred — and produced 8 rejections
        extraction_method: Optional[str] = None,
```

**Same deferral, same stripping, opposite outcomes.** The variable is where the vocabulary lives. That is the whole finding.

## What actually happened — thread `5cf60c83`

Session `5cf60c83-e479-4ee9-9029-676f35b8a1ee` ("AI Self-Hosting TCO Business Case", `gpt-5.6-sol` via codex-proxy, persistent, `permission_mode: autonomous`). Reconstructed from thread messages + the live vector DB.

**Nine failed `edit_citation` calls.** The guessing progression is the signature of a model brute-forcing a vocabulary nobody gave it:

```
edit_citation → error: locator must be valid JSON
edit_citation → error: invalid input value for enum extraction_method: "Tavily extract from raw GitHub document"
cite_web      → Web Citation Created  [2094]        ← GAVE UP on the repair, made a duplicate instead
…
edit_citation → error: invalid input value for enum extraction_method: "web_extract"
…
edit_citation ×6 (one parallel batch, all identical)
              → error: invalid input value for enum extraction_method: "direct"   ×6
edit_citation → ok: edited citation [2075]                                        ← finally landed
```

Prose → a plausible snake_case invention → a near-miss abbreviation of `direct_quote`, fired **six times in parallel** because nothing in the loop could tell it otherwise. Then 9 successful calls across 5 citations (2055, 2060, 2063, 2070, 2075).

**The model's judgment was never the problem.** Once it had the vocabulary it used it *correctly*: citation 2075 is now `extraction_method: aggregation` on a claim that genuinely aggregates two facts (price + RAM option) from one product page. `aggregation` is exactly right. It knew what it meant; it could not guess how to spell it.

**The damage is still in the database.** Both rows confirmed live via MCP:

- **Citation 2090** — `status: failed`, `extraction_method: direct_quote`. Its `verbatim_quote` is a **fabrication**: *"SRW's action-reversibility design treats routine workspace edits as git-reversible…"*. The verifier correctly caught it (*"The verbatim quote cannot be located in the source. The term 'SRW' does not appear anywhere in the source document"*). The agent's repair — which carried a **real** verbatim quote from the source — was rejected wholesale, because the UPDATE is a single atomic statement and one bad enum value kills the good fields with it (`engine.py:1008`).
- **Citation 2094** — `status: verified` (0.75). A near-duplicate of 2090: same source `[12028]`, same claim. Created only because the repair path was unusable. Its own verification notes concede the "verbatim quote" *"is actually a faithful paraphrase"* — i.e. the same mislabel, which passed this time. The verifier is not deterministic on paraphrases.

Net: the corpus now holds a permanent failed/fabricated citation **and** a duplicate, and neither was the model's fault.

## Root cause — three bugs compose into a trap

Individually each is survivable. Together they form a closed loop.

### 1. The create tools hardcode a claim they can't honour

Neither create tool exposes `extraction_method` or `confidence` to the agent at all:

```python
src/tools/citation/sources.py:334-339   async def cite_web(text, url, title=None, accessed_date=None, claim=None)
src/tools/citation/sources.py:200-206   async def cite_document(text, document_path, page=None, section=None, claim=None)
src/tools/citation/sources.py:294,409   extraction_method="direct_quote",   # hardcoded, both call sites
                                        confidence="high",                  # hardcoded, both call sites
```

So **every citation the agent creates asserts it is a verbatim direct quote made with high confidence** — regardless of what was actually passed. Hand a paraphrase to `cite_web` and the row is born mislabelled. The verifier then correctly fails it. The mislabel is *manufactured by the tool*, and the agent has no way to prevent it at create time.

This is also why `engine.py:756-763`'s validation is effectively dead code on the agent surface: no agent-reachable caller ever passes a non-default value. **The validation exists exactly where an LLM cannot trigger it, and is missing exactly where an LLM can.**

### 2. The feedback loop then demands a repair

```python
src/core/citation_feedback_injection.py:47
    "the claim. Fix each one with the edit_citation tool (correct the quote, "
    "quote_context, or claim), or remove the citation if it cannot be "
    "supported. Edited citations are automatically re-verified."
```

The system detects the failed citation and routes the agent straight into the one tool whose vocabulary is invisible. The loop is *designed* to end here.

### 3. The repair tool hides its vocabulary — three ways at once

**(a) The docstring naming the values is stripped.** The only place in the entire repo that lists them:

```python
src/tools/citation/sources.py:638
    extraction_method: How extracted (direct_quote, paraphrase, inference, aggregation, negative)
```

…is replaced at bind time:

```python
src/tools/citation/sources.py:70-78
    "edit_citation": { …
        "defer_to_workspace": True,
        "short_description": "Edit citation fields (claim, quote, confidence, etc.).",
    }
src/tools/description_manager.py:236-244   if metadata.get("defer_to_workspace", False): … _copy_with_description(tool, short_desc)
src/agent.py:2807-2809                     self._tools = apply_description_overrides(self._tools)   # worker
src/api/persistent_session.py:1311         (same, sessions)
```

Partial mitigation exists but is unenforced: full docstrings **are** written to `tools/<tool>.md` in the workspace before the override (`agent.py:2767-2776`), so `tools/edit_citation.md` does contain the list. Nothing compels the agent to read it, and in this session it never did — there is no `read_file` between the failures and the eventual success. It guessed its way there.

**(b) The bare `str` type emits no schema constraint.** `extraction_method: Optional[str] = None` (`sources.py:619`). Nothing in the JSON schema restricts it.

**(c) No validation, and the error is unactionable.** `create_citation` validates and raises a curated message; `edit_citation` does neither:

```python
src/citation_engine/engine.py:756-763      # create_citation — VALIDATES
    try:
        ExtractionMethod(extraction_method)  # Validate only
    except ValueError as e:
        raise ValueError(f"Invalid extraction_method: {extraction_method}. "
                         "Use 'direct_quote', 'paraphrase', 'inference', 'aggregation', or 'negative'.") from e

src/citation_engine/engine.py:982-983      # edit_citation — NO validation, straight to the cast
    ("confidence", confidence, "confidence_level"),
    ("extraction_method", extraction_method, "extraction_method"),
src/citation_engine/engine.py:993          placeholder = f"${idx}" + (f"::{cast}" if cast else "")
```

The resulting asyncpg error is **not** a `ValueError`, so it bypasses the tool's friendly handler:

```
InvalidTextRepresentationError → DataError → PostgresError → PostgresMessage → Exception
is ValueError subclass?: False          # verified by introspection
```

It falls through `sources.py:690` (`except ValueError`) to the generic `except Exception` at `:692-694` and returns the raw Postgres string — **which never names the valid values**. So the model cannot self-correct from the error either. Every one of the three layers that could have told it the vocabulary was closed off.

## The design principle nobody drew

`docs/features/tool_implementation.md:44` already states the mechanism, filed under "Token cost (honest accounting)":

> ⚠️ **Deferral today shortens the *description text* only. The tool stays in `bind_tools()`, so its full parameter schema is still serialized every call.** Description-shortening ≠ removing the tool from context.

The doc treats this as a **defect** ("No real context savings", gap #2 at `:53`). It is also the fix:

> **Under deferral, the type IS the documentation.** Prose is free-floating and gets stripped. Types ride the `args_schema`, which deferral never touches and which we are *already paying to serialize on every call*. Any constraint expressed in prose is lost; any constraint expressed in the type survives — **at zero marginal token cost**, because the schema ships regardless.

`web_search` obeys this by accident. The citation tools don't. The rule generalises to all 24 deferred tools, and is a good rule for the other ~109 too.

## The class — where else this lives

Audited: all 133 `@tool` decorators across 31 files under `src/tools/**` (note `grep "^@tool"` returns zero — every decorator is indented inside a `create_*_tools(context)` factory), plus all 5 Postgres enums and every write site across `src/**` and `orchestrator/**`.

**The through-line: the REST surface is safe; the LLM tool surface is not.** REST casts the *column* to text (`main.py:16596` `c.verification_status::text = ${idx}`), so a garbage filter returns zero rows. The tool layer casts the *value* into the enum, so a garbage value throws.

### Tier 1 — silent corruption (worse than the reported bug: no error at all)

- **`tag_source.action`** — `src/tools/citation/sources.py:799`, `Optional[str] = "add"`. Implementation at `:826-835` is `if action == "remove": remove() else: add()`. **Anything but the exact string `"remove"` — `"Remove"`, `"delete"`, `"rm"` — silently ADDS the tags it was asked to remove, and reports `"Added tags on source [N]"`.** Deferred, so the `"add"`/`"remove"` vocabulary is stripped; the `short_description` says *"Add or remove tags"*, telling the model the capability exists while hiding the spelling. Wrong data, confident output, no error.
- **`task_add.priority`** — `src/tools/core/session_task_tools.py:54`; `:69-70` `if priority not in ("high","medium","low"): priority = "medium"`. `"urgent"`/`"P0"` silently becomes `"medium"` and the confirmation never mentions the downgrade.
- ~~**`edit_file.position`**~~ — **retracted.** The first draft of this doc claimed a miss "silently reroutes to replace-mode". It does not: `workspace/files.py` validates explicitly and returns `Error: Invalid position '<x>'. Use 'start' to prepend, 'end' to append, or omit for replace mode.` It is a Tier-3 graceful error, not silent corruption. Still worth a `Literal` (constrains the model up front, saves a wasted round-trip) — but it was never corrupting anything, and this doc said otherwise.

### Tier 2 — vocabulary invisible even in prose

- **`propose_automation.autonomy`** — `src/tools/orchestrator/workflows.py:566`, `autonomy: str = "review"`. Docstring `:579` says only *"Job autonomy for spawned jobs, defaults to review."* The values live **solely** in `_AUTONOMY_VALUES` (`:126`), surfaced only in a post-hoc rejection at `:255-258`. **Strictly worse than the reported bug** — the model can only guess or echo the default.

### Tier 3 — same enum defect, LLM-reachable, unvalidated

- **`search_library.source_type`** — the closest twin. `engine.py:1403-1428` validates `mode` **and** `scope` with curated errors, then **skips `source_type`**, which flows to `search.py:98` `sql += f" AND s.type = ${nxt}::source_type"`. The identical skip-one-enum asymmetry. `keyword_search` (`engine.py:1458`) isn't wrapped in try/except (unlike `semantic_search` at `:1478`), so the raw error propagates.
- **`list_citations.status`** — `engine.py:1208-1211` casts `::verification_status`, unvalidated. Docstring `sources.py:572` lists `pending, verified, failed` and **omits the valid `unverified`**.
- **`annotate_source.type`** — `sources.py:700`, prose-only at `:711`; coerced at `engine.py:1515` (`AnnotationType(annotation_type)`) → `ValueError`.
- **`sudo_request_status`** — unvalidated from REST (`main.py:9535`) and MCP (`mcp/server.py:2575`), but **fails silently**: `sudo_gate.py:486-488` swallows the error and returns `[]`, so the user gets an empty list, not an error. Note `mcp/server.py:128` already uses `Literal[...]` for exactly this kind of param — the pattern exists in that file, just wasn't applied here.

### Tier 4 — prose that has already drifted out of sync

- **`kb_write.type`** — `src/tools/knowledge/knowledge_tools.py:870`. Docstring `:889` lists **nine** values; `NOTE_TYPES` (`src/services/knowledge_graph.py:46-59`) has **ten** — `"datasource"` (`:57`) is missing from the prose. Same omission repeats in `kb_list` (`:1241`). Enforced at `:976-979`. **This is what documenting a vocabulary in prose buys you: the prose is not just unenforced, it is wrong.** Also `kb_write.confidence` (`:875`).

### Confirmed non-issues (do not chase)

- **`postgres_db.py:1079-1161` `CitationsNamespace.edit`** — same unvalidated `field_map` idiom, but **dead and mis-targeted**: instantiated at `:134`, **zero production callers**, and `citations` exists only in the vector DB while `PostgresDB` is the app DB. The `sources.py:679-683` reroute comment describes this; the reroute landed. Latent only if someone rewires it. Candidate for deletion.
- **`confidence`** has the identical hole on the edit path — it simply never fired, because `high`/`medium`/`low` are guessable English. Latent, not safe.
- **`_register_source`, `_update_verification_status`, `create_citation`** and the whole REST enum surface — checked, correctly hardcoded or typed. Fine.

### Test coverage: zero

`grep -rn "extraction_method" tests/` → **zero hits**. `TestEditCitationTool` (`tests/test_graph.py:1422-1545`) covers 6 cases but **mocks the engine wholesale** (`:1439`, `engine.edit_citation = AsyncMock(return_value=None)`), so it accepts any garbage and structurally *cannot* catch an enum violation. No test asserts `create_citation` rejects a bad value either — `engine.py:756-763` is entirely untested. Only a Postgres integration test can catch this class.

## Why `Literal` is the fix — empirically verified

Two facts make this decisive rather than stylistic.

**1. `_copy_with_description` cannot touch the schema.** It only swaps the description string:

```python
src/tools/description_manager.py:276
    return tool.model_copy(update={"description": new_description})
```

`args_schema` is untouched. **A `Literal` constraint survives deferral; a docstring does not.** It is the *only* mechanism that works under `defer_to_workspace: True`.

**2. `Literal` renders as a JSON-schema `enum` on the installed versions.** Probed directly against `langchain-core 1.2.28` / `pydantic 2.12.5` (`requirements.txt:6-13` pins floors only; the `langchain` meta-package is not installed):

```json
"a": { "enum": ["x","y","z"], "type": "string", "default": "x" },                       // Literal[...]
"b": { "anyOf": [{"enum": ["p","q"], "type": "string"}, {"type":"null"}], "default": null },  // Optional[Literal[...]]
"c": { "type": "string", "default": "free" }                                            // bare str + "(foo, bar, baz)" in docstring
```

`c` is the current state: the prose landed in the description blob and **constrained nothing** — reproducing the failure exactly. `b` is the shape every `Optional[...]` param below needs.

## Proposed fix

Ordered by value-per-line. **P0 alone retires the reported bug**; P3 is what stops it recurring.

### P0 — type the vocabularies (mechanical, ~20 signatures, no behaviour change)

Replace bare `str` with `Literal[...]` on every closed-vocab param, starting with the confirmed-live ones. Follow the existing in-repo idiom (`research/web.py:179-188`), not a new one:

```python
# src/tools/citation/sources.py:611-621
    async def edit_citation(
        citation_id: int,
        …
        confidence: Optional[Literal["high", "medium", "low"]] = None,
        extraction_method: Optional[Literal[
            "direct_quote", "paraphrase", "inference", "aggregation", "negative"
        ]] = None,
```

Then Tier 1 (`tag_source.action`, `edit_file.position`, `task_add.priority`), Tier 2 (`propose_automation.autonomy`), Tier 3 (`search_library.source_type|mode|scope`, `list_citations.status` — **including the missing `unverified`**, `annotate_source.type`), Tier 4 (`kb_write.type` — **including the missing `datasource`**, `kb_write.confidence`), and `sudo_request_status` on the REST/MCP filters.

Derive the `Literal` sets from the existing single sources of truth (`ExtractionMethod`, `Confidence`, `SourceType`, `VerificationStatus` in `citation_engine/models.py`; `NOTE_TYPES`/`CONFIDENCE_LEVELS` in `services/knowledge_graph.py`; `_AUTONOMY_VALUES`) so they cannot drift again. Prefer referencing the enum where the tool layer can import it.

Implementation notes: `sources.py:13` imports `Any, Dict, List, Optional` — **`Literal` needs adding** (same for any other module in the sweep that lacks it). And `citation_engine` is an optional import inside the tool bodies (`sources.py:646-648`), so the `Literal` sets in the signature must be spelled literally or sourced from a module that is always importable — the signature is evaluated at load time, before the availability check runs.

### P1 — validation parity in `edit_citation`

Mirror `engine.py:756-763` into `edit_citation` for `confidence` + `extraction_method`. Defense-in-depth, matching the repo's stated convention for phase-restricted tools (LLM schema binding primary, runtime gate backup), and it covers non-agent callers that P0 doesn't reach.

### P2 — make the error legible

Catch `asyncpg.exceptions.DataError` in `sources.py:690` alongside `ValueError`, so any value that still slips through returns a message naming the valid set instead of a raw Postgres string.

### P3 — the actual product fix: stop manufacturing the mislabel

Expose `extraction_method` and `confidence` (as `Literal`s, defaulted to today's `direct_quote`/`high`) on `cite_web` and `cite_document`. Today every citation asserts "verbatim direct quote, high confidence" whether or not that's true; the agent cannot say "this is a paraphrase" even when it knows. That is what failed citation 2090, what triggered the repair, and what led to duplicate 2094. **With P3 there is nothing to repair.** P0–P2 fix the repair path; P3 removes the need for it.

### P4 — a test that can actually fail

`TestEditCitationTool` mocks the engine, so it cannot catch this. Add to `tests/citation_engine/test_integration_postgres.py` (real Postgres): assert `edit_citation` rejects a bad `extraction_method` with a message naming the valid values, and assert `create_citation`'s existing validation (`engine.py:756-763`, currently untested) does the same. Consider a cheap registry lint: *every `@tool` param whose docstring matches a closed-set pattern must be `Literal` or `Enum`* — that is what catches the next one.

### P5 — housekeeping while in here

Correct `docs/features/tool_implementation.md:33` (deferred inventory says ~12; actual is **24**, of which **11 are the citation tools** — the group where this bug lives) and its stale call-site refs at `:34` (`agent.py:1865` → `:2809`; `persistent_session.py:464` → `:1311`). Delete the dead `CitationsNamespace.edit` (`postgres_db.py:1079-1161`).

## Rejected alternatives

- **Put the values back in `short_description`.** Works, but fights the deferral design (the point is to *shrink* per-call description bytes) and re-creates the drift that already broke `kb_write.type` and `list_citations.status`. The schema is serialized anyway — spend the tokens there, where they're enforced.
- **Un-defer the citation tools.** Pays full docstrings for 11 tools on every call to fix a param constraint. Strictly worse than a `Literal`, and leaves the other ~109 non-deferred tools' prose vocabularies just as unenforced.
- **Tell the agent to read `tools/edit_citation.md` first.** The doc already exists and the agent didn't read it — no `read_file` appears between the 9 failures and the success. Prompt-level mitigation for a schema-level problem; also costs a round-trip per repair.
- **Coerce/fuzzy-match the value server-side** (`"direct"` → `"direct_quote"`). Papers over it, guesses at intent, and does nothing for the silent-corruption tier where the wrong value is *already* a valid-looking one.

## Acceptance criteria

1. `edit_citation`'s bound JSON schema contains `enum` arrays for `confidence` and `extraction_method` — verified by dumping `args_schema.model_json_schema()` **after** `apply_description_overrides()` runs (the override must not erase it).
2. An agent cannot emit an invalid `extraction_method` through `edit_citation`; if one is somehow submitted, the returned error names the five valid values.
3. `cite_web` / `cite_document` accept an explicit `extraction_method` + `confidence`, defaulting to current behaviour; a paraphrase can be labelled `paraphrase` at create time and does not fail verification for being one.
4. `tag_source(action="Remove")` no longer silently adds. (Ideally unrepresentable; at minimum, an error.)
5. `kb_write.type` accepts all ten `NOTE_TYPES` including `datasource`; `list_citations.status` accepts `unverified`.
6. A real-Postgres test fails if the `Literal` is reverted to bare `str`.

## Verification — what was actually run (2026-07-15)

1. **Unit** — `tests/test_tool_vocabularies.py`, 38 tests, green. Covers drift (Literal ⇄ enum/frozenset), constraint (schema carries `enum`), survival (enum still there *after* `apply_description_overrides`), engine validation (fails fast, `db.fetchrow` never called), the error humanizer, and the verbatim gating. **Proven able to fail**: reverting `edit_citation.extraction_method` to bare `str` fails exactly 2 tests, including the deferral-survival one; restoring passes 38/38.
2. **Mechanism, empirically** — against installed `langchain-core 1.2.28` / `pydantic 2.12.5`: a module-level `Literal` alias renders as a schema `enum`, **survives the real `model_copy(update={"description": ...})` deferral call**, and a bare-`str` control param with its vocabulary in the docstring constrains nothing.
3. **Real Postgres** — `tests/citation_engine/` (6 tests, normally skipped) run green against k3d `srw_vector` via `RUN_POSTGRES_TESTS=true` + port-forward.
4. **Live reproduction** — replayed the reported sequence against that same Postgres, through the real tool:
   - `"Tavily extract from raw GitHub document"`, `"web_extract"`, `"direct"` → each returns `Invalid extraction_method: <x>. Use 'direct_quote', 'paraphrase', 'inference', 'aggregation', or 'negative'.` No raw `invalid input value for enum` anywhere.
   - `ainvoke(extraction_method="direct")` → rejected by the schema before the body runs.
   - `extraction_method="aggregation"` → `ok:`, and the DB row reads `aggregation`.
   - A `paraphrase` citation stores as `paraphrase` **with `verbatim_quote` NULL** — so the verifier checks meaning, not word-for-word (P3's actual lever).
   - Fixture cleaned up afterwards.
5. **Deployed pod** — the decisive check. Inside the running k3d `srw-orchestrator`, after the real `apply_description_overrides`:

   ```
   deployed description : 'Edit citation fields (claim, quote, confidence, etc.).'
   deployed enum        : ['direct_quote', 'paraphrase', 'inference', 'aggregation', 'negative']
   ```

   That is precisely the failure condition — deferral has stripped the prose that named the values — with the vocabulary now surviving in the schema anyway. Prose gone, constraint intact, in the deployed artifact.
6. **Full suite** — `9776 passed`, `17 skipped`, **2 failures, both pre-existing and unrelated** (confirmed by re-running on a stashed clean tree): `test_endpoint_inventory_matches_manifest`, and `test_database_phase1::test_connect_disconnect` (the known `.env`-leaks-`DATABASE_URL`-into-pytest issue). `ruff check` + `ruff format --check` clean across 733 files.

### Left to do

- **A real LLM session on k3d has not been run.** Everything above exercises the code path, not a model choosing values. Worth one session (cite a web source with a deliberate paraphrase; confirm it labels it `paraphrase` at create time and needs no repair) before this is called done: `kubectl --context=k3d-srw -n srw logs -l srw/managed-by=agent-provisioner -f`.
- **Stale artifacts on the dev cluster**: citation 2090 (`failed`, fabricated quote) and 2094 (its duplicate) are leftovers of this bug. Decide whether to correct or drop.
- The Tier-3 graceful-error params listed above (`search_papers.source`, `download_paper.identifier_type`, `send_message.mode`, `browser_scroll.direction`, `loop_plan.disposition_outcome`, `return_job_with_feedback.severity`) are **not** done — each is a one-line `Literal`, but two nearby candidates (`create_worker_job.config_name`, `list_experts.expert_type`) may be **open** sets at runtime and must not be typed without checking.

## Scope / priority

**P0+P1+P2 for the citation tools is small and self-contained** — roughly two signatures, one validation block, one `except` clause — and retires the confirmed live damage. Worth doing on its own.

**Tier 1 (silent corruption) arguably outranks the reported bug** and should be its own pass: a loud enum error costs tokens and gets noticed; `tag_source` doing the *opposite* of what it was told, while reporting success, corrupts data and surfaces nowhere. Nobody has reported it — which is the point.

**P3 is the one that matters for citation quality.** The reported error is a symptom; the disease is that the create tools assert "verbatim direct quote, high confidence" on every row regardless of truth. That mislabel is what the verifier catches, what the feedback loop escalates, and what the broken repair path then strands. Fixing P0–P2 makes the repair *work*; fixing P3 means there's usually nothing to repair.

**P4 is what stops the next one.** This class has zero test coverage today and the existing test mocks the exact boundary where it lives.
