from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
import yaml

from tests.e2e.app.deterministic_provider.provider import (
    CHAT_MODEL_ID,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_ID,
    RERANK_MODEL_ID,
    ScenarioStore,
    create_control_app,
    create_inference_app,
)

pytestmark = pytest.mark.asyncio

CONTROL_TOKEN = "unit-control-token"
INFERENCE_KEY = "unit-inference-key"
CONTROL_HEADERS = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
INFERENCE_HEADERS = {"Authorization": f"Bearer {INFERENCE_KEY}"}


@pytest.fixture
def store() -> ScenarioStore:
    return ScenarioStore()


@pytest_asyncio.fixture
async def control(store: ScenarioStore):
    transport = httpx.ASGITransport(
        app=create_control_app(store, control_token=CONTROL_TOKEN)
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://control.test",
        headers=CONTROL_HEADERS,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def inference(store: ScenarioStore):
    transport = httpx.ASGITransport(
        app=create_inference_app(store, inference_api_key=INFERENCE_KEY)
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://inference.test",
        headers=INFERENCE_HEADERS,
    ) as client:
        yield client


async def arm(
    control: httpx.AsyncClient,
    run_id: str,
    *,
    scenario: str = "reply",
    required_responses: int = 1,
    chunk_delay_ms: int | None = None,
) -> dict:
    body: dict[str, object] = {
        "scenario": scenario,
        "required_responses": required_responses,
    }
    if chunk_delay_ms is not None:
        body["chunk_delay_ms"] = chunk_delay_ms
    response = await control.post(
        f"/control/scenarios/{run_id}/arm",
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


def chat_request(
    run_id: str,
    *,
    stream: bool = False,
    extra: dict | None = None,
) -> dict:
    payload = {
        "model": CHAT_MODEL_ID,
        "messages": [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": f"E2E-{run_id} verify the journey"},
        ],
        "stream": stream,
        "temperature": 0,
        "stream_options": {"include_usage": True},
    }
    payload.update(extra or {})
    return payload


def sse_payloads(response: httpx.Response) -> list[dict | str]:
    result: list[dict | str] = []
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ")
        result.append(data if data == "[DONE]" else json.loads(data))
    return result


async def test_health_model_list_and_auth_are_fail_closed(
    store: ScenarioStore,
) -> None:
    inference_app = create_inference_app(store, inference_api_key=INFERENCE_KEY)
    control_app = create_control_app(store, control_token=CONTROL_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=inference_app),
        base_url="http://inference.test",
    ) as anonymous_inference:
        health = await anonymous_inference.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        unauthorized = await anonymous_inference.get("/v1/models")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "no-store"

        models = await anonymous_inference.get("/v1/models", headers=INFERENCE_HEADERS)
        assert models.status_code == 200
        assert models.json()["object"] == "list"
        assert [model["id"] for model in models.json()["data"]] == [
            CHAT_MODEL_ID,
            EMBEDDING_MODEL_ID,
        ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app), base_url="http://control.test"
    ) as anonymous_control:
        unauthorized = await anonymous_control.get("/control/health")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "no-store"
        authorized = await anonymous_control.get(
            "/control/health", headers=CONTROL_HEADERS
        )
        assert authorized.status_code == 200


