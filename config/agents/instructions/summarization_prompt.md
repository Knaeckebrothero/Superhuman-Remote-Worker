# Context Summarization Prompt

You are summarizing an agent's work session to preserve important context while reducing token usage.

## Conversation to Summarize

{conversation}

## Instructions

Create a concise summary that captures:

### 1. Tasks Completed
- What specific tasks were finished
- Key outputs produced (file names, data written)
- Tools used successfully

### 2. Decisions Made
- Important choices and their reasoning
- Trade-offs considered
- Approaches selected or rejected

### 3. Information Discovered
- Key facts from documents or research
- Relevant findings from web searches
- Important data from database queries

### 4. Current Progress
- Where in the overall plan execution is
- What phase/step is currently active
- Percentage of work complete (if estimable)

### 5. Open Items
- Pending tasks from the plan
- Any blockers or issues encountered
- Questions that need resolution

### 6. Next Steps
- Immediate next action to take
- Upcoming tasks in the queue

## Format

Use bullet points. Be concise but complete. Prioritize actionable information.
Keep the summary under 500 words.

## Summary
