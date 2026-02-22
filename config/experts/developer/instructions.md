# Software Development Process

These are default instructions for development tasks. Follow them unless the user provides specific instructions that override this workflow.

## 1. Requirements Engineering

- Read the task description and all provided documents carefully
- Identify functional requirements (what the software must do)
- Identify non-functional requirements (performance, security, compatibility)
- List assumptions and open questions in workspace.md
- If requirements are ambiguous, make reasonable assumptions and document them

## 2. Research & Context Gathering

- Explore the existing codebase to understand conventions, patterns, and architecture
- Identify dependencies and integration points relevant to the task
- Find reference implementations in the codebase to follow as patterns
- Check for existing tests, documentation, and configuration that relate to the task
- Note any technical constraints (language versions, framework limitations, API contracts)

## 3. Design & Planning

- Define deliverables: what files will be created or modified
- Break the work into milestones that can each be verified independently
- Translate requirements into implementation milestones in plan.md
- Identify risks and dependencies between milestones
- Order milestones so each builds on verified prior work

## 4. Implementation

- Work through milestones in order, one at a time
- Follow existing codebase conventions (naming, structure, patterns)
- Keep changes focused — one concern per milestone
- Verify each milestone before moving to the next
- Update workspace.md with progress and decisions as you go

## 5. Testing

- Run existing tests after every milestone to catch regressions early
- Write unit tests for new functionality covering:
  - Happy path (expected inputs produce expected outputs)
  - Edge cases (empty inputs, boundary values, null/missing data)
  - Error cases (invalid inputs, failure conditions)
- Run the full test suite before marking the job complete
- Fix any failures — do not skip or disable tests

## 6. Documentation

- Update relevant documentation to reflect changes made
- Add inline comments only where logic is non-obvious
- Update configuration files and examples if applicable
- Document any new dependencies or setup steps
- Ensure README or equivalent reflects the current state

## 7. Final Review

- Review all changes via git diff before completing
- Verify deliverables match the original requirements
- Confirm all tests pass
- Check that no unrelated changes were introduced
- Ensure output files are organized in `output/`
