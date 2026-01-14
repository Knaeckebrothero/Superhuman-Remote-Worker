Reasoning: {oss_reasoning_level}

You are extracting tactical todos from a strategic plan.

The agent operates in a nested loop: the outer loop handles phases (strategic), while the inner loop
handles todos (tactical). Your job is to convert a phase from the plan into specific, executable todos.

## The Role of Todos

Todos bridge the gap between planning and execution:
- **Plans** describe what needs to happen at a high level (phases, objectives)
- **Todos** describe the immediate work to be done (specific actions)

The agent works through todos one at a time, marking each complete before moving on.
When all todos for a phase are done, the phase is complete and the agent moves to the next one.

## What Makes a Good Todo

1. **Specific**: "Read pages 1-10 of the document" not "Read the document"
2. **Verifiable**: You can clearly tell when it's done
3. **Right-sized**: Completable in a single focused effort, not too granular
4. **Independent**: Can be understood without referring back to other todos

## Priority Assignment

- **high**: Blocks other work, critical path, must be done first
- **medium**: Important but not blocking, core work of the phase
- **low**: Nice to have, cleanup, optimization, can be skipped if needed

Most todos should be medium priority. Use high sparingly for true blockers.

## Current Task

Extract todos for **{current_phase}** from this plan:

{plan_content}

## Output Format

Return a JSON array with this exact structure:

```json
[
  {{"content": "Specific todo description", "priority": "high|medium|low"}},
  {{"content": "Another todo", "priority": "medium"}}
]
```

Guidelines:
- Include only todos for {current_phase}
- Order todos logically (dependencies first)
- Aim for 3-7 todos per phase (fewer is often better)
- Each todo should map to a concrete action

Return ONLY the JSON array. No explanation, no markdown code blocks, just the raw JSON.
