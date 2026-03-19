# Orchestrator Logging

## Problem

Running the orchestrator locally with `LOG_LEVEL=DEBUG` produces output that is heavy on noise and light on useful detail. The two main symptoms:

### 1. Uvicorn access logs are shallow

Uvicorn's built-in access log only emits one line per request:

```
INFO:     127.0.0.1:51222 - "GET /api/health HTTP/1.1" 200 OK
INFO:     127.0.0.1:51240 - "GET /api/agents?limit=100 HTTP/1.1" 200 OK
```

This tells us nothing about what happened inside the request. A 500 response looks like:

```
INFO:     127.0.0.1:42240 - "POST /api/jobs HTTP/1.1" 500 Internal Server Error
```

No traceback, no context about which codepath failed or why. The endpoint handlers in `main.py` mostly don't log at entry/exit, so there's no app-level visibility into request processing either.

### 2. IMAP poller floods the terminal with expected-path noise

With `LOG_LEVEL=DEBUG`, the `services` namespace gets DEBUG level. The IMAP poller logs every non-routable email individually:

```
DEBUG services.imap_poller: No routing info found in email <...@community.patreon.com> — skipping
DEBUG services.imap_poller: No routing info found in email <...@news.groupe-pvcp.com> — skipping
DEBUG services.imap_poller: No routing info found in email <...@notification.norisbank.de> — skipping
... (30+ lines per poll cycle)
```

Most emails in the inbox are personal mail with no agent routing info. Skipping them is the normal, expected path — not something worth logging per-email. The actually interesting events (routable replies, parse failures) get buried.

### Summary

The problem is signal-to-noise ratio: we get too much detail on things that don't matter (every skipped email, every health check) and too little detail on things that do (request internals, error context, business logic flow).

## Current Setup

Logging is configured at the top of `orchestrator/main.py` (lines 23-40):

- `LOG_LEVEL` env var (default: `INFO`)
- When `DEBUG` without `DEBUG_ALL`: root stays at INFO, app namespaces (`orchestrator`, `main`, `database`, `security`, `services`, `uploads`, `mcp`, `graph_routes`, `workspace`) are set to DEBUG
- All services use `logger = logging.getLogger(__name__)` and inherit from this config
- Uvicorn runs with its own access logger, separate from app logging
- No request middleware for structured request/response logging

## Proposed Solutions

### A. IMAP Poller: Summary-per-cycle instead of line-per-email

Replace the per-email `logger.debug("No routing info found in email ...")` with a counter that produces a single summary line at the end of each poll cycle:

```
INFO  services.imap_poller: Poll complete: 28 checked, 0 routable, 1 duplicate
```

Keep individual logging for the interesting cases:
- Routable replies (already INFO)
- Parse failures, empty bodies (already WARNING)
- Duplicates — demote or remove, since the summary covers the count

This eliminates the 30-line block per cycle while preserving all actionable information.

### B. Request Logging Middleware

Add a FastAPI middleware that replaces uvicorn's shallow access log with app-level request logging:

**At INFO level** — one line per request with timing:
```
INFO  orchestrator.middleware: POST /api/jobs 201 (42ms)
INFO  orchestrator.middleware: GET /api/agents?limit=100 200 (8ms)
```

**At ERROR level** — full traceback for 5xx responses:
```
ERROR orchestrator.middleware: POST /api/jobs 500 (120ms)
Traceback (most recent call last):
  File "orchestrator/main.py", line 1680, in create_job
    ...
asyncpg.exceptions.UniqueViolationError: duplicate key value ...
```

**Filter health checks** — optionally suppress `/api/health` from INFO output since it fires every few seconds from the cockpit and contains no useful information.

### C. Suppress Uvicorn's Default Access Log

Once the middleware from (B) is in place, disable uvicorn's built-in access logger to avoid duplicate lines. This is done by passing `--access-log` flag or configuring it programmatically:

```python
# In uvicorn config or startup
uvicorn.run(app, access_log=False, ...)
```

Or when running from CLI:
```bash
uvicorn main:app --reload --port 8085 --no-access-log
```

The middleware provides strictly more information in a consistent format, so the default access log becomes redundant.

## Community Research

This is a well-known pain point in the FastAPI/uvicorn ecosystem. Below is a summary of the approaches and libraries the community has converged on.

