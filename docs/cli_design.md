# Chat UI Design: Reasoning & Tool Call Visibility

## Problem Statement

When our agent executes tool calls, it often returns empty text responses — just tool invocations with no accompanying message. CLI coding agents (Claude Code, Codex CLI, etc.) handle this by showing reasoning text between tool calls, e.g. "Let me read that file" or "The dispatch returned a 502, let me test the endpoint directly." Our UI currently collapses all tool calls into a summary line (`Used 12 tools: list_files x2, file_exists x3, ...`) with an expandable detail view. This works, but for less technical users it can feel like a black box — the agent is "working" but there's no insight into *why* it's doing what it's doing.

---

## Current State

### What CLI agents do (e.g. Claude Code)

- Show natural-language reasoning between tool calls: "Let me look for the heartbeat endpoint since it triggers dispatch"
- Each tool call is visible inline with its result
- The experience reads like a narrated thought process — users can follow along even if they don't understand every command
- Feels transparent and trustworthy

### What our Cockpit does today

- Tool calls are grouped into a collapsible summary: `Used 17 tools: list_files x2, file_exists x3, ...`
- Expanding shows individual tool names, arguments, and completion status
- Tool results (stdout, task lists, etc.) are shown in code blocks when expanded
- Final assistant message appears as a rich markdown bubble after all tools complete
- No reasoning/thinking text between tool calls — empty responses are simply not rendered
- Streaming: spinner + "Running [tool_name]..." text during execution
- Thinking dots (animated bouncing) when no text or tools are active yet

### What works well in our current design

- The collapsible tool summary is clean and doesn't overwhelm non-technical users
- The expanded view gives full transparency when needed
- Rich markdown in the final message looks polished
- The task/todo list at the top provides high-level progress tracking
- Permission prompts (Approve/Auto-accept/Deny) are clear and well-integrated
- Tool-only messages (no text) render as compact one-line indicators with status dots

### Current data structures

```typescript
// ChatMessage — no reasoning/thinking field exists
interface ChatMessage {
    role: 'user' | 'assistant' | 'system';
    content: string;
    toolCalls?: ToolCallInfo[];
    timestamp: Date;
    historical?: boolean;
}

// ToolCallInfo — individual tool execution
interface ToolCallInfo {
    id: string;
    tool: string;
    args: Record<string, unknown>;
    result?: string;
    status: 'pending' | 'running' | 'completed' | 'denied';
}
```

No `thinking` WebSocket event type currently exists. The `ChatReasoning` type is defined in `chat.model.ts` but only used for job chat history, not persistent sessions.

---

## Issues

1. **Empty responses look weird** — When the agent does a tool call with no accompanying text, users see nothing happening (or just a spinner). CLI agents would show reasoning like "let me read that file" and then the tool call. We should surface reasoning to make the experience feel more alive.

2. **Black box during execution** — While tools are running, the only feedback is the tool summary updating. Less technical users don't know what the agent is thinking or why it chose to run specific tools. This is especially disorienting during longer multi-tool sequences.

3. **Lost narrative** — CLI agents naturally create a story: "I found X, so I'll try Y." Our UI jumps from user message straight to tool execution and then to a final answer, losing the intermediate reasoning that builds understanding and trust.

4. **Tool summary is technical** — `list_files x2, file_exists x3, task_add x2` means nothing to a non-developer. The summary format serves power users but alienates the broader audience this UI is designed for.

---

## Industry Research

### CLI Agent Patterns

**Claude Code** — Built on Ink (React for terminals). Brief natural-language narration streams between tool calls by default. Extended thinking is hidden behind `Ctrl+O` (verbose mode). Repeated tool calls auto-group into collapsible components. Three output styles (Default/terse, Explanatory/insights, Learning/collaborative). Philosophy: *terse by default, rich on demand*.

**OpenAI Codex CLI** — Three-zone TUI layout (composer, output pane, status bar). Shows "Thinking..." during API calls. Three approval modes (Suggest/Auto-edit/Full-auto). Background terminals show 3 most recent output lines — a "window" into background work. Notable: non-interactive `codex exec` mode outputs structured JSONL for CI/CD.

**Aider** — Maximum transparency, everything scrolls by with no collapsing/summarization. A detailed user review called it "the least readable diff format I have ever encountered" and noted users learn to *ignore* the output because there's too much of it. Cautionary example of transparency without progressive disclosure.

