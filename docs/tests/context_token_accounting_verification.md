# Context Token Accounting — Verification Runbook

Verifies the four-slice fix for the multimodal context-accounting defect that
wedged dev sessions (`eb989b82`, `5dbb5770`, 2026-06-13). A multimodal PDF read
inflated the internal context meter to **9.18M "tokens" / 875%** while the real
provider count was **31,744 / 3%**, then summarization timed out 3/3 on the
base64 it was fed and the session never completed.

- **Design / what & why:** `docs/features/context_token_accounting.md`
- **Root-cause defect inventory:** `docs/issues/multimodal_image_context_explosion.md`
- **Memory:** `project_session_multimodal_pdf_context_explosion.md`

**One-line root cause:** the stack had **no image-token accounting** — every token
counter and the summarizer-input builder did `str(msg.content)` over multimodal
content, tokenizing the embedded base64 image data as if it were text.

---

## 1. What was implemented

| Slice | Change | Kills |
|---|---|---|
| **S1** Truth-anchored trigger | Image blocks counted flat (not base64-as-text); compaction trigger floored by the provider's real `input_tokens` (`record_provider_usage`) | The 9.18M phantom + every false compaction trigger |
| **S2** Prevention | Don't rasterize text-rich, image-free PDF pages (`should_render_page` / `page_render_decisions`) | Image tokens wasted on text documents at the source |
| **S3** Image-safe recovery | Summarizer input + compaction elision strip/shed image blocks; timeout names itself | A *genuine* overflow still choking the summarizer on base64 |
| **S4** Per-family estimator | Dimension-aware per-family image-token cost from the model matrix (patches/tiles/flat) | Flat-1600 imprecision; `claude-sonnet`/`haiku` counting nothing (were `multimodal:false`) |

**Files of record:** `src/core/image_tokens.py` (the estimator), `src/core/context.py`
(counters + trigger + recovery), `src/core/summarizer.py` (timeout log),
`src/utils/pdf.py` + `src/tools/workspace/files.py` (S2 gate), `src/core/loader.py`
+ `config/model_config_matrix.yaml` (per-family config plumbing).

---

## 2. Coverage map

| Layer | Proves | Needs |
|---|---|---|
| §3 Unit tests (75) | Pure logic of all four slices: counting, gate, recovery, every estimator mode | local `pytest` |
| §4 Expected values | The estimator reproduces each provider's own published numbers | reference table / Anthropic `count_tokens` oracle |
| §5 k3d component checks | The real **deployed agent image** counts honestly, sheds images, and resolves per-family config end-to-end | k3d cluster + Tilt-built agent image |
| §6 Live session (gold) | A real multimodal session completes with no false compaction and no wedge | dev/k3d cockpit + a vision model |

The compaction / summarizer / estimator code runs **inside the agent image**, not
the orchestrator. Agents are provisioned on-demand, and Tilt rebuilds the agent
image on `src/*.py` + `config/*` edits — so §5 runs against the freshly-built
image the provisioner is actually configured to use.

---

## 3. Automated tests (local)

```bash
source venv/bin/activate
# All four slices at once:
pytest tests/test_image_token_accounting.py tests/test_pdf_render_gate.py \
       tests/test_image_safe_recovery.py tests/test_image_token_estimator.py -q
# Lint (CI commands):
ruff check src/ orchestrator/ tests/
ruff format --check src/ orchestrator/ tests/
```

| Slice | File | Tests | Covers |
|---|---|---:|---|
| S1 | `tests/test_image_token_accounting.py` | 13 | `split_text_and_images` / `estimate_image_tokens`; image-aware counting on the `5dbb5770` shape (12 images → tens of thousands, **not** ~9.2M); `record_provider_usage` floors the trigger; local count wins when larger |
| S2 | `tests/test_pdf_render_gate.py` | 11 | `should_render_page` (text-rich skipped; image-bearing **or** text-sparse rendered; threshold boundary; custom threshold); `compress_ranges` |
| S3 | `tests/test_image_safe_recovery.py` | 24 | `content_to_summary_text` (image → `[image: …]` marker, never base64; mime extraction for all 3 provider shapes); `has_image_content`; `_format_messages_for_summary` no-leak; `_shed_image_messages`; `_elide_largest_tool_results` (images first, plain user turns kept, tool `tool_call_id` pairing preserved); `_emergency_truncate_tool_results`; `_describe_exc` |
| S4 | `tests/test_image_token_estimator.py` | 27 | dimension reader (PNG IHDR / JPEG SOF + APP0 skip / Anthropic `source` / Responses `input_image` / unreadable / remote-URL); every mode against the §4 worked examples; dispatch fallbacks (None → default, unknown mode → flat, unreadable dims → family flat); **loader routing** (`settings.image_tokens` → `limits`); end-to-end `ContextManager.get_token_count` |

