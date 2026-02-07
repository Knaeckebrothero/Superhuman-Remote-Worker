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

## Implementation Steps

### Phase 1: Evaluation tools
- Create `src/tools/evaluation/__init__.py` and `evaluation_tools.py`
- Register in `src/tools/registry.py`, `src/core/loader.py`
- Add to `config/defaults.yaml` and `config/schema.json`

### Phase 2: Evaluator expert
- Create `config/experts/evaluator/config.yaml`
- Create `config/experts/evaluator/instructions.md`

### Phase 3: Pipeline script
- Create `pipeline.py` at project root
- Uses `httpx` to call orchestrator API
- Polls for job completion
- Reads workspace output files
- Chains jobs with results forwarding

### Phase 4: Integration
- Test with a real completed job
- Verify evaluator can access audit trails
- Verify researcher receives evaluation context
- Verify coder receives actionable implementation brief

## Open Questions

- **Automated re-run**: Should the pipeline automatically re-run the original job after the coder finishes, creating a true infinite loop? Or should a human review the coder's PR first?
- **Training data generation**: The evaluator could extract good/bad tool call examples for fine-tuning. Format TBD.
- **Scope constraints**: How to limit what the coder can change per iteration (only prompts? configs? tool code? core agent code?)
- **Quality gate**: What metric determines if an iteration actually improved things? Evaluator score delta? Token usage reduction?
