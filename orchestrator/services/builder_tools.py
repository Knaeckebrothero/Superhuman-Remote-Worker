"""Artifact tool schemas and auto-summarization for the instruction builder.

The builder LLM receives these as tool/function definitions. When it calls them,
the backend emits SSE `tool_call` events that the frontend applies to the job form.
"""

import json
import logging
import os
from typing import Any

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
    "get_graph_changes",
    "get_llm_request",
    "search_audit",
    # Citation & source library
    "list_job_sources",
    "get_source_detail",
    "list_job_citations",
    "get_citation_detail",
    "search_job_sources",
    "get_source_annotations",
    "get_source_tags",
    "get_citation_stats",
    # Actions
    "approve_job",
    "resume_job_with_feedback",
    "cancel_job",
    "delete_job",
    "assign_job",
    "create_job",
    "create_follow_up_job",
    "test_datasource",
    "get_agent_system_info",
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
                "Use this to change the model, temperature, reasoning level, or tool availability."
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
                "List phase tags for a job (phase_1_start, phase_1_end, etc.). "
                "Use these tag names as refs in other git tools."
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
    return os.getenv("BUILDER_MODEL", "gpt-5.2-pro")


def get_builder_api_key() -> str | None:
    """Get the API key for the builder LLM.

    Falls back to OPENAI_API_KEY or ANTHROPIC_API_KEY based on provider.
    """
    explicit = os.getenv("BUILDER_API_KEY")
    if explicit:
        return explicit
    provider = get_builder_provider()
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY")
    return os.getenv("OPENAI_API_KEY")


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
    return "openai"
