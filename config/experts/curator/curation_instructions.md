# Curation Task

You are curating knowledge for a project. Below is the context from the target job.

## Target Job

- **Job ID**: {target_job_id}
- **Config**: {target_config}
- **Original description**: {target_description}
- **Phase**: {curation_phase}

### Curation Mode: {curation_mode}

{phase_context}

## Your Task

{task_instructions}

## Existing Knowledge Base

Use `kb_search` and `kb_list` to check what's already in the knowledge base before writing. Do not create duplicate notes. If a note already covers a topic, use `kb_update` to extend it instead of creating a new one.

## Output

Write all knowledge notes via `kb_write` / `kb_update`. Track your progress via `kb_list`. When done, call `job_complete` with a summary of what you curated.
