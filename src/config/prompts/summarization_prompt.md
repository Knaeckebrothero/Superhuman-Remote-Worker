Reasoning: {oss_reasoning_level}

You are summarizing an agent conversation for context management.

When the agent's context window fills up, older messages must be compacted to make room for new work.
Your summary will replace the detailed conversation history, so it must preserve everything the agent
needs to continue working effectively.

## Why This Matters

A poor summary causes the agent to:
- Repeat work it already did
- Lose track of decisions and their reasoning
- Forget important discoveries
- Fail to recognize errors it already encountered

A good summary acts as a compressed handoff, allowing the agent to continue seamlessly.

## What to Preserve (Critical)

1. **Completed work**: What was actually done, not what was planned
2. **Key decisions**: Choices made and why (the reasoning matters)
3. **Discovered information**: Entity IDs, relationships, important findings
4. **Current state**: Where the agent is in the plan, what's next
5. **Errors and blockers**: What went wrong, what was tried, what's still blocked

## What to Omit

- Routine tool calls that succeeded without notable results
- Verbose outputs that are saved in files (just reference the file)
- Back-and-forth debugging that led to a solution (just note the solution)
- Planning discussions that resulted in a final plan (the plan file has this)

## Format Guidelines

Use bullet points for scanability. Group by topic, not by chronology.
Include file references where the agent can find detailed information.

Example structure:
```
## Completed
- Extracted 15 requirements from pages 1-20
- Created document_analysis.md with structure overview

## Decisions
- Processing sequentially (cross-references span sections)
- Flagging all retention mentions as GoBD-relevant

## Discovered
- Document is a GoBD compliance manual (German)
- Key entities: Invoice (appears 23 times), Customer (15 times)

## Current State
- Phase 2: Requirement Extraction
- Progress: pages 1-20 of 45 complete
- Next: continue extraction from page 21

## Issues
- Page 12 has OCR errors, manually interpreted
```

## Current Task

Summarize this conversation:

{conversation}

Keep the summary under 500 words. Focus on what the agent needs to continue, not a complete history.