**GitHub Copilot CLI** — Plan mode (`Shift+Tab`) asks clarifying questions before coding. Reasoning visibility toggled with `Ctrl+T`. Asymmetric display: *success is quiet, failure is loud* (brief summaries on success, full output on failure). Rejection includes redirect: "No, and tell Copilot what to do differently."

**Key CLI pattern**: Tiered disclosure — Level 0 (brief narration + collapsed results), Level 1 (full tool I/O), Level 2 (thinking blocks, token counts, traces).

### Web-Based Agent UIs

**ChatGPT** — Reasoning models show "Thought for X seconds" with auto-collapsing thinking block. Tool actions appear as inline status labels ("Searching the web...", "Running code...") — human-readable, not raw tool names. Agent mode has a running sidebar log showing each step + sources + screenshots. Typing animation calibrated to 1.4s to match human reading speed.

**Claude.ai** — Animated icon + "Thinking..." label + running time counter during processing. Collapsed "Thinking" section after completion — collapsed by default, separately scrollable when expanded, bullet-pointed structure. Artifacts open a live pane beside the chat for code/docs/diagrams.

**Perplexity** — Sources appear *at the top* of each response before the answer. Pro Search shows step-by-step plan execution with expandable steps. VP of Design: "You don't want to overload the user until they are actually curious. Then, you feed their curiosity." Users tolerate longer waits when they see dynamic progress feedback. Also: "Not everything should be a chat. Actually, most products should not be a chat."

**Google Gemini** — Deep Research generates a structured research plan users can modify before execution begins. Live updates show which sources are being analyzed. Deep Think spawns multiple parallel agents testing multiple ideas.

**Cursor 3.0** — Mission Control dashboard showing what each agent is doing, what files modified, what decisions made. Up to 8 parallel subagents. Background agents run in cloud, notify on completion. Design Mode lets users annotate UI elements directly.

**Devin** — Four-panel IDE: code editor, terminal, sandboxed browser, planning tools. "Thought Process" tab shows real-time reasoning. Plan view with clickable code snippets. Paradigm: "You review the output, not the process" — but the process is visible if you want it.

### Progressive Disclosure Hierarchy (Industry Consensus)

| Layer | What's Shown | Example |
|-------|-------------|---------|
| **0: Ambient** | Persistent badge/pill showing agent is working | Cursor's background agent indicator |
| **1: Summary** | One-line status + timer | ChatGPT "Thought for 12s", Claude "Thinking... 8s" |
| **2: Steps** | Expandable step list showing plan/progress | Perplexity Pro Search steps, Gemini research plan |
| **3: Details** | Full reasoning chain, tool I/O, sources | Claude thinking block expanded, ChatGPT agent sidebar |
| **4: Technical** | Raw tool calls, API responses, diffs | Cursor diff view, Devin terminal output |

### Visual Differentiation: Reasoning vs. Response

| Technique | Used By |
|-----------|---------|
| Muted foreground color (50-60% opacity) | Claude, assistant-ui |
| Italic text | assistant-ui ChainOfThought |
| Distinct background container | ChatGPT, Claude |
| Color-coded left border | Claude, our AgentActivity |
| Icon per step type | Gemini, our AgentSteps |
| Badge/pill labels | Our AgentActivity |
| Separate scroll region | Claude |
| Reduced font size | Our AgentSteps (11px) |

Recommended hierarchy: Response text (full opacity) > Tool activity (medium opacity, monospace) > Thinking/reasoning (reduced opacity, italic, collapsible) > Metadata (minimum opacity, smallest size).

### Collapsible Reasoning Strategies

| Strategy | Used By | Behavior |
|----------|---------|----------|
| Auto-collapse on completion | ChatGPT, Grok | Expanded during thinking, auto-collapses to one line when done |
| Start collapsed, never auto-expand | Claude | Reasoning always behind a click; answer-first |
| Progressive collapse | Perplexity, our AgentSteps | Steps appear expanded as they happen; outer container collapses after |

NNG warning: Users rarely engage with collapsed content. For critical information (errors, confidence scores), use always-visible elements.

### Tool Narration Modes

