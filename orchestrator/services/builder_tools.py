"""Artifact tool schemas and auto-summarization for the instruction builder.

The builder LLM receives these as tool/function definitions. When it calls them,
the backend emits SSE `tool_call` events that the frontend applies to the job form.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm.key_ring import KeyRing

logger = logging.getLogger(__name__)


# =============================================================================
# Artifact Tool Definitions (OpenAI function-calling format)
# =============================================================================

# Workspace edit tools — forwarded to frontend as proposals requiring user approval
WORKSPACE_EDIT_TOOLS = {"write_workspace_file", "edit_workspace_file"}

# Tools that are executed server-side (not forwarded to the frontend as artifact mutations)
SERVER_SIDE_TOOLS = {
    # Research
    "web_search",
    # Job inspection
    "list_jobs",
    "get_job",
    "get_job_progress",
    "get_workspace_file",
    "get_workspace_overview",
    "get_frozen_job",
    "get_todos",
    "get_chat_history",
    "get_job_requirements",
    # Git history
    "list_job_commits",
    "get_job_diff",
    "get_job_file",
    "list_job_files",
    "list_job_tags",
    # Monitoring
    "get_job_stats",
    "get_agent_stats",
    "get_stuck_jobs",
    "list_agents",
    "list_experts",
    "get_expert",
    "list_datasources",
    # Database inspection
    "list_tables",
    "query_table",
    "get_table_schema",
    # Execution debug
    "get_audit_trail",
    "get_audit_bulk",
    "get_chat_bulk",
    "get_graph_changes",
    "get_llm_request",
    "list_llm_requests",
    "search_audit",
    "get_job_log",
    "get_job_summary",
    "get_shell_state",
    "get_audit_timerange",
    # Todo archives
    "get_current_todos",
    "list_todo_archives",
    "get_todo_archive",
    # Citation & source library
    "list_job_sources",
    "get_source_detail",
    "list_job_citations",
    "get_citation_detail",
    "search_job_sources",
    "get_source_annotations",
    "get_source_tags",
    "get_citation_stats",
    "get_memory_stats",
    # Actions
    "approve_job",
    "resume_job_with_feedback",
    "cancel_job",
    "pause_job",
    "delete_job",
    "assign_job",
    "create_job",
    "create_follow_up_job",
    "test_datasource",
    "get_agent_system_info",
    # Knowledge base
    "get_knowledge_summary",
    "list_knowledge_notes",
    "get_knowledge_note",
    "search_knowledge",
    # Projects
    "list_projects",
    "get_project",
    "create_project",
    "list_project_jobs",
    "create_project_job",
    # Datasource CRUD
    "create_datasource",
    "update_datasource",
    "delete_datasource",
    # Knowledge mutations
    "update_knowledge_note",
    "delete_knowledge_note",
    "export_knowledge",
    # Job promotion
    "promote_job",
    # Minor tools
    "get_daily_stats",
    "reload_experts",
    "deregister_agent",
    # Project management (extended)
    "update_project",
    "delete_project",
    "list_project_members",
    "add_project_member",
    "update_project_member",
    "remove_project_member",
    "list_project_experts",
    "get_project_expert",
}

BUILDER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web to research a topic before writing instructions. "
                "Use this to learn about domain best practices, methodologies, and pitfalls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-10, default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_instructions",
            "description": (
                "Replace the full instructions content. Use this when making large changes "
                "or when starting fresh. The entire instructions.md will be overwritten."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The complete new instructions content (markdown)",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_instructions",
            "description": (
                "Find and replace text within the instructions. Use this for targeted edits "
                "like fixing a section, renaming terms, or adjusting specific parts. "
                "The old_text must match exactly (including whitespace)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find in the instructions",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text",
                    },
                },
                "required": ["old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_instructions",
            "description": (
                "Insert text at a specific line in the instructions, or append to the end. "
                "Use this for adding new sections or requirements without disturbing existing content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text to insert (markdown)",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Line number to insert at (1-indexed). Omit to append to end.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_config",
            "description": (
                "Update the agent configuration. Objects merge recursively, arrays replace entirely. "
                "Use this to change the model, temperature, reasoning level, tool availability, "
                "autonomy level, scholar/verification phases, or memory settings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "llm": {
                        "type": "object",
                        "description": "LLM settings to update",
                        "properties": {
                            "model": {"type": "string", "description": "Model name (e.g. 'gpt-4o', 'claude-sonnet-4-5-20250929')"},
                            "temperature": {"type": "number", "description": "Temperature (0.0 to 2.0)"},
                            "reasoning_level": {"type": "string", "description": "Reasoning level (low, medium, high)"},
                            "strategic": {
                                "type": "object",
                                "description": "Overrides for strategic (planning) phases",
                                "properties": {
                                    "model": {"type": "string"},
                                    "temperature": {"type": "number"},
                                    "reasoning_level": {"type": "string", "enum": ["low", "medium", "high"]},
                                },
                            },
                            "tactical": {
                                "type": "object",
                                "description": "Overrides for tactical (execution) phases",
                                "properties": {
                                    "model": {"type": "string"},
                                    "temperature": {"type": "number"},
                                    "reasoning_level": {"type": "string", "enum": ["low", "medium", "high"]},
                                },
                            },
                        },
                    },
                    "tools": {
                        "type": "object",
                        "description": "Tool category overrides. Set a category to [] to disable it.",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "scholar": {
                        "type": "object",
                        "description": "Scholar (research phase) settings. The scholar runs before the main job to gather background information.",
                        "properties": {
                            "enabled": {
                                "type": "boolean",
                                "description": "Enable/disable the scholar research phase (default: true)",
                            },
                        },
                    },
                    "verification": {
                        "type": "object",
                        "description": "Verification (critic) phase settings. The critic reviews deliverables after job completion.",
                        "properties": {
                            "enabled": {
                                "type": "boolean",
                                "description": "Enable/disable the critic verification phase (default: true)",
                            },
                            "max_rounds": {
                                "type": "integer",
                                "description": "Max feedback round-trips before auto-accepting (0 = unlimited, default: 5)",
                            },
                        },
                    },
                    "autonomy": {
                        "type": "string",
                        "enum": ["full", "review", "partial", "guided", "dependent"],
                        "description": (
                            "Controls when the agent pauses for human review. "
                            "full=never pauses, review=pauses at completion, "
                            "partial=pauses at phase boundaries + completion, "
                            "guided=pauses after every tactical phase, "
                            "dependent=pauses after every phase"
                        ),
                    },
                    "memory": {
                        "type": "object",
                        "description": "Memory settings for cross-context recall",
                        "properties": {
                            "project_scoped": {
                                "type": "boolean",
                                "description": "When true, memories are shared with other jobs in the same project",
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_description",
            "description": (
                "Replace the job description. Use this when the user wants to change "
                "what the agent should accomplish."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The new job description",
                    },
                },
                "required": ["content"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Workspace Edit Tools (forwarded to frontend as proposals)
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": (
                "Write or overwrite a file in a job's workspace. Requires user approval. "
                "Use this to adjust plan.md, workspace.md, or other workspace files on "
                "frozen/paused jobs. Do NOT edit todos.yaml (it is managed internally)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'plan.md', 'workspace.md')",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full file content to write",
                    },
                },
                "required": ["job_id", "path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_workspace_file",
            "description": (
                "Find and replace text within a workspace file. Requires user approval. "
                "The old_text must match exactly. Use for targeted edits to plan.md, "
                "workspace.md, etc. Do NOT edit todos.yaml."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'plan.md', 'workspace.md')",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find in the file",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text",
                    },
                },
                "required": ["job_id", "path", "old_text", "new_text"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Job Inspection & Action Tools (server-side, loopback to orchestrator API)
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": (
                "List recent jobs. Use this when the user asks about their jobs, "
                "recent work, or wants to find a specific job."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["created", "queued", "running", "completed", "failed", "cancelled", "pending_review"],
                        "description": "Filter by job status",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max jobs to return (default 10)",
                        "default": 10,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job",
            "description": (
                "Get detailed information about a specific job including its status, "
                "config, description, and timestamps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_progress",
            "description": (
                "Get detailed progress for a job including phase, todo completion, "
                "and timing information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_file",
            "description": (
                "Read a file from a job's workspace. Common files: workspace.md (agent memory), "
                "plan.md (strategic plan), todos.yaml (current tasks), "
                "archive/phase_N_retrospective.md (phase reviews)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'workspace.md', 'plan.md', 'archive/phase_1_retrospective.md')",
                    },
                },
                "required": ["job_id", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace_overview",
            "description": (
                "Get a high-level overview of a job's workspace including file listing, "
                "truncated workspace.md/plan.md content, current todos, and archive count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_frozen_job",
            "description": (
                "Get the frozen job data (summary, deliverables, confidence) for a job "
                "that is pending review. Use this to understand what the agent produced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todos",
            "description": (
                "Get all todos for a job including current active todos and archived phases. "
                "Shows task planning and execution progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_history",
            "description": (
                "Get the agent's conversation history for a job. Shows LLM input/response pairs. "
                "Use page=-1 to get the most recent messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-indexed, -1 for last page)",
                        "default": -1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Entries per page (default 10)",
                        "default": 10,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_job",
            "description": (
                "Approve a frozen job that is pending review. This marks the job as completed. "
                "Only works on jobs with status 'pending_review'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to approve",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_job_with_feedback",
            "description": (
                "Resume a failed or frozen job, optionally with feedback for the agent. "
                "The feedback is injected into the agent's context before resuming."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to resume",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "Optional feedback or instructions for the agent",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_follow_up_job",
            "description": (
                "Create a new job. Use this to start follow-up work or create jobs based on "
                "the user's request. Returns the new job ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What the agent should accomplish",
                    },
                    "config_name": {
                        "type": "string",
                        "description": "Agent config to use (e.g. 'default', 'scholar', 'developer')",
                        "default": "default",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Additional instructions for the agent",
                    },
                },
                "required": ["description"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Job Requirements
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_job_requirements",
            "description": (
                "Get extracted requirements for a job with their validation status, "
                "priority, and metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "validating", "integrated", "rejected", "failed"],
                        "description": "Filter by validation status",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 100)",
                        "default": 100,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Git History Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_job_commits",
            "description": (
                "List git commits for a job's repository. Use since_ref to see only "
                "commits after a specific phase tag (e.g. 'phase_2_end')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Branch or tag to list from (default: main)",
                        "default": "main",
                    },
                    "since_ref": {
                        "type": "string",
                        "description": "Only show commits after this ref (e.g. 'phase_2_end')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max commits to return (default 20)",
                        "default": 20,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_diff",
            "description": (
                "Show the diff between two git refs in a job's repository. "
                "Use base='phase_2_end' to see what changed in phase 3."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "base": {
                        "type": "string",
                        "description": "Base ref (commit SHA, tag, or branch)",
                    },
                    "head": {
                        "type": "string",
                        "description": "Head ref (default: HEAD)",
                        "default": "HEAD",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Filter diff to a specific file",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Truncate diff beyond this limit (default 50000)",
                        "default": 50000,
                    },
                },
                "required": ["job_id", "base"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_file",
            "description": (
                "Read a file from a job's Gitea repo at any ref. "
                "Use ref='phase_2_end' to see the file at the end of phase 2."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path within the repo (e.g. 'workspace.md', 'output/report.md')",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Branch, tag, or commit SHA (default: HEAD)",
                    },
                },
                "required": ["job_id", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_job_files",
            "description": (
                "Browse the repository directory tree at any ref. "
                "Lists files and directories at a given path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: root)",
                        "default": "",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Branch, tag, or commit SHA (default: HEAD)",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_job_tags",
            "description": (
                "List phase tags for a job (e.g. a1b2c3d4-phase-1-tactical-complete). "
                "Tags are namespaced by job short ID. Use tag names as refs in other git tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Monitoring & System Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_job_stats",
            "description": "Get job queue statistics — total jobs and counts per status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_stats",
            "description": "Get agent workforce summary — total agents and counts per status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stuck_jobs",
            "description": (
                "Get jobs stuck in processing beyond a threshold. "
                "A job is stuck if it hasn't been updated within the threshold period."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "threshold_minutes": {
                        "type": "integer",
                        "description": "Minutes after which a job is considered stuck (default 30)",
                        "default": 30,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List registered agents with status, config, hostname, and current job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["booting", "ready", "working", "completed", "failed", "offline"],
                        "description": "Filter by agent status",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_experts",
            "description": "List available expert/agent configurations with descriptions and tags.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_expert",
            "description": (
                "Get full detail for an expert config including merged config, "
                "system prompt, tool list, and instructions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expert_id": {
                        "type": "string",
                        "description": "Expert config ID (e.g. 'default', 'scholar', 'developer')",
                    },
                },
                "required": ["expert_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_datasources",
            "description": "List configured datasources with type, connection info, and scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ds_type": {
                        "type": "string",
                        "enum": ["postgresql", "neo4j", "mongodb"],
                        "description": "Filter by datasource type",
                    },
                },
            },
        },
    },
    # -----------------------------------------------------------------
    # Database Inspection Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "List all database tables with row counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_table",
            "description": "Get paginated data from a database table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Table name (e.g. 'jobs', 'requirements', 'citations')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Rows per page (default 50, max 500)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_schema",
            "description": "Get column definitions for a database table — names, types, nullable flags, and defaults.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Table name",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Execution Debug Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_audit_trail",
            "description": (
                "Get paginated audit entries for a job's execution. "
                "Shows LLM messages, tool calls, and errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (1-indexed, -1 for last page)",
                        "default": 1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Entries per page (max 200, default 20)",
                        "default": 20,
                    },
                    "filter": {
                        "type": "string",
                        "enum": ["all", "messages", "tools", "errors"],
                        "description": "Filter category (default: all)",
                        "default": "all",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_graph_changes",
            "description": (
                "Get timeline of Neo4j graph mutations for a job. "
                "Shows Cypher queries, nodes/relationships created/modified/deleted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_llm_request",
            "description": (
                "Get full LLM request/response by MongoDB document ID. "
                "Returns complete message history, model response, and token usage. "
                "Use document IDs from audit trail entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "MongoDB ObjectId (24 hex characters)",
                    },
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_audit",
            "description": (
                "Search audit entries by content pattern. "
                "Searches across message content, tool names, and arguments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to search within",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search string (case-insensitive substring match)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20, max 100)",
                        "default": 20,
                    },
                },
                "required": ["job_id", "query"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Citation & Source Library Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "list_job_sources",
            "description": (
                "List sources registered by a job (documents, websites, databases). "
                "Omit job_id to query across all jobs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID (omit to query across all jobs)",
                    },
                    "source_type": {
                        "type": "string",
                        "enum": ["document", "website", "database", "custom"],
                        "description": "Filter by source type",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source_detail",
            "description": "Get full detail for a source including content, metadata, and content hash.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "integer",
                        "description": "Source ID",
                    },
                    "content_limit": {
                        "type": "integer",
                        "description": "Max characters of content to return (default 2000, 0 for full)",
                        "default": 2000,
                    },
                },
                "required": ["source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_job_citations",
            "description": "List all citations for a job with verification status and confidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "source_id": {
                        "type": "integer",
                        "description": "Filter by source ID",
                    },
                    "verification_status": {
                        "type": "string",
                        "enum": ["pending", "verified", "failed", "unverified"],
                        "description": "Filter by verification status",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50)",
                        "default": 50,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_citation_detail",
            "description": "Get full citation record with source info, verification details, and locator.",
            "parameters": {
                "type": "object",
                "properties": {
                    "citation_id": {
                        "type": "integer",
                        "description": "Citation ID",
                    },
                },
                "required": ["citation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_job_sources",
            "description": (
                "Search a job's source library using keyword search. "
                "Returns results with evidence labels (HIGH/MEDIUM/LOW) and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query or keywords",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Search mode (default: keyword)",
                        "default": "keyword",
                    },
                    "source_type": {
                        "type": "string",
                        "description": "Filter by source type",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags (AND logic)",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["job_id", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source_annotations",
            "description": "Get annotations (notes, highlights, summaries, questions, critiques) for a source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "source_id": {
                        "type": "integer",
                        "description": "Source ID",
                    },
                    "annotation_type": {
                        "type": "string",
                        "enum": ["note", "highlight", "summary", "question", "critique"],
                        "description": "Filter by annotation type",
                    },
                },
                "required": ["job_id", "source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source_tags",
            "description": "Get tags assigned to a source within a job.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "source_id": {
                        "type": "integer",
                        "description": "Source ID",
                    },
                },
                "required": ["job_id", "source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_citation_stats",
            "description": "Get citation statistics for a job — counts by verification status, source type, and confidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_stats",
            "description": "Get memory statistics for a job — counts by type (factual, procedural, etc.), source channel (observer, todo, etc.), tokens, accesses, and average importance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    # -----------------------------------------------------------------
    # Additional Action Tools
    # -----------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "cancel_job",
            "description": (
                "Cancel a running job. Sends a cancel signal to the agent. "
                "In-progress work may be lost."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to cancel",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_job",
            "description": (
                "Permanently delete a job and its associated data. "
                "WARNING: This action is irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to delete",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_job",
            "description": (
                "Assign a created job to a ready agent. The job must be in 'created' "
                "or 'failed' status, and the agent must be 'ready'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID to assign",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "The agent UUID to assign to",
                    },
                },
                "required": ["job_id", "agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_job",
            "description": (
                "Create a new job with full configuration options including datasources, "
                "config overrides, and context. For simple jobs, use create_follow_up_job instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Natural language task description",
                    },
                    "config_name": {
                        "type": "string",
                        "description": "Expert/agent config to use (default: 'default')",
                        "default": "default",
                    },
                    "datasource_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Global datasource UUIDs to clone as job-scoped",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Additional inline markdown instructions",
                    },
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_datasource",
            "description": "Test connectivity to a datasource. Supports PostgreSQL, Neo4j, and MongoDB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "datasource_id": {
                        "type": "string",
                        "description": "Datasource UUID to test",
                    },
                },
                "required": ["datasource_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_system_info",
            "description": (
                "Get system information from an agent's container — CPU, memory, disk, "
                "listening ports, top processes, and network connections."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent UUID",
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_job",
            "description": (
                "Pause a running job. Sends a graceful pause request to the agent. "
                "The agent finishes its current node, saves a checkpoint, and becomes available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID to pause",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_audit_bulk",
            "description": (
                "Get bulk audit entries using offset/limit pagination. "
                "Better than page-based for scanning large histories. Up to 500 per request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of entries to skip (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (max 500, default 500)",
                        "default": 500,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_bulk",
            "description": (
                "Get bulk chat history using offset/limit pagination. "
                "Better than page-based for scanning large conversations. Up to 500 per request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of entries to skip (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return (max 500, default 500)",
                        "default": 500,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_summary",
            "description": (
                "Get a comprehensive one-shot summary of a job. "
                "Fetches status, progress, todos, workspace overview, and recent tool "
                "calls in parallel — everything in a single response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_log",
            "description": (
                "Read the tail of a job's log file with optional filtering by "
                "log level and/or grep pattern. Useful for diagnosing agent errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of tail lines (max 1000, default 100)",
                        "default": 100,
                    },
                    "grep": {
                        "type": "string",
                        "description": "Case-insensitive substring filter",
                    },
                    "level": {
                        "type": "string",
                        "description": "Log level filter (DEBUG, INFO, WARNING, ERROR)",
                        "enum": ["DEBUG", "INFO", "WARNING", "ERROR"],
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_llm_requests",
            "description": (
                "List LLM requests for a job with model, timestamp, token usage, "
                "and tool call names. Use doc_id with get_llm_request for full details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries (max 100, default 20)",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shell_state",
            "description": (
                "Get shell tab state from the agent processing a job. "
                "Returns open terminal tabs with their type and recent output. "
                "Requires the job to be actively processing on an agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID (must be in 'processing' status)",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    # ---- Todo Archives & Current Todos ----
    {
        "type": "function",
        "function": {
            "name": "get_current_todos",
            "description": (
                "Get only the current active todos from todos.yaml. "
                "Lighter than get_todos which includes all archives."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todo_archives",
            "description": (
                "List all archived todo files for a job. "
                "Returns metadata for each phase archive (filename, phase name, timestamp). "
                "Use get_todo_archive to read the full content of a specific archive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todo_archive",
            "description": (
                "Get the full content of an archived todo file for a specific phase. "
                "Use list_todo_archives first to get available filenames."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Archive filename from list_todo_archives (e.g. 'todos_phase1_20260124_183618.md')",
                    },
                },
                "required": ["job_id", "filename"],
            },
        },
    },
    # ---- Audit Time Range ----
    {
        "type": "function",
        "function": {
            "name": "get_audit_timerange",
            "description": (
                "Get the first and last timestamps for a job's audit entries. "
                "Quick way to see when a job started and last had activity. "
                "Requires MongoDB to be available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job UUID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    # ---- Knowledge Base ----
    {
        "type": "function",
        "function": {
            "name": "get_knowledge_summary",
            "description": (
                "Get knowledge base summary statistics for a project. "
                "Shows total notes, counts by type and status, and recent notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_notes",
            "description": (
                "List knowledge notes for a project with optional filters. "
                "Shows note previews with type, status, tags, and confidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "note_type": {
                        "type": "string",
                        "description": "Filter by note type (insight, decision, pattern, issue, etc.)",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status (active, resolved, superseded, archived)",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Filter by originating job UUID",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-200, default 50)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_knowledge_note",
            "description": (
                "Get a single knowledge note with full content and Neo4j relationships. "
                "Use list_knowledge_notes or search_knowledge to find note IDs first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "note_id": {
                        "type": "string",
                        "description": "Note ID",
                    },
                },
                "required": ["project_id", "note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search the project knowledge base using hybrid search "
                "(dense vector + sparse keyword). Falls back to keyword-only "
                "when embeddings are unavailable. Use this to find relevant "
                "knowledge from previous jobs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-50, default 10)",
                        "default": 10,
                    },
                },
                "required": ["project_id", "query"],
            },
        },
    },
    # ---- Projects ----
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": (
                "List projects. Optionally filter by user membership. "
                "Shows project name, status, goal, and last update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Filter to projects this user belongs to (optional)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project",
            "description": (
                "Get full details for a specific project including "
                "name, description, goal, and default config."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": (
                "Create a new project. Projects organize related jobs, "
                "accumulate knowledge, and can have team members."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Project name",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Owner user UUID",
                    },
                    "description": {
                        "type": "string",
                        "description": "Project description (optional)",
                    },
                    "goal": {
                        "type": "string",
                        "description": "Project goal statement (optional)",
                    },
                    "default_config_name": {
                        "type": "string",
                        "description": "Default expert config for new jobs (optional)",
                    },
                },
                "required": ["name", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_jobs",
            "description": (
                "List jobs belonging to a project. "
                "Optionally filter by status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status (created, processing, completed, failed, etc.)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-500, default 100)",
                        "default": 100,
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project_job",
            "description": (
                "Create a job within a project context. Uses the project's "
                "default config if not overridden. The job is automatically "
                "linked to the project and gets a Gitea branch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "description": {
                        "type": "string",
                        "description": "Task description",
                    },
                    "config_name": {
                        "type": "string",
                        "description": "Expert/agent config (default: uses project default or 'default')",
                        "default": "default",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Additional inline instructions (optional)",
                    },
                    "config_override": {
                        "type": "object",
                        "description": "Per-job config overrides (optional)",
                    },
                    "datasource_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Global datasource IDs to clone (optional)",
                    },
                },
                "required": ["project_id", "description"],
            },
        },
    },
    # ---- Datasource CRUD ----
    {
        "type": "function",
        "function": {
            "name": "create_datasource",
            "description": (
                "Create a new datasource (PostgreSQL, Neo4j, or MongoDB). "
                "Use job_id=null for global datasources available to all jobs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "User-provided label for the datasource",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["postgresql", "neo4j", "mongodb"],
                        "description": "Datasource type",
                    },
                    "connection_url": {
                        "type": "string",
                        "description": "Full connection string (e.g. postgresql://user:pass@host:5432/db)",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this datasource contains (optional)",
                    },
                    "credentials": {
                        "type": "object",
                        "description": "Additional auth details, e.g. {\"username\": \"neo4j\", \"password\": \"...\"} (optional)",
                    },
                    "read_only": {
                        "type": "boolean",
                        "description": "Whether the agent is restricted to read-only access (default true)",
                        "default": True,
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID for job-scoped datasource (omit for global)",
                    },
                },
                "required": ["name", "type", "connection_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_datasource",
            "description": (
                "Update an existing datasource's connection details, "
                "name, description, or read-only flag."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "datasource_id": {
                        "type": "string",
                        "description": "Datasource UUID",
                    },
                    "name": {
                        "type": "string",
                        "description": "New label (optional)",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                    "connection_url": {
                        "type": "string",
                        "description": "New connection string (optional)",
                    },
                    "credentials": {
                        "type": "object",
                        "description": "New auth details (optional)",
                    },
                    "read_only": {
                        "type": "boolean",
                        "description": "New read-only flag (optional)",
                    },
                },
                "required": ["datasource_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_datasource",
            "description": (
                "Permanently delete a datasource. This does not affect "
                "jobs that have already cloned it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "datasource_id": {
                        "type": "string",
                        "description": "Datasource UUID",
                    },
                },
                "required": ["datasource_id"],
            },
        },
    },
    # ---- Knowledge Mutations ----
    {
        "type": "function",
        "function": {
            "name": "update_knowledge_note",
            "description": (
                "Update a knowledge note's status or tags. "
                "Use to mark notes as resolved, superseded, or archived, "
                "or to organize notes with tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "note_id": {
                        "type": "string",
                        "description": "Note ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "resolved", "superseded", "archived"],
                        "description": "New status (optional)",
                    },
                    "add_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to add (optional)",
                    },
                    "remove_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to remove (optional)",
                    },
                },
                "required": ["project_id", "note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_knowledge_note",
            "description": (
                "Permanently delete a knowledge note from both PostgreSQL "
                "and Neo4j. This is irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "note_id": {
                        "type": "string",
                        "description": "Note ID",
                    },
                },
                "required": ["project_id", "note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_knowledge",
            "description": (
                "Export a project's knowledge base as Obsidian-compatible "
                "markdown files. Each note becomes a .md file with YAML "
                "frontmatter and wikilink relationships."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    # ---- Job Promotion ----
    {
        "type": "function",
        "function": {
            "name": "promote_job",
            "description": (
                "Promote a completed job into a dedicated project. "
                "Creates a new project, seeds its repo from the job's "
                "branch (preserving git history), and moves the job. "
                "The job must be completed and in a default project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job UUID (must be completed)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the new project",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Owner user UUID",
                    },
                    "description": {
                        "type": "string",
                        "description": "Project description (optional)",
                    },
                    "goal": {
                        "type": "string",
                        "description": "Project goal (optional)",
                    },
                },
                "required": ["job_id", "name", "user_id"],
            },
        },
    },
    # ---- Project Management (Extended) ----
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": (
                "Update a project's name, description, goal, status, "
                "or default config settings. Only provided fields are changed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "name": {
                        "type": "string",
                        "description": "New project name (optional)",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional)",
                    },
                    "goal": {
                        "type": "string",
                        "description": "New goal statement (optional)",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (optional)",
                    },
                    "default_config_name": {
                        "type": "string",
                        "description": "Default agent config for new jobs (optional)",
                    },
                    "default_config_override": {
                        "type": "object",
                        "description": "Default config overrides for new jobs (optional)",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_project",
            "description": (
                "Permanently delete a project and its associated data. "
                "Cannot delete default projects. This is irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID to delete",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_members",
            "description": (
                "List all members of a project with their roles "
                "(owner, editor, viewer) and profile info."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_project_member",
            "description": (
                "Add a user to a project with a specified role. "
                "Returns 409 if the user is already a member."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "User UUID to add",
                    },
                    "role": {
                        "type": "string",
                        "description": "Member role: owner, editor, or viewer (default: editor)",
                        "default": "editor",
                    },
                },
                "required": ["project_id", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_member",
            "description": (
                "Change a project member's role. "
                "Valid roles: owner, editor, viewer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "User UUID of the member to update",
                    },
                    "role": {
                        "type": "string",
                        "description": "New role: owner, editor, or viewer",
                    },
                },
                "required": ["project_id", "user_id", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_project_member",
            "description": (
                "Remove a member from a project. "
                "Cannot remove the last owner of a project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "User UUID to remove",
                    },
                },
                "required": ["project_id", "user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_experts",
            "description": (
                "List project-specific expert configurations stored "
                "in the project's Gitea repository."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_expert",
            "description": (
                "Get detailed configuration for a project-specific expert, "
                "including merged config and instructions content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project UUID",
                    },
                    "expert_name": {
                        "type": "string",
                        "description": "Expert config name (e.g. 'scholar', 'developer')",
                    },
                },
                "required": ["project_id", "expert_name"],
            },
        },
    },
    # ---- Minor Tools ----
    {
        "type": "function",
        "function": {
            "name": "get_daily_stats",
            "description": (
                "Get daily job statistics (created, completed, failed, cancelled) "
                "for the past N days. Useful for tracking system activity trends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (1-90, default 7)",
                        "default": 7,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_experts",
            "description": (
                "Force reload of expert configurations from disk. "
                "Use after modifying expert YAML files to pick up changes "
                "without restarting the orchestrator."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deregister_agent",
            "description": (
                "Remove an agent from the system. "
                "Use for cleaning up agents that are offline or no longer needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent UUID to deregister",
                    },
                },
                "required": ["agent_id"],
            },
        },
    },
]


# =============================================================================
# Auto-Summarization
# =============================================================================

SUMMARY_SYSTEM_PROMPT = """You are a conversation summarizer. Condense the following chat messages into a brief summary that preserves:
- Key decisions made about the instructions/configuration
- Important context the user provided about their use case
- Any constraints or preferences expressed

