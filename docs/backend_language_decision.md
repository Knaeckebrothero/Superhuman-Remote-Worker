# Backend Language Decision — Go (over Java / Rust / C)

Supplements `docs/go_rewrite.md`, which proposed Go for the eventual backend rewrite but never compared alternatives. This doc records the comparison and locks the language choice. **Timing is unchanged** — `go_rewrite.md`'s "not now, not next quarter" guidance still applies; this doc decides only *what language*, not *when*.

## Decision

Use **Go** for the eventual rewrite of agent + orchestrator + frontend (full-stack scope, matching `go_rewrite.md`).

## Scope assumed (from go_rewrite.md)

- Agent: replace LangGraph with a hand-rolled state machine.
- Orchestrator: replace the ~11.5k-line FastAPI monolith.
- Frontend: replace Angular SPA with server-rendered, agent-composable UI in the same binary.

Unchanged: Postgres schema, pgvector, SSH/SFTP workspace backends, YAML config system, HTTP contract between agent and orchestrator (preserved for incremental cutover).

## Why "0 Go experience on the team" is not the blocker it sounds like

The system writes its own code — the Developer agent dispatches Claude Code sessions to produce PRs; humans review and steer. This shifts the selection criterion from *"what can the team write fluently"* to *"what can the models produce well and the team review confidently."*

Go scores high on both axes: small regular language, well-represented in training data, less ceremony per PR than Java, easier to read cold than Spring Boot or Rust. Expect 2–3 months of slower review while learning idioms, then normal cadence.

## Languages considered

### Go — chosen

Pros:
- Memory ~30 MB/worker vs Python's ~300–500 MB → ~10x worker density per node.
- Cold start <100 ms → HPA scale-to-zero becomes viable.
- Goroutines + channels map natively to the per-turn-per-worker model in `go_rewrite.md`.
- Single static binary serves API + server-rendered HTML + WebSocket + voice channel — collapses the deploy story.
- ~50 MB images.
- Strong model code-generation quality.

Cons:
- No prior team experience (mitigated above).
- Less expressive type system than Rust/Java for sum types (interface + type switch instead of true enums); workable, just less elegant for the State/ToolCall/Phase modeling.

### Java with Quarkus + GraalVM native — rejected

The "leverage existing Java skills" option. Quarkus native can match Go's perf claims (~50 MB binary, ~30 ms cold start).

Why rejected:
- GraalVM native is operationally a different beast — reflection config, long build times, library-compat breakage. You pay this tax forever.
- Model code quality on Quarkus is weaker than Spring Boot or Go (less idiomatic Quarkus in training data).
- The "team familiarity" advantage shrinks when agents write the code.
- Quarkus's typed wins (sealed interfaces, records, pattern matching) are real but not worth the operational complexity of GraalVM native for this workload.

### Java with Spring Boot — rejected (the "Netflix trap")

Included not because it's a candidate, but because "Netflix runs Spring Boot" is a common counter-argument that needs to be addressed directly.

Why rejected:
- 3–5 s cold start, 300–500 MB resident, 200 MB+ images — defeats every architectural argument in `go_rewrite.md`. Moving from Python to Spring Boot lands roughly where Python was on the metrics that motivated the rewrite.
- "Netflix runs it" is survivorship bias. Netflix has dozens of JVM specialists and built half the modern JVM ecosystem (Hystrix, Eureka, Zuul, etc.). Their tradeoffs are not ours.
- Basic familiarity is enough to make bad architecture decisions (over-using `@Autowired` magic, fat singleton services), not enough to fix them. The risk profile is worse than picking an unfamiliar language reviewed via agent PRs.

### Rust — rejected (but the only serious alternative to Go)

Pros over Go:
- First-class sum types — `enum Phase { Plan, Execute, Verify }` with exhaustive matching maps to State/ToolCall/Phase modeling better than Go's interface+switch.
- Same single-binary, small-image, fast-cold-start story.
- No GC pauses (real but irrelevant for I/O-bound LLM work).
- Strong async story via tokio.

Why rejected:
- Borrow checker tax is real, especially around shared mutable state in worker pools.
- Slow build times.
- **Model code quality on Rust is meaningfully behind Go.** Agents fight the borrow checker, miss idioms, produce code that compiles but isn't natural. For an agent-written codebase reviewed by humans, that's a productivity hit on every PR.

Honest framing: Rust would win if optimizing for "the most theoretically correct system." Go wins when optimizing for "the best system the agents can produce and the team can review." For I/O-bound LLM orchestration (each turn is ~2 s waiting on the model API), Rust's strengths don't show up in the bottom-line numbers. Its weakness does.

### C — rejected as wrong-tool

Not a hussle calculation, a wrong-tool one. C is for kernels, embedded, drivers, ultra-hot inner loops. The workload is bound by LLM API latency; saving microseconds on string ops is meaningless when each turn waits ~2 s on the model. You'd also reinvent everything `net/http`, `encoding/json`, `database/sql` give Go for free.

## Comparison summary

| Axis | Python (now) | Go (chosen) | Quarkus native | Spring Boot | Rust | C |
|---|---|---|---|---|---|---|
| Cold start | 3–5 s | <100 ms | ~30 ms | 3–5 s | <100 ms | <100 ms |
| Memory/worker | 300–500 MB | ~30 MB | ~50 MB | 300–500 MB | ~30 MB | ~10 MB |
| Image size | ~1.2 GB | ~50 MB | ~80 MB | ~200 MB | ~50 MB | ~10 MB |
| Single-binary full stack | no | yes | yes (native) | no | yes | yes |
| Goroutine-equivalent concurrency | asyncio | goroutines | Mutiny reactive | WebFlux | tokio | pthreads |
| Type system fit (State/ToolCall/Phase) | weak | OK | strong | strong | strongest | weak |
| Model code-gen quality | very high | very high | medium | high | medium | medium |
| Operational complexity | medium | low | high (GraalVM) | medium | medium | high |
| Team familiarity | high | none | basic | basic | none | basic |

## Migration approach (per go_rewrite.md, not changed)

Incremental, not big-bang:
1. Keep Python orchestrator. Rewrite agent in Go first. HTTP contract is already defined and stable.
2. Once Go agents are stable in production, port orchestrator endpoints router-by-router.
3. Frontend rewrite happens alongside orchestrator port (server-rendered HTML lives inside the orchestrator binary).

Keep checkpoint format as JSON (not pickle, not protobuf) so Go and Python agents can coexist on the same orchestrator during cutover.

## When to actually pull the trigger

Unchanged from `go_rewrite.md`. Four triggers to revisit:
1. Memory cost of agent pods becomes a real bill line item.
2. Cold-start latency on autoscale starts hurting UX.
3. Agent codebase stops changing weekly (design has converged).
4. Multi-week window where the team can stop shipping features.

Until then: this doc + `go_rewrite.md` are reference material, not active work.

## Out of scope here (open questions to resolve when the rewrite is greenlit)

- Frontend stack within Go: templ vs `html/template` vs Phoenix-LiveView-style alternative.
- Voice pipeline implementation details (covered in `go_rewrite.md`).
- Testing approach (pgx + dockertest vs shared test instance with schema-per-test).
- LangGraph interrupt-model port (covered in `go_rewrite.md` open questions).
- Auxiliary LLM task scheduling pattern in Go (goroutines on bounded channels — sketch is in `go_rewrite.md`).
