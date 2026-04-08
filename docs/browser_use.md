# Browser Automation — Architecture Analysis & Design Options

> **Purpose**: Collect all findings about browser automation patterns before deciding
> on a target architecture. This is a living design doc, not a spec.
>
> **Last updated**: 2026-04-08

---

## 1. Current Implementation

### Architecture: Sub-agent delegation

Our browser automation lives in `src/tools/research/browser.py` and uses the
[browser-use](https://github.com/browser-use/browser-use) Python library in
**autonomous sub-agent mode**.

**How it works:**

1. The main agent calls `browse_website(url, task)` or `download_from_website(url, task)` — a single tool invocation.
2. Inside the tool, a **separate browser-use `Agent`** is created with its own LLM instance.
3. That sub-agent runs an autonomous loop internally: read page state → decide action → execute → observe result → repeat until done.
4. The main agent receives a **text-only summary** of the final result via `_extract_result()`.

The main agent never sees the browser — no screenshots, no DOM, no page state.
It's fire-and-forget: "go do this task, report back."

### Browser sub-agent LLM

Hardcoded to `gpt-4o-mini` via `_get_browser_llm()`. Configurable only through
environment variables:

- `BROWSER_LLM_MODEL` — model name (default: `gpt-4o-mini`)
- `BROWSER_LLM_API_KEY` — API key (falls back to `OPENAI_API_KEY`)
- `BROWSER_LLM_BASE_URL` — custom endpoint

The main agent's model, provider, and credentials are **not used** for browsing.
The sub-agent always creates a plain `ChatOpenAI` instance regardless of what
provider the main agent uses (Anthropic, Google, etc.).

### Exposed tools (2 total)

| Tool | Description | Vision | Phase |
|------|-------------|--------|-------|
| `browse_website(url, task, use_vision=False)` | Navigate + extract info | Configurable per-call | Tactical |
| `download_from_website(url, download_task)` | Navigate + download file | Always False | Tactical |

### Configuration (`config/defaults.yaml` → `browser:`)

```yaml
browser:
  headless: true
  timeout: 60000        # ms
  use_vision: false     # DOM-based default (works with any LLM)
  remote: auto          # "auto" | "local"
```

### Remote browser support

When the workspace uses a remote backend (container pod or VM), Chromium is
started on the workspace host and controlled via CDP over the network. Downloads
land directly on the workspace filesystem. The lifecycle is:

1. `_start_remote_chromium()` — kill leftovers, start headless Chromium with
   `--remote-debugging-port`, poll for CDP WebSocket URL.
2. browser-use connects via `cdp_url`.
3. `_stop_remote_chromium()` — kill Chromium in the `finally` block.

Already uses `--user-data-dir=/tmp/chromium-cdp-profile` on the remote
workspace, but this is thrown away because Chromium is killed after every call.

### Vision mode

The `use_vision` parameter controls how the **sub-agent** (not the main agent)
perceives pages:

- **False (default)**: DOM/accessibility tree extraction via CDP. Text-only,
  works with any LLM. Cheaper in tokens.
- **True**: Screenshot-based. Sub-agent receives page screenshots and decides
  actions visually. Requires a multimodal LLM.

**Note**: browser-use has migrated from Playwright to **direct CDP** via the
`cdp-use` library. The old `buildDomTree.js` approach is gone. DOM extraction
now uses `Accessibility.getFullAXTree()` + `DOMSnapshot.captureSnapshot()` via
CDP, merged into `EnhancedDOMTreeNode` objects and serialized to text.

### Existing non-browser research tools

Our agents already have a tiered approach. The browser is the heavyweight
fallback, not the default:

- `web_search` — Tavily API (cheapest, fastest)
- `extract_webpage` — Tavily content extraction (HTTP-based, no JS)
- `crawl_website` / `map_website` — Tavily crawl/sitemap
- `browse_website` — browser-use sub-agent (full browser, JS-capable)
- `download_from_website` — browser-use sub-agent (download flows)

This is architecturally sound. Benchmark data (WebArena) shows browser-only
achieves 14.8% task success, API-only 29.2%, and hybrid 38.9% — a **24pp
improvement** from combining both approaches.

### Limitations of current approach

1. **No context sharing**: The main agent can't see what the browser is doing.
   If browsing fails or returns unexpected results, the main agent has no way
   to debug or guide the process.
2. **Fixed sub-agent model**: Always `gpt-4o-mini` regardless of what model
   the main agent uses. Can't leverage stronger models for complex navigation.
3. **No persistence**: Each tool call creates a fresh browser instance.
   No cookies, sessions, or state carry over between calls.
4. **No interactivity**: The main agent can't pause the browser mid-task,
   inspect state, or course-correct.
5. **Vision mode is disconnected**: The `use_vision` toggle only affects the
   sub-agent, and the sub-agent's LLM is fixed — so enabling vision with a
   non-multimodal sub-agent model would fail silently or degrade.
6. **No security controls**: No URL validation, no domain allowlists, no
   content boundary markers. The browser can navigate anywhere, and raw page
   content enters the agent's context unfiltered.
7. **No SSRF protection**: The remote Chromium can reach internal services,
   cloud metadata endpoints (`169.254.169.254`), and Kubernetes DNS.

---

## 2. Industry Landscape (2025-2026)

### Pattern A: Direct MCP tools (Claude Code, Playwright MCP)

**Claude Code** uses a Chrome extension exposing **18 MCP tools** to the main
model: `read_page` (accessibility tree with element refs), `computer`
(mouse/keyboard/screenshots), `navigate`, `form_input`, `read_console_messages`,
`read_network_requests`, `javascript_tool`, etc.

**Playwright MCP** (Microsoft, `@playwright/mcp`) exposes **21 tools** in
snapshot mode:

- Navigation (3): `browser_navigate`, `browser_navigate_back`, `browser_tabs`
- Interaction (8): `browser_click`, `browser_type`, `browser_hover`,
  `browser_drag`, `browser_select_option`, `browser_press_key`,
  `browser_file_upload`, `browser_fill_form`
- Observation (4): `browser_snapshot`, `browser_take_screenshot`,
  `browser_console_messages`, `browser_network_requests`
- Control (4): `browser_wait_for`, `browser_handle_dialog`, `browser_resize`,
  `browser_close`
- Code (1): `browser_run_code`

Snapshot format uses ARIA roles with element refs:
```
- heading "todos" [level=1]
- textbox "What needs to be done?" [ref=e5]
- listitem:
  - checkbox "Toggle Todo" [ref=e10]
  - text: "Buy groceries"
- button "Submit" [ref=e12]
```

Both systems: **the main LLM directly decides each browser action** — no
sub-agent. Page state returned as compact accessibility trees. Screenshots
available on demand.

**Pros**: Full context for the main agent, single model, course-correction.
**Cons**: Many small LLM calls for navigation (expensive with large models),
high schema overhead (~14K-17K tokens just for tool definitions).

### Pattern B: Sub-agent delegation (OpenHands classic, us)

OpenHands' `CodeActAgent` delegates browsing to a specialized `BrowsingAgent`
via `AgentDelegateAction`. Uses BrowserGym action primitives.

**Pros**: Sandboxed, specialized agent can be optimized independently. Provides
**accidental prompt injection isolation** — the sub-agent's context is separate
from the main agent.
**Cons**: No context sharing with main agent, token-expensive (sub-agent runs
many internal LLM calls), delegation overhead.

### Pattern C: Persistent CLI tool (Vercel agent-browser)

Vercel's `agent-browser` is a **Rust CLI + Node.js daemon** designed as a tool
for AI agents (not itself an agent).

- **Persistent daemon**: first command starts background daemon; subsequent
  commands reuse it. Browser state (cookies, tabs, localStorage) persists.
- **Compact snapshots**: `agent-browser snapshot -i` returns only **interactive
  elements** with stable refs (`@e1`, `@e2`, `@e3`) — roughly **200-400 tokens**.
- **Minimal response payloads**: `click` returns "Done" (6 chars) vs. Playwright
  MCP returning the full updated accessibility tree (thousands of chars).
- **Batch execution**: `agent-browser batch "open ..." "snapshot -i" "click @e3"`
  runs multiple commands atomically in one shell invocation.
- **Security**: CSPRNG nonce content boundaries, domain allowlists, action
  policies for destructive operations.
- **Auth vault**: Encrypted credential storage, never exposed to LLM.
- **Zero tool-definition overhead**: LLM learns the CLI from a SKILL.md file,
  not from JSON tool schemas.

**Pros**: Token-efficient, persistent state, main LLM drives directly, good
security model, works via shell tool (agents already have shell access).
**Cons**: Requires Rust/Node.js infrastructure, not Python-native, no
autonomous fallback for simple tasks.

### Pattern D: browser-use as direct tools

browser-use supports direct control without the Agent wrapper:

```python
# Direct BrowserSession usage (no Agent class)
from browser_use import BrowserSession

session = BrowserSession(cdp_url="http://workspace:9222")
await session.start()

# Get DOM state (what the Agent's LLM normally sees)
state = await session.get_browser_state_summary(include_screenshot=True)
dom_text = state.dom_state.llm_representation()  # text for LLM
screenshot_b64 = state.screenshot                # base64 PNG

# Execute actions via Tools/Controller (no Agent LLM loop)
from browser_use.tools.service import Tools
tools = Tools()
result = await tools.registry.execute_action(
    action_name="click_element",
    params={"index": 42},
    browser_session=session,
)
```

Also supports manual `step()` control, custom actions via
`@tools.registry.action()`, and domain-filtered actions.

**Pros**: Reuses browser-use infrastructure we already have, Python-native,
full control over the LLM loop.
**Cons**: Less documented path, CDP-based (browser-use migrated away from
Playwright), event bus architecture adds complexity.

### Pattern E: Stagehand — Deterministic-first, AI-when-needed

[Stagehand](https://github.com/browserbase/stagehand) extends Playwright/CDP
with three AI helper methods: `act()`, `extract()`, `observe()`. The majority
of workflow remains explicit deterministic code; AI engages only where selectors
might fail.

Key innovation: **caches successful AI-resolved selectors** and replays them
deterministically on subsequent runs. Re-engages AI only when cached selectors
break. Stagehand v3 moved to CDP-native, cutting round-trip time by 44%.

### Summary table

| Pattern | Who uses it | Browser control | Main agent sees pages? | Persistence | Token cost (10-step) |
|---------|-------------|-----------------|----------------------|-------------|---------------------|
| A. Direct MCP tools | Claude Code, Playwright MCP | Main LLM | Yes (trees + screenshots) | Yes | ~114K tokens |
| B. Sub-agent delegation | OpenHands classic, **us** | Separate LLM | No (text summary only) | No | Opaque (sub-agent internal) |
| C. Persistent CLI tool | Vercel agent-browser | Main LLM via shell | Yes (compact snapshots) | Yes (daemon) | **~7K tokens** |
| D. browser-use direct | OpenHands SDK V1 | Main LLM | Yes (via DOM API) | Possible | ~10K-25K tokens |
| E. Stagehand | Browserbase | Deterministic + AI fallback | Partially | Possible | Lowest (cached paths) |

### Industry trend

The clear direction is **away from sub-agent delegation** and **toward direct
control** by the main LLM. Key enablers:

1. Compact page representations (interactive-only accessibility trees with
   stable refs, ~200-400 tokens vs. thousands for full trees/screenshots).
2. CLI-based tools outperform MCP tools on token efficiency (4x cheaper).
3. Persistent browser daemons that maintain state across tool calls.

---

## 3. Token Cost Analysis

Token efficiency is a primary design driver. Hard numbers from benchmarks:

### Tool definition overhead (one-time per conversation)

| Approach | Schema Tokens | Notes |
|----------|--------------|-------|
| Shell CLI (agent-browser) | 0 | Learned from SKILL.md in system prompt |
| Playwright MCP | ~13,700 | 21 tool JSON schemas |
| Chrome DevTools MCP | ~17,000 | 29 tool JSON schemas |

### Per-page snapshot cost

| Page | Playwright MCP (full tree) | Optimized (interactive-only) | agent-browser |
|------|---------------------------|-------------------------------|---------------|
| Wikipedia | 16,044 tokens | ~7,860 tokens | ~200-400 |
| GitHub repo | 19,409 tokens | ~4,304 tokens | ~200-400 |
| Hacker News | 14,547 tokens | ~3,052 tokens | ~200-400 |
| Simple login form | ~3,800 tokens | ~1,400 tokens | ~100-200 |

### Per-action response cost

| Operation | agent-browser | Playwright MCP |
|-----------|--------------|----------------|
| Click | ~6 chars ("Done") | Full re-snapshot (~14K tokens) |
| Fill | ~6 chars | Full re-snapshot |
| Navigate | URL confirmation | Full page snapshot |

### 10-step workflow total

| Approach | Total Tokens | Relative Cost |
|----------|-------------|---------------|
| agent-browser (CLI) | ~7,000 | 1x (baseline) |
| Playwright CLI | ~27,000 | ~4x |
| Playwright MCP | ~114,000 | ~16x |

### Representation comparison

| Representation | Tokens per page | Notes |
|----------------|----------------|-------|
| Raw DOM (`outerHTML`) | 100K-600K+ | Completely impractical |
| Full accessibility tree | 14K-19K | What Playwright MCP returns |
| Interactive-only a11y tree | 1K-5K | What agent-browser returns |
| Screenshot (1024x768) | ~765-1,300 | Requires multimodal model |
| Downsampled DOM (D2Snap) | ~1,300-5K | Comparable to screenshots |

### Context accumulation over time

- **Naive approach**: Linear token growth → 43K+ tokens after ~15 steps.
- **Single snapshot retention**: Only keep the latest snapshot, discard previous
  ones. Constant ~12,600 tokens regardless of step count.
- **Prefix caching**: 74.9% of input tokens served from cache, reducing
  effective cost by 89% for extended sessions.

### Cost per task

| Model | Per-task cost | Notes |
|-------|-------------|-------|
| GPT-4o-mini (sub-agent) | ~$0.01-0.05 | Current default, cheapest |
| Browser Use Cloud | ~$0.10 | Optimized infrastructure |
| GPT-4o / Claude Sonnet | ~$0.10-0.50 | Mid-range |
| Claude Opus / GPT-5 | ~$0.50-1.00 | Expensive for micro-actions |

---

## 4. Benchmark Data & Real-World Usage

### What agents actually use browsers for

From production systems and benchmarks:

**Coding agents** (Devin, OpenHands, Cursor, Windsurf):
- **Verifying their own work** visually (rendered HTML, UI changes)
- **Documentation lookup** when context is insufficient
- **Downloading resources** that have no API
- Browser is secondary to file editing and shell commands

**Research agents** (our primary use case):
- **Search-then-browse**: Search API first to get URLs, then selectively browse
- **Academic paper access**: Paywalled content requiring institutional login
- **Data extraction**: Tables, forms, structured content from web pages
- **Prefer API/HTTP when possible**: Browser is the heavyweight fallback

**OpenHands benchmark data**:
- GAIA benchmark: ~40% browser + ~40% search engine usage
- The Agent Company benchmark: ~70% browser-dominant (corporate intranets)
- SWE-Bench: Primarily bash/file tools; browser for visual verification

### Task success rates by complexity

| Difficulty | Steps | Success Rate | Notes |
|-----------|-------|-------------|-------|
| Easy | ≤5 | 54.2% avg | Most agents handle these |
| Medium | 6-10 | 22.6% avg | 31.6pp drop |
| Hard | 11+ | 7.2% avg | Catastrophic degradation |

**Critical finding**: Success collapses beyond 10 steps. If a browser task
needs more, break it into sub-tasks with fresh context.

### DOM vs. vision: which works better?

| Approach | Token Cost | Strengths | Weaknesses |
|----------|-----------|-----------|------------|
| Accessibility tree | 50-90% cheaper | Structured, deterministic, works with any LLM | Misses visual cues, canvas content, unlabeled elements |
| Screenshots | ~10K+ tokens/page | Sees what humans see, handles visual content | Fails on dense interfaces (24px calendar cells), expensive |
| Hybrid (SOTA) | Moderate | Best of both, 75% cost reduction via prefix caching | More complex architecture |

**WALT** (current SOTA) achieves 52.9% on VisualWebArena using hybrid
multimodal DOM + vision, with 10-30% success rate gains over single-mode.

**Production recommendation**: Use accessibility tree as primary perception,
add vision selectively for visual verification tasks.

### Common failure modes

1. **Filter/sort errors** (57.7% of failures): Complex UI filters, dropdowns
   with many options, multi-criteria sorts.
2. **Navigation errors** (19.6%): Wrong page, can't find target element.
3. **Infinite loops**: Hard loops (same action repeated), soft loops (minimal
   variation), retry storms, semantic loops (rephrasing without progress).
4. **Anti-bot detection**: Fingerprinting, behavioral analysis. Stealth plugins
   are largely ineffective against modern systems.
5. **Element selection failures**: Overlays, slow-loading content, shadow DOM.
6. **Cookie banners**: Block automation if not dismissed. Solutions: CSS
   injection, browser extensions, pre-navigation hooks.
7. **Context degradation**: Playwright MCP accumulates 60K-80K tokens of stale
   page state by step 15. Performance degrades noticeably.

### Action space design (AgentOccam, ICLR 2025)

Reducing the action space **improved WebArena success by 161%** (16.5% → 43.1%).
Removing non-essential actions alone contributed +16.8pp.

**Actions that hurt performance when included**: `noop`, tab operations, `go_forward`,
`hover`, `press` (require embodied knowledge LLMs lack).

**Actions that help**: `click`, `type`, `stop`, `go_back`, `go_home`, `note`
(record observations).

---

## 5. Security Analysis

### Threat model

When an AI agent reads web page content, that content enters the LLM's context.
A malicious page can embed hidden instructions that the LLM follows. This is
OWASP's #1 vulnerability for LLM applications (2025), found in 73% of assessed
deployments.

### Known CVEs and attacks

| CVE / Attack | Target | Impact |
|---|---|---|
| CVE-2025-47241 | **browser-use** (our dep) | Domain validation bypass via crafted URLs, enables SSRF |
| CVE-2025-53773 | GitHub Copilot | RCE through prompt injection |
| CVE-2026-25253 | OpenClaw | RCE; 21K exposed instances leaking credentials |
| CVE-2025-32711 | Microsoft 365 Copilot (CVSS 9.3) | Data exfiltration from OneDrive/SharePoint/Teams |
| "Tainted Memories" | OpenAI Atlas | CSRF that poisons long-term AI memory persistently |

### Our current security gaps

1. **No content boundaries**: Raw page content enters the agent context unfiltered.
2. **No URL validation**: `browse_website` and `download_from_website` accept any URL.
3. **No SSRF protection**: Remote Chromium can reach internal services, cloud
   metadata endpoints, Kubernetes DNS.
4. **Credentials in environment**: `OPENAI_API_KEY`, `TAVILY_API_KEY` accessible
   to the browser process.
5. **CVE-2025-47241 applies to us**: We use browser-use's domain validation.

### Mitigation strategies (prioritized)

**Priority 1 — Immediate (low effort, high impact):**

1. **Content boundary nonces** (Vercel pattern): Wrap all returned page content
   with CSPRNG-nonce-tagged markers before it enters the parent agent's context.
   ```
   [BOUNDARY_START_a8f3c91d2e...]
   <untrusted page content>
   [BOUNDARY_END_a8f3c91d2e...]
   ```
   The nonce is unpredictable — malicious pages cannot forge boundary markers.

2. **URL validation**: Pre-navigation validation that rejects:
   - Private IP ranges (RFC 1918), loopback, link-local
   - Cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`)
   - Dangerous schemes (`file://`, `javascript://`, `data:`)
   - Kubernetes internal DNS (`*.cluster.local`, `*.svc`)

3. **Strip secrets from browser environment**: Ensure API keys are not
   accessible to the Chromium process on the workspace.

**Priority 2 — Short term (medium effort):**

4. **Domain allowlist in config**:
   ```yaml
   browser:
     security:
       allowed_domains: ["*.arxiv.org", "*.github.com", "*.wikipedia.org"]
       blocked_domains: ["localhost", "127.0.0.1", "169.254.169.254"]
       blocked_schemes: ["file", "javascript", "data"]
   ```

5. **Kubernetes NetworkPolicy for workspace pods**: Block egress to private IP
   ranges and cloud metadata. Allow only ports 80/443 to public internet.

6. **Separate context for web content**: Process web content in a separate LLM
   call to extract structured data, then inject only the extraction (not raw
   HTML/text) into the main agent's context. Our sub-agent pattern already
   provides this accidentally.

**Priority 3 — Medium term:**

7. **Egress proxy**: Route workspace browser traffic through an HTTP proxy that
   logs requests and blocks suspicious patterns.
8. **Output scanning**: Scan browser tool return values for API key patterns.
9. **Per-expert domain allowlists**: Scholar → academic domains; developer →
   GitHub/docs; curator → no browser access.

### How comparable systems handle security

| System | Approach |
|--------|----------|
| Claude Code | Permission-based, separate context for web fetches, command blocklist, RL-trained prompt injection resistance (~1% attack success) |
| OpenHands | Docker-sandboxed, fine-grained tool control |
| Devin | Full VM isolation, proxy-based network controls |
| Vercel agent-browser | CSPRNG boundaries, domain allowlists, action policies, auth vault |

---

## 6. Session Persistence & State Management

### Current state: ephemeral (no persistence)

Each tool call spins up fresh Chromium, does work, tears it down. No cookies,
login state, or localStorage carries over.

### Three persistence patterns

**Pattern A — Long-lived CDP connection (recommended):**
Start Chromium once per job, reconnect via CDP between tool calls. The browser
process maintains all session state naturally.

```python
# First call: start if not running, store CDP URL in ToolContext
# Subsequent calls: check health, reconnect via CDP
browser = await playwright.chromium.connect_over_cdp("http://workspace:9222")
context = browser.contexts[0]  # Reuse existing context
page = context.pages[0]        # Reuse existing page
```

**Pattern B — `user_data_dir` persistence:**
Chromium writes its profile to disk. Even if killed between calls, next launch
restores cookies/localStorage from disk. Our code already uses
`--user-data-dir=/tmp/chromium-cdp-profile` but kills Chromium after each call.

**Pattern C — `storageState` export/import:**
Save cookies to JSON, restore in new browser context. Playwright-compatible.
```python
state = await context.storage_state(path="auth_state.json")
# Later:
context = await browser.new_context(storage_state="auth_state.json")
```
**Limitation**: Only saves cookies, not localStorage/sessionStorage.

### Recommended approach for our system

1. **Make Chromium lifecycle per-job, not per-tool-call.** Start on first
   browser tool call (lazy), kill on job completion/archive. Store CDP URL
   in ToolContext.
2. **Move `user_data_dir` into workspace directory** (e.g.,
   `/home/agent-host/.browser-profile/`). Scoped to job, destroyed
   with workspace. Survives Chromium crashes.
3. **Add health-check reconnection**: Before each browser tool call, check if
   Chromium is alive. If yes, reconnect. If no, restart with same `user_data_dir`.
4. **Export `storageState` at phase boundaries** as a crash-recovery fallback.

### Authentication flow handling

| Challenge | Pattern |
|-----------|---------|
| Login pages | `storageState` or `user_data_dir` — login once, reuse |
| OAuth | Human completes OAuth in real browser, export tokens to agent |
| 2FA/MFA | Use our freeze mechanism (`freeze_data` with `blocking_message`) to pause and ask human |
| CAPTCHAs | Freeze for human intervention, or residential proxy rotation |

### Resource management

- **Memory leaks**: Chromium accumulates memory over time. Monitor RSS, restart
  every N calls or M minutes. `user_data_dir` survives restart.
- **Chrome flags for containers**: `--disable-extensions`, `--disable-background-networking`,
  `--js-flags="--max-old-space-size=512"`, `--mute-audio`.
- **`shm_size: 2g`** in pod spec instead of `--disable-dev-shm-usage` for
  better performance.
- **Tab cleanup**: Close pages after each tool call. If using persistent browser,
  need explicit tab management.

---

## 7. browser-use Library Deep Dive

### Key architectural change: CDP, not Playwright

browser-use has migrated from Playwright to **direct CDP** via the `cdp-use`
library. `Browser` is now an alias for `BrowserSession`. There is no separate
`BrowserContext` class — `BrowserSession` IS the context.

### DOM extraction pipeline

1. `Accessibility.getFullAXTree()` via CDP → full accessibility tree
2. `DOMSnapshot.captureSnapshot()` via CDP → computed styles, bounding boxes
3. `DomService` merges into `EnhancedDOMTreeNode` objects
4. `DOMTreeSerializer.serialize_accessible_elements()` → `SerializedDOMState`
5. `SerializedDOMState.llm_representation()` → final text string

Output format:
```
[42]<input type="text" placeholder="Search..." value="" />
[43]<button>Search</button>
[44]<a href="/about" title="About Us">About</a>
|scroll element|<div role="main" /> (scrolled 25% of 3200px)
*[47]<input type="email" placeholder="Newsletter" />  <!-- * = newly appeared -->
```

- `[N]` = interactive element with backend_node_id N
- `*[N]` = newly appeared since previous state
- `|scroll element|` = scrollable container with scroll info
- Default cap: 40,000 characters (~10,000 tokens)

### Using browser-use without the Agent class

```python
from browser_use import BrowserSession, BrowserProfile
from browser_use.tools.service import Tools

# Start session
session = BrowserSession(cdp_url="http://workspace:9222")
await session.start()

# Get page state for our LLM
state = await session.get_browser_state_summary(include_screenshot=True)
dom_text = state.dom_state.llm_representation()
screenshot_b64 = state.screenshot

# Execute actions programmatically
tools = Tools()
result = await tools.registry.execute_action(
    action_name="click_element",
    params={"index": 42},
    browser_session=session,
)

# Custom actions
@tools.registry.action(description="Get page title")
async def get_title(browser_session: BrowserSession):
    cdp = await browser_session.get_or_create_cdp_session()
    result = await cdp.cdp_client.send.Runtime.evaluate(
        params={"expression": "document.title", "returnByValue": True},
        session_id=cdp.session_id,
    )
    return ActionResult(extracted_content=result["result"]["value"])
```

### Default built-in actions

`search`, `navigate`, `go_back`, `click_element`, `input`, `switch_tab`,
`close_tab`, `scroll`, `send_keys`, `extract`, `done`, `upload_file`,
`get_dropdown_options`, `select_dropdown_option`, `screenshot`, `save_as_pdf`,
`search_page`, `find_elements`.

### Performance characteristics

- **Per step**: Exactly 1 LLM call for action selection. Optionally +1 for
  extraction, +1 for judge evaluation.
- **Typical task**: 5-20 steps for simple tasks, 20-50+ for complex workflows.
- **Browser state capture**: ~200-500ms (CDP calls)
- **LLM call**: 3-15 seconds (model-dependent, dominant cost)
- **Action execution**: 100-500ms per action
- **Total step**: ~5-20 seconds depending on LLM speed

### Important gotchas

1. **CDP, not Playwright**: Playwright features (`page.wait_for_selector()`)
   are NOT available. All interaction goes through CDP events.
2. **Telemetry ON by default**: Set `ANONYMIZED_TELEMETRY=false` in production.
3. **`step()` returns None**: Results are in `agent.state.last_model_output`
   and `agent.state.last_result`.
4. **`storage_state` only saves cookies**: `export_storage_state()` does NOT
   export localStorage, sessionStorage, or IndexedDB.
5. **Single Chrome profile lock**: Cannot run parallel sessions against the
   same `user_data_dir`.
6. **Message compaction at ~25 steps**: Older messages summarized by LLM,
   can lose context for long-running tasks.
7. **Loop detection**: Fingerprint-based, last 20 actions. Injects nudges
   on repetition.
8. **litellm removed**: Dropped after supply chain compromise (March 2026).
   Now optional dependency.

---

## 8. Design Options

### Option 1: Minimal — Configurable sub-agent model

Keep the current sub-agent architecture but make the browser LLM configurable
via YAML config.

**Changes:**
- Add `browser.model`, `browser.api_key`, `browser.base_url` to config schema.
- Modify `_get_browser_llm()` to read from `ToolContext.config["browser"]`,
  fall back to env vars, fall back to `gpt-4o-mini`.
- Optionally: allow `browser.model: "agent"` to use the main agent's LLM config
  via `ToolContext._llm_config`.

**Pros**: Minimal code change, no architectural shift. Makes `use_vision`
actually useful (pair with multimodal model).
**Cons**: Doesn't address fundamental limitations (no context sharing, no
persistence, no interactivity, no security).

**Effort**: Small (1-2 hours).

### Option 2: Hybrid — Direct tools + autonomous fallback

Expose browser primitives as tools the main agent can call directly, **and**
keep the autonomous `browse_website` as a convenience for simple tasks.

**New direct-control tools** (7-10 tools, based on AgentOccam research):

| Tool | Returns |
|------|---------|
| `browser_navigate(url)` | Accessibility snapshot (interactive elements) |
| `browser_snapshot()` | Current page accessibility snapshot |
| `browser_click(ref)` | Updated snapshot |
| `browser_type(ref, text)` | Updated snapshot |
| `browser_select(ref, value)` | Updated snapshot |
| `browser_scroll(direction)` | Updated snapshot |
| `browser_screenshot()` | Base64 image (if model is multimodal) |
| `browser_back()` | Updated snapshot |
| `browser_close()` | Confirmation |

**Existing autonomous tools** (kept as-is):
- `browse_website(url, task)` → fire-and-forget sub-agent
- `download_from_website(url, task)` → fire-and-forget sub-agent

**Implementation approach**: Use browser-use's `BrowserSession` +
`Tools.registry.execute_action()` directly. Maintain a `BrowserSession` on the
`ToolContext` so state persists across tool calls within a job/session.

**Snapshot format**: Interactive-only accessibility tree (~200-1K tokens),
matching the agent-browser pattern. Only the latest snapshot kept in context;
previous snapshots discarded.

**Mode selection**: Let the main agent's LLM decide based on tool descriptions:
- `browse_website`: "Delegate multi-page browsing task to autonomous browser
  agent. Use for simple extraction or multi-page navigation."
- `browser_navigate`: "Open URL and get page structure. Use when you need
  precise control over specific page interactions."

**Security**: Content boundary nonces on all snapshot returns. URL validation
before navigation. Domain allowlist from config.

**Pros**: Main agent gets full browser context. Can inspect, debug,
course-correct. Autonomous mode still available for simple tasks. Matches
industry direction. Uses existing browser-use dep.
**Cons**: More complex, more tool definitions in the schema. Two code paths
to maintain.

**Effort**: Medium (2-3 days).

### Option 3: Full direct control — CLI-based

Replace browser-use entirely. Expose browser control via shell commands
(agent-browser style) or custom MCP tools. No autonomous sub-agent.

**Approach A — Adopt agent-browser**: Install Vercel's agent-browser in
workspace containers. Agents use it via `run_command` shell tool. Compact
snapshots, persistent daemon, built-in security.

**Approach B — Build our own**: Playwright-based CLI or MCP tools with
custom DOM extraction. More work but full control over the implementation.

**Pros**: Cleanest architecture, most token-efficient (~7K per workflow),
built-in security (approach A), matches industry best practice.
**Cons**: Largest effort. New dependency (approach A) or significant new code
(approach B). Loses convenience of autonomous "just go do it" for simple tasks.

**Effort**: Large (3-5 days for A, 5-10 days for B).

---

## 9. Target Architecture — Adaptive Hybrid Browser Control

> **Design principle**: Capability over cost. The agent must be able to do
> everything a human can do in a browser — fill forms, verify design, check
> usability, handle multi-step workflows. The browser tool must never be the
> bottleneck.

### Three modes, one persistent browser

The system offers three browser interaction modes. All share a single
persistent Chromium instance per job/session (started lazily on first use,
killed on completion). The agent picks the appropriate mode — or the system
auto-selects based on model capabilities.

#### Mode 1: Direct control + vision (default for multimodal models)

The main agent gets 7-10 direct browser tools. Every navigation/interaction
returns **both** an accessibility tree (for precise element targeting) **and**
a screenshot (for visual/design assessment).

**Tools** (based on AgentOccam action-space research — sweet spot is 7-10):

| Tool | Returns | Notes |
|------|---------|-------|
| `browser_navigate(url)` | DOM snapshot + screenshot | Opens URL, validates against security rules |
| `browser_snapshot()` | DOM snapshot + screenshot | Current page state without navigation |
| `browser_click(ref)` | DOM snapshot + screenshot | Click element by ref number from snapshot |
| `browser_type(ref, text)` | DOM snapshot + screenshot | Type into input field |
| `browser_select(ref, value)` | DOM snapshot + screenshot | Select dropdown option |
| `browser_scroll(direction, amount?)` | DOM snapshot + screenshot | Scroll page or element |
| `browser_screenshot()` | Screenshot only | Lightweight visual check |
| `browser_back()` | DOM snapshot + screenshot | Navigate back |
| `browser_close()` | Confirmation | Close browser / end session |

**When to use**: Default for any multimodal model (Claude Opus, GPT-5,
Gemini, GPT-4o, etc.). Best for: form filling, design verification,
usability checking, precise multi-step interactions, debugging UI issues.

**DOM snapshot format** (full accessibility tree, not interactive-only —
capability over token savings):

```
[42]<input type="text" placeholder="Search..." value="" />
[43]<button>Search</button>
[44]<a href="/about" title="About Us">About</a>
|scroll element|<div role="main" /> (scrolled 25% of 3200px)
*[47]<input type="email" placeholder="Newsletter" />  <!-- * = newly appeared -->
```

Numbers are `backend_node_id` refs from browser-use's DOM extraction. The
agent uses these refs in `browser_click(ref=42)`, `browser_type(ref=47, ...)`.

#### Mode 2: Direct control + DOM only (for non-multimodal models)

Same tools as Mode 1, but screenshots are omitted from return values. The
agent works purely from the accessibility tree text.

**When to use**: Auto-selected when the main model's `multimodal` setting
is `false` in the settings matrix (e.g., DeepSeek, gpt-oss, Qwen text-only).
Still gives the agent full interactive control — just no visual perception.

#### Mode 3: Autonomous sub-agent delegation

The existing `browse_website(url, task)` and `download_from_website(url, task)`
tools. Fire-and-forget: sub-agent runs autonomously, returns text summary.

**Key change**: The sub-agent model becomes configurable, not hardcoded to
`gpt-4o-mini`. Config hierarchy:

1. `browser.model` in YAML config (explicit override)
2. `BROWSER_LLM_MODEL` env var (backwards-compatible)
3. Fall back to a capable multimodal default (e.g., `gpt-4o`)

**When to use**:
- Bulk research: "go read these 5 URLs and summarize findings"
- When the main model is non-multimodal but the task needs vision
  (sub-agent uses a multimodal model regardless)
- Simple extraction where direct control would be overkill
- As the agent's choice — tool descriptions guide the LLM:
  - `browse_website`: *"Delegate a browsing task to an autonomous browser
    agent. Best for simple extraction or multi-page research across many
    URLs. The browser agent runs independently and returns a text summary."*
  - `browser_navigate`: *"Open a URL and see the page. Use when you need
    to inspect, interact with, or visually verify a specific page."*

### Adaptive mode selection

The system auto-selects the default mode based on the main agent's model
capabilities (from `settings_matrix.yaml`):

```
if config.multimodal:
    direct_tools return DOM + screenshot    (Mode 1)
else:
    direct_tools return DOM only            (Mode 2)

autonomous tools always available           (Mode 3)
```

The agent can always explicitly choose any mode. A multimodal agent that
wants a quick lookup can still call `browse_website`. A text-only agent
that needs visual verification can delegate to a multimodal sub-agent.

### Persistent browser lifecycle

```
Job/session starts
    │
    ├── First browser tool call (any mode)
    │       └── Start Chromium (lazy), store CDP URL in ToolContext
    │
    ├── Subsequent browser tool calls
    │       └── Health-check CDP → reconnect or restart
    │           (user_data_dir preserves state across restarts)
    │
    ├── Phase boundary
    │       └── Export storageState as crash-recovery backup
    │
    └── Job/session ends
            └── Kill Chromium, cleanup
```

- **`user_data_dir`** moved into workspace: `/home/agent-host/.browser-profile/`
  Scoped to job, survives Chromium crashes, destroyed with workspace.
- **Tab management**: Close stale tabs after each tool call. Direct-control
  tools work on the active tab; snapshot includes tab list.
- **Memory guard**: Monitor Chromium RSS. Restart if exceeding threshold
  (512MB default). Profile dir preserves all state.

### CAPTCHA and blocker handling

| Blocker | Strategy |
|---------|----------|
| CAPTCHA | Freeze job with screenshot, ask human to solve, resume |
| Cookie banners | Auto-dismiss via common selectors (pre-navigation hook) |
| Login/OAuth | Persist session via `user_data_dir`; freeze for initial login if needed |
| 2FA/MFA | Freeze for human intervention |
| Anti-bot | Not solvable in general; residential proxy if configured |

The freeze mechanism already exists (`freeze_data` with `blocking_message`
type). Browser freezes include a screenshot so the human sees exactly what
the agent sees.

### Security (built-in, not phased)

All security measures ship with the initial implementation:

1. **Content boundary nonces**: All DOM/text content returned to the agent
   is wrapped with CSPRNG-nonce markers. Unpredictable — malicious pages
   cannot forge boundaries.

2. **URL validation** (pre-navigation):
   - Block private IPs (RFC 1918), loopback, link-local
   - Block cloud metadata (`169.254.169.254`, `metadata.google.internal`)
   - Block dangerous schemes (`file://`, `javascript://`, `data:`)
   - Block K8s internal DNS (`*.cluster.local`, `*.svc`)

3. **Domain allowlist/blocklist** in config:
   ```yaml
   browser:
     security:
       allowed_domains: []           # empty = allow all public domains
       blocked_domains: ["localhost", "127.0.0.1", "169.254.169.254"]
       blocked_schemes: ["file", "javascript", "data"]
   ```

4. **Secrets isolation**: Strip API keys from Chromium's environment.

5. **Per-expert browser policy**: Configurable per expert config —
   scholar gets full browser, curator gets none, etc.

### Prompt/instruction changes

The agent must be **encouraged** to use the browser, not reluctant:

- System prompt addition: *"After making UI changes, verify the result by
  opening the page in the browser. Check layout, usability, and visual
  appearance. Use `browser_navigate` to open the page and inspect it."*
- Development expert instructions: *"Always visually verify your UI work.
  Open the page, check responsiveness, form behavior, and design quality."*
- Research expert instructions: *"When web search results are insufficient,
  use the browser to access the full page content."*

### Config schema changes

```yaml
browser:
  headless: true
  timeout: 60000
  use_vision: auto                  # "auto" | true | false
                                    # auto = true if model is multimodal
  remote: auto
  model: null                       # Sub-agent model override (null = gpt-4o)
  api_key: null                     # Sub-agent API key (null = OPENAI_API_KEY)
  base_url: null                    # Sub-agent base URL
  chromium_max_memory_mb: 512       # Restart threshold
  snapshot:
    include_screenshot: auto        # "auto" | true | false (auto = if multimodal)
    max_dom_chars: 40000            # browser-use default, no artificial cap
  security:
    allowed_domains: []
    blocked_domains: ["localhost", "127.0.0.1", "169.254.169.254"]
    blocked_schemes: ["file", "javascript", "data"]
  persistence:
    user_data_dir: .browser-profile # Relative to workspace root
    export_state_on_phase: true     # storageState backup at phase boundaries
```

### Implementation: tool registration

New tool category `browser_direct` in `TOOL_REGISTRY`:

```python
# In src/tools/registry.py
"browser_direct": {
    "browser_navigate": {"phase": "tactical", "category": "browser_direct"},
    "browser_snapshot":  {"phase": "tactical", "category": "browser_direct"},
    "browser_click":     {"phase": "tactical", "category": "browser_direct"},
    "browser_type":      {"phase": "tactical", "category": "browser_direct"},
    "browser_select":    {"phase": "tactical", "category": "browser_direct"},
    "browser_scroll":    {"phase": "tactical", "category": "browser_direct"},
    "browser_screenshot":{"phase": "tactical", "category": "browser_direct"},
    "browser_back":      {"phase": "tactical", "category": "browser_direct"},
    "browser_close":     {"phase": "tactical", "category": "browser_direct"},
}
```

Enabled by default in `defaults.yaml` and `persistent_defaults.yaml`.
Cockpit agent-settings gets a "Browser (direct)" toggle alongside the
existing "Research" toggle.

### Implementation: browser session on ToolContext

```python
# In src/tools/context.py — new fields on ToolContext
_browser_session: Optional[Any] = None      # browser_use.BrowserSession
_browser_cdp_url: Optional[str] = None      # CDP WebSocket URL
_browser_started: bool = False

async def get_browser_session(self) -> BrowserSession:
    """Lazy-start Chromium, return persistent BrowserSession."""
    if self._browser_session and await self._health_check():
        return self._browser_session
    # Start or restart Chromium, connect via CDP
    ...

async def close_browser(self):
    """Kill Chromium, cleanup. Called on job/session end."""
    ...
```

### Implementation: using browser-use internals

Direct tools use browser-use's `BrowserSession` + `Tools.registry`:

```python
from browser_use import BrowserSession
from browser_use.tools.service import Tools

async def browser_navigate(url: str, ctx: ToolContext) -> dict:
    validate_url(url)  # Security: block private IPs, bad schemes
    session = await ctx.get_browser_session()
    tools = Tools()
    await tools.registry.execute_action("navigate", {"url": url}, session)
    state = await session.get_browser_state_summary(
        include_screenshot=ctx.should_include_screenshots()
    )
    return wrap_with_nonce({
        "dom": state.dom_state.llm_representation(),
        "screenshot": state.screenshot if ctx.should_include_screenshots() else None,
        "url": state.url,
        "title": state.title,
    })
```

### What stays the same

- Existing `browse_website` and `download_from_website` tools — kept as-is
  but with configurable model
- `web_search`, `extract_webpage`, `crawl_website`, `map_website` — the
  tiered research approach is still architecturally sound
- Remote workspace Chromium support via `_start_remote_chromium()`
- browser-use as the core dependency (no new browser libraries)

---

## References

### External

- [browser-use](https://github.com/browser-use/browser-use) — our current dependency
- [Vercel agent-browser](https://github.com/vercel-labs/agent-browser) — persistent CLI pattern
- [Playwright MCP](https://github.com/microsoft/playwright-mcp) — Microsoft's MCP tools
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) — Google's MCP tools
- [Stagehand](https://github.com/browserbase/stagehand) — deterministic-first AI browser automation
- [Claude Code Chrome](https://code.claude.com/docs/en/chrome) — MCP direct control
- [OpenHands](https://github.com/All-Hands-AI/OpenHands) — browser architecture
- [BrowserGym](https://github.com/ServiceNow/BrowserGym) — browser environment for agents
- [AgentOccam (ICLR 2025)](https://arxiv.org/abs/2410.13825) — action space optimization
- [Building Browser Agents (arxiv)](https://arxiv.org/html/2511.19477v1) — architecture/security survey
- [WebArena hybrid agents](https://yueqis.github.io/API-Based-Agent/) — API vs browser benchmarks
- [WALT](https://arxiv.org/html/2510.01524) — hybrid DOM+vision SOTA
- [Amazon: What Makes Browser Use Hard](https://labs.amazon.science/blog/what-makes-browser-use-hard-for-ai-agents)
- [Anthropic prompt injection defenses](https://www.anthropic.com/research/prompt-injection-defenses)
- [OpenAI Atlas hardening](https://openai.com/index/hardening-atlas-against-prompt-injection/)
- [browser-use CVE-2025-47241](https://github.com/browser-use/browser-use/security/advisories) — SSRF via domain validation bypass

### Internal files

- `src/tools/research/browser.py` — current implementation
- `src/tools/research/web.py` — Tavily-based web tools (search, extract, crawl)
- `src/tools/context.py` — ToolContext (`_llm_config`, `_current_phase`)
- `src/core/loader.py` — `create_llm()`, `LLMConfig`
- `config/defaults.yaml` → `browser:` section
- `config/persistent_defaults.yaml` → `browser:` section
- `docker/Dockerfile.workspace` — workspace container with Chromium
- `deployment/21d-workspace-network-policy.yaml` — CDP network policy