| Pattern | Example | Best For |
|---------|---------|----------|
| **Pre-action narration** | "Let me check that file..." | Conversational agents, supervised mode |
| **Post-action summary** | "Found 3 matches in config.yaml" | Developer tools, autonomous agents |
| **Activity log/timeline** | `Reading config.yaml [spinner] -> Done` | Complex multi-step operations, auditing |
| **Hybrid (emerging best practice)** | Compact indicator during execution -> collapsible summary after | General use (what our cockpit already does) |

Configurable narration modes proposed by OpenClaw: **Silent** (no commentary), **Verbose** (conversational narration), **Auto** (model determines based on complexity). Maps to our existing permission modes.

### Trust & Transparency

- 63% of users more likely to rely on AI systems that display confidence levels or explain reasoning (NNG 2024)
- Consistent behavior increases user trust by 47%
- 72% of users say AI language (tone, clarity, transparency) directly impacts trust
- Source attribution over reasoning traces — display citations as clickable chips, not chain-of-thought dumps
- "Because you said X, I did Y" logic grounding > step-by-step reasoning walkthroughs (NNG says those are post-hoc rationalizations)
- Streaming reduces perceived wait time by 55-70%
- Skeleton screens reduce perceived load time by 40% vs spinners

### Accessibility & Non-Technical Users

- **Plain language narration**: "Saving your changes" not "Executing POST /api/data". "Searching your code" not "Running grep -r 'pattern' ./src"
- **Confidence as binary**: "High confidence" / "Not sure" outperforms percentage displays for user decision speed
- **Layered explanations by expertise**: Novice (outcomes + confidence), Regular (decision factors + sources), Expert (full logs) — drives 84% higher engagement
- **Streaming accessibility**: `aria-live="polite"` + `aria-atomic="false"` on streaming containers
- `role="status"` on loading indicators, keyboard focus management on completion

### Mobile Considerations