### Approach 1: structlog + stdlib bridging

The most widely recommended approach for production FastAPI apps. The key idea: use structlog's `ProcessorFormatter` to bridge structlog and Python's stdlib `logging`, so logs from uvicorn, FastAPI internals, background services, and your application code all flow through one processor pipeline with consistent output.

**How it works:**
- structlog uses `contextvars` to bind request-scoped data (correlation ID, HTTP method, path, user) at the start of each request
- Every log emitted during that request automatically includes that context — no manual threading of request IDs
- `ProcessorFormatter` wraps stdlib formatters, so uvicorn's own log calls also go through structlog
- Dev mode uses `ConsoleRenderer` (colored, human-readable); prod uses `JSONRenderer` (machine-parseable)

**Canonical reference:** [nymous's structlog+FastAPI gist](https://gist.github.com/nymous/f138c7f06062b7c43c060bf03759c29e) — the most referenced complete example, with extensive comments. Covers structlog, correlation IDs, and Datadog trace enrichment.

**Walkthrough:** [Integrating FastAPI with Structlog (wazaari.dev)](https://wazaari.dev/blog/fastapi-structlog-integration) — covers contextvars binding, exception handling in middleware, and why FastAPI's `@app.exception_handler` does *not* fire for exceptions raised in middleware.

### Approach 2: loguru + InterceptHandler

The simpler alternative, popular for smaller projects. Uses an `InterceptHandler` class that intercepts all stdlib logging and redirects to loguru's global logger.

**Trade-offs vs structlog:**
- Pro: Simpler API, better exception tracebacks out of the box, zero-config pretty printing
- Con: Writes to stdout directly (can conflict with Sentry SDK and tools that hook into stdlib). Less flexible processor pipeline. Custom formatters that error can crash loguru silently.

**Critical setup detail:** Pass `log_config=None` to uvicorn to prevent it from overriding your logging config. Explicitly set handlers on the `"uvicorn"`, `"uvicorn.access"`, and `"uvicorn.error"` loggers — they don't appear in the root logger manager.

**Canonical reference:** [Unify logging for a gunicorn/uvicorn app (pawamoy)](https://pawamoy.github.io/posts/unify-logging-for-a-gunicorn-uvicorn-app/)

### Approach 3: Custom middleware + stdlib (no new deps)

The minimal approach: disable uvicorn's access logger, write a `@app.middleware("http")` that captures method/path/status/timing yourself. Works for basic needs but no structured output or automatic context propagation.

### Libraries

| Library | Purpose | Notes |
|---------|---------|-------|
| [asgi-correlation-id](https://github.com/snok/asgi-correlation-id) | Request ID propagation | Reads/generates `X-Request-ID`, stores in contextvar, adds to response headers. Includes a logging filter for `%(correlation_id)s`. Works with structlog and loguru. |
| [fastapi-structlog](https://pypi.org/project/fastapi-structlog/) | Batteries-included structlog wrapper | Middleware, Sentry integration, DB log model, Pydantic-based config. More opinionated. |
| [fastapi-structured-logging](https://github.com/babs/fastapi-structured-logging) | structlog + OpenTelemetry | `AccessLogMiddleware` with path filtering, trusted-proxy handling, trace/span ID enrichment. Good for observability stacks. |
| [fastapi-route-logger-middleware](https://pypi.org/project/fastapi-route-logger-middleware/) | Route-level logging | Has `skip_regexes` parameter for filtering noisy routes like `/health`. |

### Filtering /health and other noisy endpoints

Common pain point with multiple GitHub issues. Two standard solutions:

1. **Custom `logging.Filter`** on the `uvicorn.access` logger — check `record.args` for the path and return `False` for `/health`, `/readiness`, etc. ([joshdimella.com](https://joshdimella.com/blog/filtering-fastapi-logs), [dev.to](https://dev.to/mukulsharma/taming-fastapi-access-logs-3idi))

2. **Middleware-based filtering** — if you replace uvicorn's access log with your own middleware, just skip logging for excluded paths. Cleaner since you own the entire pipeline.

There's an open FastAPI discussion ([#13523](https://github.com/fastapi/fastapi/discussions/13523)) requesting native endpoint-level log suppression, but no built-in support yet.

### Getting tracebacks for 500 errors

Another major pain point. Uvicorn swallows tracebacks by default for unhandled exceptions (made worse since uvicorn 0.108+, see [FastAPI discussion #11060](https://github.com/fastapi/fastapi/discussions/11060)).

**Recommended pattern:** Exception-handling ASGI middleware with `try/except` around `await call_next(request)`. Use `logger.exception()` in the except block, then return `JSONResponse(status_code=500)`. Important: `@app.exception_handler(Exception)` only catches route-handler exceptions, not middleware exceptions — so the middleware approach is necessary for full coverage.

**Middleware ordering:** FastAPI middleware follows an onion model. Error-handling middleware must be registered outermost to catch exceptions from inner layers ([FastAPI discussion #10404](https://github.com/fastapi/fastapi/discussions/10404)).

### Recommendation

For this project, **structlog + asgi-correlation-id** appears to be the strongest fit:

1. **Unified output** — structlog's `ProcessorFormatter` bridges all logging sources (uvicorn, FastAPI, app code, background services like the IMAP poller) through one pipeline
2. **Request correlation** — `asgi-correlation-id` + `structlog.contextvars` gives per-request correlation with minimal boilerplate
3. **Noise control** — per-module log levels via stdlib `dictConfig` + path-based filtering in middleware
4. **500 tracebacks** — exception middleware with `logger.exception()` through structlog gives structured tracebacks with correlation IDs
5. **Dev/prod split** — `ConsoleRenderer` in dev, `JSONRenderer` in prod, same code

That said, the no-dependency approach (custom middleware + stdlib) may be sufficient given the orchestrator is primarily a single-user dev tool. structlog adds a dependency but solves the correlation and structured output problems more completely.

### Sources

- [nymous's structlog+FastAPI+Datadog gist](https://gist.github.com/nymous/f138c7f06062b7c43c060bf03759c29e)
- [Integrating FastAPI with Structlog — wazaari.dev](https://wazaari.dev/blog/fastapi-structlog-integration)
- [Unify Python logging for Gunicorn/Uvicorn/FastAPI — pawamoy](https://pawamoy.github.io/posts/unify-logging-for-a-gunicorn-uvicorn-app/)
- [A complete guide to logging in FastAPI — Apitally](https://apitally.io/blog/fastapi-logging-guide)
- [Filtering FastAPI Logs — joshdimella.com](https://joshdimella.com/blog/filtering-fastapi-logs)
- [Taming FastAPI Access Logs — dev.to](https://dev.to/mukulsharma/taming-fastapi-access-logs-3idi)
- [FastAPI Server Errors And Logs — Roy Pasternak (Medium)](https://medium.com/@roy-pstr/fastapi-server-errors-and-logs-take-back-control-696405437983)
- [Structured JSON Logging using FastAPI — sheshbabu.com](https://www.sheshbabu.com/posts/fastapi-structured-json-logging/)
- [Production-Grade Logging for FastAPI — Medium (2026)](https://medium.com/@laxsuryavanshi.dev/production-grade-logging-for-fastapi-applications-a-complete-guide-f384d4b8f43b)
- [FastAPI Middleware Patterns 2026 — johal.in](https://johal.in/fastapi-middleware-patterns-custom-logging-metrics-and-error-handling-2026-2/)
- [Uvicorn exceptions not shown since 0.108 — GitHub Discussion #11060](https://github.com/fastapi/fastapi/discussions/11060)
- [Disable logging for certain endpoints — GitHub Discussion #13523](https://github.com/fastapi/fastapi/discussions/13523)
- [How to handle errors in middleware — GitHub Discussion #10404](https://github.com/fastapi/fastapi/discussions/10404)

## Open Questions

- Should the middleware add a request ID (e.g. short UUID) for correlating log lines within a single request? Useful when multiple requests are in flight, but adds noise if the orchestrator is mostly single-user during development.
- Should we log request bodies for POST/PUT at DEBUG level? Helpful for debugging but could leak sensitive data (API keys, credentials).
- Are there other noisy loggers besides the IMAP poller that should be tuned? (e.g. NATS heartbeats, agent heartbeat processing)
- structlog vs no-dependency approach: is the added dependency worth it given the orchestrator is primarily used locally for development?
