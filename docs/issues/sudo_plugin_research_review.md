# Agent Job Review: Sudo Approval Plugin Research

**Job ID:** `2f7d3f66-abe4-4678-a9ba-65e171f1f489` (scholar)
**Validator:** `424437f5-997f-4c57-904a-d25d13ed9744` (critic, approved)
**Model:** `openrouter/minimax/minimax-m2.5` (MiniMax M2.5, mid-tier)
**Date:** 2026-03-18
**Duration:** ~109 minutes, 968 audit entries (175 tool calls, 172 LLM calls)
**Sources archived:** 111 web pages
**Knowledge notes created:** 10 (6 learning, 2 goal, 2 decision)
**Deliverable:** `output/sudo-approval-plugin-research.md` (42.8 KB, 4,471 words)
**Reference baseline:** `docs/researches/sudo_plugin/` (two prior research PDFs on the same topic)

---

## Verdict

The agent produced a structurally complete report covering all 7 required topics. It reached the correct high-level conclusions (approval plugin type 4, Go daemon, NATS request/reply, fail-closed semantics). However, when compared against the reference documents, the output has significant technical inaccuracies, shallow security analysis, and missing implementation details that would prevent a developer from building directly from it.

**Context:** This was run on MiniMax M2.5 (mid-tier model via OpenRouter), not a frontier model. Some issues below may be attributable to model capability limits rather than systemic agent problems. The reference documents were produced by a frontier model with the same tooling.

---

## Issues Found

### I1: Buggy C Plugin Skeleton — `user_info[]` accessed in `check()`

In the agent's plugin skeleton (Section 1.5), the `approval_check()` function iterates over `user_info[]` to extract the username. But `user_info[]` is only passed to `open()`, not `check()`. The `check()` signature receives `command_info[]`, `run_argv[]`, `run_envp[]`, and `errstr` — no user info.

The correct approach (demonstrated in both reference PDFs) is to cache `user_info` fields as static variables during `open()` and reference them in `check()`.

**Impact:** The plugin skeleton would segfault or read garbage memory if compiled as-is. This is the primary code example in the report.

---

### I2: No Proper JSON Handling in C Code

The agent's plugin uses raw `snprintf` to build JSON:
```c
snprintf(buffer, sizeof(buffer),
    "{\"command\":\"%s\",\"user\":\"%s\",\"cwd\":\"%s\"}",
    command, user, cwd);
```

This has no escaping for special characters in command paths or arguments. A command containing quotes, backslashes, or control characters would produce malformed JSON or enable injection.

The reference documents use cJSON (a vendored single-file C library) with `cJSON_CreateObject` / `cJSON_AddStringToObject` / `cJSON_PrintUnformatted` — proper, memory-safe JSON serialization with zero runtime dependencies.

**Impact:** Security vulnerability in the primary code example. Also a functional bug for any command containing JSON-special characters.

---

### I3: No Message Framing on Unix Socket

The agent's code does raw `send()` / `recv()` with no length prefix or delimiter. This works for small payloads in testing but breaks with:
- Partial reads (large commands with many arguments)
- Multiple messages on the same connection
- TCP-style stream semantics of `SOCK_STREAM`

The reference documents use a 4-byte big-endian length prefix (`htonl` / `ntohl`) before the JSON payload, which is the standard pattern for stream socket protocols.

---

### I4: Flat Timeout Architecture (Race Condition Risk)

The agent sets 300 seconds uniformly across all components:

| Component | Agent's Timeout |
|-----------|----------------|
| Plugin | 300s |
| Daemon | 300s |
| NATS | 300s |
| Orchestrator | 300s |

The reference documents use a cascading hierarchy where each outer layer has a slightly longer timeout:

| Component | Reference Timeout |
|-----------|------------------|
| Orchestrator | 295s |
| NATS client | 300s |
| Plugin (`poll()`) | 305s |

With flat timeouts, the orchestrator could expire a request at the same instant the plugin times out, creating a race where an "APPROVED" response arrives after the plugin has already returned DENY. The cascading approach ensures inner components always expire first.