async def test_streaming_reply_has_role_chunks_finish_usage_and_done(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "stream-001"
    await arm(control, run_id)

    response = await inference.post(
        "/v1/chat/completions", json=chat_request(run_id, stream=True)
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = sse_payloads(response)
    assert events[-1] == "[DONE]"
    chunks = [event for event in events if isinstance(event, dict)]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    content = "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in chunks
        if chunk["choices"]
    )
    assert content == f"E2E_REPLY:{run_id}"
    assert any(
        chunk["choices"] and chunk["choices"][0]["finish_reason"] == "stop"
        for chunk in chunks
    )
    usage_chunks = [chunk for chunk in chunks if chunk.get("usage")]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["choices"] == []
    assert usage_chunks[0]["usage"]["total_tokens"] == 2

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["consumed_required_responses"] == 1
    assert state["remaining_required_responses"] == 0
    assert state["unexpected_count"] == 0
    assert state["pending_calls"] == 0
    assert state["counters"] == [
        {
            "run_id": run_id,
            "model": CHAT_MODEL_ID,
            "endpoint": "chat.completions",
            "stream": True,
            "outcome": "success",
            "count": 1,
        }
    ]


async def test_non_streaming_reply_and_exhaustion_are_accounted(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "exhaust-001"
    await arm(control, run_id)

    response = await inference.post("/v1/chat/completions", json=chat_request(run_id))
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": f"E2E_REPLY:{run_id}",
    }
    assert body["choices"][0]["finish_reason"] == "stop"

    exhausted = await inference.post("/v1/chat/completions", json=chat_request(run_id))
    assert exhausted.status_code == 409
    assert exhausted.json()["error"]["type"] == "scenario_exhausted"

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["remaining_required_responses"] == 0
    assert state["unexpected_count"] == 1
    assert {counter["outcome"] for counter in state["counters"]} == {
        "success",
        "unexpected_exhausted",
    }


async def test_concurrent_required_calls_cannot_share_one_reserved_response(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "concurrent-001"
    await arm(
        control,
        run_id,
        scenario="slow-stream",
        required_responses=1,
        chunk_delay_ms=25,
    )

    responses = await asyncio.gather(
        inference.post("/v1/chat/completions", json=chat_request(run_id, stream=True)),
        inference.post("/v1/chat/completions", json=chat_request(run_id, stream=True)),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["type"] == "scenario_exhausted"

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["consumed_required_responses"] == 1
    assert state["reserved_required_responses"] == 0
    assert state["remaining_required_responses"] == 0
    assert state["pending_calls"] == 0
    assert state["unexpected_count"] == 1
    success_counters = [
        counter
        for counter in state["counters"]
        if counter["endpoint"] == "chat.completions" and counter["outcome"] == "success"
    ]
    assert len(success_counters) == 1
    assert success_counters[0]["count"] == 1


async def test_lifecycle_structured_outputs_remain_available_after_exhaustion(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "structured-001"
    await arm(control, run_id)
    assert (
        await inference.post("/v1/chat/completions", json=chat_request(run_id))
    ).status_code == 200

    title_request = chat_request(
        run_id,
        extra={
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ConversationTitle",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                },
            }
        },
    )
    title_response = await inference.post("/v1/chat/completions", json=title_request)
    assert title_response.status_code == 200
    title = json.loads(title_response.json()["choices"][0]["message"]["content"])
    assert title == {"title": f"E2E-{run_id} deterministic assistant reply session"}

    memory_request = chat_request(
        run_id,
        extra={
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ExtractedMemories",
                    "schema": {
                        "type": "object",
                        "properties": {"memories": {"type": "array", "items": {}}},
                        "required": ["memories"],
                    },
                },
            }
        },
    )
    memory_response = await inference.post("/v1/chat/completions", json=memory_request)
    assert memory_response.status_code == 200
    content = memory_response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"memories": []}

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["remaining_required_responses"] == 0
    assert state["unexpected_count"] == 0
    assert len(state["calls"]) == 3