- Auto-collapse reasoning (mandatory on mobile)
- Single-line status indicators instead of multi-step timelines
- Bottom-sheet/modal for expanded reasoning details (don't push content)
- No nested `<details>` (touch targets too small)
- Maximum one level of tool call hierarchy
- Our `simple/` mobile layout should default to single-line collapsed indicators with tap-to-expand

---

## Design Direction

We want a middle ground between the pure CLI narrated style and our current grouped-message approach. The goal: give users insight into agent reasoning without turning the chat into a terminal.

### Approach A: Inline Reasoning Snippets

Show short reasoning text above/before tool call groups, extracted from the agent's thinking. E.g., "Checking your project structure..." before a `list_files` call, or "Writing the calculator app..." before a `write_file` call. Styled subtly (smaller font, muted color, italic) so they don't compete with the final message.

**Pros**: Feels conversational and human, builds narrative naturally.
**Cons**: Requires backend changes to surface reasoning text. If the agent changes course, pre-action narration becomes misleading.
**Industry precedent**: Claude Code (default mode), ChatGPT agent sidebar.

### Approach B: Progressive Status Updates

Instead of waiting for all tools to finish, show a live feed of what the agent is doing in plain language: "Reading workspace.md", "Creating task: Build calculator app", "Writing output/calculator.py". More descriptive than tool names, less verbose than full CLI output.

**Pros**: Real-time feedback without narration overhead. Can be derived from tool call events without backend changes.
**Cons**: Still somewhat technical. Doesn't explain *why* the agent is doing something.
**Industry precedent**: ChatGPT tool indicators, Perplexity step tabs.

### Approach C: Phased Summaries

Group tool calls by intent rather than by type. Instead of `list_files x2, write_file x3`, show phases like "Explored project structure", "Built calculator app", "Ran tests and verified". Each phase is expandable for details.

**Pros**: Meaningful to non-technical users. Clean, compact display.
**Cons**: Requires either backend intelligence to group by intent, or heuristic grouping on the frontend. Hard to do in real-time during streaming.
**Industry precedent**: Perplexity Pro Search phases, Gemini Deep Research plans.

### Approach D: Thinking Bubbles

Show the agent's reasoning as distinct, visually differentiated elements (lighter styling, italic, or a "thinking" indicator). These appear in real-time as the agent works, creating the narrative that CLI agents provide naturally.

**Pros**: Maximum transparency. Creates engagement during wait times.
**Cons**: Requires `thinking` WebSocket event from backend. Can create noise if reasoning is verbose.
**Industry precedent**: Claude.ai thinking blocks, ChatGPT o1/o3 reasoning display, DeepSeek R1.

### Approach E: Hybrid — Reasoning + Collapsed Tools (Recommended Starting Point)

Keep our current collapsible tool summary but enhance it with:
1. **Human-readable status labels** during execution ("Searching your code..." not "Running grep")
2. **Brief reasoning snippet** prepended to each tool group when available
3. **Asymmetric detail** — quiet on success, verbose on failure (Copilot CLI pattern)
4. **Auto-collapse on completion** — expanded during execution, collapses to one-line summary when done

**Pros**: Minimal disruption to current UX. Incremental improvement. Doesn't require backend thinking events.
**Cons**: Without backend reasoning data, the narration is limited to tool-name-to-description mapping.
**Industry precedent**: This is the emerging consensus pattern — exactly what Claude Code (default mode), Codex CLI, and ChatGPT agent do.

---

## Implementation Considerations

### What we can do without backend changes

1. **Human-readable tool labels** — Map tool names to plain-language descriptions on the frontend:
   ```
   list_files -> "Exploring files"
   file_exists -> "Checking file"
   read_file -> "Reading [filename]"
   write_file -> "Writing [filename]"
   run_command -> "Running command"
   web_search -> "Searching the web"
   task_add -> "Planning task"
   task_complete -> "Completing task"
   ```

2. **Asymmetric detail** — Show brief one-liner for successful tools, expand automatically on failure/denial.

3. **Auto-collapse after completion** — Tool summary is expanded during streaming, auto-collapses when turn completes.

4. **Better streaming indicator** — Replace "Running [tool_name]..." with the human-readable label + the first argument as context.

### What requires backend changes

1. **Reasoning/thinking events** — New `thinking` WebSocket event type to stream model reasoning.
2. **Intent grouping** — Backend groups tool calls by phase/intent and sends group labels.
3. **Pre-action narration** — Model generates narration text before tool execution (costs extra tokens).
4. **Configurable narration mode** — Per-session setting (silent/verbose/auto) sent to the agent.

### Suggested phasing

**Phase 1 (frontend only)**: Human-readable tool labels, auto-collapse, asymmetric detail, better streaming indicators. No backend changes needed.

**Phase 2 (minor backend)**: Add `thinking` WebSocket event. Display reasoning as collapsed-by-default section above tool calls.

**Phase 3 (full integration)**: Configurable narration modes, intent-grouped tool summaries, pre-action narration tied to permission modes.

---

## Open Questions

- How much extra latency/cost is acceptable for pre-action narration (extra LLM tokens per tool call)?
- Should narration mode be per-session, per-user, or global?
- Do we want the mobile `simple/` layout to have a different default verbosity than the desktop layout?
- Should we differentiate between strategic reasoning ("I'll approach this by...") and tactical narration ("Reading file X...")?
- How do we handle the "misleading narration" problem (agent says "let me check X" then doesn't)?
- Should tool narration be configurable independently of permission mode, or tied to it?

---

## References

### Products Analyzed
- Claude Code CLI (Anthropic) — tiered disclosure, terse by default
- OpenAI Codex CLI — three-zone TUI, approval modes, background terminal previews
- Aider — maximum transparency cautionary example
- GitHub Copilot CLI — plan-before-build, asymmetric display
- ChatGPT / ChatGPT Agent — collapsible reasoning, human-readable status labels, agent sidebar
- Claude.ai — collapsed thinking, artifacts side panel
- Perplexity AI — sources-first, step-by-step plans, progressive curiosity
- Google Gemini — editable research plans, parallel agents
- Cursor 3.0 — Mission Control, background agents, diff previews
- Devin (Cognition) — four-panel IDE, thought process tab

### Design Guidelines & Research
- Nielsen Norman Group — Explainable AI, progressive disclosure warnings
- Google PAIR Guidebook — AI design patterns
- Smashing Magazine — Designing Agentic AI: Practical UX Patterns (Feb 2026)
- AI UX Design Guide — 36 patterns for agent interfaces
- assistant-ui / Vercel AI SDK — ChainOfThought component
- OpenTelemetry — AI Agent Observability
- Shape of AI — Stream of Thought pattern

### Key Stats
- 85% of users prefer streaming for conversational interfaces
- 63% more likely to rely on AI that explains reasoning (NNG)
- Streaming reduces perceived wait time by 55-70%
- Consistent behavior increases trust by 47%
- Three-tier expertise layering drives 84% higher engagement
