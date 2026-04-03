"""FastAPI application for Persistent Agent mode.

Provides WebSocket endpoint for interactive sessions. Completely separate
from app.py (worker mode) — own globals, own lifespan, no shared state.

Start with: python agent.py --mode persistent --thread-id <uuid> --port 8001
Connect with: websocat ws://localhost:8001/ws/chat
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .orchestrator_client import OrchestratorClient, create_orchestrator_client_from_env
from .persistent_session import PersistentSession
from ..agent import UniversalAgent
from ..persistent_graph import (
    APPROVE_SENTINEL,
    DENY_SENTINEL,
    IdleTimeoutError,
    PersistentLoopCallbacks,
    run_persistent_loop,
)

logger = logging.getLogger(__name__)

# --- Module globals (session-scoped, not shared with worker app.py) ---
_agent: Optional[UniversalAgent] = None
_session: Optional[PersistentSession] = None
_config_path: Optional[str] = None
_thread_id: Optional[str] = None
_orchestrator_client: Optional[OrchestratorClient] = None
_heartbeat_task: Optional[asyncio.Task] = None
_started_at: Optional[datetime] = None


def _get_agent_metrics() -> Optional[Dict[str, Any]]:
    """Collect metrics for heartbeat."""
    try:
        import psutil

        proc = psutil.Process()
        return {
            "memory_mb": round(proc.memory_info().rss / 1_048_576, 1),
            "cpu_percent": proc.cpu_percent(interval=0),
        }
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize persistent agent, register with orchestrator, start heartbeat."""
    global \
        _agent, \
        _session, \
        _orchestrator_client, \
        _heartbeat_task, \
        _started_at, \
        _thread_id

    _started_at = datetime.now()
    logger.info(
        f"Starting persistent agent: config={_config_path}, thread={_thread_id}"
    )

    # 1. Create and initialize UniversalAgent (same as worker)
    _agent = UniversalAgent.from_config(_config_path)
    await _agent.initialize()

    # 2. Connect to orchestrator and auto-create thread if needed
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    if orchestrator_url:
        try:
            _orchestrator_client = create_orchestrator_client_from_env(
                _agent.config.agent_id
            )
            await _orchestrator_client.connect()

            # Auto-create thread if none was provided
            if _thread_id is None:
                created_id = await _orchestrator_client.create_thread(
                    config_name=_config_path or "interactive",
                    permission_mode=_agent.config.interactive.permission_mode,
                    title=f"Local Session ({_config_path or 'interactive'})",
                )
                if created_id:
                    _thread_id = created_id
                    logger.info(f"Auto-created thread: {_thread_id}")
                else:
                    logger.warning("Failed to create thread — generating local UUID")

            # Register with the (now known) thread_id
            if _thread_id is None:
                import uuid

                _thread_id = str(uuid.uuid4())

            await _orchestrator_client.register(
                agent_mode="persistent",
                thread_id=_thread_id,
            )

            # Start heartbeat
            _heartbeat_task = asyncio.create_task(
                _orchestrator_client.run_heartbeat_loop(
                    get_status=lambda: "ready",
                    get_job_id=lambda: None,
                    get_metrics=_get_agent_metrics,
                )
            )
            logger.info("Registered with orchestrator as persistent agent")
        except Exception as e:
            logger.warning(f"Failed to register with orchestrator (non-fatal): {e}")
            _orchestrator_client = None
    else:
        logger.info("No ORCHESTRATOR_URL — running standalone")

    # Fallback: generate UUID if still None (standalone mode)
    if _thread_id is None:
        import uuid

        _thread_id = str(uuid.uuid4())

    # 2b. Wait for workspace container (if orchestrator is provisioning one)
    workspace_override = None
    if _orchestrator_client and _thread_id:
        workspace_override = await _poll_workspace_ready(
            _orchestrator_client, _thread_id, timeout=120
        )
        if workspace_override:
            logger.info(
                f"Workspace container ready: {workspace_override['remote']['host']}"
            )
        else:
            logger.info("No workspace container — using local backend")

    # 3. Apply config overrides and project_ids from thread metadata
    config_override = (workspace_override or {}).get("config_override")
    project_ids = (workspace_override or {}).get("project_ids") or []
    if (not config_override or not project_ids) and _orchestrator_client and _thread_id:
        # No workspace container but might still have config overrides / project_ids
        try:
            ws_info = await _orchestrator_client.get_thread_workspace(_thread_id)
            if ws_info:
                if not config_override:
                    config_override = ws_info.get("config_override")
                if not project_ids:
                    project_ids = ws_info.get("project_ids") or []
        except Exception:
            pass

    effective_config = _agent.config
    llm = _agent._tactical_llm or _agent._llm
    if config_override:
        import dataclasses
        from ..core.loader import (
            deep_merge,
            load_agent_config_from_dict,
            create_llm,
        )

        base_dict = dataclasses.asdict(effective_config)
        merged = deep_merge(base_dict, config_override)
        effective_config = load_agent_config_from_dict(
            merged, deployment_dir=effective_config._deployment_dir
        )
        # Recreate LLM if model or temperature changed
        if config_override.get("llm"):
            llm = create_llm(effective_config.llm, effective_config.limits)
            logger.info(
                f"Config override applied: model={effective_config.llm.model}, "
                f"temperature={effective_config.llm.temperature}"
            )

    # 4. Create PersistentSession (thread_id is now guaranteed)
    _session = PersistentSession(
        thread_id=_thread_id,
        config=effective_config,
        project_ids=project_ids,
    )
    if project_ids:
        logger.info(f"Session scoped to {len(project_ids)} project(s): {project_ids}")
    git_remote_url = (
        workspace_override.get("git_remote_url") if workspace_override else None
    )
    await _session.setup(
        llm=llm,
        auxiliary_llm=_agent._auxiliary_llm,
        postgres_conn=_agent.postgres_conn,
        vector_conn=getattr(_agent, "vector_conn", None),
        workspace_override=workspace_override,
        git_remote_url=git_remote_url,
    )

    # 5. Restore message history from DB (for pod restart / session resume)
    await _restore_session_messages()

    logger.info(f"Persistent agent ready: thread={_thread_id}")

    # Mark thread as active now that the agent is fully initialized
    await _update_thread_status("active")

    yield

    # --- Shutdown ---
    logger.info("Shutting down persistent agent")

    # Mark thread as idle on graceful shutdown (NOT ended — only /done sets ended)
    await _update_thread_status("idle")

    # Final git commit + push before cleanup
    if _session and _session.workspace_manager:
        git_mgr = getattr(_session.workspace_manager, "git_manager", None)
        if git_mgr and git_mgr.is_active:
            try:
                if git_mgr.has_uncommitted_changes():
                    git_mgr.commit(f"Session end: thread {_thread_id}")
                git_mgr.push()
            except Exception as e:
                logger.warning(f"Final git push failed (non-fatal): {e}")

    if _orchestrator_client:
        try:
            _orchestrator_client.stop_heartbeat()
            if _heartbeat_task:
                _heartbeat_task.cancel()
                try:
                    await _heartbeat_task
                except asyncio.CancelledError:
                    pass
            await _orchestrator_client.deregister()
            await _orchestrator_client.close()
        except Exception as e:
            logger.warning(f"Orchestrator cleanup error: {e}")

    if _session:
        await _session.cleanup()

    if _agent:
        await _agent.shutdown()

    logger.info("Persistent agent shutdown complete")


