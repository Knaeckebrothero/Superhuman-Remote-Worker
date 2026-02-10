# Continuous Improvement Loop

Self-improving pipeline where the agent system debugs, researches, and improves itself after every job run.

## Concept

After any human-created job completes, we chain three follow-up jobs through the orchestrator API:

```
Human Job (completes)
    |
    v
Evaluator Agent ── analyzes the job's audit trail, todos, workspace, errors
    |                produces structured evaluation report with issues
    v
Researcher Agent ── takes the issues list, researches solutions
    |                 searches web, papers, docs for fixes and patterns
    v
Coder Agent ── takes evaluation + research, implements improvements
    |            works on a branch, makes PRs
    v
(loop: re-run the original job with improved codebase)
```

Each stage is a normal job submitted via `POST /api/jobs`. The pipeline script reads each completed job's workspace output and passes it as input documents/instructions to the next stage.

## Architecture

### What already exists

- **Orchestrator API** (`POST /api/jobs`) for creating and monitoring jobs
- **Expert configs** for researcher (`config/experts/researcher/`) and coder (`config/experts/coder/`)
- **MCP server / REST API** exposing full job introspection: audit trails, chat history, todos, workspace files, graph changes
- **MongoDB** logging every LLM request/response for analysis
- **Workspace artifacts** per job: `workspace.md`, `plan.md`, `todos.yaml`, `archive/`, `output/`

### What needs to be built

1. **Evaluator expert** (`config/experts/evaluator/`) - new agent role with evaluation tools
2. **Evaluation tool category** (`src/tools/evaluation/`) - tools that query the orchestrator API to inspect other jobs
3. **Pipeline script** - external script that chains jobs via the orchestrator API

### Evaluator Agent

New expert type that can introspect completed jobs. Needs a new tool category (`evaluation`) with tools that call the orchestrator REST API:

| Tool | Orchestrator Endpoint | Purpose |
|------|----------------------|---------|
| `get_job_details` | `GET /api/jobs/{id}` | Job metadata, status, config, token usage |
| `get_job_audit` | `GET /api/jobs/{id}/audit` | Paginated audit trail (LLM messages, tool calls, errors) |
| `get_job_chat` | `GET /api/jobs/{id}/chat` | Conversation turns showing reasoning flow |
| `get_job_todos` | `GET /api/jobs/{id}/todos` | Current + archived todos across phases |
| `get_job_workspace_file` | `GET /api/jobs/{id}/workspace/{file}` | Read workspace.md, plan.md from another job |
| `search_job_audit` | `GET /api/jobs/{id}/audit` + filtering | Search for specific patterns, errors, tool names |