---

### I5: Shallow Security Analysis

The agent's security section (Section 4) lists generic attack vectors and basic hardening (file permissions, systemd directives). It misses the adversarial-depth analysis present in the reference documents:

| Security Mechanism | Reference Docs | Agent Output |
|-------------------|---------------|--------------|
| `SO_PEERCRED` peer verification | Kernel-enforced PID/UID/GID check, cross-reference with `/proc/{pid}/exe` | Not mentioned |
| TOCTOU mitigation | UUID nonces + SHA-256 hash of command binary at request time | Not mentioned |
| NATS auth | NKey/JWT credentials restricting each VM daemon to its own subjects | Not mentioned |
| RBAC for approvers | `sudo_admin`, `sudo_approver`, `sudo_viewer` roles, self-approval prevention | Not mentioned |
| Input sanitization | ANSI escape stripping, Unicode bidi control chars (U+202A-U+202E), Angular template escaping | Not mentioned |
| Auto-approval safety | `fnmatch` (no backtracking, no ReDoS) instead of regex, sub-second timeout wrapper | Not mentioned |
| `LD_PRELOAD` / `LD_LIBRARY_PATH` | Explained that SUID execution strips these, preventing library injection | Not mentioned |

These are the mechanisms that actually make a security gate secure against an adversarial agent. Without them, the report describes a gate that an intelligent agent could potentially bypass.

---

### I6: Missing `sudo_dso_public` Export

The agent's plugin struct declaration:
```c
struct approval_plugin sudo_approval_plugin = { ... };
```

The reference documents use:
```c
sudo_dso_public struct approval_plugin sudo_gate_approval = { ... };
```

`sudo_dso_public` is the macro that controls symbol visibility. Without it, the symbol may not be exported from the shared library depending on compiler defaults (`-fvisibility=hidden`), causing sudo to fail to load the plugin silently.

---

### I7: Incorrect Description of sudo_pair

The agent describes sudo_pair (Square) as a C plugin. It is actually written in **Rust with C FFI**, and it implements an **I/O plugin** (type 2), not an approval plugin (type 4). This matters because:
- It predates the approval plugin API (sudo 1.9)
- It uses I/O plugin hooks as a workaround, which has different lifecycle semantics
- The Rust/C FFI boundary is architecturally relevant for anyone evaluating the reference

The reference documents correctly identify these distinctions and note that "no existing open-source project implements the exact approval plugin -> daemon -> orchestrator pattern."

---

### I8: Missing `sample_approval.c` from Sudo Source Tree

The sudo project ships an official `sample_approval.c` at `plugins/sample_approval/` in its source tree. This is the authoritative minimal example of the approval plugin API — maintained by the sudo developers themselves. The reference documents found it; the agent did not.

This is a significant research gap. The official sample would have corrected several of the agent's code issues (struct initialization, symbol export, `open()` vs `check()` parameter availability).

---

### I9: No Citations or Source Attribution

The agent's report contains zero citations. Every technical claim is unsourced. The reference documents cite 60+ sources (man7.org, sudo.ws, GitHub repos, Apple Open Source, Go Packages, blog posts, etc.).

For a research deliverable, citations serve two purposes:
1. **Verifiability** — the reader can confirm claims
2. **Recency** — the reader knows whether information is current

Without them, there's no way to distinguish researched facts from LLM confabulation. This is especially problematic for security-critical details like exact API signatures and return code semantics.

---

### I10: No Orchestrator/UI Implementation Detail

The reference documents provide:
- Full PostgreSQL schema with enum type, computed `expires_at` column, partial indexes
- SSE vs WebSocket comparison table with architectural reasoning
- Angular 19 Signals integration (`WritableSignal`, `ApplicationRef.tick()` for zone-less apps)
- Auto-approval rule engine design (fnmatch patterns, priority ordering, always-deny list)
- Approval UX patterns drawn from GitHub Actions / Terraform Cloud (risk badges, countdown timer)