Be concise (2-4 paragraphs). Focus on information the AI will need to continue the conversation coherently."""


def estimate_token_count(text: str) -> int:
    """Rough token estimate (4 chars per token)."""
    return len(text) // 4


def build_message_context(
    messages: list[dict[str, Any]],
    summary: str | None = None,
    max_context_tokens: int = 6000,
) -> tuple[list[dict[str, str]], bool]:
    """Build conversation context from messages, respecting token budget.

    Returns a tuple of (context_messages, needs_summarization).
    If the messages exceed the budget, returns only recent ones and signals
    that older messages should be summarized.

    Args:
        messages: All session messages in chronological order
        summary: Existing summary of older messages (if any)
        max_context_tokens: Token budget for conversation history

    Returns:
        Tuple of (messages for LLM context, whether summarization is needed)
    """
    context: list[dict[str, str]] = []
    needs_summarization = False

    # Start with summary if available
    if summary:
        context.append({
            "role": "system",
            "content": f"Summary of earlier conversation:\n{summary}",
        })

    # Calculate total token cost of all messages
    total_tokens = sum(
        estimate_token_count(m.get("content") or "") + estimate_token_count(
            json.dumps(m.get("tool_calls") or [])
        )
        for m in messages
    )

    if total_tokens <= max_context_tokens:
        # All messages fit
        for m in messages:
            content = m.get("content") or ""
            if m.get("tool_calls"):
                content += f"\n[Tool calls: {json.dumps(m['tool_calls'])}]"
            context.append({"role": m["role"], "content": content})
    else:
        # Need to trim — keep recent messages, signal summarization needed
        needs_summarization = True
        running_tokens = 0
        recent: list[dict[str, str]] = []

        for m in reversed(messages):
            msg_tokens = estimate_token_count(m.get("content") or "") + estimate_token_count(
                json.dumps(m.get("tool_calls") or [])
            )
            if running_tokens + msg_tokens > max_context_tokens:
                break
            content = m.get("content") or ""
            if m.get("tool_calls"):
                content += f"\n[Tool calls: {json.dumps(m['tool_calls'])}]"
            recent.append({"role": m["role"], "content": content})
            running_tokens += msg_tokens

        context.extend(reversed(recent))

    return context, needs_summarization


def build_summarization_prompt(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build a prompt to summarize older messages.

    Args:
        messages: Messages to summarize

    Returns:
        Messages formatted for the summarization LLM call
    """
    conversation_text = "\n".join(
        f"[{m['role']}]: {m.get('content') or ''}"
        + (f" [tools: {json.dumps(m.get('tool_calls') or [])}]" if m.get("tool_calls") else "")
        for m in messages
    )

    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": conversation_text},
    ]


