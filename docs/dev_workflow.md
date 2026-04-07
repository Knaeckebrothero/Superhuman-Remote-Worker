# Dev Workflow: Automated Development Pipeline

## Problem Statement

We're rolling out features faster than we can test them. Unit tests written by coding agents are shallow -- they satisfy coverage but miss real behavior. Models are reluctant to test thoroughly even when instructed. We need a structured pipeline where AI agents handle the full development lifecycle: requirements, research, implementation, testing, and review.

## Tester Expert

### Concept

A new expert agent role dedicated to testing. Given a PR or feature branch, it:

1. **Reads the diff** via git tools to understand what changed
2. **Starts the application** via `run_command` (Angular dev server, FastAPI, etc.)
3. **Explores the changes** via browser automation (browser-use + CDP) to verify UI behavior
4. **Writes Playwright test scripts** as actual `.spec.ts` files for UI regression
5. **Writes comprehensive unit tests** (pytest for backend, vitest for cockpit) that go deeper than what the developer wrote
6. **Runs all tests** and fixes small issues found during testing
7. **Protocols everything** in a structured test report

### Why Not Just Better Instructions for Developers?

Developer agents optimize for feature delivery. Even with explicit testing instructions, they:
- Write the minimum tests to satisfy the instruction
- Skip edge cases and error paths
- Don't start the app and manually verify behavior
- Don't test interactions between the new feature and existing functionality

A dedicated tester agent has testing as its **primary goal**, not an afterthought. Its prompts, tools, and success criteria are all oriented around finding problems.

### Key Design Decisions

**Browser automation approach**: The agent uses browser-use for exploratory testing (navigating the app, checking if things work) and writes Playwright scripts for reproducible regression tests. These are two different activities -- exploration finds problems, scripts prevent regressions.

**Prompt discipline**: The tester's prompts must be ruthlessly specific. Models will try to shortcut testing. Required behaviors:
- "You MUST start the application and verify it loads without errors"
- "You MUST navigate to every route affected by the changes"
- "You MUST NOT mark a test as passing without actually running it"
- "You MUST NOT skip testing because something 'looks correct' in the code"
- "If a test fails, you MUST investigate and either fix the issue or document it as a bug"
- "You MUST write at least one negative test (invalid input, error state) per feature"

**Test report structure**: The tester outputs a structured report covering:
- What was tested (routes, endpoints, components)
- What passed and what failed
- New test files created (with paths)
- Bugs found (with severity and reproduction steps)
- Areas that couldn't be tested and why

### Tool Access

| Category | Tools | Purpose |
|----------|-------|---------|
| workspace | read/write/edit/list/search | Read code, write test files |
| coding | run_command, shell_read | Start app, run tests, check logs |
| research | browse_website | UI exploration and visual verification |
| git | git_log, git_show, git_diff, git_status | Understand what changed |
| core | next_phase_todos, todo_complete, etc. | Phase management |

No delegation, no citation, no knowledge base tools. Tester stays focused.

### Config Structure

```
config/experts/tester/
  config.yaml              # Expert config ($extends: defaults)
  prompt_matrix.yaml       # Model-family prompt routing
  persona.txt              # "You are a meticulous QA engineer..."
  strategic.txt            # Planning phase: analyze diff, identify test targets
  tactical.txt             # Execution phase: write and run tests
  test_instructions.md     # Injected instructions template (diff, criteria, app startup)
```

### Instruction Template Variables

The tester's instructions template receives context from the pipeline:

- `{target_job_id}` -- the developer job that produced the changes
- `{diff_summary}` -- summary of changed files and their purpose
- `{acceptance_criteria}` -- from the requirements agent (if pipeline is active)
- `{app_startup_commands}` -- how to start each component
- `{test_commands}` -- how to run existing test suites
- `{changed_files}` -- list of modified/added files
- `{affected_components}` -- which parts of the system are impacted

---

## Dev Pipeline

### Current Subjob System

The orchestrator already supports subjob chains:

```
Today:     Scholar ------> Main Job ------> Critic
                              ^                |
                              +---feedback-----+
           Curator runs in parallel -----------> Final pass
```

- Scholar runs before the main job, holds parent in `waiting` status
- Critic runs after completion, can return the job with feedback for fix loops
- Curator runs alongside and does a final pass after critic approval

### Proposed Pipeline

```
Stage 0 (optional):   Designer ←→ User (interactive, produces design_spec/)
                              |
Stage 1 (parallel):   Requirements Engineer + Scholar
                              |
Stage 2:              Developer (receives research + requirements + design_spec/)
                              |
Stage 3:              Tester (receives diff, criteria, runs tests)
                              |
Stage 4:              Critic (reviews ALL agents' work, including design fidelity)
```

The Designer stage is optional and interactive — it runs before the automated pipeline as a collaborative session between the designer agent and the user. When a design_spec/ exists, it's injected into the developer's workspace alongside requirements and research. The critic also checks whether the implementation matches the design.

### Stage Details

#### Stage 1: Research (parallel)

**Requirements Engineer** (new expert)
- Reads the issue/goal description
- Examines the relevant parts of the codebase
- Outputs a structured `requirements.yaml`:

```yaml
feature: "Add batch export to cockpit"
acceptance_criteria:
  - "User can select multiple jobs from the job list"
  - "Export button appears when 2+ jobs are selected"
  - "Exported ZIP contains one folder per job with all output files"
  - "Export works for jobs the user has permission to access"
  - "Error toast shown if any job fails to export"
affected_components:
  - cockpit/src/app/pages/jobs/
  - orchestrator (new endpoint: GET /api/jobs/export)
risk_areas:
  - "Permission check must respect project membership"
  - "Large exports could timeout -- needs streaming response"
test_scenarios:
  - "Select 3 jobs, export, verify ZIP structure"
  - "Select job from project user doesn't belong to -- expect 403"
  - "Export single job with large output files"
  - "Cancel export mid-download"
```

**Scholar** (existing expert, runs in parallel)
- Researches the problem domain, similar implementations, relevant docs
- Outputs `research/brief.md` with findings and recommendations

Both complete before Stage 2 begins. Their outputs are merged into the developer job's workspace.

#### Stage 2: Build

**Developer** (existing expert)
- Receives scholar research + requirements as input context
- Plans implementation against the acceptance criteria
- Builds the feature
- Writes basic tests (developer-level, not comprehensive)
- Commits to feature branch

#### Stage 3: Test

**Tester** (new expert)
- Checks out the developer's branch
- Reads the diff to understand what changed
- Reads `requirements.yaml` for acceptance criteria
- Starts the application
- Tests each acceptance criterion:
  - UI features: browser exploration + Playwright scripts
  - API features: curl/httpie commands + pytest tests
  - Unit behavior: vitest/pytest for edge cases
- Fixes small issues directly (typos, missing null checks, off-by-one)
- Commits test files and small fixes
- Outputs test report with pass/fail per criterion

**If tests fail significantly**: Tester documents failures and the job completes with `pending_review`. The pipeline can either:
- Return to developer with tester's feedback (similar to critic feedback loop)
- Escalate to human review

#### Stage 4: Review

**Critic** (existing expert, extended scope)
- Reviews ALL prior agents' work, not just the developer:
  - Did requirements capture the right criteria?
  - Did the scholar find relevant information?
  - Did the developer meet the requirements?
  - Did the tester actually test everything, or take shortcuts?
- Verdict: approve or return with feedback (existing mechanism)
- If returned: feedback targets the specific agent that fell short

### Pipeline Configuration

New top-level config key for jobs:

```yaml
pipeline:
  enabled: true
  stages:
    - name: design
      optional: true
      agents:
        - config: designer
          interactive: true   # Runs as persistent session with user
    - name: research
      parallel: true
      agents:
        - config: scholar
          enabled: true
        - config: requirements
          enabled: true
    - name: build
      agents:
        - config: developer
    - name: test
      agents:
        - config: tester
    - name: review
      agents:
        - config: critic
          max_rounds: 3
```