The agent's Section 3 has a basic schema sketch and brief SSE mention. No Angular detail, no auto-approval engine, no UX patterns. The report was supposed to be a blueprint for implementation — this gap means the orchestrator/UI sections aren't actionable.

---

### I11: Missing Ubuntu 24.04 Details

The reference documents include:
- sudo-rs transition planned for Ubuntu 25.10 (Questing Quokka) — relevant for forward compatibility
- Version matrix table (sudo 1.9.15p5, systemd 255.4, kernel 6.8, GCC 14, cJSON 1.7.17, Python 3.12)
- `/usr/libexec/sudo/` vs `/usr/lib/sudo/` path distinction and how `plugin_dir` resolves it
- `apt-get source sudo` to obtain `sudo_plugin.h` (not packaged separately)

The agent covers the basics (sudo version, plugin directory) but misses the version matrix and the sudo-rs migration note.

---

### I12: Wrong Sudo Man Page Version Fetched

The agent's first `extract_webpage` call [81] fetched:
```
https://www.sudo.ws/docs/man/1.8.7/sudo_plugin.man/
```

Sudo **1.8.7** predates the approval plugin API, which was introduced in **sudo 1.9.0** (May 2020). The 1.8.x documentation does not include the `approval_plugin` struct, `SUDO_APPROVAL_PLUGIN` type constant, or the `check()` function specific to approval plugins. The agent then had to piece together the approval-specific API from other sources — the GitHub header file for sudo_pair [131] and blog posts — rather than reading the authoritative, version-correct man page.

The correct URL would have been `https://www.sudo.ws/docs/man/sudo_plugin.man/` (latest) or version 1.9+. This source version mismatch likely contributed to the `user_info[]` bug in `check()` and the missing `sudo_dso_public` export.

---

### I13: Entire Report Written in a Single LLM Call

The audit trail shows the 42,816-byte report was generated in one `write_file` call [812]. Before this, the agent read exactly 3 knowledge notes [778, 782, 786]:
- `sudo-approval-plugin-complete-research` (learning)
- `reference-implementations-sudo-approval` (learning)
- `sudo-plugin-security-hardening` (learning)

This means 111 archived web sources were compressed into ~3 knowledge note summaries, then the entire report was synthesized from those summaries in a single generation. The knowledge notes were the agent's own interpretations of sources, not the sources themselves.

**Consequence:** The report is a third-generation synthesis (sources → knowledge notes → report) with no source URLs preserved through the pipeline. This explains both the citation gap and the accumulation of small inaccuracies — each compression step loses detail and introduces paraphrasing errors. The reference documents, by contrast, cited sources inline as they wrote each section.

---

### I14: `browse_website` Tool Failure on sudo_pair

At audit entry [616], the agent attempted to browse the sudo_pair GitHub repository:
```
[616] Tool [FAIL] browse_website: Browser automation failed: 'ChatOpenAI' object has no attribute 'provider'
```

This is a **tool infrastructure bug** (the vision model config is incompatible with the browse tool), not an agent error. The fallback `extract_webpage` [620] only retrieved 348 words from the sudo_pair README — just the prompt template documentation, not the actual Rust source code or plugin architecture.

Despite this incomplete extraction, the agent marked the todo as complete [668] and wrote the knowledge note. This explains why the report incorrectly describes sudo_pair as a C plugin — the agent never saw the Rust source files, only a snippet of the README.

**Systemic issue:** The agent should have flagged that it couldn't deeply inspect the repository and noted this limitation in its knowledge note. Instead, it synthesized a description from incomplete data.

---

### I15: Noisy Source Corpus (Low Signal-to-Noise Ratio)

Of the 111 archived sources, many are irrelevant or low-quality:
- YouTube videos (Linux sudo bug, Common Sudo Mistakes, Linux Security — not parseable as research sources)
- `Fedora 40 nvidia drivers not working` [87] — completely unrelated
- Generic Stack Overflow answers about sudo permissions
- Medium blog posts about sudo misconfigurations (privilege escalation from a pentester perspective, not plugin development)
- Multiple redundant sources on the same topic (3 separate systemd hardening guides, 4 sudo permission troubleshooting pages)

