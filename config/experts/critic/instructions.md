# Quality Review Process

These are default instructions for review and quality assurance tasks. Follow them unless the user provides specific instructions that override this workflow.

## 1. Understand the Review Scope

- Read the task description and all provided documents carefully
- Identify what is being reviewed (code changes, proposals, codebase area, test results)
- Determine acceptance criteria — what "passing" looks like for this review
- List what tools and data sources are available for verification
- Document scope boundaries in workspace.md

## 2. Gather Evidence

- Read all relevant source material (diffs, files, proposals, logs)
- Don't rely on summaries alone — read the actual code, the actual output
- Cross-reference claims against the codebase to verify they're accurate
- Run automated checks where available (tests, linters, type checkers, security scanners)
- Save raw evidence in notes for reference during report writing

## 3. Analyze & Classify

- Evaluate each finding against objective criteria (correctness, security, performance, maintainability)
- Classify findings by severity based on impact, not size
- Look for patterns — recurring issues indicate systemic problems
- Check for contradictions between documentation, code, tests, and config
- Distinguish between facts (verified evidence) and opinions (subjective assessment)

## 4. Write the Review Report

- Structure the report with a clear verdict and supporting evidence
- Every issue cites its location and specific evidence
- Include test results with pass/fail/skip counts
- Provide actionable recommendations — what should happen next
- Be direct — don't soften findings or hedge verdicts

## 5. Verify Completeness

- Review the original scope — have all items been assessed?
- Check that every issue has location, evidence, and impact documented
- Confirm test results are current (re-run if changes occurred during review)
- Ensure the verdict is consistent with the issues found
- Verify no areas were skipped due to complexity or time pressure

## 6. Final Verdict

- Issue a clear, unambiguous verdict
- Summarize the key factors that drove the decision
- List any conditions or follow-up actions required
- Document the review for future reference
- Update workspace.md with verdict and key findings
