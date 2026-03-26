# Creator/Validator Legacy Removal (DONE)

The creator/validator concept was a two-agent pipeline where a "Creator" agent extracted requirements from documents and a "Validator" agent assessed them against quality standards (ISO 29148) and a Neo4j domain model. The actual agents and orchestration logic were removed long ago, but the data model scaffolding remained across all layers.

## Status: Code removed, DB migration pending

All code references have been removed (~920 lines). The database still has the old columns and table — a migration is needed to drop them:

```sql
-- Drop requirements table
DROP TABLE IF EXISTS requirements CASCADE;

-- Drop creator/validator columns from jobs
ALTER TABLE jobs DROP COLUMN IF EXISTS creator_status;
ALTER TABLE jobs DROP COLUMN IF EXISTS validator_status;

-- Drop index
DROP INDEX IF EXISTS idx_jobs_creator_status;
```

Until the migration runs, the old columns/table will remain in the DB as harmless dead weight (no code reads or writes them).

## What was removed

- **Frontend**: "C: pending" / "V: pending" progress display, CREATOR/VALIDATOR stuck badges, RequirementSummary/Requirement/RequirementsResponse interfaces, `getJobRequirements()` API method, 'requirements' from debug table viewer
- **Backend**: `creator_status`/`validator_status` params from `update_job_status()`, `get_requirements()`, `get_requirement_summary()`, requirements REST endpoint, MCP tool, builder tool + dispatcher, `format_requirements()` formatter, creator/validator stuck job detection branches
- **Agent**: `RequirementsNamespace` class (~430 lines), `_update_requirement_after_validation()`, `--requirement-id` CLI flag, requirement_data flow
- **Schema**: `requirements` table definition, `creator_status`/`validator_status` columns + constraints + index, requirements join in `job_summary` view
- **Tests**: Updated mocks and assertions
- **Soft refs**: Default config changed from "creator" to "default", docstring examples updated