def create_persistent_app(config_path: str, thread_id: Optional[str] = None) -> FastAPI:
    """Create the persistent-mode FastAPI application.

    Args:
        config_path: Agent config name or path
        thread_id: Session thread UUID

    Returns:
        FastAPI app with WebSocket and health endpoints
    """
    global _config_path, _thread_id
    _config_path = config_path
    _thread_id = thread_id

    app = FastAPI(
        title="Persistent Agent API",
        description="Interactive persistent agent with WebSocket transport",
        version="1.0.0",
        lifespan=lifespan,
    )

    # --- Health endpoints (same pattern as worker) ---

    @app.get("/health")
    async def health():
        return JSONResponse(
            {
                "status": "healthy",
                "mode": "persistent",
                "thread_id": _thread_id,
                "uptime_seconds": (datetime.now() - _started_at).total_seconds()
                if _started_at
                else 0,
            }
        )

    @app.get("/ready")
    async def ready():
        is_ready = _session is not None and _session.llm_with_tools is not None
        return JSONResponse(
            {"ready": is_ready, "mode": "persistent", "thread_id": _thread_id},
            status_code=200 if is_ready else 503,
        )

    @app.get("/status")
    async def status():
        return JSONResponse(
            {
                "mode": "persistent",
                "thread_id": _thread_id,
                "config": _config_path,
                "permission_mode": _session.permission_mode if _session else None,
                "turn_count": _session.turn_count if _session else 0,
                "message_count": len(_session.messages) if _session else 0,
                "tools": [t.name for t in _session.tools]
                if _session and _session.tools
                else [],
            }
        )

    # --- WebSocket endpoint ---

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        await ws.accept()

        if not _session or not _session.llm_with_tools:
            await _ws_send(ws, "error", {"message": "Agent not ready"})
            await ws.close(code=4503, reason="Agent not ready")
            return

        logger.info(f"WebSocket connected: thread={_thread_id}")

        # Send current session state so the client can sync
        await _ws_send(
            ws,
            "session.state",
            {
                "thread_id": _thread_id,
                "permission_mode": _session.permission_mode,
                "turn_count": _session.turn_count,
                "message_count": len(_session.messages),
            },
        )

        # Send greeting only on first connect (no messages yet)
        if not _session.messages or _session.turn_count == 0:
            greeting = _session.config.interactive.greeting
            if greeting:
                await _ws_send(ws, "greeting", {"content": greeting})

        # Queue for user input (bridges WS receive loop → persistent loop)
        user_queue: asyncio.Queue[str] = asyncio.Queue()
        interrupt_flag = False
        # Track last user message for persistence
        _last_user_content: List[str] = [""]

        def check_interrupt() -> bool:
            nonlocal interrupt_flag
            if interrupt_flag:
                interrupt_flag = False
                return True
            return False

        # Compute idle timeout from config
        idle_timeout_minutes = _session.config.interactive.idle_timeout_minutes
        idle_timeout_seconds = (
            idle_timeout_minutes * 60 if idle_timeout_minutes > 0 else None
        )

        async def get_user_input() -> str:
            await _ws_send(ws, "ready", {})
            if idle_timeout_seconds:
                try:
                    return await asyncio.wait_for(
                        user_queue.get(), timeout=idle_timeout_seconds
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"Idle timeout ({idle_timeout_minutes}min) "
                        f"for thread {_thread_id}"
                    )
                    await _ws_send(
                        ws,
                        "session.idle_timeout",
                        {
                            "thread_id": _thread_id,
                            "message": "Session paused due to inactivity. "
                            "Your work has been saved.",
                            "timeout_minutes": idle_timeout_minutes,
                        },
                    )
                    try:
                        await ws.close(code=4408, reason="Idle timeout")
                    except Exception:
                        pass
                    raise IdleTimeoutError(
                        f"Idle timeout after {idle_timeout_seconds}s"
                    )
            return await user_queue.get()

        async def on_token(token: str) -> None:
            await _ws_send(ws, "token", {"content": token})

        async def on_tool_start(
            tool_name: str, tool_args: Dict[str, Any], tool_call_id: str
        ) -> None:
            await _ws_send(
                ws,
                "tool.started",
                {
                    "tool": tool_name,
                    "args": _safe_serialize(tool_args),
                    "id": tool_call_id,
                },
            )

        async def on_tool_result(
            tool_name: str, result: str, tool_call_id: str
        ) -> None:
            # Truncate large results for WS (full result is in message history)
            display_result = result[:2000] + "..." if len(result) > 2000 else result
            await _ws_send(
                ws,
                "tool.completed",
                {
                    "tool": tool_name,
                    "result": display_result,
                    "id": tool_call_id,
                },
            )

            # Notify frontend of file checkpoint availability after writes
            if tool_name in ("write_file", "edit_file"):
                await _ws_send(
                    ws,
                    "file.checkpoint",
                    {
                        "turn_id": _session.turn_count,
                    },
                )

            # Broadcast task state after task tool calls
            if (
                tool_name in ("task_add", "task_complete", "task_list")
                and _session.session_task_manager
            ):
                await _ws_send(
                    ws,
                    "tasks.updated",
                    {
                        "tasks": _session.session_task_manager.to_dict_list(),
                    },
                )

        async def permission_check(tool_name: str, tool_args: Dict[str, Any]) -> bool:
            mode = _session.permission_mode

            if mode == "autonomous":
                return True

            if mode == "auto_accept":
                # Auto-accept reads and writes; still ask for shell commands
                shell_tools = {"run_command", "shell_execute", "shell_read"}
                if tool_name not in shell_tools:
                    return True

            # Supervised mode (or shell in auto_accept): ask user
            await _ws_send(
                ws,
                "permission.request",
                {
                    "tool": tool_name,
                    "args": _safe_serialize(tool_args),
                },
            )

            # Wait for approval (with timeout)
            try:
                response = await asyncio.wait_for(user_queue.get(), timeout=300)
                return response == APPROVE_SENTINEL
            except asyncio.TimeoutError:
                return False

        async def on_turn_start(turn_id: int) -> None:
            _session.turn_count = turn_id
            await _ws_send(ws, "turn.started", {"turn_id": turn_id})
            # Save user message to DB (bounded await — no messages lost on crash)
            if _orchestrator_client and _last_user_content[0]:
                try:
                    await asyncio.wait_for(
                        _save_message(
                            _orchestrator_client,
                            _thread_id,
                            "user",
                            _last_user_content[0],
                            None,
                            turn_id,
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("User message save timed out (5s) — proceeding")

        async def on_turn_complete(turn_id: int, metrics: dict | None = None) -> None:
            await _ws_send(ws, "turn.completed", {"turn_id": turn_id})
            # Save AI messages from this turn to DB (bounded await)
            if _orchestrator_client:
                try:
                    await asyncio.wait_for(
                        _save_turn_ai_messages(
                            _orchestrator_client,
                            _thread_id,
                            _session.messages,
                            turn_id,
                            metrics=metrics,
                        ),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("AI message save timed out (5s) — proceeding")

            # Auto-generate title after first turn (fire-and-forget)
            if turn_id == 1 and _session.postgres_conn:
                asyncio.create_task(_auto_title_after_first_turn(ws))

        async def on_error(message: str) -> None:
            await _ws_send(ws, "error", {"message": message})

        async def on_vm_upgrade_needed(freeze_data: Dict[str, Any]) -> None:
            """Notify client that sudo was detected and VM upgrade is available."""
            await _ws_send(
                ws,
                "vm_upgrade.needed",
                {
                    "reason": freeze_data.get("reason", "sudo detected"),
                    "command": freeze_data.get("command"),
                },
            )

        callbacks = PersistentLoopCallbacks(
            get_user_input=get_user_input,
            on_token=on_token,
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,
            permission_check=permission_check,
            on_turn_start=on_turn_start,
            on_turn_complete=on_turn_complete,
            on_error=on_error,
            check_interrupt=check_interrupt,
            on_vm_upgrade_needed=on_vm_upgrade_needed,
        )

        # Start persistent loop as background task
        loop_task = asyncio.create_task(
            run_persistent_loop(
                llm_with_tools=_session.llm_with_tools,
                tools=_session.tools,
                context_manager=_session.context_manager,
                config=_session.config,
                system_prompt=_session.system_prompt,
                callbacks=callbacks,
                messages=_session.messages,
                auxiliary_llm=_session.auxiliary_llm,
                workspace_content=_session.get_workspace_content,
                recall_store=_session.recall_store,
                knowledge_store=_session.knowledge_store,
                project_id=_session.project_id,
                project_ids=_session.project_ids,
                tool_context=_session.tool_context,
                initial_turn_count=_session.turn_count,
                get_current_tools=lambda: (_session.llm_with_tools, _session.tools),
            )
        )

        # --- WebSocket receive loop ---
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    # Plain text → treat as message
                    data = {"method": "message", "content": raw}

                method = data.get("method", "message")

                if method == "message":
                    content = data.get("content", "")
                    if content:
                        _last_user_content[0] = content
                        await user_queue.put(content)

                elif method == "approve":
                    await user_queue.put(APPROVE_SENTINEL)

                elif method == "deny":
                    await user_queue.put(DENY_SENTINEL)

                elif method == "interrupt":
                    interrupt_flag = True

                elif method == "mode.set":
                    new_mode = data.get("mode", "supervised")
                    if new_mode in ("supervised", "auto_accept", "autonomous"):
                        _session.permission_mode = new_mode
                        await _ws_send(ws, "mode.changed", {"mode": new_mode})
                        logger.info(f"Permission mode changed to: {new_mode}")
                    else:
                        await _ws_send(
                            ws,
                            "error",
                            {"message": f"Invalid mode: {new_mode}"},
                        )

                elif method == "compact":
                    # Manual compaction trigger (/compact command)
                    focus = data.get("focus", "")
                    asyncio.create_task(_handle_compact(ws, focus))

                elif method == "archive":
                    # End session (/done command)
                    asyncio.create_task(_handle_archive(ws))

                elif method == "upgrade-to-vm":
                    # Upgrade workspace from container to VM
                    asyncio.create_task(_handle_vm_upgrade(ws))

                elif method == "undo":
                    turn_id = data.get("turn_id")
                    restored = _session.undo_turn(turn_id)
                    if restored:
                        await _ws_send(
                            ws,
                            "files.restored",
                            {
                                "paths": restored,
                                "turn_id": turn_id,
                            },
                        )
                    else:
                        await _ws_send(
                            ws,
                            "error",
                            {"message": "No checkpoints available to undo"},
                        )

                else:
                    await _ws_send(
                        ws, "error", {"message": f"Unknown method: {method}"}
                    )

        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected: thread={_thread_id}")
        except Exception as e:
            logger.exception(f"WebSocket error: {e}")
        finally:
            # Check if loop exited due to idle timeout
            idle_timed_out = False
            if loop_task.done() and not loop_task.cancelled():
                try:
                    loop_task.result()
                except IdleTimeoutError:
                    idle_timed_out = True
                except Exception:
                    pass

            if not loop_task.done():
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass

            if idle_timed_out:
                await _handle_idle_archive()

            logger.info(f"WebSocket session ended: thread={_thread_id}")

    return app


# --- Helpers ---


async def _ws_send(ws: WebSocket, method: str, params: Dict[str, Any]) -> None:
    """Send a JSON message over WebSocket. Silently drops if connection is closed."""
    try:
        await ws.send_json({"method": method, "params": params})
    except Exception:
        pass  # Connection already closed


def _safe_serialize(obj: Any) -> Any:
    """Make an object JSON-serializable (best effort)."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


async def _restore_session_messages() -> None:
    """Restore LangChain message history from DB into session.messages.

    Called during lifespan startup. On a fresh session this is a no-op.
    On pod restart or session resume, this restores the LLM's conversation
    context so it doesn't start with amnesia.
    """
    if not _session or not _agent or not _agent.postgres_conn or not _thread_id:
        return

    try:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        db_messages = await _agent.postgres_conn.get_thread_messages_history(
            thread_id=_thread_id,
            limit=500,
        )

        if not db_messages:
            return

        restored: list = []
        # Track tool_call_ids from the last AIMessage for ToolMessage pairing
        pending_tool_call_ids: list[str] = []

        for db_msg in db_messages:
            role = db_msg["role"]
            content = db_msg["content"] or ""
            tool_calls = db_msg.get("tool_calls")

            if role in ("human", "user"):
                restored.append(HumanMessage(content=content))

            elif role in ("ai", "assistant"):
                lc_tool_calls = []
                if tool_calls:
                    lc_tool_calls = [
                        {
                            "id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "args": tc.get("args", {}),
                        }
                        for tc in tool_calls
                    ]
                    pending_tool_call_ids = [tc["id"] for tc in lc_tool_calls]
                else:
                    pending_tool_call_ids = []
                restored.append(AIMessage(content=content, tool_calls=lc_tool_calls))

            elif role == "tool":
                # Pair with the next pending tool_call_id from the last AIMessage
                tool_call_id = (
                    pending_tool_call_ids.pop(0) if pending_tool_call_ids else ""
                )
                restored.append(ToolMessage(content=content, tool_call_id=tool_call_id))

            # Skip system messages — the loop adds a fresh one from current config

        if restored:
            _session.messages.extend(restored)
            # Set turn_count from the last message's turn_number
            last_turn = max((m.get("turn_number") or 0 for m in db_messages), default=0)
            _session.turn_count = last_turn
            logger.info(
                f"Restored {len(restored)} messages for thread {_thread_id} "
                f"(last turn: {last_turn})"
            )

    except Exception as e:
        logger.warning(f"Failed to restore session messages (non-fatal): {e}")


async def _save_message(
    client: Any,
    thread_id: str,
    role: str,
    content: Optional[str],
    tool_calls: Optional[Any],
    turn_number: int,
) -> None:
    """Fire-and-forget: save a single message via orchestrator REST."""
    try:
        await client.save_thread_message(
            thread_id=thread_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            turn_number=turn_number,
        )
    except Exception as e:
        logger.warning(f"Failed to save message (non-fatal): {e}")


async def _save_turn_ai_messages(
    client: Any,
    thread_id: str,
    messages: List[Any],
    turn_number: int,
    metrics: dict | None = None,
) -> None:
    """Fire-and-forget: save AI + tool messages from the most recent turn via orchestrator REST."""
    try:
        # Walk backwards from the end to find messages from this turn
        # (after the last HumanMessage)
        to_save = []
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type in ("human", "HumanMessageChunk"):
                break
            to_save.append(msg)
        to_save.reverse()

        for msg in to_save:
            raw_type = getattr(msg, "type", "unknown")
            # Normalize LangChain chunk types: AIMessageChunk → ai, etc.
            _role_map = {
                "ai": "ai",
                "AIMessageChunk": "ai",
                "human": "human",
                "HumanMessageChunk": "human",
                "tool": "tool",
                "ToolMessageChunk": "tool",
                "system": "system",
                "SystemMessageChunk": "system",
            }
            role = _role_map.get(raw_type, raw_type)
            content = msg.content if hasattr(msg, "content") else None
            tc = None
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tc = [
                    {"name": t.get("name"), "args": t.get("args"), "id": t.get("id")}
                    for t in msg.tool_calls
                ]
            # Normalize content for Anthropic list-of-dicts format
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            # Attach metrics only to AI messages (not tool results)
            msg_metrics = metrics if role == "ai" else None
            await client.save_thread_message(
                thread_id=thread_id,
                role=role,
                content=content,
                tool_calls=tc,
                turn_number=turn_number,
                metrics=msg_metrics,
            )
    except Exception as e:
        logger.warning(f"Failed to save turn messages (non-fatal): {e}")


async def _handle_compact(ws: WebSocket, focus: str = "") -> None:
    """Handle /compact command — trigger manual context compaction."""
    try:
        if not _session or not _session.context_manager:
            await _ws_send(ws, "error", {"message": "Session not ready"})
            return

        before_count = len(_session.messages)
        _session.messages[:] = await _session.context_manager.summarize_and_compact(
            messages=_session.messages,
            auxiliary=_session.auxiliary_llm,
            max_summary_length=getattr(
                _session.config.context_management, "max_summary_length", 10000
            ),
        )
        after_count = len(_session.messages)

        await _ws_send(
            ws,
            "context.compacted",
            {
                "before": before_count,
                "after": after_count,
                "focus": focus,
            },
        )
        logger.info(f"Manual compaction: {before_count} → {after_count} messages")

        # Commit + push workspace to Gitea on compaction (natural checkpoint boundary)
        if _session.workspace_manager:
            git_mgr = getattr(_session.workspace_manager, "git_manager", None)
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(
                            f"Compaction checkpoint ({before_count} → {after_count} msgs)"
                        )
                    git_mgr.push()
                except Exception as e:
                    logger.debug(f"Git push on compaction failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"Compaction failed: {e}")
        await _ws_send(ws, "error", {"message": f"Compaction failed: {e}"})


async def _handle_archive(ws: WebSocket) -> None:
    """Handle /done command — end the session with memory extraction and title."""
    try:
        if not _session:
            await _ws_send(ws, "error", {"message": "Session not ready"})
            return

        # 1. Extract final memories
        recall_store = (
            getattr(_session.tool_context, "recall_store", None)
            if _session.tool_context
            else None
        )
        if recall_store and _session.auxiliary_llm and _session.messages:
            try:
                from ..services.auxiliary import extract_and_store_memories

                await extract_and_store_memories(
                    auxiliary_llm=_session.auxiliary_llm,
                    recall_store=recall_store,
                    messages=_session.messages,
                    memory_extraction_prompt=_session.config.memory.extraction_prompt
                    or "",
                )
                logger.info("Final memory extraction complete")
            except Exception as e:
                logger.warning(f"Final memory extraction failed (non-fatal): {e}")

        # 2. Generate title if untitled
        if _session.postgres_conn:
            try:
                thread = await _session.postgres_conn.get_thread(_thread_id)
                current = thread.get("title", "") if thread else ""
                if (
                    not current
                    or current.startswith("Local Session")
                    or current == "Untitled Session"
                ):
                    title = await _generate_title(
                        _session.messages, _session.auxiliary_llm
                    )
                    if title:
                        async with _session.postgres_conn.acquire() as conn:
                            await conn.execute(
                                "UPDATE threads SET title = $2 WHERE id = $1",
                                _thread_id,
                                title,
                            )
            except Exception as e:
                logger.warning(f"Title generation failed (non-fatal): {e}")

            # 3. Mark thread as ended
            try:
                await _session.postgres_conn.end_thread(_thread_id)
            except Exception as e:
                logger.warning(f"Thread end update failed: {e}")

        await _ws_send(ws, "session.ended", {"thread_id": _thread_id})
        logger.info(f"Session archived: thread={_thread_id}")
    except Exception as e:
        logger.warning(f"Archive failed: {e}")
        await _ws_send(ws, "error", {"message": f"Archive failed: {e}"})


async def _update_thread_status(status: str) -> None:
    """Update thread status via orchestrator REST (preferred) or direct DB."""
    if _orchestrator_client and _thread_id:
        try:
            await _orchestrator_client.update_thread_status(_thread_id, status)
            return
        except Exception:
            pass
    # Fallback to direct DB
    if _session and _session.postgres_conn and _thread_id:
        try:
            await _session.postgres_conn.update_thread_status(_thread_id, status)
        except Exception as e:
            logger.warning(f"Failed to update thread status to {status}: {e}")


async def _handle_idle_archive() -> None:
    """Handle idle timeout — archive session state, set thread to idle."""
    try:
        if not _session:
            return

        # 1. Extract memories
        recall_store = (
            getattr(_session.tool_context, "recall_store", None)
            if _session.tool_context
            else None
        )
        if recall_store and _session.auxiliary_llm and _session.messages:
            try:
                from ..services.auxiliary import extract_and_store_memories

                await extract_and_store_memories(
                    auxiliary_llm=_session.auxiliary_llm,
                    recall_store=recall_store,
                    messages=_session.messages,
                    memory_extraction_prompt=_session.config.memory.extraction_prompt
                    or "",
                )
                logger.info("Idle archive: memory extraction complete")
            except Exception as e:
                logger.warning(f"Idle archive memory extraction failed: {e}")

        # 2. Generate title if untitled
        if _session.postgres_conn:
            try:
                thread = await _session.postgres_conn.get_thread(_thread_id)
                current = thread.get("title", "") if thread else ""
                if (
                    not current
                    or current.startswith("Local Session")
                    or current == "Untitled Session"
                ):
                    title = await _generate_title(
                        _session.messages, _session.auxiliary_llm
                    )
                    if title:
                        async with _session.postgres_conn.acquire() as conn:
                            await conn.execute(
                                "UPDATE threads SET title = $2 WHERE id = $1",
                                _thread_id,
                                title,
                            )
            except Exception as e:
                logger.warning(f"Idle title generation failed: {e}")

        # 3. Set thread to 'idle' (NOT 'ended' — resumable)
        await _update_thread_status("idle")

        # 4. Git commit + push
        if _session.workspace_manager:
            git_mgr = getattr(_session.workspace_manager, "git_manager", None)
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(f"Idle timeout: thread {_thread_id}")
                    git_mgr.push()
                except Exception as e:
                    logger.warning(f"Idle git push failed: {e}")

        logger.info(f"Idle archive complete: thread={_thread_id}")
    except Exception as e:
        logger.warning(f"Idle archive failed: {e}")


async def _poll_workspace_ready(
    client: Any,
    thread_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Poll orchestrator for workspace container readiness.

    Returns:
        Workspace config dict {"backend": "remote", "remote": {host, port, ...}}
        or None if timeout, unavailable, or no workspace provisioned.
    """
    import time

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ws = await client.get_thread_workspace(thread_id)
        if not ws:
            return None

        # Check VM workspace first (takes precedence over container)
        vm_status = ws.get("vm_status")
        if vm_status == "ready" and ws.get("vm_ssh_host"):
            return {
                "backend": "remote",
                "remote": {
                    "host": ws["vm_ssh_host"],
                    "port": ws.get("vm_ssh_port", 22),
                    "username": "agent-host",
                    "key_path": "/run/secrets/vm-ssh-key",
                    "workspace_path": "/home/agent-host/workspace",
                },
                "git_remote_url": ws.get("git_remote_url"),
                "config_override": ws.get("config_override"),
            }

        # Check container workspace
        status = ws.get("status", "none")

        if status == "ready" and ws.get("pod_ip"):
            return {
                "backend": "remote",
                "remote": {
                    "host": ws["pod_ip"],
                    "port": 22,
                    "username": "agent-host",
                    "key_path": "/run/secrets/vm-ssh-key",
                    "workspace_path": "/home/agent-host/workspace",
                },
                "git_remote_url": ws.get("git_remote_url"),
                "config_override": ws.get("config_override"),
            }
        if status == "failed" and (not vm_status or vm_status == "failed"):
            logger.warning(f"Workspace provisioning failed: {ws}")
            return None
        if status == "none" and not vm_status:
            # No workspace provisioned for this thread (no K8s)
            return None

        # Still creating — wait and poll again
        await asyncio.sleep(poll_interval)

    logger.warning(f"Workspace polling timed out after {timeout}s")
    return None


async def _handle_vm_upgrade(ws: WebSocket) -> None:
    """Handle VM upgrade request from cockpit.

    Flow: request VM provisioning → poll until ready → hot-swap backend.
    """
    if not _session or not _orchestrator_client or not _thread_id:
        await _ws_send(ws, "vm_upgrade.failed", {"reason": "Session not ready"})
        return

    await _ws_send(ws, "vm_upgrade.started", {"thread_id": _thread_id})

    try:
        # 1. Request VM provisioning via orchestrator
        ok = await _orchestrator_client.request_thread_vm_upgrade(_thread_id)
        if not ok:
            await _ws_send(
                ws,
                "vm_upgrade.failed",
                {"reason": "Orchestrator rejected VM upgrade request"},
            )
            return

        # 2. Poll for VM readiness (up to 5 minutes)
        vm_config = await _poll_vm_ready(_orchestrator_client, _thread_id, timeout=300)
        if not vm_config:
            await _ws_send(
                ws,
                "vm_upgrade.failed",
                {"reason": "VM did not become ready in time"},
            )
            return

        # 3. Create new RemoteBackend pointing at VM
        from ..core.backends.remote import RemoteBackend

        shell_config = _session.config.extra.get("shell", {})
        new_backend = RemoteBackend(
            host=vm_config["ssh_host"],
            port=vm_config.get("ssh_port", 22),
            username="agent-host",
            key_path="/run/secrets/vm-ssh-key",
            workspace_path="/home/agent-host/workspace",
            job_id=_thread_id,
            default_timeout=shell_config.get("default_timeout", 120),
            max_tabs=shell_config.get("max_tabs", 15),
        )

        # 4. Hot-swap backend on session
        _session.swap_backend(new_backend)

        # 5. VM has its own sudo gate — allow sudo through
        if _session.shell_manager and hasattr(_session.shell_manager, "sudo_action"):
            _session.shell_manager.sudo_action = "allow"

        await _ws_send(
            ws,
            "vm_upgrade.complete",
            {
                "thread_id": _thread_id,
                "ssh_host": vm_config["ssh_host"],
                "ssh_port": vm_config.get("ssh_port", 22),
            },
        )
        logger.info(f"VM upgrade complete for thread {_thread_id}")

    except Exception as e:
        logger.exception(f"VM upgrade failed for thread {_thread_id}")
        await _ws_send(ws, "vm_upgrade.failed", {"reason": str(e)})


async def _poll_vm_ready(
    client: Any,
    thread_id: str,
    timeout: int = 300,
    poll_interval: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Poll orchestrator for VM readiness.

    Returns:
        VM config dict {"ssh_host": ..., "ssh_port": ...} or None on timeout/failure.
    """
    import time

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ws = await client.get_thread_workspace(thread_id)
        if not ws:
            await asyncio.sleep(poll_interval)
            continue

        vm_status = ws.get("vm_status")
        if vm_status == "ready" and ws.get("vm_ssh_host"):
            return {
                "ssh_host": ws["vm_ssh_host"],
                "ssh_port": ws.get("vm_ssh_port", 22),
            }
        if vm_status == "failed":
            logger.warning(f"VM provisioning failed for thread {thread_id}")
            return None

        await asyncio.sleep(poll_interval)

    logger.warning(f"VM polling timed out after {timeout}s for thread {thread_id}")
    return None


async def _generate_title(messages: List[Any], auxiliary_llm: Any) -> Optional[str]:
    """Generate a short title from conversation using AuxiliaryLLM."""
    if not auxiliary_llm or not messages:
        return None
    try:
        from ..services.auxiliary import SummarizeTask

        # Grab first few exchanges for title generation
        sample = []
        for m in messages[:10]:
            if hasattr(m, "content") and isinstance(m.content, str) and m.content:
                sample.append(m.content[:200])
        if not sample:
            return None

        result = await auxiliary_llm.run_task(
            SummarizeTask,
            text="\n".join(sample),
            instructions="Generate a short title (5-8 words) for this conversation. Return ONLY the title, no quotes or punctuation.",
            mode="chain",
        )
        return result.strip()[:100] if result else None
    except Exception as e:
        logger.warning(f"Title generation error: {e}")
        return None


async def _auto_title_after_first_turn(ws: WebSocket) -> None:
    """Generate and push a title after the first assistant turn (fire-and-forget)."""
    try:
        if not _session or not _session.postgres_conn or not _thread_id:
            return
        # Check current title is still a default placeholder
        thread = await _session.postgres_conn.get_thread(_thread_id)
        current = thread.get("title", "") if thread else ""
        if (
            current
            and not current.startswith("Local Session")
            and current != "Untitled Session"
        ):
            return  # already has a real title
        title = await _generate_title(_session.messages, _session.auxiliary_llm)
        if not title:
            return
        async with _session.postgres_conn.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET title = $2 WHERE id = $1",
                _thread_id,
                title,
            )
        await _ws_send(ws, "title.updated", {"title": title})
        logger.info(f"Auto-titled thread {_thread_id}: {title}")
    except Exception as e:
        logger.warning(f"Auto-title generation failed (non-fatal): {e}")
