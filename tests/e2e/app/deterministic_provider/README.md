# Deterministic E2E model provider

This test-owned service replaces only SRW's external model boundary in the
full-stack application journey. The agent still makes real HTTP requests and consumes
OpenAI-compatible streaming responses. The service never belongs in a customer Helm
release.

It exposes two listeners from one non-root process:

- inference on `:8000`: public `/health`, authenticated `/v1/models`,
  `/v1/chat/completions`, `/v1/embeddings`, and `/v1/rerank`;
- control on loopback `:8001`: bearer-protected `/control/health` and
  `/control/scenarios/...` routes.

The Kubernetes Service publishes inference only. The harness reaches control with
`kubectl port-forward` to the owned pod. `E2E_CONTROL_TOKEN` and
`E2E_INFERENCE_API_KEY` are required process environment variables and come from the
run-owned `srw-e2e-model-fixture` Secret; neither has a checked-in default.

## Control contract

Arm an isolated run before inference:

```text
POST /control/scenarios/{run-id}/arm
Authorization: Bearer <run-owned-token>
Content-Type: application/json

{"scenario":"reply","required_responses":1}
```

`GET /control/scenarios/{run-id}` returns required/consumed/remaining response
counts, `unexpected_count`, grouped counters, and sanitized call metadata. It never
returns prompts, messages, tool arguments, or headers. `DELETE` on the same URL resets
the run after browser transport and resource cleanup have finished.

Normal messages carry `E2E-{run-id}` and receive `E2E_REPLY:{run-id}`. Lifecycle
requests without a token are associated only when exactly one run is armed. Unknown,
ambiguous, exhausted, unsupported-schema, and wrong-model requests fail closed.

The `search-job` scenario is a narrow live-gate driver. It advances the real agent
through its strategic setup, stages a two-todo tactical phase, calls `web_search`,
then returns through `job_complete` and the remaining strategic todos. It reads the
todo guide required by the enforced staging contract before staging, and reads the
verification guide at each completion boundary. Tool results still pass through SRW
normally and are never retained by the fixture.

The `fetch-job` scenario follows the same fail-closed pattern for the off-pod fetch
boundary. It calls `extract_webpage` and `crawl_website` against `example.com` before
completing the job. It reads both guides required by the runtime's enforced staging and
completion contracts at their phase boundaries; fetched content still flows only through
SRW and is not retained.

## Local contract tests

From the repository root (using the repository Python environment):

```bash
pytest tests/e2e/app/deterministic_provider/test_provider.py -q
ruff check tests/e2e/app/deterministic_provider/
```

The container build context is this directory:

```bash
docker build -t srw-e2e-model-fixture:local tests/e2e/app/deterministic_provider
```