The `web_search` tool archives all results automatically, but the agent didn't filter or curate. Approximately 30-40% of sources appear to have zero relevance to the task. While this doesn't directly hurt the output (the agent summarizes selectively), it inflates the source count and makes the job look more thoroughly researched than it actually was.

---

### I16: Redundant Tool Calls

The audit trail shows repetitive patterns:

**`list_files output/` called 10 times** [239, 349, 353, 367, 510, 560, 690, 732, 877, 921] — each strategic phase checked whether the deliverable existed yet, even though the plan clearly stated writing was Phase 5. This is ceremonial overhead from the phase alternation model.

**`read_file todo_guide.md` read 3 times** [13, 556, 735] and `todo_crafting_guide.md` attempted twice [403, 549] before the agent remembered the correct filename. The filename confusion (`todo_crafting_guide.md` vs `todo_guide.md`) wasted 3 tool calls.

**`read_file plan.md` read 6 times** [236, 382, 514, 702, 905, and during writing phase] — plan was re-read at every strategic boundary despite being recently written by the agent itself.

This is roughly 20-25 redundant tool calls (~15% of total), adding latency without information gain.

---

## Tool Failure Summary

| Entry | Tool | Error | Impact |
|-------|------|-------|--------|
| [403] | `read_file` | File not found: `todo_crafting_guide.md` | Minor — wrong filename, self-corrected |
| [406] | `next_phase_todos` | Too few todos: 4 < 5 | Minor — added one more todo, retried |
| [549] | `file_exists` | Not found: `todo_crafting_guide.md` | Minor — repeated wrong filename |
| [616] | `browse_website` | `'ChatOpenAI' object has no attribute 'provider'` | **Major** — couldn't inspect sudo_pair source, led to incorrect description (I7) |
| [620] | `extract_webpage` | Partial failure — only 348 words from sudo_pair | Moderate — incomplete data marked as complete |

The `browse_website` failure is a tool infrastructure bug that should be investigated independently. The `todo_crafting_guide.md` confusion suggests the filename should be standardized or the model should be given a file listing before attempting reads.

---

## Validator Shortcomings

The validator (`424437f5`) approved the report after:
1. Reading the deliverable
2. Searching for `TODO` and `Placeholder` markers
3. Cross-referencing the knowledge base

It did **not** catch any of the issues above. The validator treated verification as "does the file exist and look complete" rather than "is the content technically correct." This matches failure pattern **P1 (Superficial Verification)** from `results.md`.

A more effective validation would have:
- Compared the `check()` function signature against the struct definition in the same document
- Verified the C code compiles (or at least checked parameter availability across functions)
- Checked for source citations in a research deliverable
- Compared against existing project knowledge (the reference PDFs existed before this job)

---

## Process Observations

### Phase Overhead

11 archived phases and 968 audit entries for a research-then-write task is high. The strategic/tactical alternation pattern adds ceremony that may not pay for itself on focused research tasks.

**Useful work:**
- Phase 1 tactical: Sudo Plugin API research (5 todos, 5 web searches, 3 extractions)
- Phase 3 tactical: Daemon architecture research (4 web searches, 1 knowledge note)
- Phase 5 tactical: Security & implementation research (4 web searches, 1 extraction, 1 knowledge note)
- Phase 7 tactical: Reference implementation research (3 web searches, 1 failed browse, 1 knowledge note)
- Phase 9 tactical: Write report (1 write, 1 word count, 3 placeholder searches)

**Overhead:**
- 6 strategic phases (retrospectives, plan rewrites, knowledge base reviews, todo staging)
- Each strategic phase: ~8-12 tool calls (git_tags, git_log, list_files, read plan, write retrospective, kb_search, write plan, read todo_guide, next_phase_todos, 4x todo_complete)

Estimated overhead: ~60 tool calls across strategic phases, roughly 35% of total tool usage. For a single-deliverable research task, a lighter touch (fewer phases, combined research steps) would be more efficient.

