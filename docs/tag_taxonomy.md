# Tag Taxonomy

Based on analysis of 48 markdown documents in `documents/`.

## Tag Taxonomy (25 tags)

| # | Tag | Description | Example Documents |
|---|-----|-------------|-------------------|
| 1 | `agent-architecture` | Agent design, loop patterns, phase management | agent_improvements.md, continuous_improvement_loop.md |
| 2 | `bug-fix` | Bug reports, issues, troubleshooting | 01_issues.md, key_issues.md, phase_number_issues.md |
| 3 | `citation-engine` | Citation management, source tracking, verification | citation_engine_roadmap.md, citation_engine_rework.md, citation_issues.md |
| 4 | `cloud-infrastructure` | Cloud deployment, containers, workspace management | cloud_workspace.md, deployment.md |
| 5 | `coding-tools` | Code execution, shell commands, dev tools | coding_agent.md, universal_shell_command.md, cli_wrapper.md |
| 6 | `context-management` | Context handling, summarization, memory management | context_management.md, working_memory.md, memories_mechanism.md |
| 7 | `data-management` | Data sources, vectorization, graph databases | datasources.md, vectorization.md, graph_change_detection.md |
| 8 | `debugging` | Debugging tools, performance analysis | debug_cockpit.md, cockpit_performance_issues.md |
| 9 | `finetuning` | Model training, optimization | finetuning.md, llamacpp_optimization.md |
| 10 | `git-integration` | Git operations, version control | git.md |
| 11 | `knowledge-management` | Obsidian, Zettelkasten, note-taking | obsidian.md |
| 12 | `llm-configuration` | Model settings, prompts, tool configuration | prompts.md, tools_description.md, advanced_job_configuration.md |
| 13 | `orchestrator` | Job orchestration, MCP server, API | 01_issues.md, metamodel-compliance-architecture.md |
| 14 | `planning` | Strategic planning, task management, sprints | interactive_planning.md, sprints.md |
| 15 | `security` | Security, compliance, access control | security_checklist.md |
| 16 | `tool-development` | Tool creation, tool issues, patch tools | tool_issues.md, patch_tool.md, auxiliary_tasks.md |
| 17 | `user-interface` | Cockpit UI, web interface | cockpit_ds.md |
| 18 | `web-search` | Search tools, research capabilities | advanced_websearch.md |
| 19 | `writing` | Writing instructions, documentation | writing_instructions.md, deliverables.md |
| 20 | `latex` | LaTeX document processing | latex.md |
| 21 | `email` | Email integration, mobile access | email_and_mobile.md |
| 22 | `business` | Financing, business logic | financing.md |
| 23 | `media` | Media generation, podcasts | podcast_generation.md |
| 24 | `configuration` | Config management, settings issues | config_issues.md |
| 25 | `model-issues` | LLM model problems, limitations | model_issues.md |

## Tag Usage Guidelines

- **Format**: lowercase, hyphenated (e.g., `context-management`)
- **Frontmatter**: Add as YAML list under `tags:`
- **Aliases**: Use `aliases:` for alternative names (e.g., `["memory", "workspace"]`)
- **Related**: Add `related:` list with wiki-links to related documents

## Document Count by Tag

| Tag | Count |
|-----|-------|
| bug-fix | 8 |
| agent-architecture | 5 |
| tool-development | 4 |
| context-management | 3 |
| citation-engine | 3 |
| coding-tools | 3 |
| cloud-infrastructure | 2 |
| knowledge-management | 1 |
| planning | 2 |
| debugging | 2 |
| ... | ... |

*Full counts to be computed after tagging all documents*