# =============================================================================
# LLM Provider Configuration
# =============================================================================

def get_builder_model() -> str:
    """Get the model name for the builder LLM."""
    return os.getenv("BUILDER_MODEL", "openai/gpt-oss-120b")


_builder_key_rings: dict[str, KeyRing | None] = {}
_builder_key_ring_lock = __import__("threading").Lock()


def _raw_key_for_provider(provider: str) -> str | None:
    """Return the raw (possibly comma-separated) key string for a provider.

    Resolution order:
    1. BUILDER_ANTHROPIC_API_KEY / BUILDER_OPENAI_API_KEY (provider-specific builder key)
    2. BUILDER_API_KEY (generic builder key)
    3. ANTHROPIC_API_KEY / OPENAI_API_KEY (global provider key)
    """
    if provider == "anthropic":
        return (
            os.getenv("BUILDER_ANTHROPIC_API_KEY")
            or os.getenv("BUILDER_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
        )
    return (
        os.getenv("BUILDER_OPENAI_API_KEY")
        or os.getenv("BUILDER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )


def get_builder_key_ring(provider: str | None = None):
    """Get or create a shared KeyRing for the builder LLM.

    Creates a separate KeyRing per provider so that OpenAI and Anthropic keys
    are rotated independently.

    Args:
        provider: "openai" or "anthropic". Defaults to get_builder_provider().
    """
    if provider is None:
        provider = get_builder_provider()

    with _builder_key_ring_lock:
        if provider in _builder_key_rings:
            return _builder_key_rings[provider]

        try:
            from src.llm.key_ring import KeyRing, parse_key_string
        except (ImportError, ModuleNotFoundError):
            # When running from orchestrator/ dir, resolve repo root
            try:
                import importlib.util
                from pathlib import Path
                _kr_path = Path(__file__).resolve().parent.parent.parent / "src" / "llm" / "key_ring.py"
                spec = importlib.util.spec_from_file_location("_key_ring", str(_kr_path))
                if spec and spec.loader:
                    _mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(_mod)
                    KeyRing = _mod.KeyRing
                    parse_key_string = _mod.parse_key_string
                else:
                    raise ImportError("spec not found")
            except Exception:
                _builder_key_rings[provider] = None
                return None

        raw = _raw_key_for_provider(provider)
        keys = parse_key_string(raw)
        if not keys:
            _builder_key_rings[provider] = None
            return None

        cooldown = float(os.getenv("KEY_COOLDOWN_SECONDS", "1800"))
        ring = KeyRing(
            keys, cooldown_seconds=cooldown, provider=f"builder-{provider}"
        )
        _builder_key_rings[provider] = ring
        return ring


def get_builder_api_key(provider: str | None = None) -> str | None:
    """Get the current API key for the builder LLM.

    Uses KeyRing for automatic rotation across comma-separated keys.
    Falls back to env vars with first-key extraction.

    Args:
        provider: "openai" or "anthropic". Defaults to get_builder_provider().
    """
    if provider is None:
        provider = get_builder_provider()

    ring = get_builder_key_ring(provider)
    if ring is not None:
        try:
            return ring.current_key
        except RuntimeError:
            pass

    # Fallback: read directly from env, taking the first comma-separated key
    raw = _raw_key_for_provider(provider)
    if raw:
        return raw.split(",")[0].strip()
    return None


def rotate_builder_key(reason: str, provider: str | None = None) -> str | None:
    """Rotate the builder API key after a failure. Returns new key or None.

    Args:
        reason: Why the key failed (for logging).
        provider: "openai" or "anthropic". Defaults to get_builder_provider().
    """
    if provider is None:
        provider = get_builder_provider()
    ring = get_builder_key_ring(provider)
    if ring is None or not ring.has_alternatives:
        return None
    return ring.rotate(reason)


def is_auth_or_quota_error(error: Exception) -> bool:
    """Check if an exception is an auth (401/403) or quota (429) error."""
    error_str = str(error)
    # Check for HTTP status codes in error messages
    for code in ("401", "403", "429", "Incorrect API key", "invalid_api_key",
                 "quota", "rate_limit"):
        if code.lower() in error_str.lower():
            return True
    # Check for typed SDK exceptions
    type_name = type(error).__name__
    if type_name in ("AuthenticationError", "PermissionDeniedError",
                     "RateLimitError"):
        return True
    return False


def get_builder_base_url() -> str | None:
    """Get the base URL for the builder LLM.

    Falls back to OPENAI_BASE_URL for OpenAI-compatible providers.
    Anthropic doesn't use a base URL override.
    """
    explicit = os.getenv("BUILDER_BASE_URL")
    if explicit:
        return explicit
    provider = get_builder_provider()
    if provider == "anthropic":
        return None
    return os.getenv("OPENAI_BASE_URL")


def get_builder_provider() -> str:
    """Detect the LLM provider for the builder.

    If BUILDER_LLM_PROVIDER is set, use that.
    Otherwise auto-detect from model name.
    """
    explicit = os.getenv("BUILDER_LLM_PROVIDER")
    if explicit:
        return explicit.lower()

    model = get_builder_model()
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("codex/"):
        return "openai"  # Codex proxy is OpenAI-compatible
    return "openai"
