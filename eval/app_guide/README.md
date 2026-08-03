# App Guide evaluation

This standalone harness has two held-out suites:

- `routing` is the 30-case M1 release corpus. It measures whether a fresh SRW
  session model routes product questions to `read_product_guide`, avoids that
  reader for near misses, selects the focused topic, and stays inside
  deterministic fact boundaries.
- `capability` is the eight-case M2 matrix. It distinguishes stable and
  capability-near-miss guide-only answers from dynamic guide →
  `get_product_capabilities` trajectories, checks per-layer partial/mixed
  results, requires a real operation after an advisory snapshot, and keeps
  rollback guidance available when the capability tool is absent.

It evaluates the real bundled catalog, production skill-menu fencing, and
production reader. The held-out prompts and expectations stay here rather than
inside `config/skills/app-guide/`, where the runtime skill could see them.
This is a model evaluation, not an ordinary unit test; a skipped run is not
release evidence.

## Current release evidence

As of the 2026-08-03 documentation reconciliation, M1 is closed and the M2
harness/implementation is offline-green, but M2 release acceptance remains
open. The first deployed M2 run ended **BLOCKED** because the intended release
model and several live fixtures were unavailable; its three complete
`gemma-4-moe` runs failed diagnostically at 1/8, 0/8, and 1/8 passing cases,
with zero critical forbidden claims. Do not use those fallback results as a
passing baseline or combine their cells with a later candidate.

See the [verification record](../../docs/tests/app_guide_m2_verification.md),
the [sanitized first-run
results](../../docs/tests/app_guide_m2_live_acceptance_results_2026-07-28.md),
and the [re-run handoff](../../docs/tests/app_guide_m2_live_acceptance_handoff.md).

## Validate the corpus

No endpoint or key is needed:

```bash
python -m eval.app_guide.run --validate-only
python -m eval.app_guide.run --suite capability --validate-only
```

Ordinary CI validates both corpora. M1 retains its deliberate trigger balance
and near-miss coverage. M2 validates exact registry capability IDs, fixture
names, guide-only cases, all six trajectory categories, and each synthetic
fixture against the production capability-output model.

## Run the live evaluation

Credentials are environment-only and are never written to artifacts:

```bash
export APP_GUIDE_EVAL_MODEL="<openai-compatible-model-id>"
export APP_GUIDE_EVAL_API_KEY="<key>"
# Optional for a non-OpenAI endpoint:
export APP_GUIDE_EVAL_BASE_URL="https://example.invalid/v1"

python -m eval.app_guide.run \
  --arm current \
  --arm no-skill

# M2 live-state behavior; repeat the full suite three times for release evidence.
python -m eval.app_guide.run \
  --suite capability \
  --arm current
```

Each case starts with a fresh message list and tool context. `current` loads
the running checkout. `no-skill` keeps the other system-skill catalog entries
but removes the App Guide and its tool, providing a useful prior-only
baseline for the routing suite; the capability suite requires the managed
guide and rejects `no-skill`. To compare a prior skill snapshot, point at a
directory containing
`app-guide/SKILL.md` and its `references/` directory:

```bash
python -m eval.app_guide.run \
  --arm current \
  --arm previous=/path/to/old/config/skills
```

Use `--case CASE_ID` (repeatable) or `--limit N` for development only. A
partial run is marked `complete_corpus: false` and cannot satisfy the release
gate. Use `--out /new/empty/directory` to choose an artifact location.

## Scoring and artifacts

The output directory contains:

- `results.jsonl` — one row per arm/case, including the synthetic prompt and
  answer;
- `summary.json` — routing, topic, grounding, false-positive, and comparison
  metrics; and
- `run_meta.json` — model, commit/dirty state, corpus and harness digests, and
  the managed guide bundle digest.

Tool trajectory is scored separately from answer text. In M1, a positive case
passes only if the model calls `read_product_guide`; a correct answer from
priors is not a routing pass. A near miss passes only without that reader. In
M2, stable and capability-near-miss questions require zero capability calls,
dynamic questions require one exact capability-ID call strictly after the
focused guide call, and the action case requires `email_send` strictly after
the capability snapshot. The operation cannot carry snapshot/authorization
fields.

M2 capability outputs are synthetic but are constructed and serialized through
the production Pydantic contract. The changed-state operation result is a safe
model fixture; deterministic tests separately prove that the real bound email
operation refuses after a live detach. This suite is therefore model
trajectory evidence, not a substitute for authenticated endpoint probes or
fresh/resumed deployed-session acceptance.

Trajectory rows retain safe logical arguments, result status, size, and
digest—not raw guide/capability results or email content.

Required facts and forbidden claims use deterministic normalized phrase
alternatives, not an LLM judge. Required facts permit at most three
intervening modifier tokens between expected tokens, which accepts faithful
wording without becoming an unbounded semantic matcher; intervening negation
or failure tokens invalidate a positive required-fact match. Expectations
marked `affirmative` additionally require a denial- and uncertainty-free
clause. Forbidden claims remain contiguous phrase matches so inserted negation
is not mistaken for a forbidden claim. They measure grounding rather than
prose quality. Any
forbidden hit fails its case, and `critical_forbidden_count` has zero
tolerance. `release_gate_pass` additionally requires the complete corpus, no
provider errors, and every case passing. Review failures rather than loosening
expectations solely to improve a score. A complete run exits nonzero when that
release gate fails; intentionally partial development runs remain identified
by `complete_corpus: false`.

Artifacts contain no API key, base URL, local previous-snapshot path, raw
provider exception, or private session data. All prompts are synthetic and
versioned in `cases.yaml` or `capability_cases.yaml`.