The orchestrator walks through stages sequentially. Within a stage, all agents run in parallel. A stage completes when all its agents complete. Outputs from each stage are available to the next.

### Orchestrator Changes

The current completion handler (`_trigger_verification_on_complete`) already spawns post-job agents. The pipeline extends this with:

1. **Pipeline state tracking**: New `pipeline_state` field in job context JSONB, tracking current stage index, completed stages, and per-stage results.

2. **Stage advancement**: When all agents in a stage complete, the completion handler checks if there's a next stage and spawns those agents. Similar to how scholar completion currently unblocks the main job.

3. **Cross-stage artifact passing**: Each stage's outputs (research briefs, requirements, diffs, test reports) are merged into the next stage's workspace, similar to how scholar artifacts are currently injected.

4. **Feedback routing**: When the critic returns feedback, the pipeline identifies which stage/agent the feedback targets and re-runs that stage (not the whole pipeline).

---

## Implementation Plan

### Phase 1: Tester Expert (standalone, no pipeline changes)

Create `config/experts/tester/` with all config, prompts, and instruction templates. Can be used immediately as a manual subjob -- create a tester job, point it at a feature branch, and let it test.

**Deliverables**:
- `config/experts/tester/config.yaml`
- `config/experts/tester/prompt_matrix.yaml`
- `config/experts/tester/persona.txt`
- `config/experts/tester/strategic.txt`
- `config/experts/tester/tactical.txt`
- `config/experts/tester/test_instructions.md`
- Orchestrator support: `is_testing_enabled()`, `format_test_instructions()`, `_spawn_tester_subjob()` in completion service
- Completion handler wiring: spawn tester after developer completes (before critic)

### Phase 2: Designer Expert

Create `config/experts/designer/` with all config, prompts, and instruction templates. Can be used immediately in two modes: (a) interactive persistent session for collaborative design with the user, (b) worker-mode subjob that produces design_spec/ artifacts for the pipeline.

**Deliverables** (DONE):
- `config/experts/designer/config.yaml`
- `config/experts/designer/prompt_matrix.yaml`
- `config/experts/designer/instruction_matrix.yaml`
- `config/experts/designer/persona.txt`
- `config/experts/designer/strategic.txt`
- `config/experts/designer/tactical.txt`
- `config/experts/designer/instructions.md`
- `config/experts/designer/design_guide.md` (Catppuccin design system reference)
- `config/experts/designer/workspace_template.md`
- `config/experts/designer/strategic_todos_initial.yaml`

**Future work**:
- Persistent/interactive variant config (`$extends: persistent_defaults`)
- Orchestrator support: inject design_spec/ into developer workspace
- Completion handler: optional designer stage before pipeline begins
- Cockpit UI: mockup preview panel (render HTML mockups inline)

### Phase 3: Requirements Expert

Create `config/experts/requirements/` for structured requirements capture. Can run as a parallel subjob alongside scholar.

**Deliverables**:
- `config/experts/requirements/config.yaml`
- `config/experts/requirements/prompt_matrix.yaml`
- `config/experts/requirements/persona.txt`
- `config/experts/requirements/strategic.txt`
- `config/experts/requirements/tactical.txt`
- `config/experts/requirements/requirements_instructions.md`
- Output schema: `requirements.yaml` format definition

### Phase 4: Pipeline Orchestration

Wire the stages together in the orchestrator. Extend the completion handler to support multi-stage pipelines with parallel agents per stage.

**Deliverables**:
- Pipeline config schema (in `defaults.yaml`)
- Pipeline state tracking (new JSONB fields on jobs)
- Stage advancement logic in completion handler
- Cross-stage artifact merging
- Feedback routing to specific stages
- Cockpit UI: pipeline progress visualization

### Phase 5: Iteration

- Tune tester prompts based on real job results (are tests actually comprehensive?)
- Tune requirements prompts (are criteria actually testable?)
- Add critic awareness of pipeline (review all agents, not just developer)
- Metrics: track how many bugs tester finds vs. what makes it to human review