The tools accept a `target_job_id` parameter (not the evaluator's own job). Implementation reuses the sync `CockpitClient` from `orchestrator/mcp/client.py` which already wraps every needed endpoint.

#### Direct data access

In addition to the orchestrator REST API proxy tools, the evaluator gets direct access to underlying data sources for deeper analysis:

| Access | Tool / Mechanism | Purpose |
|--------|-----------------|---------|
| **MongoDB** | `mongo_query`, `mongo_aggregate` (via datasource connector) | Query raw LLM request/response logs, aggregate token usage, find patterns across multiple jobs |
| **Git** | `git_log`, `git_diff`, `git_show` (workspace tools) | Inspect the target job's workspace git history — what files changed per phase, diff sizes, reverted edits |
| **MCP** | Direct MCP client (same as Claude Code integration) | Access the full MCP tool surface for job introspection without REST overhead |

The evaluator config attaches the system MongoDB as a datasource and mounts the target job's workspace directory (read-only) so git tools operate on the target's history, not the evaluator's own workspace.

```yaml
# config/experts/evaluator/config.yaml (relevant excerpt)
tools:
  evaluation:
    - get_job_details
    - get_job_audit
    - get_job_chat
    - get_job_todos
    - get_job_workspace_file
    - search_job_audit
  mongodb:
    - mongo_query
    - mongo_aggregate
    - mongo_schema
  git:
    - git_log
    - git_diff
    - git_show
    - git_status
```

#### What the evaluator analyzes

- **Iteration efficiency**: loops where the agent repeated the same action, wasted iterations
- **Tool usage patterns**: which tools failed, were misused, or called with bad arguments
- **Reasoning quality**: did strategic phases produce coherent plans? Were plans followed?
- **Context management**: context compaction events, information loss
- **Phase progression**: right-sized phases? Completed vs abandoned todos?
- **Error patterns**: recurring errors, retry storms, unrecoverable failures
- **Prompt effectiveness**: were instructions followed? Misunderstandings?

#### Evaluator output

Structured JSON report in `output/evaluation_report.json`:

```json
{
  "target_job_id": "uuid",
  "target_config": "expert_name",
  "overall_score": 0.72,
  "summary": "Brief narrative",
  "issues": [
    {
      "id": "ISSUE-001",
      "category": "iteration_efficiency",
      "severity": "high",
      "title": "Agent looped 15 times on failing edit_file",
      "description": "Detailed description with evidence",
      "evidence": { "audit_steps": [42, 43, 44] },
      "suggested_fix": {
        "type": "prompt_change|config_change|code_change",
        "target_file": "config/experts/writer/instructions.md",
        "description": "Add instruction to read file before editing"
      }
    }
  ],
  "metrics": {
    "total_iterations": 47,
    "total_tokens": 125000,
    "total_phases": 6,
    "error_count": 3,
    "wasted_iterations": 15
  },
  "improvement_priorities": [
    "ISSUE-003: Fix edit_file retry storm (saves ~30% iterations)",
    "ISSUE-001: Better strategic planning (reduces phase count)"
  ]
}
```

### Live Debugging

Beyond post-mortem analysis, the evaluator can attach to the agent's runtime state for deeper diagnostics. This uses a Python remote debugger embedded in the agent process.

#### Debug server

When `debug.enable_remote_debugger: true` is set in the agent config, the agent starts a `debugpy` server on a configurable port at process startup. The evaluator (or a human) can attach to inspect live state.

```yaml
# config/defaults.yaml (new keys)
debug:
  enable_remote_debugger: false        # Off by default
  debugger_port: 5678                  # debugpy listen port
  debugger_host: "0.0.0.0"            # Bind address
  wait_for_client: false               # Don't block startup
  snapshot_on_error: true              # Auto-dump state on unhandled exceptions
```

#### What live debugging enables

- **State snapshots**: Dump the full `UniversalAgentState` (messages, todos, phase info, token counts) at any point during execution, not just at phase boundaries
- **Breakpoint injection**: Set conditional breakpoints on specific tool calls (e.g., break when `edit_file` fails 3 times in a row) to capture the exact agent state at failure points
- **Memory inspection**: Examine `workspace.md` contents, plan state, and context window composition in real-time to understand what the agent "sees"
- **Post-mortem on crashes**: When `snapshot_on_error` is enabled, unhandled exceptions trigger a full state dump to `workspace/debug/crash_<timestamp>.json` before the process exits

#### Implementation

```python
# src/core/debugger.py
import debugpy

def start_debug_server(config: dict):
    """Start debugpy server if enabled in config."""
    debug_cfg = config.get("debug", {})
    if not debug_cfg.get("enable_remote_debugger", False):
        return

    host = debug_cfg.get("debugger_host", "0.0.0.0")
    port = debug_cfg.get("debugger_port", 5678)
    debugpy.listen((host, port))

    if debug_cfg.get("wait_for_client", False):
        debugpy.wait_for_client()
```

The evaluator can request a state snapshot from a running job via a new orchestrator endpoint (`POST /api/jobs/{id}/debug/snapshot`) which signals the agent process to serialize its current state.

### Pipeline Script

External Python script that orchestrates the loop using the orchestrator REST API. Does not run agents itself - just creates jobs and waits.

```
python pipeline.py --job-id <completed-job-uuid>
python pipeline.py --job-id <uuid> --skip-research    # evaluator + coder only
python pipeline.py --job-id <uuid> --evaluate-only     # just the evaluation
```

#### Flow

```python
# 1. Read the completed job's details
job = GET /api/jobs/{job_id}

# 2. Create evaluator job
eval_job = POST /api/jobs {
    config_name: "evaluator",
    description: "Evaluate job {job_id} for performance issues",
    context: { target_job_id: job_id }
}
# Wait for completion, read output/evaluation_report.json from workspace

# 3. Create researcher job
research_job = POST /api/jobs {
    config_name: "researcher",
    description: "Research solutions for these agent issues: ...",
    instructions: "<evaluation report summary + issue list>"
}
# Wait for completion, read research output from workspace

# 4. Create coder job
coder_job = POST /api/jobs {
    config_name: "coder",
    description: "Implement improvements based on evaluation and research",
    context: { git_url: "<repo>", git_branch: "improvement/iter_N" },
    instructions: "<evaluation + research findings>"
}
# Wait for completion
```

#### Inter-stage data flow

Each stage's workspace output becomes the next stage's input via the `instructions` or `context` fields in `POST /api/jobs`. The pipeline script reads workspace files from the filesystem (`workspace/job_<uuid>/output/`) and injects them into the next job's creation request.

### Iteration Tracking

The pipeline tracks results across iterations in a results directory:

```
pipeline_results/<source_job_id>/
  iteration_001/
    eval_job_id.txt
    research_job_id.txt
    coder_job_id.txt
    evaluation_report.json
    metrics.json          # {iterations, tokens, phases, score, delta vs previous}
  iteration_002/
    ...
  summary.json            # aggregate improvement over iterations
```

### Training Data Generation

The evaluator extracts examples from each job run to build a fine-tuning dataset. Good and bad examples are captured from the audit trail and stored in a structured format.

#### Example types

| Type | Source | Description |
|------|--------|-------------|
| **Good tool calls** | Audit trail entries where a tool succeeded on the first attempt | Positive examples of correct tool usage with proper arguments |
| **Bad tool calls** | Sequences where the same tool was retried 2+ times | Negative examples showing incorrect arguments, missing context, wrong tool choice |
| **Recovery patterns** | Sequences where the agent recovered from an error | Teaches self-correction behavior |
| **Wasted loops** | Sequences flagged as wasted iterations | Negative examples of repetitive, unproductive behavior |
| **Phase planning** | Strategic phase outputs (plan.md, todos) vs actual execution | Examples of good/bad task decomposition and estimation |

#### Output format

Training examples are saved to `output/training_data.jsonl` in the evaluator's workspace. Each line is a self-contained example:

```json
{
  "id": "TRAIN-001",
  "source_job_id": "uuid",
  "type": "bad_tool_call",
  "category": "retry_storm",
  "messages": [
    {"role": "assistant", "content": "I'll edit the file...", "tool_calls": [...]},
    {"role": "tool", "content": "Error: old_string not found in file"},
    {"role": "assistant", "content": "Let me try again...", "tool_calls": [...]}
  ],
  "correction": {
    "role": "assistant",
    "content": "I should read the file first to get the exact content.",
    "tool_calls": [{"name": "read_file", "arguments": {"path": "..."}}]
  },
  "lesson": "Always read a file before attempting to edit it",
  "tags": ["edit_file", "read_before_write", "retry_prevention"]
}
```

The `messages` field contains the raw conversation turns from the audit trail. The `correction` field is generated by the evaluator as the ideal response that should have been produced instead. Over time, this dataset can be used for:

- Fine-tuning smaller models to match the behavior of corrected runs
- Few-shot prompt examples injected into agent instructions
- Regression testing — replay scenarios to verify improvements

### Automated Re-run and Loop Control

After the coder finishes implementing improvements, the pipeline automatically re-runs the original job with the improved codebase. This creates a true closed loop.

#### Loop control

The pipeline enforces safety boundaries to prevent runaway loops:

```
pipeline.py --job-id <uuid>                          # Default: max 3 iterations
pipeline.py --job-id <uuid> --max-iterations 5       # Custom limit
pipeline.py --job-id <uuid> --min-score-delta 0.05   # Stop if improvement < 5%
pipeline.py --job-id <uuid> --require-approval        # Pause for human review between iterations
```

#### Convergence criteria

The loop stops when any of these conditions are met:

1. **Max iterations reached** (default: 3)
2. **Score plateau**: evaluator score delta between iterations drops below `--min-score-delta` (default: 0.05)
3. **No issues found**: evaluator reports zero high/medium severity issues
4. **Regression detected**: evaluator score decreases compared to the previous iteration — the coder's changes made things worse, so the pipeline rolls back and stops
5. **Human halt**: user sends `SIGINT` or sets a stop flag via `pipeline.py --stop <source_job_id>`

#### Rollback on regression

If iteration N scores lower than iteration N-1, the pipeline:
1. Reverts the coder's branch (`git reset` to pre-iteration commit)
2. Records the regression in `pipeline_results/<id>/iteration_N/regression.json`
3. Stops the loop and reports which changes caused the regression

## Implementation Steps

### Phase 1: Evaluation tools + direct access
- Create `src/tools/evaluation/__init__.py` and `evaluation_tools.py`
- Register in `src/tools/registry.py`, `src/core/loader.py`
- Add to `config/defaults.yaml` and `config/schema.json`
- Configure MongoDB datasource attachment for evaluator
- Implement read-only target workspace mounting for git tool access

### Phase 2: Live debugging infrastructure
- Create `src/core/debugger.py` with `debugpy` integration
- Add `debug` config keys to `config/defaults.yaml` and `config/schema.json`
- Add `POST /api/jobs/{id}/debug/snapshot` endpoint to orchestrator
- Implement `snapshot_on_error` crash dump to `workspace/debug/`

### Phase 3: Evaluator expert
- Create `config/experts/evaluator/config.yaml` (with mongodb + git tools)
- Create `config/experts/evaluator/instructions.md`
- Include training data extraction logic in evaluator instructions

### Phase 4: Pipeline script
- Create `pipeline.py` at project root
- Uses `httpx` to call orchestrator API
- Polls for job completion
- Reads workspace output files
- Chains jobs with results forwarding
- Implement loop control (max iterations, score delta, regression rollback)
- Implement convergence detection and automatic stopping

### Phase 5: Training data pipeline
- Define JSONL schema for training examples
- Implement extraction of good/bad tool call pairs from audit trail
- Implement correction generation in evaluator instructions
- Add training data aggregation across iterations in `pipeline_results/`

### Phase 6: Integration
- Test with a real completed job
- Verify evaluator can access audit trails, MongoDB, and target workspace git
- Verify researcher receives evaluation context
- Verify coder receives actionable implementation brief
- Run a full multi-iteration loop and verify convergence/rollback behavior

## Open Questions

- **Scope constraints**: How to limit what the coder can change per iteration (only prompts? configs? tool code? core agent code?)
- **Quality gate weighting**: The evaluator score is the primary convergence metric, but what sub-metrics should be weighted most? Iteration count, token usage, error count, phase count — relative importance TBD.
- **Training data volume**: How many iterations/jobs are needed before the training dataset is large enough to be useful for fine-tuning? Minimum viable dataset size TBD.
- **Debug server security**: The `debugpy` server exposes internal state. In multi-tenant or networked setups, authentication/access control for the debug port needs consideration.
