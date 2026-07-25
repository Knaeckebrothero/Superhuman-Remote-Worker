# App Guide evaluation

This standalone harness measures whether a fresh SRW session model routes
product questions to the managed `read_product_guide` reader, avoids that
reader for near misses, selects the focused topic, and keeps its answer inside
deterministic required/forbidden fact boundaries.

It evaluates the real bundled catalog, production skill-menu fencing, and
production reader. The held-out prompts and expectations stay here rather than
inside `config/skills/app-guide/`, where the runtime skill could see them.
This is a model evaluation, not an ordinary unit test; a skipped run is not
release evidence.

## Validate the corpus

No endpoint or key is needed:

```bash
python -m eval.app_guide.run --validate-only
```

Ordinary CI also validates schema version, unique IDs and prompts, topic IDs,
positive/negative balance, broad/workflow/availability coverage, paraphrases,
the required near-miss classes, an honest off-document case, and critical
forbidden claims.

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
```

Each case starts with a fresh message list and tool context. `current` loads
the running checkout. `no-skill` keeps the other system-skill catalog entries
but removes the App Guide and its tool, providing a useful prior-only
baseline. To compare a prior skill snapshot, point at a directory containing
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

Tool trajectory is scored separately from answer text. A positive case routes
successfully only if the model actually calls `read_product_guide`; a correct
answer from model priors is not a routing pass. A near miss passes routing only
if it does not call that reader. Trajectory rows retain topic, result status,
size, and digest—not the full guide result.

Required facts and forbidden claims are deterministic substring alternatives,
not an LLM judge. They measure grounding rather than prose quality. Any
forbidden hit fails its case, and `critical_forbidden_count` has zero
tolerance. `release_gate_pass` additionally requires the complete corpus, no
provider errors, and every case passing. Review failures rather than loosening
expectations solely to improve a score.

Artifacts contain no API key, base URL, local previous-snapshot path, raw
provider exception, or private session data. All prompts are synthetic and
versioned in `cases.yaml`.
