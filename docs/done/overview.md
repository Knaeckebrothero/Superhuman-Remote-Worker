# How the Agent Works - A Guide for Non-Technical Team Members

## The Big Picture

The agent is like an **autonomous worker** that processes documents to extract requirements. Think of it as a very methodical assistant that:

1. Receives a task (a document to analyze)
2. Creates a plan for how to do the work
3. Breaks the plan into small to-do items
4. Works through each to-do item one by one
5. Reports when finished

---

## The Two Loops: How Work Gets Done

The agent operates with a **nested loop** structure - a "big loop" for strategy and a "small loop" for execution:

```
┌─────────────────────────────────────────────────────────────────────┐
│                          STARTUP (once)                             │
│                                                                     │
│   1. Read Instructions → 2. Create Plan → 3. Create First To-Dos   │
│                                                                     │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    OUTER LOOP (Strategic Thinking)                  │
│                                                                     │
│   "What phase am I in? What are my goals for this phase?"          │
│                                                                     │
│   • Reads the main plan                                             │
│   • Updates memory with what it learned                             │
│   • Creates to-do items for the current phase                       │
│                                                                     │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    INNER LOOP (Actual Work)                         │
│                                                                     │
│   "What's my next task? Let me do it."                             │
│                                                                     │
│        ┌─────────────────────────────────┐                          │
│        │                                 │                          │
│        ▼                                 │                          │
│   Do Work ──► Check To-Dos ──► Done? ─no─┘                          │
│     │                           │                                   │
│     │                          yes                                  │
│     ▼                           │                                   │
│   Use Tools                     ▼                                   │
│   (read files,           Archive & Move                             │
│    search web,           to Next Phase                              │
│    save data)                   │                                   │
│                                 │                                   │
└─────────────────────────────────┼───────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          GOAL CHECK                                 │
│                                                                     │
│   "Am I finished with everything?"                                  │
│                                                                     │
│   NO  → Go back to OUTER LOOP (next phase)                          │
│   YES → Mark job complete and stop                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Concepts in Simple Terms

### 1. Instructions (The Mission Brief)
- **What it is**: A document that tells the agent what it should do
- **Where it lives**: `configs/<agent-name>/instructions.md`
- **Your role**: You can edit this to change *what* the agent focuses on, *what rules* it follows, and *how* it approaches tasks

### 2. The Plan (The Strategy)
- **What it is**: A document the agent creates at the start of each job
- **How it works**: The agent reads the instructions, thinks about the task, and writes a multi-phase plan
- **Key point**: The plan is divided into **phases** (like chapters in a book)

### 3. To-Do Items (The Checklist)
- **What they are**: Small, concrete tasks the agent needs to complete
- **How they work**:
  - Each phase gets its own to-do list
  - The agent works through items one by one
  - When all items are done, the phase is complete
- **Statuses**: Pending → In Progress → Completed

### 4. Memory (The Notepad)
- **What it is**: A file called `workspace.md` where the agent keeps notes
- **Purpose**: Helps the agent remember important things across phases
- **Updated when**: After each phase completes

### 5. Tools (The Abilities)
- **What they are**: Actions the agent can take (read files, search the web, save data, etc.)
- **Where they're listed**: In the instructions file under "Tools"
- **Your role**: You can guide which tools the agent should prefer for different situations

---

## The Files You Can Edit

### Agent Instructions (`configs/<agent>/instructions.md`)
This is the most important file for shaping agent behavior. It contains:

| Section | Purpose | What You Can Customize |
|---------|---------|------------------------|
| **Mission** | The agent's overall goal | Refine the focus or scope |
| **Tools** | Available capabilities | Add guidance on when to use each |
| **Phases** | Step-by-step workflow | Change the order, add/remove steps |
| **Indicators** | Keywords to look for | Add domain-specific terms |
| **Rules** | Decision criteria | Adjust priority rules, skip conditions |

### Framework Prompts (`src/agent/config/prompts/`)
These control specific agent behaviors:

| File | When Used | What It Controls |
|------|-----------|------------------|
| `planning_prompt.md` | At job start | How the agent creates its plan |
| `todo_extraction_prompt.md` | At phase start | How the agent creates to-dos from the plan |
| `memory_update_prompt.md` | After each phase | What the agent remembers |
| `summarization_prompt.md` | When context gets long | How the agent summarizes past work |
| `workspace_template.md` | At job start | Initial structure of the memory file |

---

## Example: How a Job Flows

**Scenario**: The agent receives a PDF document about data retention policies.

1. **Startup**
   - Agent reads `instructions.md` → understands it should extract requirements
   - Creates a plan: "Phase 1: Analyze document structure, Phase 2: Extract requirements, Phase 3: Verify completions"
   - Creates to-dos for Phase 1: "Get document info", "Read first 5 pages", "Write analysis"

2. **Inner Loop (Phase 1)**
   - Uses `get_document_info` tool → learns it's 42 pages
   - Uses `read_file` tool → reads pages 1-5
   - Uses `write_file` tool → saves analysis
   - Marks each to-do complete
   - All done → archives to-dos, moves to Phase 2

3. **Outer Loop Transition**
   - Updates memory with Phase 1 findings
   - Creates to-dos for Phase 2: "Read pages 6-15", "Extract requirements from section A", etc.

4. **Inner Loop (Phase 2)**
   - Reads more pages
   - Creates citations for found requirements
   - Submits each requirement to the database
   - Continues until all pages processed

5. **Completion**
   - Phase 3: Verifies all requirements were saved
   - All phases done → marks job complete

---

## Tips for Writing Good Prompts

### Do:
- Be specific about what you want ("Extract requirements from legal documents")
- Provide examples of good output
- List keywords and patterns to look for
- Define clear categories and priority rules
- Explain when to skip something

### Don't:
- Be vague ("Do a good job")
- Assume technical knowledge
- Mix instructions for different agents
- Forget to explain edge cases

### Example of Good Instruction:
```markdown
### Priority Assignment
- **High**: GoBD/GDPR compliance, legal obligations ("must", "shall")
- **Medium**: Core business features
- **Low**: Nice-to-have suggestions

### Skip When:
- The text is informational only, not a requirement
- You've already extracted the same requirement
- The scope is outside the system being built
```

---

## Glossary

| Term | Meaning |
|------|---------|
| **Phase** | A major step in the plan (like a chapter) |
| **To-Do** | A specific task within a phase |
| **Tool** | An action the agent can take |
| **Workspace** | The agent's working folder for a job |
| **Memory** | The agent's notes that persist across phases |
| **Archive** | Where completed to-dos are saved |
| **Citation** | A reference to where information came from |
