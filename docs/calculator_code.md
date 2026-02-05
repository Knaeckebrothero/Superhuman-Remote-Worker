# Calculator & Code Execution Tools

Ideas for adding computational tools to the agent.

## Motivation

The agent sometimes needs to:
- Perform calculations (math, statistics, unit conversions)
- Generate visualizations or charts
- Transform/process data programmatically
- Test code snippets or algorithms

Currently it can only reason about these things, not actually execute them.

---

## How Anthropic Does It

Anthropic has three approaches to code execution worth studying:

### 1. Claude API Code Execution Tool (Beta)

A server-side sandboxed environment available via the API. Claude can run Bash commands and manipulate files.

**API Usage:**
```python
response = client.beta.messages.create(
    model="claude-sonnet-4-5",
    betas=["code-execution-2025-08-25"],
    max_tokens=4096,
    messages=[{"role": "user", "content": "Calculate mean of [1,2,3,4,5]"}],
    tools=[{
        "type": "code_execution_20250825",
        "name": "code_execution"
    }]
)
```

**Container specs:**
- Python 3.11.12 on Linux x86_64
- 5 GiB RAM, 5 GiB disk, 1 CPU
- **No internet access** (completely disabled)
- Pre-installed: pandas, numpy, scipy, scikit-learn, matplotlib, seaborn, sympy, etc.
- Containers expire after 30 days, can be reused across requests

**Sub-tools provided:**
- `bash_code_execution` - Run shell commands
- `text_editor_code_execution` - View, create, edit files

**Pricing:** 1,550 free hours/month, then $0.05/hour per container.

### 2. Analysis Tool (Claude.ai)

A built-in JavaScript sandbox in the Claude.ai web interface. Users enable it in settings.

**Capabilities:**
- Runs JavaScript code in a sandboxed environment
- Complex math, data analysis, iteration before answering
- CSV processing and exploration

**Note:** As of Nov 2025, being replaced by more powerful code execution (file generation, visualizations).

### 3. Claude Code Sandboxing

For the CLI tool, Anthropic uses OS-level sandboxing with **two security boundaries**:

**Filesystem isolation:**
- Uses Linux bubblewrap / macOS seatbelt
- Read/write access only to current working directory
- Blocks modification of files outside the project

**Network isolation:**
- All traffic routed through a Unix domain socket proxy
- Proxy validates domains, requests user confirmation for new ones
- Organizations can customize traffic rules

**Open source:** [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) - lightweight sandboxing without containers.

**Result:** 84% reduction in permission prompts while maintaining security.

---

## Tool 1: Calculator (Wolfram Alpha Style)

A structured tool for mathematical and computational queries.

### Option A: Wolfram Alpha API

```python
@tool
def wolfram_alpha(query: str) -> str:
    """
    Query Wolfram Alpha for mathematical computations,
    unit conversions, scientific facts, and data analysis.

    Args:
        query: Natural language or mathematical expression
               e.g., "integrate x^2 from 0 to 5"
               e.g., "convert 100 miles to kilometers"
               e.g., "GDP of Germany 2023"

    Returns:
        Computed result or factual answer
    """
```

**Pros:**
- Handles complex math (integrals, derivatives, matrices)
- Unit conversions, date calculations
- Scientific facts and data
- No code execution security concerns

**Cons:**
- Requires API key ($)
- Rate limited
- Can't do custom logic

### Option B: Simple Calculator (sympy/math)

```python
@tool
def calculate(expression: str) -> str:
    """
    Evaluate a mathematical expression safely.

    Args:
        expression: Math expression like "sqrt(16) + 2^3" or "sin(pi/4)"

    Returns:
        Numeric result
    """
    import sympy
    return str(sympy.sympify(expression).evalf())
```

**Pros:**
- Free, no API needed
- Fast
- Deterministic

**Cons:**
- Limited to pure math
- No data lookup capability

### Implementation Considerations

- Add to `domain` tools in config
- Consider caching for repeated queries
- Add timeout for complex expressions

---

## Tool 2: Python Interpreter

A sandboxed Python environment where the agent can write and execute code.

### Architecture Options

#### Option A: Docker-based Sandbox

```
┌─────────────────────────────────────────────────────────┐
│ Agent Process                                           │
│                                                         │
│  execute_python(code, packages=[...])                   │
│         │                                               │
│         ▼                                               │
│  ┌─────────────────────────────────────────────────┐   │
│  │ Docker Container (ephemeral)                     │   │
│  │                                                  │   │
│  │  pip install packages                            │   │
│  │  exec(code)                                      │   │
│  │  return stdout, files                            │   │
│  │                                                  │   │
│  │  - Resource limits (CPU, memory, time)           │   │
│  │  - No network (optional)                         │   │
│  │  - Mounted workspace dir (read-only or r/w)      │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Implementation:**

```python
@tool
def execute_python(
    code: str,
    packages: list[str] | None = None,
    timeout_seconds: int = 60,
    allow_network: bool = False,
) -> dict:
    """
    Execute Python code in an isolated container.

    Args:
        code: Python code to execute
        packages: pip packages to install (e.g., ["pandas", "matplotlib"])
        timeout_seconds: Max execution time
        allow_network: Whether container can access network

    Returns:
        {
            "stdout": "...",
            "stderr": "...",
            "return_value": "...",
            "files": ["output.png", "results.csv"]  # files created
        }
    """