**Full suite:** `pytest tests/ -q` → **6495 pass**. Two unrelated pre-existing
failures are **not** part of this feature (see §7): `test_endpoint_inventory`
(stale route manifest from the `feat(datasources)` commit) and
`test_database_phase1::test_connect_disconnect` (needs a live Postgres).

> Note: `tests/test_settings_matrix.py` was updated — `image_tokens` is a
> passthrough `limits` leaf (not derived from the window base), so its
> `_derived_leaves(data)` helper drops it before the derived-leaf equality.

---

## 4. Expected values (the estimator oracle)

All verified against each provider's primary-source docs (2026-06-14) and the
provider's own published examples. Worked for a **1700×2200 px** page render:

| Family (`family_of`) | Mode | 1700×2200 | Notes |
|---|---|---:|---|
| `gpt-5` (gpt-5.4/5.5) | `openai_patches`, budget 10000 | **3726** | `⌈w/32⌉·⌈h/32⌉` ≤ budget → 54·69 |
| `claude-sonnet`/`haiku` (legacy) | `anthropic_patches`, 1568/1568 | **1496** | 28px patches after the two-constraint resize |
| `claude-opus` (4.7+/Fable5) | `anthropic_patches`, 2576/4784 | **4758** | high-res cap |
| `o-series` (o1/o3) | `openai_tiles`, 75+150/tile | **675** | 4 tiles after 2048→768 fit |
| `gpt-4o` (if used) | `openai_tiles`, 85+170/tile | **765** | 4 tiles |
| `gemini` | `flat` | **2304** | biased-high; API (not Vertex) |
| `gemma` | `flat` | **320** | Gemma-4 default 280 ×1.14 |

Anthropic self-check examples the port reproduces: **1000×1000 → 1296**, A4
(1075×1520) → **1551**. For an exact, unbilled oracle on real images, Anthropic's
free `POST /v1/messages/count_tokens` counts image blocks (pass the same model
ID); compare against `estimate_image_block_tokens` — the estimate must be
**≥ actual** (biased-high) and within a small band.

---

## 5. k3d verification (deployed agent image)

Run the actual slice code inside the image the provisioner uses, with the **real
tiktoken counter** and the **real `model_config_matrix.yaml`** baked into `/app`.

### 5.0 Probe-pod harness

```bash
CTX=k3d-srw; NS=srw
IMG=$(kubectl --context=$CTX -n $NS get cm srw-config -o jsonpath='{.data.AGENT_IMAGE}')
kubectl --context=$CTX -n $NS run srw-verify --image="$IMG" --restart=Never \
  --image-pull-policy=IfNotPresent --command -- sleep 600
kubectl --context=$CTX -n $NS wait --for=condition=Ready pod/srw-verify --timeout=90s
# ... cp + exec a script (below) ...
kubectl --context=$CTX -n $NS cp ./verify.py srw-verify:/tmp/verify.py
kubectl --context=$CTX -n $NS exec srw-verify -- python /tmp/verify.py
kubectl --context=$CTX -n $NS delete pod srw-verify --now      # cleanup
```

If you just edited `src/` or `config/`, force the agent rebuild first
(`tilt trigger srw`) and confirm `kubectl ... get cm srw-config -o
jsonpath='{.data.AGENT_IMAGE}'` advanced to a new `tilt-…` tag.

### 5.1 S1 — honest counting (`verify.py`)

```python
from langchain_core.messages import HumanMessage
from src.core.context import ContextConfig, ContextManager

b64 = "Z" * 200_000  # ~one page render's worth of base64
msgs = [HumanMessage(content=[
    {"type": "text", "text": f"Image content from tool call call_{i}"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
]) for i in range(12)]

n = ContextManager(config=ContextConfig()).get_token_count(msgs)
print("12 images (~2.4M base64 chars) ->", n, "tokens")
assert n < 50_000, "PHANTOM: base64 counted as text"   # was ~9.2M
print("PASS: honest (flat ~1600/image, not base64)")
```

### 5.2 S3 — image-safe recovery (`verify.py`)

