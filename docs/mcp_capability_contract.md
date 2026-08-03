# MCP Capability Contract

**Status:** Authoritative for the orchestrator-facing MCP server
**Schema revision:** 3
**Implementation:** `orchestrator/mcp/capabilities.py`

## Authority and schema

`TOOL_CAPABILITIES` is the single capability/risk inventory for all registered
MCP tools. `server.py` refuses to register a tool without a matching entry, and
the contract test requires the registered and declared name sets to be exactly
equal (currently 104 tools).

Each entry records:

- tool name and REST operation;
- intended authorization (the orchestrator remains the enforcement boundary);
- side effects and destructive behavior;
- semantic idempotency and transport retry policy;
- the source of input/output schemas; and
- executable test coverage.

FastMCP derives `inputSchema` and `outputSchema` from the typed tool signature.
Registration derives `readOnlyHint`, `destructiveHint`, `idempotentHint`, and
`openWorldHint` from the contract and publishes the REST/auth/side-effect/retry
fields in each tool's `io.srw.capability` metadata. This avoids a second schema
copy drifting from `tools/list`.

The production Docker build writes the canonical raw schema to
`/app/tool-schema.json`. `/health` reports the live schema digest, baked schema
digest, match status, tool count, image digest, source revision, release,
Python version, FastMCP version, and MCP SDK version. A configured but missing
or mismatched schema artifact makes readiness degraded.

## Discovery and authorization

Tool discovery is intentionally stable rather than role-filtered. Privileged
tools remain visible with their authorization contract in tool metadata; the
orchestrator performs the authoritative role/resource/scope check on every
operation. This makes schema caching and digest comparison deterministic while
avoiding any reliance on discovery filtering as a security control.

## Retry contract

- Safe GET operations retry connect/timeout transport failures up to three
  attempts.
- Non-GET reads are sent once.
- Mutations are sent once, even when semantically idempotent.
- Read/write/response-stream failures after a mutation may have committed and
  are reported as an unknown outcome. Callers must verify through a read tool
  before deciding whether to issue another mutation.
- A future mutation may opt into retry only after an idempotency key is accepted
  and deduplicated end to end by the orchestrator.

## REST workflow decisions

`WORKFLOW_DECISIONS` in the authoritative manifest is the machine-checked
record. The current decisions are:

| Workflow | Decision | Reason |
|---|---|---|
| Direct job message-thread retrieval | Superseded | `get_message_thread` filters the authorized message collection. |
| Job accept/reject review actions | Required | Review semantics are not equivalent to approve/resume. |
| Job export | Required | Portable deliverable retrieval is a supported user workflow. |
| Job workspace upgrade, snapshot, and IDE controls | Intentionally excluded | Interactive infrastructure controls need explicit safety UX and remain Cockpit-only. |
| Persistent-session input and interrupt | Required | Read/create/resume alone do not complete the interactive workflow. |
| Persistent approvals | Required | Supervised sessions need a capability-scoped decision path. |
| Persistent configuration and tool groups | Intentionally excluded | Broad runtime reconfiguration needs a narrower schema and threat review. |
| Persistent cloud-diff internals | Intentionally excluded | Staging internals are not a stable user workflow. |
| Connector eligibility | Required | Agents need an authorized picker before attachment. |
| Connector indexing status and reindex | Required | Knowledge connectors need progress and recovery controls. |
| User expert and skill administration | Intentionally excluded | Package authoring/upload remains Cockpit-only pending validation design. |

## Verification

Run the source contract and client-safety tests:

```bash
pytest tests/test_mcp_capabilities.py \
  tests/test_mcp_client_safety.py \
  tests/test_mcp_client_contracts.py -q
```

Run the shipped-image protocol smoke test:

```bash
docker build -t srw-mcp-smoke -f docker/Dockerfile.mcp .
docker run --rm srw-mcp-smoke python image_smoke.py
```

The smoke test starts only a disposable in-container stub. It initializes two
fresh stdio MCP connections, compares raw `tools/list` with the baked artifact,
and executes representative read, mutation, denied, and degraded-service calls.