```

**Pros:**
- Full Python ecosystem
- Can install any package
- Strong isolation
- Can generate files (charts, CSVs, etc.)

**Cons:**
- Requires Docker
- Slower startup
- More complex infrastructure

#### Option B: RestrictedPython (in-process)

```python
from RestrictedPython import compile_restricted, safe_builtins

@tool
def execute_python_restricted(code: str) -> str:
    """Execute Python with restricted builtins (no file/network access)."""
    byte_code = compile_restricted(code, '<inline>', 'exec')
    exec(byte_code, {'__builtins__': safe_builtins})
```

**Pros:**
- No Docker needed
- Fast

**Cons:**
- Very limited (no imports, no file access)
- Still potential security risks
- Not suitable for data analysis

#### Option C: E2B Code Interpreter (hosted)

Use [E2B](https://e2b.dev/) cloud sandbox:

```python
from e2b_code_interpreter import CodeInterpreter

@tool
def execute_python_e2b(code: str) -> dict:
    """Execute Python in E2B cloud sandbox."""
    with CodeInterpreter() as sandbox:
        execution = sandbox.notebook.exec_cell(code)
        return {
            "stdout": execution.logs.stdout,
            "stderr": execution.logs.stderr,
            "results": execution.results,
        }
```

**Pros:**
- Fully managed, secure
- Pre-installed data science packages
- Supports file uploads/downloads

**Cons:**
- Requires E2B API key ($)
- Network latency
- External dependency

---

## Recommended Approach

### Phase 1: Simple Calculator

Add a basic `calculate` tool using sympy:

```yaml
# config/defaults.yaml
tools:
  domain:
    - calculate  # New tool
```

Low risk, immediate value for math queries.

### Phase 2: Docker Python Sandbox

For jobs that need data processing or visualization:

```yaml
# config/data_analyst.yaml
tools:
  domain:
    - calculate
    - execute_python  # Docker-based
```

Requirements:
- Docker/Podman available on host
- Base image with common packages pre-installed
- Resource limits configured

### Phase 3: Persistent Environment (optional)

For long-running analysis jobs, maintain a persistent container:

```yaml
code_execution:
  enabled: true
  mode: persistent  # vs ephemeral
  base_image: python:3.11-slim
  pre_installed:
    - pandas
    - numpy
    - matplotlib
    - scikit-learn
  resource_limits:
    memory: 2G
    cpu: 1.0
    timeout: 300
```

---

## Security Considerations

| Risk | Mitigation |
|------|------------|
| Arbitrary code execution | Docker isolation, resource limits |
| Package supply chain attacks | Allowlist of approved packages |
| Data exfiltration | Disable network access by default |
| Resource exhaustion | CPU/memory/time limits |
| Persistent state | Ephemeral containers (destroy after execution) |

### Package Allowlist Example

```yaml
code_execution:
  allowed_packages:
    - pandas
    - numpy
    - matplotlib
    - seaborn
    - scikit-learn
    - scipy
    - sympy
    - requests  # only if allow_network: true
```

---

## Integration with Workspace

The agent should be able to:

1. **Read workspace files** in code:
   ```python
   # Agent's code
   df = pd.read_csv('/workspace/data/input.csv')
   ```

2. **Write results back**:
   ```python
   # Generated chart saved to workspace
   plt.savefig('/workspace/analysis/chart.png')
   ```

3. **Reference outputs in responses**:
   > I've generated the analysis chart. See `analysis/chart.png` in your workspace.

Mount strategy:
```python
docker_volumes = {
    workspace_path / "documents": {"bind": "/workspace/data", "mode": "ro"},
    workspace_path / "analysis": {"bind": "/workspace/output", "mode": "rw"},
}
```

---

## Example Usage

### Math Calculation

**Agent prompt:** "What's the area under y=x^2 from x=0 to x=3?"

**Tool call:**
```python
calculate("integrate(x**2, (x, 0, 3))")
# Returns: "9"
```

### Data Analysis

**Agent prompt:** "Analyze the CSV I uploaded and create a summary chart"

**Tool call:**
```python
execute_python(
    code="""
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('/workspace/data/sales.csv')
summary = df.groupby('region')['revenue'].sum()
summary.plot(kind='bar')
plt.title('Revenue by Region')
plt.savefig('/workspace/output/revenue_chart.png')
print(summary.to_string())
""",
    packages=["pandas", "matplotlib"],
)
```

---

## Open Questions

- [ ] Should code execution be opt-in per job or globally available?
- [ ] How to handle package version conflicts across jobs?
- [ ] Should we support other languages (R, Julia)?
- [ ] How to display generated images in cockpit UI?
- [ ] Persistent vs ephemeral environments - what's the default?
- [ ] Use Anthropic's API code execution tool directly, or self-host?
- [ ] Consider OS-level sandboxing (bubblewrap/seatbelt) instead of Docker?

---

## References

- [Claude API Code Execution Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool) - Official docs
- [Analysis Tool Announcement](https://claude.com/blog/analysis-tool) - Claude.ai built-in sandbox
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) - OS-level isolation approach
- [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) - Open source lightweight sandboxing
- [E2B Code Interpreter](https://e2b.dev/) - Third-party hosted sandbox option