```python
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from src.core.context import ContextConfig, ContextManager
from src.core.summarizer import _describe_exc, count_text_tokens
import asyncio

b64 = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
              for _ in range(3200))[:200_000]
def img(i):
    return HumanMessage(content=[{"type": "text", "text": f"img {i}"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}])

msgs = [img(i) for i in range(12)] + [ToolMessage(content="ok", tool_call_id="t",
        name="read_file"), AIMessage(content="done")]
mgr = ContextManager(config=ContextConfig())

# (a) summarizer input has no base64 — the leak that timed out gemma 3/3
parts = mgr._format_messages_for_summary(msgs)
blob = "\n".join(parts)
assert "base64," not in blob and b64[:64] not in blob
print("summarizer-input tokens:", count_text_tokens(blob), "(was ~hundreds of k of base64)")

# (b) elision sheds image messages (lossless), keeps tool pairing
elided = mgr._elide_largest_tool_results(msgs, target_tokens=mgr.get_token_count(msgs)//2)
shed = sum(1 for m in elided if isinstance(m, HumanMessage)
           and isinstance(m.content, str) and m.content.startswith("[image content elided"))
print("image messages shed:", shed, "/ 12")

# (c) a timeout names itself instead of logging "failed ()"
assert _describe_exc(asyncio.TimeoutError()) == "TimeoutError"
print("PASS: no base64 leak, images shed, timeout named")
```

### 5.3 S4 — per-family estimator + full chain (`verify.py`)

```python
import base64, struct
from langchain_core.messages import HumanMessage
from src.core.context import ContextConfig, ContextManager
from src.core.image_tokens import estimate_image_block_tokens, read_image_dimensions
from src.core.loader import _apply_settings_matrix

def page(w, h):  # a minimal valid PNG header carrying the dimensions
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\x0d" + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    url = "data:image/png;base64," + base64.b64encode(sig + ihdr).decode()
    return {"type": "image_url", "image_url": {"url": url}}

P = page(1700, 2200)
assert read_image_dimensions(P) == (1700, 2200)

# Full chain: matrix -> limits.image_tokens -> ContextManager count
data = {"llm": {"model": "gpt-5.5"}}; _apply_settings_matrix(data, set())
cfg = data["limits"]["image_tokens"]
assert cfg["mode"] == "openai_patches"
mgr = ContextManager(config=ContextConfig(image_tokens=cfg), model="gpt-5.5")
n = mgr.get_token_count([HumanMessage(content=[{"type": "text", "text": "x"}, P])])
print("gpt-5.5 page count:", n, "(~3726 + text)"); assert 3726 <= n < 3800

# claude-sonnet row now exists (was falling to default multimodal:false)
d2 = {"llm": {"model": "claude-sonnet-4-6"}}; _apply_settings_matrix(d2, set())
assert d2["llm"]["multimodal"] is True
assert d2["limits"]["image_tokens"]["mode"] == "anthropic_patches"

# Per-mode worked examples (must be exact)
assert estimate_image_block_tokens(P, cfg) == 3726
assert estimate_image_block_tokens(P, {"mode":"anthropic_patches","patch_px":28,"max_edge":1568,"max_tokens":1568,"flat":1568}) == 1496
assert estimate_image_block_tokens(P, {"mode":"anthropic_patches","patch_px":28,"max_edge":2576,"max_tokens":4784,"flat":4784}) == 4758
assert estimate_image_block_tokens(P, {"mode":"openai_tiles","base":75,"per_tile":150,"tile_px":512,"flat":1000}) == 675
print("PASS: chain + every mode exact")
```

### 5.4 S2 — PDF render gate (`verify.py`, needs `pdfplumber` → agent image, not orchestrator)

```python
from src.utils.pdf import should_render_page, page_render_decisions, PDF_AVAILABLE
assert should_render_page(chars=5000, has_images=False) is False   # text page skipped
assert should_render_page(chars=5000, has_images=True) is True     # pictogram kept
assert should_render_page(chars=10, has_images=False) is True      # scanned/figure kept
print("PDF_AVAILABLE:", PDF_AVAILABLE)  # True in agent, False (fail-open) in orchestrator
# On a real PDF in the pod, page_render_decisions(path, 1, N) returns
# {page: {render, chars, has_images}} — fail-open returns render=True for all.
```