async def test_embeddings_are_stable_and_exactly_4096_dimensions(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "embedding-001"
    await arm(control, run_id)
    request = {
        "model": EMBEDDING_MODEL_ID,
        "input": ["same input", "different input"],
        "metadata": {"e2e_run_id": run_id},
        "encoding_format": "float",
    }

    first = await inference.post("/v1/embeddings", json=request)
    second = await inference.post("/v1/embeddings", json=request)
    assert first.status_code == second.status_code == 200
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert [item["index"] for item in first_data] == [0, 1]
    assert all(len(item["embedding"]) == EMBEDDING_DIMENSIONS for item in first_data)
    assert first_data[0]["embedding"] == second_data[0]["embedding"]
    assert first_data[0]["embedding"] != first_data[1]["embedding"]

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["remaining_required_responses"] == 1
    assert state["unexpected_count"] == 0
    assert state["counters"][0]["count"] == 2


@pytest.mark.parametrize("model", [EMBEDDING_MODEL_ID, RERANK_MODEL_ID])
async def test_rerank_returns_every_document_in_deterministic_score_order(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
    model: str,
) -> None:
    run_id = "rerank-001"
    await arm(control, run_id)
    response = await inference.post(
        "/v1/rerank",
        json={
            "model": model,
            "query": f"E2E-{run_id} alpha beta",
            "documents": ["unrelated", "alpha", "alpha beta"],
        },
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["index"] for result in results] == [2, 1, 0]
    assert all(
        results[index]["relevance_score"] >= results[index + 1]["relevance_score"]
        for index in range(len(results) - 1)
    )


async def test_manifest_publishes_inference_but_not_control() -> None:
    manifest_path = __file__.replace("test_provider.py", "kubernetes.yaml")
    with open(manifest_path, encoding="utf-8") as manifest_file:
        documents = list(yaml.safe_load_all(manifest_file))

    assert {document["kind"] for document in documents} == {"Deployment", "Service"}
    deployment = next(
        document for document in documents if document["kind"] == "Deployment"
    )
    service = next(document for document in documents if document["kind"] == "Service")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {port["name"] for port in container["ports"]} == {"inference", "control"}
    assert container["ports"][1]["containerPort"] == 8001
    assert [port["name"] for port in service["spec"]["ports"]] == ["inference"]
    assert [port["port"] for port in service["spec"]["ports"]] == [8000]


async def test_error_once_is_expected_then_consumes_success(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "error-once-001"
    await arm(control, run_id, scenario="error-once")

    first = await inference.post("/v1/chat/completions", json=chat_request(run_id))
    second = await inference.post("/v1/chat/completions", json=chat_request(run_id))
    assert first.status_code == 503
    assert first.json()["error"]["type"] == "fixture_retryable_error"
    assert second.status_code == 200

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["remaining_required_responses"] == 0
    assert state["unexpected_count"] == 0
    assert {counter["outcome"] for counter in state["counters"]} == {
        "retryable_error",
        "success",
    }


async def test_tool_call_scenario_requires_tool_result_before_final_response(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "tool-call-001"
    await arm(control, run_id, scenario="tool-call")
    request = chat_request(
        run_id,
        extra={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_fixture",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        },
    )

    tool_call = await inference.post("/v1/chat/completions", json=request)
    assert tool_call.status_code == 200
    choice = tool_call.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"] == {
        "name": "lookup_fixture",
        "arguments": "{}",
    }
    mid_state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert mid_state["remaining_required_responses"] == 1

    request["messages"].append(
        {"role": "tool", "tool_call_id": "call_1", "content": "secret tool result"}
    )
    final = await inference.post("/v1/chat/completions", json=request)
    assert final.status_code == 200
    assert final.json()["choices"][0]["message"]["content"] == f"E2E_REPLY:{run_id}"
    final_state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert final_state["remaining_required_responses"] == 0


async def test_search_job_scenario_drives_search_completion_and_todos(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "search-job-001"
    await arm(control, run_id, scenario="search-job", required_responses=2)

    incidental = await inference.post("/v1/chat/completions", json=chat_request(run_id))
    assert incidental.status_code == 200
    assert incidental.json()["choices"][0]["message"]["content"] == (
        f"E2E_REPLY:{run_id}"
    )

    def tools(*names: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in names
        ]

    async def next_function(*names: str, stream: bool = False) -> dict:
        response = await inference.post(
            "/v1/chat/completions",
            json=chat_request(
                run_id,
                stream=stream,
                extra={"tools": tools(*names)},
            ),
        )
        assert response.status_code == 200
        if not stream:
            return response.json()["choices"][0]["message"]["tool_calls"][0]["function"]
        chunks = [event for event in sse_payloads(response) if isinstance(event, dict)]
        return next(
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"]
            for chunk in chunks
            if chunk["choices"] and chunk["choices"][0]["delta"].get("tool_calls")
        )

    strategic_tools = (
        "read_file",
        "todo_complete",
        "next_phase_todos",
        "job_complete",
    )
    guide = await next_function(*strategic_tools)
    assert guide == {
        "name": "read_file",
        "arguments": '{"path":"skills/todo-guide/SKILL.md"}',
    }
    for _ in range(4):
        strategic = await next_function(*strategic_tools)
        assert strategic["name"] == "todo_complete"

    staged = await next_function(*strategic_tools)
    assert staged["name"] == "next_phase_todos"
    assert json.loads(staged["arguments"]) == {
        "todos": [
            "Run one live SearXNG web search for the official documentation.",
            "Verify the search answer and close the research phase.",
        ],
        "phase_name": "SearXNG live search gate",
    }
    transition = await next_function(*strategic_tools)
    assert transition["name"] == "todo_complete"

    tactical_tools = ("read_file", "todo_complete", "web_search")
    verification = await next_function(*tactical_tools)
    assert verification == {
        "name": "read_file",
        "arguments": '{"path":"skills/verify-before-done/SKILL.md"}',
    }
    search_function = await next_function(*tactical_tools)
    assert search_function["name"] == "web_search"
    assert json.loads(search_function["arguments"]) == {
        "query": "SearXNG official documentation",
        "max_results": 3,
    }
    for _ in range(2):
        tactical = await next_function(*tactical_tools)
        assert tactical["name"] == "todo_complete"

    verification = await next_function(*strategic_tools)
    assert verification == {
        "name": "read_file",
        "arguments": '{"path":"skills/verify-before-done/SKILL.md"}',
    }
    complete_function = await next_function(*strategic_tools)
    assert complete_function["name"] == "job_complete"
    assert json.loads(complete_function["arguments"]) == {
        "summary": f"Completed the SearXNG live search gate for E2E-{run_id}.",
        "deliverables": [],
        "confidence": 1.0,
    }

    todo_function = await next_function(*strategic_tools, stream=True)
    assert todo_function == {
        "name": "todo_complete",
        "arguments": '{"completion_note":"PASS: SearXNG live search gate completed."}',
    }

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["unexpected_count"] == 0
    assert state["pending_calls"] == 0
    assert state["remaining_required_responses"] == 1
    assert state["search_job_tool_steps"] == 14
    assert len(state["calls"]) == 15


async def test_search_job_scenario_refuses_an_expected_phase_tool_gap(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "search-job-gap-001"
    await arm(control, run_id, scenario="search-job")
    request = chat_request(
        run_id,
        extra={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "job_complete",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        },
    )

    rejected = await inference.post("/v1/chat/completions", json=request)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["type"] == "required_tool_missing"
    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["unexpected_count"] == 1
    assert state["search_job_tool_steps"] == 0


async def test_fetch_job_scenario_drives_extract_crawl_completion_and_todos(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "fetch-job-001"
    await arm(control, run_id, scenario="fetch-job", required_responses=2)

    def tools(*names: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in names
        ]

    async def next_function(*names: str, stream: bool = False) -> dict:
        response = await inference.post(
            "/v1/chat/completions",
            json=chat_request(
                run_id,
                stream=stream,
                extra={"tools": tools(*names)},
            ),
        )
        assert response.status_code == 200
        if not stream:
            return response.json()["choices"][0]["message"]["tool_calls"][0]["function"]
        chunks = [event for event in sse_payloads(response) if isinstance(event, dict)]
        return next(
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"]
            for chunk in chunks
            if chunk["choices"] and chunk["choices"][0]["delta"].get("tool_calls")
        )

    strategic_tools = (
        "read_file",
        "todo_complete",
        "next_phase_todos",
        "job_complete",
    )
    guide = await next_function(*strategic_tools)
    assert guide == {
        "name": "read_file",
        "arguments": '{"path":"skills/todo-guide/SKILL.md"}',
    }
    for _ in range(4):
        assert (await next_function(*strategic_tools))["name"] == "todo_complete"

    staged = await next_function(*strategic_tools)
    assert staged["name"] == "next_phase_todos"
    assert json.loads(staged["arguments"]) == {
        "todos": [
            "Extract the stable public example page through Crawl4AI.",
            "Crawl the same public origin through Crawl4AI.",
            "Verify both fetch answers and close the research phase.",
        ],
        "phase_name": "Crawl4AI live fetch gate",
    }
    assert (await next_function(*strategic_tools))["name"] == "todo_complete"

    tactical_tools = (
        "read_file",
        "todo_complete",
        "extract_webpage",
        "crawl_website",
    )
    verification = await next_function(*tactical_tools)
    assert verification == {
        "name": "read_file",
        "arguments": '{"path":"skills/verify-before-done/SKILL.md"}',
    }
    extracted = await next_function(*tactical_tools)
    assert extracted == {
        "name": "extract_webpage",
        "arguments": '{"urls":"https://example.com/"}',
    }
    assert (await next_function(*tactical_tools))["name"] == "todo_complete"
    crawled = await next_function(*tactical_tools)
    assert crawled["name"] == "crawl_website"
    assert json.loads(crawled["arguments"]) == {
        "url": "https://example.com/",
        "max_depth": 1,
        "max_breadth": 2,
        "limit": 2,
    }
    for _ in range(2):
        assert (await next_function(*tactical_tools))["name"] == "todo_complete"

    verification = await next_function(*strategic_tools)
    assert verification == {
        "name": "read_file",
        "arguments": '{"path":"skills/verify-before-done/SKILL.md"}',
    }
    complete_function = await next_function(*strategic_tools)
    assert complete_function["name"] == "job_complete"
    assert json.loads(complete_function["arguments"]) == {
        "summary": f"Completed the Crawl4AI live fetch gate for E2E-{run_id}.",
        "deliverables": [],
        "confidence": 1.0,
    }
    assert await next_function(*strategic_tools, stream=True) == {
        "name": "todo_complete",
        "arguments": '{"completion_note":"PASS: Crawl4AI live fetch gate completed."}',
    }

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["unexpected_count"] == 0
    assert state["pending_calls"] == 0
    assert state["remaining_required_responses"] == 2
    assert state["fetch_job_tool_steps"] == 16
    assert len(state["calls"]) == 16


async def test_fetch_job_scenario_refuses_an_expected_fetch_tool_gap(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "fetch-job-gap-001"
    await arm(control, run_id, scenario="fetch-job")
    request = chat_request(
        run_id,
        extra={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "job_complete",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        },
    )

    rejected = await inference.post("/v1/chat/completions", json=request)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["type"] == "required_tool_missing"
    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["unexpected_count"] == 1
    assert state["fetch_job_tool_steps"] == 0


async def test_numbered_stream_is_ordered_and_exactly_once(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "numbered-001"
    await arm(control, run_id, scenario="numbered-stream")
    response = await inference.post(
        "/v1/chat/completions", json=chat_request(run_id, stream=True)
    )
    chunks = [event for event in sse_payloads(response) if isinstance(event, dict)]
    content_pieces = [
        chunk["choices"][0]["delta"]["content"]
        for chunk in chunks
        if chunk["choices"] and chunk["choices"][0]["delta"].get("content")
    ]
    assert content_pieces == [
        "E2E_PART:1",
        "E2E_PART:2",
        f"E2E_REPLY:{run_id}",
    ]


async def test_slow_stream_uses_the_same_complete_wire_contract(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "slow-stream-001"
    await arm(control, run_id, scenario="slow-stream", chunk_delay_ms=1)
    response = await inference.post(
        "/v1/chat/completions", json=chat_request(run_id, stream=True)
    )
    events = sse_payloads(response)
    chunks = [event for event in events if isinstance(event, dict)]
    content = "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in chunks
        if chunk["choices"]
    )
    assert content == f"E2E_REPLY:{run_id}"
    assert events[-1] == "[DONE]"
    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["remaining_required_responses"] == 0
    assert state["unexpected_count"] == 0


async def test_run_isolation_ambiguous_requests_and_reset(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    await arm(control, "isolation-a")
    await arm(control, "isolation-b")

    response_a = await inference.post(
        "/v1/chat/completions", json=chat_request("isolation-a")
    )
    response_b = await inference.post(
        "/v1/chat/completions", json=chat_request("isolation-b")
    )
    assert response_a.status_code == response_b.status_code == 200
    assert (
        response_a.json()["choices"][0]["message"]["content"] == "E2E_REPLY:isolation-a"
    )
    assert (
        response_b.json()["choices"][0]["message"]["content"] == "E2E_REPLY:isolation-b"
    )

    ambiguous = await inference.post(
        "/v1/embeddings",
        json={"model": EMBEDDING_MODEL_ID, "input": "uncorrelated"},
    )
    assert ambiguous.status_code == 409
    assert ambiguous.json()["error"]["type"] == "run_correlation_required"
    overview = (await control.get("/control/scenarios")).json()
    assert overview["unscoped_unexpected_calls"] == 1

    reset = await control.delete("/control/scenarios/isolation-a")
    assert reset.status_code == 200
    assert reset.json() == {"run_id": "isolation-a", "reset": True}
    missing = await control.get("/control/scenarios/isolation-a")
    assert missing.status_code == 404
    repeated_reset = await control.delete("/control/scenarios/isolation-a")
    assert repeated_reset.status_code == 404


async def test_wrong_models_and_unknown_schemas_fail_and_are_visible(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "strict-001"
    await arm(control, run_id)
    wrong_model = chat_request(run_id)
    wrong_model["model"] = "vendor-fallback"
    rejected_model = await inference.post("/v1/chat/completions", json=wrong_model)
    assert rejected_model.status_code == 400
    assert rejected_model.json()["error"]["type"] == "unknown_model"

    unsupported_schema = chat_request(
        run_id,
        extra={
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "UnmodelledSchema",
                    "schema": {"type": "object"},
                },
            }
        },
    )
    rejected_schema = await inference.post(
        "/v1/chat/completions", json=unsupported_schema
    )
    assert rejected_schema.status_code == 422
    assert rejected_schema.json()["error"]["type"] == "unsupported_schema"

    state = (await control.get(f"/control/scenarios/{run_id}")).json()
    assert state["unexpected_count"] == 2
    assert {counter["outcome"] for counter in state["counters"]} == {
        "unexpected_model",
        "unexpected_schema",
    }


async def test_unknown_and_malformed_inference_calls_are_accounted_without_payloads(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "strict-routing-001"
    await arm(control, run_id)

    unknown = await inference.post(
        "/v1/not-a-provider-route",
        json={
            "model": CHAT_MODEL_ID,
            "messages": [{"role": "user", "content": f"E2E-{run_id} secret"}],
        },
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["type"] == "unknown_route"

    missing_model = await inference.post(
        "/v1/chat/completions",
        json={
            "metadata": {"e2e_run_id": run_id},
            "messages": [{"role": "user", "content": "sensitive prompt"}],
        },
    )
    assert missing_model.status_code == 400

    invalid_json = await inference.post(
        "/v1/embeddings",
        content="this is not json and must not be retained",
        headers={"Content-Type": "application/json"},
    )
    assert invalid_json.status_code == 400

    state_text = (await control.get(f"/control/scenarios/{run_id}")).text
    assert "sensitive prompt" not in state_text
    assert "this is not json" not in state_text
    state = json.loads(state_text)
    assert state["unexpected_count"] == 3
    assert {counter["outcome"] for counter in state["counters"]} == {
        "unexpected_route",
        "unexpected_invalid_request",
        "unexpected_invalid_json",
    }
    assert all(call["model"] in {CHAT_MODEL_ID, "<invalid>"} for call in state["calls"])


async def test_unscoped_unknown_route_and_invalid_correlation_are_globally_visible(
    store: ScenarioStore,
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    unknown = await inference.get("/v1/unknown")
    assert unknown.status_code == 404

    await arm(control, "valid-run-001")
    invalid_correlation = await inference.post(
        "/v1/embeddings",
        json={
            "model": EMBEDDING_MODEL_ID,
            "input": "data",
            "metadata": {"e2e_run_id": "not valid!"},
        },
    )
    assert invalid_correlation.status_code == 400

    overview = await store.overview()
    assert overview["unscoped_unexpected_calls"] == 2


async def test_control_state_never_retains_prompts_tool_arguments_or_headers(
    control: httpx.AsyncClient,
    inference: httpx.AsyncClient,
) -> None:
    run_id = "redaction-001"
    await arm(control, run_id)
    sensitive = "DO-NOT-RETAIN-THIS-SECRET"
    request = chat_request(run_id)
    request["messages"][0]["content"] = sensitive
    request["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "safe_name",
                "parameters": {"type": "object"},
                "description": sensitive,
            },
        }
    ]
    response = await inference.post(
        "/v1/chat/completions",
        json=request,
        headers={"Authorization": f"Bearer {INFERENCE_KEY}", "X-Secret": sensitive},
    )
    assert response.status_code == 200

    serialized_state = (await control.get(f"/control/scenarios/{run_id}")).text
    assert sensitive not in serialized_state
    assert INFERENCE_KEY not in serialized_state
    state = json.loads(serialized_state)
    assert set(state["calls"][0]) == {
        "run_id",
        "sequence",
        "correlation_id",
        "model",
        "endpoint",
        "stream",
        "outcome",
        "duration_ms",
    }