### Knowledge Base Compression Problem

The agent built 10 knowledge notes during research phases and compiled them into the final report. The writing phase read exactly 3 notes before generating the entire report. This creates a **lossy compression pipeline**:

```
111 web sources → 10 knowledge notes → 3 notes read at write time → 1 report
```

Each step loses information:
1. **Web search → extraction**: Only 6 of 111 sources were actually extracted; the rest were search result snippets
2. **Extraction → knowledge note**: The agent summarized multi-thousand-word pages into paragraph-length notes
3. **Knowledge notes → report**: Only 3 of 10 notes were read before writing (the `go-vs-rust` decision note and other architectural notes may not have been re-read)

The reference documents avoided this by writing section-by-section with inline citation as they researched each topic, keeping source material adjacent to where it was used.

### Model Capability Factor

This job ran on `openrouter/minimax/minimax-m2.5`, a mid-tier model. The reference documents were likely produced by a frontier model (based on the citation density, code quality, and analytical depth). Some issues may be inherent to the model's capability ceiling:
- C code correctness (I1, I2, I3, I6) — requires precise understanding of C API contracts
- Security depth (I5) — requires adversarial reasoning about system boundaries
- Source curation (I15) — requires judgment about source relevance

Other issues are more clearly systemic/process problems:
- No citations (I9) — the agent has `cite_web` tools but never used them; instructions didn't require it
- Knowledge compression (I13) — architectural issue with the research → write pipeline
- Tool failure handling (I14) — the agent should flag incomplete data rather than marking complete
- Wrong man page version (I12) — source selection requires attention to URL version numbers

### What the Agent Did Well

Credit where due:
- **Correct architectural conclusions**: Approval plugin type 4, Go over Rust, NATS request/reply, fail-closed — all match the reference documents
- **Good research coverage**: Found sudo.ws, man7.org, Sigma-star blog, sudo_pair — the right sources were in the corpus
- **Methodical knowledge base usage**: The research → knowledge note → write pipeline worked as designed, even if lossy
- **Self-correction on tool failures**: Recovered from wrong filename, insufficient todos, and browse failure without getting stuck
- **Clean workspace management**: Proper git versioning, phase tags, structured archive

---

## Recommendations

### For Instructions
1. **Require citations:** Research task instructions should explicitly require inline source citations with URLs. The current instructions asked for "comprehensive technical document" but didn't mandate citations. Adding `"All technical claims must include a source URL"` would force the agent to use `cite_web` tools and maintain source traceability.
2. **Specify source version awareness:** Add `"When citing documentation, verify you're reading the version that matches the target environment"` — this would have prevented the 1.8.7 man page issue.

### For the Validator
3. **Cross-reference code correctness:** Validator should verify that code examples are internally consistent (e.g., function parameters in skeleton match the struct definition). Even a simple "do the function signatures in section 1.5 match the struct in section 1.2?" check would have caught I1.
4. **Provide reference material:** If reference documents exist on the same topic, give them to the validator as comparison material.

### For the Agent Framework
5. **Fix `browse_website` tool:** The `'ChatOpenAI' object has no attribute 'provider'` error is a tool infrastructure bug that prevented deep inspection of the primary reference implementation.
6. **Preserve source URLs in knowledge notes:** Knowledge notes should automatically include the source IDs/URLs from which they were derived, so the writing phase can emit citations without re-fetching.
7. **Add a `cite_web` instruction file:** An instruction file triggered before `write_file` on research tasks could remind the agent to include source citations.
8. **Reduce strategic overhead for research tasks:** Consider a lighter phase model for single-deliverable research (e.g., skip retrospectives when no code output exists yet, combine research phases).

### For Tool Reliability
9. **Standardize filename references:** The `todo_crafting_guide.md` vs `todo_guide.md` confusion wasted 3 tool calls. Either standardize the name or inject the correct filename into the agent's context.
10. **Flag incomplete extractions:** When `extract_webpage` returns significantly less content than expected (348 words from a major GitHub repo), the tool should warn that the extraction may be incomplete.