**Expected k3d results** (recorded 2026-06-13/14): S1 = ~19k honest (orchestrator
+ agent image); S2 = real Cryogel SDS 1/6 pages render (pictogram p1 kept, text
p2–6 skipped); S2 fail-open in orchestrator (no `pdfplumber`) returns render-all;
S3 = summarizer input 263k→767 tok, 12/12 images shed, `TimeoutError` named; S4 =
full chain count 3732, claude-sonnet `multimodal:true`, per-mode 3726/1496/4758/675.

---

## 6. Live session (gold standard — ✅ VERIFIED 2026-06-14)

Proves the graph loop, not just components.

**Result (2026-06-14) — verified observationally against a real post-fix production
session, which is stronger than a synthetic repro: it's the exact failing model +
workload, run by a real user against the deployed fix.**

Session `0ed8c0e0-5928-4db9-94f3-8782d840f278` (dev cluster, ns
`superhuman-remote-worker`, created **18:12, after the S1–S4 deploy**):

- **gpt-5.5** via codex-proxy, `persistent_defaults` — the same model that wedged
  `eb989b82`/`5dbb5770`.
- Read **10 multi-page PDFs** → 10 `Image content from tool call` HumanMessages +
  22 tool results (45 messages total). S2 visibly active: a 9-page doc rendered
  pages 1–5 (`seq 5480: [Pages 1-5 of 9]`).
- **No compaction fired** — the role breakdown has **zero `role='summary'` rows**;
  the `COMPACTING … 875%` phantom never appeared (correct counting ⇒ ~tens of k
  real tokens, far under the window).
- **Turn completed** — final message `seq 5483` is a substantive **10,399-char**
  AI answer.

Contrast pre-fix `eb989b82`: wedged at the trailing image HumanMessage, 2.79M
phantom, stuck compacting (`total_turns=1`, last_role=human-image). (`total_tokens`
is unreliable as a wedge signal — it reads `0` for *all* persistent threads incl.
completed multi-turn ones; the real signal is whether an AI message with content
follows the images.)

A controlled browser-driven repro remains an option but is now **redundant** — the
real session above exercised the exact failing path end-to-end and completed; every
underlying component is also proven in §5.

1. Place a multi-page PDF (ideally with a pictogram/figure page) in the session's
   cloud/workspace.
2. Start a session on a **vision** model (e.g. a gemma-vision or gpt-5.5 session)
   via the cockpit and ask it to read the PDF, then continue the conversation a
   few turns.
3. Assert, via the cockpit + `kubectl ... logs -l srw/managed-by=agent-provisioner -f`:
   - the bottom-bar **CTX %** stays single-digit for a small doc and tracks the
     real `input_tokens` (no "875%" banner);
   - **no** `COMPACTING …` banner appears unless the context genuinely nears the
     window;
   - if you force a genuine overflow (load a real >1M-token context), summarization
     **completes** (no 240s aux timeout, no `failed ()` log) and the turn finishes
     with a summary + intact recent tail;
   - S2: the agent logs `[Did not rasterize N text-only page(s) …]` for a
     text-heavy PDF, and pictogram pages still render.

A scripted browser/API drive was never needed — the real session above is the
gold-standard evidence; every underlying component is also proven in §5. (If a
controlled repro is ever wanted: start a gpt-5.5 session, read several multi-page
PDFs, assert no `COMPACTING` banner and a completed turn.)

---

## 7. Known non-issues & open knobs

**Not regressions from this feature:**
- `test_endpoint_inventory::test_endpoint_inventory_matches_manifest` — stale API
  route manifest from the separate `feat(datasources)` commit (`/api/datasources/eligible`).
- `test_database_phase1::test_connect_disconnect` — env-dependent, needs a live Postgres.

**Robustness guard:** `ContextManager.__init__` reads the new field via
`getattr(self.config, "image_tokens", None)` — some callers (a few tests) hand it
a whole `AgentConfig` rather than a `ContextConfig`; absent field → flat
estimation, never a constructor crash.

**Open / biased approximations (documented, safe because the trigger re-anchors on
the real `input_tokens` every turn):**
- OpenAI full-model patch→token multiplier is unpublished → using `1.0`; the
  biased-high budget (10000) covers it.
- `o4-mini` is actually patch-based but inherits the `o-series` tile config
  (unused today; biased-low — noted in the YAML).
- `gemini` flat 2304 assumes the **ai.google.dev** API, not Vertex (~7× higher) —
  log the endpoint if that changes.
- Per-family render **DPI** (the S2 downscaling-raises-tiles caveat) is still
  deferred — `document_renderer.py` uses a fixed `DEFAULT_DPI=150`.
