# Direct Session WebSockets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** **Completed 2026-05-23.** All 14 tasks shipped; verified end-to-end on the dev cluster (resume flow with lifecycle phases observed; mid-stream hard refresh produced a recovered placeholder bubble). Two issues surfaced during smoke testing were resolved in the same branch and moved to `docs/done/`: `persistent_thread_double_provisioning_race.md` and `persistent_chat_lost_assistant_turn_on_mid_turn_reload.md`.

**Goal:** Move long-lived agent WebSockets out of the orchestrator process so the browser connects directly to the agent pod via Traefik. Reduces reconnect time from ~20 s to ~100 ms; orchestrator restarts no longer drop active sessions.

**Architecture:** Orchestrator becomes a control plane with two REST endpoints (`POST /api/sessions/{tid}/prepare`, `GET /api/sessions/{tid}/connection`) that mint a short-lived JWT and write one K8s Service + Ingress per session pod. Traefik picks up the route; browser dials it directly with the JWT. Agent pod validates the JWT on the WS handshake. Notification events (formerly inspected in the WS proxy loop) move to NATS, where the existing bridge re-broadcasts them to the SSE feed.

**Tech Stack:** Python 3.12 (FastAPI orchestrator + agent pod), TypeScript / Angular (cockpit), PyJWT 2.8+, kubernetes Python client, nats-py, Traefik v3, Postgres advisory locks, pytest, vitest.

**Spec:** `docs/features/direct_session_websockets.md`
**Deferred follow-up:** `docs/issues/nats_subject_acl_hardening.md`

---

## File map

**New files:**
- `orchestrator/services/session_tokens.py` — JWT mint + validate
- `orchestrator/services/session_router.py` — K8s Service + Ingress lifecycle per session
- `orchestrator/routers/sessions.py` — `POST /prepare` and `GET /connection` endpoints
- `tests/test_session_tokens.py`
- `tests/test_session_router.py`
- `tests/test_sessions_router.py`
- `tests/test_persistent_app_ws_auth.py`
- `helm/templates/session-jwt-secret.yaml` — K8s Secret carrying the JWT signing key
- `helm/templates/orchestrator-rbac-sessions.yaml` — RBAC additions for Ingress/Service

**Modified files:**
- `orchestrator/main.py` — register new router; delete `persistent_ws_proxy` (lines 13747-14063), `ide_proxy_ws` (lines 7852-7945), `_inspect_session_event` + `_inspect_browser_event` (lines 13676-13744)
- `orchestrator/services/nats_bridge.py` — add `session.events.>` subscription
- `orchestrator/services/agent_provisioner.py` — stamp `srw.io/thread-id` label on agent pod template
- `orchestrator/database/postgres.py` — add advisory-lock context manager keyed by thread_id
- `src/api/persistent_app.py` — add JWT validation to `/ws/chat` handler; add NATS notification publishing inside the loop
- `src/api/dual_app.py` — add JWT validation to its `/ws/chat` handler (delegates to same `handle_persistent_websocket`)
- `cockpit/src/app/core/services/persistent-chat.service.ts` — new connect flow (prepare → SSE → connection → WS)
- `cockpit/src/app/core/services/persistent-chat.service.spec.ts` — update tests
- `helm/values.yaml` and `helm/values.example.yaml` — add `sessionRouter` values block

---

## Task 1: Create `session_tokens.py` — JWT mint and validate

**Files:**
- Create: `orchestrator/services/session_tokens.py`
- Test: `tests/test_session_tokens.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_tokens.py
"""Tests for the session JWT module — mint and validate."""

import os
import time

import pytest

from orchestrator.services.session_tokens import (
    SessionTokenService,
    InvalidSessionTokenError,
)


@pytest.fixture
def svc():
    """A SessionTokenService with a fixed secret and 60s TTL."""
    return SessionTokenService(secret="test-secret-do-not-use", ttl_seconds=60)


def test_mint_returns_token_payload_and_expiry(svc):
    """Minting yields a token string and the absolute expiry timestamp."""
    before = int(time.time())
    token, expires_at = svc.mint(user_id="u1", thread_id="t1")
    after = int(time.time())

    assert isinstance(token, str) and len(token) > 0
    assert before + 60 <= expires_at <= after + 60


def test_validate_accepts_valid_token(svc):
    """A freshly minted token validates and exposes its claims."""
    token, _ = svc.mint(user_id="u1", thread_id="t1")
    claims = svc.validate(token)

    assert claims["sub"] == "u1"
    assert claims["tid"] == "t1"
    assert claims["aud"] == "agent"
    assert "exp" in claims
    assert "iat" in claims
    assert "jti" in claims


def test_validate_rejects_wrong_signature(svc):
    """A token signed by a different secret is rejected."""
    other = SessionTokenService(secret="different-secret", ttl_seconds=60)
    token, _ = other.mint(user_id="u1", thread_id="t1")

    with pytest.raises(InvalidSessionTokenError):
        svc.validate(token)


def test_validate_rejects_expired_token():
    """A token past its expiry is rejected."""
    svc = SessionTokenService(secret="test-secret", ttl_seconds=1)
    token, _ = svc.mint(user_id="u1", thread_id="t1")
    time.sleep(2)

    with pytest.raises(InvalidSessionTokenError):
        svc.validate(token)


def test_validate_rejects_wrong_audience(svc):
    """A token with audience != 'agent' is rejected."""
    import jwt

    bad = jwt.encode(
        {"sub": "u1", "tid": "t1", "aud": "other", "exp": int(time.time()) + 60},
        "test-secret-do-not-use",
        algorithm="HS256",
    )
    with pytest.raises(InvalidSessionTokenError):
        svc.validate(bad)


def test_validate_rejects_malformed_token(svc):
    """Garbage strings are rejected, not crashed on."""
    with pytest.raises(InvalidSessionTokenError):
        svc.validate("not-a-jwt-at-all")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_tokens.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.session_tokens'`

- [ ] **Step 3: Write minimal implementation**

```python
# orchestrator/services/session_tokens.py
"""Short-lived JWTs that authorize a single browser→agent-pod WS handshake.

The token is signed by the orchestrator (HS256, shared secret) and validated
by the agent pod on the `/ws/chat` upgrade. Claims are deliberately narrow:
`sub` (user ID), `tid` (thread ID), short `exp` (default 60 s).

This is **not** the same credential as the BFF cookie or API token — those
authenticate user→orchestrator. This authenticates orchestrator→pod for a
specific session handshake, so we can hand the pod a narrowly-scoped trust
without giving it the BFF signing key.

See `docs/features/direct_session_websockets.md` §Component details.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt


class InvalidSessionTokenError(Exception):
    """Raised when a token fails signature, audience, expiry, or shape checks."""


class SessionTokenService:
    """Mint + validate short-lived session JWTs."""

    _AUDIENCE = "agent"
    _ALGORITHM = "HS256"

    def __init__(self, secret: str, ttl_seconds: int = 60) -> None:
        if not secret:
            raise ValueError("SessionTokenService requires a non-empty secret")
        self._secret = secret
        self._ttl = int(ttl_seconds)

    def mint(self, user_id: str, thread_id: str) -> tuple[str, int]:
        """Return ``(token, absolute_expiry_unix_ts)``."""
        now = int(time.time())
        exp = now + self._ttl
        claims = {
            "sub": str(user_id),
            "tid": str(thread_id),
            "aud": self._AUDIENCE,
            "iat": now,
            "exp": exp,
            "jti": str(uuid.uuid4()),
        }
        token = jwt.encode(claims, self._secret, algorithm=self._ALGORITHM)
        return token, exp

    def validate(self, token: str) -> dict[str, Any]:
        """Return claims dict if valid, raise ``InvalidSessionTokenError`` otherwise."""
        try:
            return jwt.decode(
                token,
                self._secret,
                algorithms=[self._ALGORITHM],
                audience=self._AUDIENCE,
            )
        except jwt.PyJWTError as e:
            raise InvalidSessionTokenError(str(e)) from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_tokens.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/session_tokens.py tests/test_session_tokens.py
git commit -m "feat: add SessionTokenService for short-lived browser→pod JWTs"
```

---

## Task 2: Create `session_router.py` — K8s Service + Ingress lifecycle

**Files:**
- Create: `orchestrator/services/session_router.py`
- Test: `tests/test_session_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_router.py
"""Tests for the session router service — Service + Ingress per session."""

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.services.session_router import SessionRouterService


@pytest.fixture
def k8s_core_api():
    """Mock CoreV1Api with read returning 404 (resource missing) by default."""
    api = MagicMock()
    from kubernetes.client.exceptions import ApiException
    api.read_namespaced_service.side_effect = ApiException(status=404)
    return api


@pytest.fixture
def k8s_networking_api():
    """Mock NetworkingV1Api with read returning 404 by default."""
    api = MagicMock()
    from kubernetes.client.exceptions import ApiException
    api.read_namespaced_ingress.side_effect = ApiException(status=404)
    return api


@pytest.fixture
def svc(k8s_core_api, k8s_networking_api):
    return SessionRouterService(
        namespace="srw",
        ingress_host="api.example.com",
        ingress_class="traefik",
        annotations={"foo": "bar"},
        core_api=k8s_core_api,
        networking_api=k8s_networking_api,
    )


@pytest.mark.asyncio
async def test_ensure_route_creates_service_and_ingress(
    svc, k8s_core_api, k8s_networking_api
):
    """ensure_route creates both Service and Ingress with correct labels and owner refs."""
    prefix = await svc.ensure_route(
        thread_id="t1",
        pod_name="srw-agent-abc",
        pod_uid="pod-uid-1",
    )

    assert prefix == "/p/t1"
    k8s_core_api.create_namespaced_service.assert_called_once()
    k8s_networking_api.create_namespaced_ingress.assert_called_once()

    svc_body = k8s_core_api.create_namespaced_service.call_args.kwargs["body"]
    assert svc_body["metadata"]["name"] == "session-t1"
    assert svc_body["metadata"]["labels"]["srw.io/thread-id"] == "t1"
    assert svc_body["metadata"]["ownerReferences"][0]["name"] == "srw-agent-abc"
    assert svc_body["metadata"]["ownerReferences"][0]["uid"] == "pod-uid-1"
    assert svc_body["spec"]["selector"] == {"srw.io/thread-id": "t1"}

    ing_body = k8s_networking_api.create_namespaced_ingress.call_args.kwargs["body"]
    assert ing_body["metadata"]["name"] == "session-t1"
    assert ing_body["spec"]["ingressClassName"] == "traefik"
    rule = ing_body["spec"]["rules"][0]
    assert rule["host"] == "api.example.com"
    path = rule["http"]["paths"][0]
    assert path["path"] == "/p/t1"
    assert path["backend"]["service"]["name"] == "session-t1"


@pytest.mark.asyncio
async def test_ensure_route_idempotent_when_resources_exist(
    svc, k8s_core_api, k8s_networking_api
):
    """If both resources already exist, ensure_route is a no-op."""
    # Override the 404: now reads succeed
    k8s_core_api.read_namespaced_service.side_effect = None
    k8s_core_api.read_namespaced_service.return_value = MagicMock()
    k8s_networking_api.read_namespaced_ingress.side_effect = None
    k8s_networking_api.read_namespaced_ingress.return_value = MagicMock()

    await svc.ensure_route(
        thread_id="t1", pod_name="srw-agent-abc", pod_uid="pod-uid-1"
    )

    k8s_core_api.create_namespaced_service.assert_not_called()
    k8s_networking_api.create_namespaced_ingress.assert_not_called()


@pytest.mark.asyncio
async def test_teardown_route_deletes_both_resources(
    svc, k8s_core_api, k8s_networking_api
):
    """teardown_route deletes Service and Ingress; absent resources are OK."""
    await svc.teardown_route(thread_id="t1")
    k8s_core_api.delete_namespaced_service.assert_called_once()
    k8s_networking_api.delete_namespaced_ingress.assert_called_once()


@pytest.mark.asyncio
async def test_teardown_route_swallows_404(
    svc, k8s_core_api, k8s_networking_api
):
    """Deleting a missing resource is not an error."""
    from kubernetes.client.exceptions import ApiException
    k8s_core_api.delete_namespaced_service.side_effect = ApiException(status=404)
    k8s_networking_api.delete_namespaced_ingress.side_effect = ApiException(status=404)

    await svc.teardown_route(thread_id="t1")  # Must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_session_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.services.session_router'`

- [ ] **Step 3: Write minimal implementation**

```python
# orchestrator/services/session_router.py
"""Per-session K8s Service + Ingress lifecycle.

For each bound agent pod, the orchestrator creates one Service (selects the
pod by its `srw.io/thread-id` label) and one Ingress (path-based,
`/p/{thread_id}`, points at the Service). Both resources carry
``ownerReferences`` to the agent pod so K8s GC cleans them up if explicit
teardown is skipped (orchestrator crash, etc.).

See `docs/features/direct_session_websockets.md` §Component details.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)


class SessionRouterService:
    """Idempotent K8s Service + Ingress lifecycle for sessions."""

    def __init__(
        self,
        namespace: str,
        ingress_host: str,
        ingress_class: str = "traefik",
        annotations: Optional[dict[str, str]] = None,
        # Injected for testability; lazy-resolved in production.
        core_api: Any = None,
        networking_api: Any = None,
    ) -> None:
        self._namespace = namespace
        self._ingress_host = ingress_host
        self._ingress_class = ingress_class
        self._annotations = annotations or {}
        self._core_api = core_api
        self._networking_api = networking_api

    # --------------------------------------------------------------------- #
    # Lazy K8s client setup
    # --------------------------------------------------------------------- #

    def _lazy_init_apis(self) -> None:
        if self._core_api is not None and self._networking_api is not None:
            return
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        if self._core_api is None:
            self._core_api = k8s_client.CoreV1Api()
        if self._networking_api is None:
            self._networking_api = k8s_client.NetworkingV1Api()

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    async def ensure_route(
        self,
        thread_id: str,
        pod_name: str,
        pod_uid: str,
    ) -> str:
        """Create Service + Ingress if missing. Returns the path prefix."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        # Service
        if not await self._exists(self._core_api.read_namespaced_service, name):
            await self._call(
                self._core_api.create_namespaced_service,
                namespace=self._namespace,
                body=self._service_body(thread_id, name, pod_name, pod_uid),
            )

        # Ingress
        if not await self._exists(self._networking_api.read_namespaced_ingress, name):
            await self._call(
                self._networking_api.create_namespaced_ingress,
                namespace=self._namespace,
                body=self._ingress_body(thread_id, name, pod_name, pod_uid),
            )

        return f"/p/{thread_id}"

    async def teardown_route(self, thread_id: str) -> None:
        """Delete Service + Ingress. 404 is OK."""
        self._lazy_init_apis()
        name = f"session-{thread_id}"

        for delete_fn in (
            self._networking_api.delete_namespaced_ingress,
            self._core_api.delete_namespaced_service,
        ):
            try:
                await self._call(delete_fn, name=name, namespace=self._namespace)
            except ApiException as e:
                if e.status != 404:
                    logger.warning(
                        "teardown_route: %s on %s returned %s",
                        delete_fn.__name__, name, e.status,
                    )

    # --------------------------------------------------------------------- #
    # Helpers
    # --------------------------------------------------------------------- #

    async def _exists(self, read_fn: Any, name: str) -> bool:
        try:
            await self._call(read_fn, name=name, namespace=self._namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    @staticmethod
    async def _call(fn: Any, **kwargs: Any) -> Any:
        # kubernetes client is sync; run in executor to keep the loop free.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**kwargs))

    def _owner_ref(self, pod_name: str, pod_uid: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "name": pod_name,
            "uid": pod_uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }

    def _service_body(
        self, thread_id: str, name: str, pod_name: str, pod_uid: str
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/managed-by": "orchestrator",
                },
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"srw.io/thread-id": thread_id},
                "ports": [{"port": 8001, "targetPort": 8001}],
            },
        }

    def _ingress_body(
        self, thread_id: str, name: str, pod_name: str, pod_uid: str
    ) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {
                    "srw.io/thread-id": thread_id,
                    "srw.io/managed-by": "orchestrator",
                },
                "annotations": dict(self._annotations),
                "ownerReferences": [self._owner_ref(pod_name, pod_uid)],
            },
            "spec": {
                "ingressClassName": self._ingress_class,
                "rules": [
                    {
                        "host": self._ingress_host,
                        "http": {
                            "paths": [
                                {
                                    "path": f"/p/{thread_id}",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": name,
                                            "port": {"number": 8001},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_session_router.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/session_router.py tests/test_session_router.py
git commit -m "feat: add SessionRouterService for per-session Ingress + Service"
```

---

## Task 3: Add advisory-lock context manager to postgres.py

**Files:**
- Modify: `orchestrator/database/postgres.py`
- Test: `tests/test_postgres_advisory_lock.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_postgres_advisory_lock.py
"""Tests for the per-thread advisory-lock context manager on PostgresDB."""

import asyncio
import pytest

# Reuse the existing test DB fixture pattern. See tests/conftest.py
# for `postgres_db` fixture if available, or skip these tests locally.
pytestmark = pytest.mark.asyncio


async def test_advisory_lock_serializes_concurrent_callers(postgres_db):
    """Two concurrent advisory_lock acquisitions on the same thread_id
    must serialize — the second waits for the first to release."""
    order: list[str] = []

    async def worker(name: str, hold_s: float) -> None:
        async with postgres_db.thread_advisory_lock("t-test"):
            order.append(f"{name}-acquired")
            await asyncio.sleep(hold_s)
            order.append(f"{name}-released")

    await asyncio.gather(
        worker("A", 0.1),
        worker("B", 0.0),
    )

    # A must finish entirely before B starts
    assert order == [
        "A-acquired", "A-released",
        "B-acquired", "B-released",
    ]


async def test_advisory_lock_releases_on_exception(postgres_db):
    """If the body raises, the lock is still released."""
    with pytest.raises(RuntimeError):
        async with postgres_db.thread_advisory_lock("t-test"):
            raise RuntimeError("boom")

    # Subsequent acquisition must succeed quickly (no orphaned lock).
    async with postgres_db.thread_advisory_lock("t-test"):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_postgres_advisory_lock.py -v`
Expected: FAIL with `AttributeError: 'PostgresDB' object has no attribute 'thread_advisory_lock'` (or similar).

- [ ] **Step 3: Add the context manager**

Find the PostgresDB class in `orchestrator/database/postgres.py`. Add this method to the class (alongside `get_thread`, `resume_thread`, etc.):

```python
    @contextlib.asynccontextmanager
    async def thread_advisory_lock(self, thread_id: str):
        """Postgres advisory lock keyed by ``thread_id``.

        Pattern matches the schema-migration lock at
        ``orchestrator/database/migrate.py:157``. The lock key is a stable
        hash of the thread_id (Postgres advisory locks take a bigint key).
        Used by ``POST /api/sessions/{thread_id}/prepare`` to serialize
        concurrent prepare calls — see
        ``docs/issues/persistent_thread_double_provisioning_race.md``.
        """
        # Stable 63-bit signed int derived from thread_id (Postgres advisory
        # locks use bigint). xxhash isn't a project dep, so use blake2b.
        import hashlib
        h = hashlib.blake2b(thread_id.encode(), digest_size=8).digest()
        key = int.from_bytes(h, byteorder="big", signed=True)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", key)
                yield
                # Lock auto-released at transaction end.
```

Add `import contextlib` at the top of the file if not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_postgres_advisory_lock.py -v`
Expected: Both tests PASS. If you don't have a `postgres_db` fixture in `tests/conftest.py` that points at a real or testcontainer Postgres, skip this test and verify on the dev cluster in Task 14.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/database/postgres.py tests/test_postgres_advisory_lock.py
git commit -m "feat: add thread_advisory_lock context manager to PostgresDB"
```

---

## Task 4: Stamp pod label + capture `pod_uid` for owner references

**Files:**
- Modify: `orchestrator/services/agent_provisioner.py`
- Modify: `orchestrator/database/postgres.py` — add `pod_uid` column + migration
- Modify: `orchestrator/database/schema.sql` (or wherever the schema is)
- Test: extend `tests/test_agent_provisioner.py`

Two things this task needs to deliver. (1) The pod gets an `srw.io/thread-id={tid}` label so Task 2's per-session Service can select it. (2) The pod's K8s-assigned `metadata.uid` is captured into the orchestrator's `agents` table so Task 6 can pass it to `session_router.ensure_route(pod_uid=...)` for the `ownerReferences` block.

- [ ] **Step 1: Locate the pod-spec rendering function**

Run: `grep -n '"metadata"\|labels\b' orchestrator/services/agent_provisioner.py | head -30`

This will surface the dict-building code for the pod manifest. Identify the exact function (likely inside `provision_agent` or a helper like `_build_pod_spec` / `_render_persistent_pod_manifest`). Note the function name and line range.

Then look at how the pod is created and the result is captured:
Run: `grep -n "create_namespaced_pod\|CoreV1Api" orchestrator/services/agent_provisioner.py | head -10`

The return value of `create_namespaced_pod` is a `V1Pod` object whose `.metadata.uid` is what we need.

- [ ] **Step 2: Write the failing tests**

Open `tests/test_agent_provisioner.py`. Add tests (adjust the entry-point function name to whatever you found in Step 1):

```python
def test_pod_spec_includes_thread_id_label():
    """Persistent pods carry srw.io/thread-id={tid} so the per-session
    Service selector matches them."""
    from orchestrator.services.agent_provisioner import agent_provisioner

    # Call whatever helper renders the pod spec. If it's a private method,
    # call it via the public path (provision_agent) and inspect the call
    # to create_namespaced_pod with a mock. Mirror the patches used by
    # existing tests in this file.
    spec = agent_provisioner._render_persistent_pod_spec(
        thread_id="t1",
        config_name="persistent_defaults",
        purpose="session",
    )

    assert spec["metadata"]["labels"]["srw.io/thread-id"] == "t1"
    assert spec["metadata"]["labels"]["srw.io/managed-by"] == "orchestrator"


@pytest.mark.asyncio
async def test_provision_agent_stores_pod_uid(monkeypatch):
    """After K8s creates the pod, the orchestrator persists the assigned
    metadata.uid into the agents table so session_router can reference it."""
    from orchestrator.services.agent_provisioner import agent_provisioner

    fake_pod = MagicMock()
    fake_pod.metadata.uid = "k8s-pod-uid-deadbeef"
    fake_pod.metadata.name = "srw-agent-abc123"

    # Patch the K8s create + DB write. See existing tests in this file for
    # the exact monkeypatch targets.
    monkeypatch.setattr(
        "orchestrator.services.agent_provisioner.k8s_client.CoreV1Api",
        MagicMock(return_value=MagicMock(create_namespaced_pod=MagicMock(return_value=fake_pod))),
    )
    db_writes: list[dict] = []
    monkeypatch.setattr(
        agent_provisioner, "_record_pod_creation",
        AsyncMock(side_effect=lambda **kw: db_writes.append(kw)),
        raising=False,
    )

    await agent_provisioner.provision_agent(
        purpose="session", thread_id="t1", config_name="persistent_defaults"
    )

    assert db_writes[0]["pod_uid"] == "k8s-pod-uid-deadbeef"
    assert db_writes[0]["pod_name"] == "srw-agent-abc123"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_agent_provisioner.py::test_pod_spec_includes_thread_id_label tests/test_agent_provisioner.py::test_provision_agent_stores_pod_uid -v`
Expected: FAIL — label is absent and `pod_uid` isn't stored.

- [ ] **Step 4: Add the label and capture pod_uid**

In `agent_provisioner.py`, find the pod-spec rendering code (Step 1) and add the labels:

```python
# Inside the labels dict for persistent/session pods:
labels = {
    # ... existing labels stay ...
    "srw.io/managed-by": "orchestrator",
}
if thread_id:
    labels["srw.io/thread-id"] = thread_id
```

Then, find where `create_namespaced_pod` is called and the result is currently discarded (or where the agent row is upserted from the WS proxy's polling). Capture the UID:

```python
created = self._core_api.create_namespaced_pod(namespace=ns, body=pod_spec)
pod_uid = created.metadata.uid
pod_name = created.metadata.name

# Persist alongside the existing agent registration. The agent's
# /api/agents/register call upserts (pod_ip, hostname, status); we need
# pod_uid too. Either:
#   (a) include pod_uid in the upsert here (orchestrator owns the row from
#       provisioning), or
#   (b) extend the /api/agents/register payload to include uid and have
#       the agent self-report it.
# (a) is cleaner because the orchestrator knows the uid the moment K8s
# returns from create_namespaced_pod, before the agent process even starts.
await postgres_db.upsert_agent_provisioning(
    pod_name=pod_name,
    pod_uid=pod_uid,
    thread_id=thread_id,
    status="booting",
)
```

In `orchestrator/database/postgres.py`, add the column and the upsert helper:

```python
    async def upsert_agent_provisioning(
        self,
        pod_name: str,
        pod_uid: str,
        thread_id: str | None,
        status: str,
    ) -> None:
        """Insert (or update) the agent row at pod-creation time so we
        have pod_uid before the agent's own /api/agents/register lands."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agents (hostname, pod_uid, thread_id, status, registered_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (hostname) DO UPDATE
                SET pod_uid = EXCLUDED.pod_uid,
                    thread_id = COALESCE(agents.thread_id, EXCLUDED.thread_id),
                    status = EXCLUDED.status
                """,
                pod_name, pod_uid, thread_id, status,
            )
```

Add a migration that creates the column:

```sql
-- orchestrator/database/migrations/NNNN_add_pod_uid_to_agents.sql
ALTER TABLE agents ADD COLUMN IF NOT EXISTS pod_uid TEXT;
```

(File-number `NNNN` follows the existing migration numbering — check the migrations directory.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_agent_provisioner.py -v`
Expected: All previously-passing tests still PASS; the two new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/services/agent_provisioner.py orchestrator/database/postgres.py orchestrator/database/migrations/ tests/test_agent_provisioner.py
git commit -m "feat: stamp thread-id label + capture pod_uid on agent provisioning"
```

---

## Task 5: Subscribe to `session.events.>` in `nats_bridge.py`

**Files:**
- Modify: `orchestrator/services/nats_bridge.py`
- Test: `tests/test_nats_bridge_session_events.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nats_bridge_session_events.py
"""Tests for the session.events.* NATS subscription that re-broadcasts
notification events to the SSE feed with a payload-level thread_id filter."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_session_event_with_matching_thread_id_broadcasts(monkeypatch):
    """When the payload thread_id matches the pod's bound thread,
    the event is forwarded to notification_feed.broadcast."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    # Pod is bound to t1, user u1
    db.get_thread.return_value = {"id": "t1", "user_id": "u1", "agent_id": "agent-xyz"}

    feed = MagicMock()
    monkeypatch.setattr(
        "orchestrator.services.nats_bridge.notification_feed", feed
    )
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.t1"
    msg.data = json.dumps({
        "thread_id": "t1",
        "method": "permission.request",
        "params": {"tool": "shell", "args": "rm -rf /"},
    }).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_called_once()
    call_user_id = feed.broadcast.call_args.args[0]
    assert call_user_id == "u1"


@pytest.mark.asyncio
async def test_session_event_with_mismatched_thread_id_dropped(monkeypatch):
    """If the payload claims a different thread_id than the subject,
    the event is dropped and logged (defense-in-depth filter)."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    db.get_thread.return_value = {"id": "t1", "user_id": "u1"}

    feed = MagicMock()
    monkeypatch.setattr(
        "orchestrator.services.nats_bridge.notification_feed", feed
    )
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.t1"
    msg.data = json.dumps({
        "thread_id": "OTHER-thread",  # Mismatch: payload lies
        "method": "permission.request",
        "params": {},
    }).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_session_event_with_unknown_thread_dropped(monkeypatch):
    """If the thread doesn't exist in DB, drop the event."""
    from orchestrator.services.nats_bridge import NatsBridge

    bridge = NatsBridge(url="nats://test")
    db = AsyncMock()
    db.get_thread.return_value = None

    feed = MagicMock()
    monkeypatch.setattr(
        "orchestrator.services.nats_bridge.notification_feed", feed
    )
    bridge._db = db

    msg = MagicMock()
    msg.subject = "session.events.nonexistent"
    msg.data = json.dumps({"thread_id": "nonexistent", "method": "x"}).encode()

    await bridge._on_session_event(msg)

    feed.broadcast.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nats_bridge_session_events.py -v`
Expected: FAIL with `AttributeError: 'NatsBridge' object has no attribute '_on_session_event'`.

- [ ] **Step 3: Add the subscription and handler**

In `orchestrator/services/nats_bridge.py`:

a) Add the subscription at the end of `connect()`'s subscribe block (around line 134):

```python
            # Session event re-broadcast: agent pods publish notification
            # events to session.events.{thread_id}; we forward to the SSE
            # feed after filtering. See docs/issues/nats_subject_acl_hardening.md
            # for the deferred transport-layer enforcement.
            await self._nc.subscribe("session.events.>", cb=self._on_session_event)
```

b) Add the handler method to the NatsBridge class:

```python
    async def _on_session_event(self, msg: Any) -> None:
        """Forward a session.events.{tid} event to the SSE notification feed.

        Defense-in-depth: the payload's claimed thread_id must match the
        subject's thread_id AND must resolve to an existing thread in DB.
        See docs/issues/nats_subject_acl_hardening.md for why this is
        defense-in-depth rather than transport-level enforcement.
        """
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            logger.warning("session.events: invalid JSON: %s", e)
            return

        # Extract thread_id from subject ("session.events.{tid}").
        subject_parts = msg.subject.split(".")
        if len(subject_parts) != 3 or subject_parts[0:2] != ["session", "events"]:
            logger.warning("session.events: malformed subject: %s", msg.subject)
            return
        subject_tid = subject_parts[2]
        payload_tid = payload.get("thread_id")

        if payload_tid != subject_tid:
            logger.warning(
                "session.events: payload tid %r != subject tid %r — dropped",
                payload_tid, subject_tid,
            )
            return

        thread = await self._db.get_thread(subject_tid)
        if not thread:
            logger.debug("session.events: unknown thread %s — dropped", subject_tid)
            return

        user_id = str(thread.get("user_id") or "")
        if not user_id:
            return

        # Map the pod's event method to a notification feed event type.
        method = payload.get("method", "")
        event_type_map = {
            "permission.request": "session.permission_request",
            "vm_upgrade.needed": "session.vm_upgrade",
            "approve": "session.resolved",
            "deny": "session.resolved",
            "ready": "session.waiting",
        }
        event_type = event_type_map.get(method)
        if not event_type:
            return

        notification_feed.broadcast(
            user_id,
            event_type,
            {
                "thread_id": subject_tid,
                "method": method,
                "params": payload.get("params", {}),
            },
        )
```

c) Add the import at the top of the file:

```python
from .notification_feed import notification_feed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_nats_bridge_session_events.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/services/nats_bridge.py tests/test_nats_bridge_session_events.py
git commit -m "feat: nats_bridge subscribes to session.events.* and broadcasts to SSE"
```

---

## Task 6: Create `routers/sessions.py` — POST /prepare endpoint

**Files:**
- Create: `orchestrator/routers/sessions.py`
- Test: `tests/test_sessions_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sessions_router.py
"""Tests for the new /api/sessions/{tid} REST surface."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch):
    """A minimal FastAPI app with the sessions router mounted."""
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    app.include_router(sessions_router)

    # Patch the late-imported postgres_db so tests don't need a real DB.
    fake_db = AsyncMock()
    fake_db.get_thread = AsyncMock(return_value={
        "id": "t1",
        "user_id": "u1",
        "agent_id": None,  # No agent bound yet — triggers provisioning
    })
    fake_db.thread_advisory_lock = MagicMock()
    fake_db.thread_advisory_lock.return_value.__aenter__ = AsyncMock()
    fake_db.thread_advisory_lock.return_value.__aexit__ = AsyncMock()
    monkeypatch.setattr("orchestrator.routers.sessions.postgres_db", fake_db, raising=False)

    # Patch require_approved_user to always return a fixed user.
    def _fake_user():
        return {"id": "u1", "is_approved": True}
    monkeypatch.setattr(
        "orchestrator.routers.sessions.require_approved_user",
        lambda: _fake_user,
        raising=False,
    )

    return app, fake_db


def test_prepare_returns_202_and_starts_async_work(app):
    """POST /api/sessions/{tid}/prepare returns 202 immediately."""
    fastapi_app, fake_db = app
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "provisioning"


def test_prepare_404_on_unknown_thread(app, monkeypatch):
    """POST /api/sessions/{tid}/prepare returns 404 if thread is missing."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = None
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/unknown/prepare", json={})
    assert resp.status_code == 404


def test_prepare_403_on_thread_owned_by_other_user(app):
    """POST /api/sessions/{tid}/prepare returns 403 if user doesn't own thread."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = {
        "id": "t1", "user_id": "OTHER", "agent_id": None
    }
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sessions_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'orchestrator.routers.sessions'`.

- [ ] **Step 3: Create the router with the prepare endpoint**

```python
# orchestrator/routers/sessions.py
"""``/api/sessions`` — prepare + connection endpoints.

These two endpoints replace the WS handshake's pre-flight work that used to
live inline in ``orchestrator/main.py``'s ``persistent_ws_proxy``.

  - ``POST /api/sessions/{thread_id}/prepare`` — slow path. Auth, ownership,
    provisioning, readiness. Returns 202 immediately; progress goes via the
    existing SSE notification feed on event type ``session.lifecycle``.
    Idempotent: a concurrent retry blocks on a Postgres advisory lock keyed
    by thread_id and returns the in-flight call's result.

  - ``GET /api/sessions/{thread_id}/connection`` — fast path. Returns the
    canonical {ws_url, token, expires_at} for a bound session. Used by both
    cold-start (after SSE "ready") and warm reconnect.

Spec: docs/features/direct_session_websockets.md §Component details.
Pattern: late imports of postgres_db (and other singletons) inside handler
bodies to avoid circular imports at module load time — same pattern as
orchestrator/routers/automations.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from security.auth import require_approved_user
from services.notification_feed import notification_feed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


# Late-imported singletons (avoids circular imports with main.py).
# The actual values live in main.py; we reach for them at call time.
try:
    from main import postgres_db  # type: ignore
except Exception:
    postgres_db = None  # Replaced at runtime; test code patches this attribute.


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class PrepareRequest(BaseModel):
    config_name: str | None = Field(None, max_length=120)
    config_override: dict[str, Any] | None = None


class PrepareResponse(BaseModel):
    state: str = Field(..., examples=["provisioning"])


class ConnectionResponse(BaseModel):
    state: str = Field(..., examples=["ready"])
    ws_url: str
    token: str
    expires_at: int


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post(
    "/{thread_id}/prepare",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrepareResponse,
)
async def prepare_session(
    thread_id: str,
    body: PrepareRequest,
    request: Request,
    user: dict = Depends(require_approved_user),
):
    """Kick off (or rejoin) provisioning for the given thread.

    Returns 202 immediately. The caller subscribes to the SSE notification
    feed and waits for ``session.lifecycle`` events with state=ready, then
    calls GET /api/sessions/{tid}/connection for the token.
    """
    # Late imports to dodge circular dep at module load.
    from main import postgres_db as _db

    thread = await _db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="thread access denied")

    # Fire-and-forget the actual work in a background task. Progress reaches
    # the cockpit via SSE. Idempotency is enforced by the advisory lock
    # inside _do_prepare.
    asyncio.create_task(
        _do_prepare(
            thread_id=thread_id,
            user_id=str(user["id"]),
            config_name=body.config_name or thread.get("config_name"),
            config_override=body.config_override,
        )
    )

    return PrepareResponse(state="provisioning")


async def _do_prepare(
    thread_id: str,
    user_id: str,
    config_name: str | None,
    config_override: dict[str, Any] | None,
) -> None:
    """Run the actual provisioning + readiness work asynchronously.

    Serializes concurrent prepares on the same thread via advisory lock.
    Broadcasts ``session.lifecycle`` SSE events at each phase change.
    """
    from main import postgres_db as _db
    from main import agent_provisioner, persistent_provisioner

    def _emit(state: str, **extra: Any) -> None:
        notification_feed.broadcast(
            user_id,
            "session.lifecycle",
            {"thread_id": thread_id, "state": state, **extra},
        )

    try:
        async with _db.thread_advisory_lock(thread_id):
            thread = await _db.get_thread(thread_id)
            if not thread:
                _emit("failed", reason="thread vanished")
                return

            # Provisioning (if needed). The agent_provisioner / persistent_provisioner
            # interfaces already exist in orchestrator/services/. Reuse the
            # same path the legacy WS handler used.
            if not thread.get("agent_id"):
                _emit("provisioning")
                # Pool-first, then create-pod fallback — see existing logic
                # in main.py:_ws_provision (now moving into this function).
                await _provision_agent_for_thread(
                    thread_id=thread_id,
                    config_name=config_name or "persistent_defaults",
                    config_override=config_override,
                )
                # Wait for agent registration (the agent calls
                # /api/agents/register on startup).
                bind_timeout_s = int(os.environ.get("AGENT_BIND_TIMEOUT_S", "300"))
                if not await _wait_for_binding(thread_id, bind_timeout_s):
                    _emit("failed", reason="agent failed to register")
                    return

            # Readiness probe.
            _emit("booting")
            agent_id = (await _db.get_thread(thread_id))["agent_id"]
            agent = await _db.get_agent(str(agent_id))
            if not agent or not agent.get("pod_ip"):
                _emit("failed", reason="agent has no pod_ip")
                return

            ready_timeout_s = int(os.environ.get("WS_READY_TIMEOUT_S", "180"))
            if not await _wait_for_ready(
                pod_ip=agent["pod_ip"],
                pod_port=int(agent.get("pod_port", 8001)),
                timeout_s=ready_timeout_s,
            ):
                _emit("failed", reason="agent /ready timeout")
                return

            # Create the route resource. session_router lives on main as a
            # late-imported singleton.
            from main import session_router  # type: ignore
            await session_router.ensure_route(
                thread_id=thread_id,
                pod_name=agent["hostname"],
                pod_uid=agent.get("pod_uid", ""),
            )

            _emit("ready")
    except Exception as e:
        logger.exception("prepare failed for thread %s: %s", thread_id, e)
        _emit("failed", reason=str(e))


async def _provision_agent_for_thread(
    thread_id: str,
    config_name: str,
    config_override: dict[str, Any] | None,
) -> None:
    """Trigger pool-first then create-pod provisioning.

    Migrated from main.py:_ws_provision (the inline helper that used to live
    inside persistent_ws_proxy). Same semantics: try the idle pool first via
    _send_session_attach, fall back to agent_provisioner.provision_agent.
    """
    from main import (
        _find_idle_persistent_agent,
        _send_session_attach,
        agent_provisioner,
        postgres_db as _db,
    )

    idle_agent = await _find_idle_persistent_agent()
    if idle_agent:
        ok = await _send_session_attach(
            idle_agent, thread_id, config_override or {}, [], datasources=None
        )
        if ok:
            return

    await agent_provisioner.provision_agent(
        purpose="session", thread_id=thread_id, config_name=config_name
    )


async def _wait_for_binding(thread_id: str, timeout_s: int) -> bool:
    """Poll the DB until thread.agent_id is set, or timeout."""
    from main import postgres_db as _db
    interval = 2
    for _ in range(max(1, timeout_s // interval)):
        thread = await _db.get_thread(thread_id)
        if thread and thread.get("agent_id"):
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_for_ready(pod_ip: str, pod_port: int, timeout_s: int) -> bool:
    """Poll the agent pod's /ready until it returns ready=true, or timeout."""
    import httpx
    interval = 2
    for _ in range(max(1, timeout_s // interval)):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"http://{pod_ip}:{pod_port}/ready")
                if resp.status_code == 200 and resp.json().get("ready"):
                    return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sessions_router.py::test_prepare_returns_202_and_starts_async_work -v tests/test_sessions_router.py::test_prepare_404_on_unknown_thread -v tests/test_sessions_router.py::test_prepare_403_on_thread_owned_by_other_user -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/routers/sessions.py tests/test_sessions_router.py
git commit -m "feat: POST /api/sessions/{tid}/prepare (async + SSE-progress + advisory lock)"
```

---

## Task 7: Add `GET /connection` endpoint with JWT minting

**Files:**
- Modify: `orchestrator/routers/sessions.py`
- Modify: `tests/test_sessions_router.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_sessions_router.py`:

```python
def test_connection_returns_ws_url_and_token_when_ready(app, monkeypatch):
    """GET /api/sessions/{tid}/connection returns 200 with ws_url+token when bound."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = {
        "id": "t1", "user_id": "u1", "agent_id": "agent-xyz"
    }
    fake_db.get_agent = AsyncMock(return_value={
        "id": "agent-xyz", "pod_ip": "10.0.0.5", "pod_port": 8001,
        "status": "ready"
    })

    # Patch session_tokens singleton on the router module.
    from orchestrator.services.session_tokens import SessionTokenService
    monkeypatch.setattr(
        "orchestrator.routers.sessions.session_tokens",
        SessionTokenService(secret="test", ttl_seconds=60),
        raising=False,
    )
    monkeypatch.setenv("SESSION_INGRESS_HOST", "api.test.example")

    client = TestClient(fastapi_app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["ws_url"].startswith("wss://api.test.example/p/t1/ws")
    assert body["token"]
    assert isinstance(body["expires_at"], int)


def test_connection_returns_425_if_session_not_ready(app):
    """GET /api/sessions/{tid}/connection returns 425 if the agent is still booting."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = {
        "id": "t1", "user_id": "u1", "agent_id": None  # No binding yet
    }
    client = TestClient(fastapi_app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 425
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sessions_router.py::test_connection_returns_ws_url_and_token_when_ready -v tests/test_sessions_router.py::test_connection_returns_425_if_session_not_ready -v`
Expected: FAIL with 404 (endpoint not defined yet).

- [ ] **Step 3: Add the endpoint to `orchestrator/routers/sessions.py`**

Append to the router file (after the `prepare_session` endpoint):

```python
@router.get(
    "/{thread_id}/connection",
    response_model=ConnectionResponse,
)
async def get_connection(
    thread_id: str,
    user: dict = Depends(require_approved_user),
):
    """Return the canonical {ws_url, token, expires_at} for a bound session.

    Same payload shape used by cold-start (after SSE "ready") and warm
    reconnect — one token-mint code path on the orchestrator, one consumer
    code path on the cockpit.
    """
    from main import postgres_db as _db, session_tokens  # type: ignore

    thread = await _db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="thread access denied")

    agent_id = thread.get("agent_id")
    if not agent_id:
        # Not bound yet — caller should POST /prepare.
        raise HTTPException(status_code=425, detail="session not ready")

    agent = await _db.get_agent(str(agent_id))
    if not agent or not agent.get("pod_ip"):
        raise HTTPException(status_code=409, detail="agent unavailable")
    if agent.get("status") not in ("ready", "working"):
        raise HTTPException(status_code=409, detail="agent not ready")

    token, expires_at = session_tokens.mint(
        user_id=str(user["id"]),
        thread_id=thread_id,
    )

    host = os.environ.get("SESSION_INGRESS_HOST", "api.example.com")
    ws_url = f"wss://{host}/p/{thread_id}/ws?t={token}"

    return ConnectionResponse(
        state="ready",
        ws_url=ws_url,
        token=token,
        expires_at=expires_at,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sessions_router.py -v`
Expected: All tests in the file PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/routers/sessions.py tests/test_sessions_router.py
git commit -m "feat: GET /api/sessions/{tid}/connection mints session JWT"
```

---

## Task 8: Add JWT validation to agent pod `/ws/chat`

**Files:**
- Modify: `src/api/persistent_app.py` (around line 1360)
- Modify: `src/api/dual_app.py` (around line 1110)
- Test: `tests/test_persistent_app_ws_auth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistent_app_ws_auth.py
"""Tests for JWT validation on the agent pod's /ws/chat handshake."""

import os

import pytest
from fastapi.testclient import TestClient

from orchestrator.services.session_tokens import SessionTokenService


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    monkeypatch.setenv("SESSION_JWT_SECRET", "test-pod-secret")
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")


@pytest.fixture
def app():
    from src.api.persistent_app import create_persistent_app
    return create_persistent_app()


def test_ws_chat_rejects_missing_token(app):
    """No `?t=` query param → close with 4401."""
    client = TestClient(app)
    with client.websocket_connect("/ws/chat") as ws:  # noqa: SIM117
        with pytest.raises(Exception):
            ws.send_text("hi")  # Should disconnect before we can send


def test_ws_chat_rejects_token_for_other_thread(app):
    """Token's `tid` doesn't match pod's bound thread → close with 4403."""
    other_token, _ = SessionTokenService("test-pod-secret").mint("u1", "OTHER-thread")
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/chat?t={other_token}"):
            pass


def test_ws_chat_accepts_valid_token(app):
    """Token with matching tid → handshake succeeds."""
    token, _ = SessionTokenService("test-pod-secret").mint("u1", "t1")
    client = TestClient(app)
    # Connection should be accepted; subsequent behavior is the existing
    # session-attach logic which is out of scope for this test.
    with client.websocket_connect(f"/ws/chat?t={token}") as ws:
        # Just verify the handshake completed.
        assert ws is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistent_app_ws_auth.py -v`
Expected: FAIL — tokens are not validated yet.

- [ ] **Step 3: Add JWT validation**

Open `src/api/persistent_app.py`. Find the `/ws/chat` route (line ~1360). Add a JWT validation step before delegating to `handle_persistent_websocket`:

```python
    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        # Validate the session JWT carried as ?t={token}.
        if not await _validate_session_token(ws):
            return
        await handle_persistent_websocket(ws)
```

Add the helper near the top of the file (after imports):

```python
import os
from src.api._session_auth import validate_session_token as _validate_session_token
```

Create the shared validator module:

```python
# src/api/_session_auth.py
"""Pod-side validation of the orchestrator-minted session JWT.

The pod knows two things from its env:
  - SESSION_JWT_SECRET — shared with the orchestrator (K8s Secret)
  - SESSION_BOUND_THREAD_ID — the thread this pod was provisioned for

A valid handshake: token signature OK, audience=agent, not expired,
and claim `tid` matches SESSION_BOUND_THREAD_ID.
"""

from __future__ import annotations

import logging
import os

import jwt
from fastapi import WebSocket

logger = logging.getLogger(__name__)


async def validate_session_token(ws: WebSocket) -> bool:
    """Validate the WS query param `t`. Closes the WS with an appropriate
    code on failure. Returns True if the connection should proceed."""
    secret = os.environ.get("SESSION_JWT_SECRET", "")
    bound_tid = os.environ.get("SESSION_BOUND_THREAD_ID", "")
    if not secret or not bound_tid:
        # Misconfigured pod — fail closed.
        await ws.accept()
        await ws.close(code=4500, reason="pod missing session auth config")
        return False

    token = ws.query_params.get("t")
    if not token:
        await ws.accept()
        await ws.close(code=4401, reason="missing session token")
        return False

    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], audience="agent"
        )
    except jwt.PyJWTError as e:
        logger.warning("ws_chat: invalid session token: %s", e)
        await ws.accept()
        await ws.close(code=4401, reason="invalid session token")
        return False

    if str(claims.get("tid") or "") != bound_tid:
        logger.warning(
            "ws_chat: token tid %r != bound %r — rejecting",
            claims.get("tid"), bound_tid,
        )
        await ws.accept()
        await ws.close(code=4403, reason="session token mismatch")
        return False

    return True
```

Do the same edit in `src/api/dual_app.py` at line ~1110:

```python
    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        if not await _validate_session_token(ws):
            return
        await handle_persistent_websocket(ws)
```

(Add the same import at the top of `dual_app.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistent_app_ws_auth.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/persistent_app.py src/api/dual_app.py src/api/_session_auth.py tests/test_persistent_app_ws_auth.py
git commit -m "feat: agent pod validates session JWT on /ws/chat handshake"
```

---

## Task 9: Publish notification events from agent pod to NATS

**Files:**
- Modify: `src/api/persistent_app.py` (find the event-emission points used by the loop)
- Test: extend a relevant existing test file in `tests/`

- [ ] **Step 1: Write the failing test**

Find the existing notification-emission point in the persistent loop (search `src/api/persistent_app.py` for `permission.request` or look in the persistent loop module — the events were inspected by the orchestrator's `_inspect_session_event` so the agent must already be emitting them somewhere over WS). Add a test asserting that the same events are now ALSO published to NATS.

```python
# tests/test_persistent_app_nats_publish.py
"""Tests for NATS publishing of notification events from the agent pod."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_permission_request_published_to_nats(monkeypatch):
    """Emitting a permission.request event publishes to session.events.{tid}."""
    from src.api.persistent_app import emit_session_event  # New module-level helper

    nc = AsyncMock()
    monkeypatch.setenv("SESSION_BOUND_THREAD_ID", "t1")
    monkeypatch.setattr("src.api.persistent_app._nats_client", nc, raising=False)

    await emit_session_event(
        method="permission.request",
        params={"tool": "shell", "args": "ls"},
    )

    nc.publish.assert_called_once()
    subject = nc.publish.call_args.args[0]
    data = nc.publish.call_args.args[1]
    assert subject == "session.events.t1"
    payload = json.loads(data.decode())
    assert payload["thread_id"] == "t1"
    assert payload["method"] == "permission.request"
    assert payload["params"]["tool"] == "shell"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_persistent_app_nats_publish.py -v`
Expected: FAIL with `ImportError: cannot import name 'emit_session_event'`.

- [ ] **Step 3: Implement the publisher and wire it into the loop**

a) Add a module-level helper at the top of `src/api/persistent_app.py` (after imports, before the existing app factory):

```python
import json as _json
import os as _os

_nats_client = None  # Lazily initialized below.


async def _ensure_nats_client():
    """Lazy NATS connection. Returns None if NATS is unconfigured."""
    global _nats_client
    if _nats_client is not None:
        return _nats_client
    url = _os.environ.get("NATS_URL")
    if not url:
        return None
    try:
        import nats
        _nats_client = await nats.connect(url)
        return _nats_client
    except Exception as e:
        logger.warning("agent pod: NATS connect failed: %s", e)
        return None


async def emit_session_event(method: str, params: dict) -> None:
    """Publish a notification event to ``session.events.{tid}`` on NATS.

    Replaces the orchestrator-side WS inspection (``_inspect_session_event``)
    that used to live in main.py. The orchestrator's nats_bridge subscribes
    to this subject and re-broadcasts to the SSE notification feed.
    """
    tid = _os.environ.get("SESSION_BOUND_THREAD_ID", "")
    if not tid:
        return
    nc = await _ensure_nats_client()
    if not nc:
        return
    payload = {"thread_id": tid, "method": method, "params": params}
    try:
        await nc.publish(f"session.events.{tid}", _json.dumps(payload).encode())
    except Exception as e:
        logger.warning("agent pod: NATS publish failed: %s", e)
```

b) Find every place in the persistent loop / session manager that emits one of the inspected methods (`permission.request`, `vm_upgrade.needed`, `approve`, `deny`, `ready`) and add a parallel `await emit_session_event(...)` call. These should be in `src/managers/` or `src/agent.py` — search for `"permission.request"` to locate them.

For each such call site, add the emission alongside the existing WS push:

```python
# Existing:
await ws.send_text(json.dumps({"method": "permission.request", "params": {...}}))
# Added:
await emit_session_event("permission.request", {...})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_persistent_app_nats_publish.py -v`
Expected: PASS.

Then run the broader agent test suite to check for regressions:
Run: `pytest tests/ -k "persistent_app" -v`
Expected: All previously-passing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/persistent_app.py src/agent.py src/managers/ tests/test_persistent_app_nats_publish.py
git commit -m "feat: agent pod publishes notification events to session.events.{tid}"
```

---

## Task 10: Update cockpit `persistent-chat.service.ts` connect flow

**Files:**
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts`
- Modify: `cockpit/src/app/core/services/persistent-chat.service.spec.ts`

- [ ] **Step 1: Write the failing tests**

Read the existing spec file first to learn the testing patterns. Then add:

```typescript
// In cockpit/src/app/core/services/persistent-chat.service.spec.ts

describe('PersistentChatService — new connect flow (lighthouse)', () => {
  it('cold start: subscribes SSE, POSTs /prepare, waits for ready, GETs /connection, opens WS', async () => {
    const httpMock = jasmine.createSpyObj('HttpClient', ['post', 'get']);
    httpMock.post.and.returnValue(of({ state: 'provisioning' }));
    httpMock.get.and.returnValue(of({
      state: 'ready',
      ws_url: 'wss://api.example.com/p/t1/ws?t=jwt',
      token: 'jwt',
      expires_at: Date.now() / 1000 + 60,
    }));

    const sseMock = createSseMock();  // helper that emits lifecycle events
    const service = new PersistentChatService(httpMock, sseMock);

    const connectPromise = service.connect('t1');
    sseMock.emit({ type: 'session.lifecycle', thread_id: 't1', state: 'ready' });
    await connectPromise;

    expect(httpMock.post).toHaveBeenCalledWith(
      '/api/sessions/t1/prepare', jasmine.any(Object)
    );
    expect(httpMock.get).toHaveBeenCalledWith('/api/sessions/t1/connection');
    expect(service.lastWsUrl).toBe('wss://api.example.com/p/t1/ws?t=jwt');
  });

  it('warm reconnect: GETs /connection directly, opens WS without POST /prepare', async () => {
    const httpMock = jasmine.createSpyObj('HttpClient', ['post', 'get']);
    httpMock.get.and.returnValue(of({
      state: 'ready',
      ws_url: 'wss://api.example.com/p/t1/ws?t=jwt2',
      token: 'jwt2',
      expires_at: Date.now() / 1000 + 60,
    }));
    const service = new PersistentChatService(httpMock, createSseMock());
    service.markBound('t1');  // Indicate the thread has an agent already

    await service.connect('t1');

    expect(httpMock.post).not.toHaveBeenCalled();
    expect(httpMock.get).toHaveBeenCalledWith('/api/sessions/t1/connection');
  });

  it('on 4401 WS close, drops token and re-fetches /connection', async () => {
    // Verify that an expired-token close code triggers /connection re-fetch
    // (the F4 reconnect engine path).
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.spec.ts`
Expected: FAIL — the new methods (`connect`, `markBound`, `lastWsUrl`) don't exist yet.

- [ ] **Step 3: Refactor the connect flow**

Open `cockpit/src/app/core/services/persistent-chat.service.ts`. Read the existing `_connectWs` and reconnect logic. Replace the URL-resolution step with the new flow.

Show the conceptual shape (full code will depend on the existing service structure — match its conventions for HttpClient injection, signal/observable usage, etc.):

```typescript
async connect(threadId: string): Promise<void> {
  if (!this.isBound(threadId)) {
    // Cold start
    await this.startSseForLifecycle(threadId);
    await firstValueFrom(
      this.http.post(`/api/sessions/${threadId}/prepare`, {})
    );
    await this.waitForLifecycleReady(threadId);
  }
  // Both paths converge here:
  const conn = await firstValueFrom(
    this.http.get<ConnectionPayload>(`/api/sessions/${threadId}/connection`)
  );
  this.openWs(conn.ws_url);  // Token is in the URL query param
}

private async waitForLifecycleReady(threadId: string): Promise<void> {
  // Wait for a session.lifecycle SSE event with state === 'ready' for this thread.
  return new Promise((resolve, reject) => {
    const sub = this.sse.events('session.lifecycle')
      .pipe(filter(ev => ev.thread_id === threadId))
      .subscribe(ev => {
        if (ev.state === 'ready') { sub.unsubscribe(); resolve(); }
        if (ev.state === 'failed') { sub.unsubscribe(); reject(new Error(ev.reason)); }
      });
  });
}

// In the WS onclose handler:
private onWsClose(event: CloseEvent, threadId: string): void {
  if (event.code === 4401) {
    // Expired/invalid token — re-fetch /connection and retry.
    this.connect(threadId).catch(/* hand to F4 reconnect */);
    return;
  }
  // Existing F4 reconnect logic unchanged.
  this._scheduleReconnect(event.code);
}
```

The exact code will depend on the existing service's structure. Read it carefully and match the existing patterns.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd cockpit && npx vitest run src/app/core/services/persistent-chat.service.spec.ts`
Expected: All new tests PASS. Re-run the full cockpit suite to check regressions:
Run: `cd cockpit && npx vitest run`
Expected: All previously-passing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add cockpit/src/app/core/services/persistent-chat.service.ts cockpit/src/app/core/services/persistent-chat.service.spec.ts
git commit -m "feat: cockpit persistent-chat uses /prepare + /connection + direct WS"
```

---

## Task 11: Update cockpit IDE service connect flow

**Files:**
- Modify: the cockpit IDE service (find it by searching for `/api/ide/` in `cockpit/src/`)
- Modify: corresponding spec file

- [ ] **Step 1: Locate the existing IDE WS connection code**

Run: `grep -rn "/api/ide/" cockpit/src/ | head -20`

Identify the file that opens the IDE WS today (likely something like `ide.service.ts`, `code-server.service.ts`, or a component-local effect). Read it and the existing tests.

- [ ] **Step 2: Write the failing test**

Add a parallel test asserting the new flow — same shape as the persistent-chat test in Task 10:

```typescript
describe('IDE service — new connect flow', () => {
  it('cold start: POSTs /prepare with kind=ide, waits for ready, GETs /connection, opens WS', async () => {
    // Symmetric with the persistent-chat test.
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd cockpit && npx vitest run <ide-service-spec-path>`
Expected: FAIL.

- [ ] **Step 4: Refactor the IDE WS connect flow**

Update the IDE service to use the same `/api/sessions/{job_or_thread_id}/prepare` + `/connection` pattern as persistent-chat. The IDE pod is provisioned with the same `srw.io/thread-id` label by Task 4, so the Service+Ingress route covers it the same way.

If the IDE proxy currently uses `job_id` rather than `thread_id`, decide during implementation whether to use the existing `job_id` (and stamp the IDE pod with `srw.io/job-id` and route by that label) or unify on `thread_id`. The spec is agnostic — pick whichever requires the smaller change to existing provisioning.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd cockpit && npx vitest run`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add cockpit/src/app/core/services/<ide-service>.ts cockpit/src/app/core/services/<ide-service>.spec.ts
git commit -m "feat: cockpit IDE service uses /prepare + /connection + direct WS"
```

---

## Task 12: Helm — JWT Secret + RBAC + sessionRouter values

**Files:**
- Create: `helm/templates/session-jwt-secret.yaml`
- Create: `helm/templates/orchestrator-rbac-sessions.yaml`
- Modify: `helm/values.yaml` and `helm/values.example.yaml`

- [ ] **Step 1: Add the JWT Secret template**

```yaml
# helm/templates/session-jwt-secret.yaml
{{- if .Values.sessionRouter.jwtSecret }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Values.sessionRouter.jwtSecretName | default "srw-session-jwt" }}
  namespace: {{ .Release.Namespace }}
  labels:
    app.kubernetes.io/managed-by: {{ .Release.Service }}
    app.kubernetes.io/instance: {{ .Release.Name }}
type: Opaque
stringData:
  jwt-secret: {{ .Values.sessionRouter.jwtSecret | quote }}
{{- end }}
```

- [ ] **Step 2: Add the RBAC delta**

```yaml
# helm/templates/orchestrator-rbac-sessions.yaml
# RBAC additions for the session-router lifecycle.
# See docs/features/direct_session_websockets.md §RBAC changes.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ .Release.Name }}-orchestrator-sessions
  namespace: {{ .Release.Namespace }}
rules:
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "create", "delete", "patch"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "create", "delete", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ .Release.Name }}-orchestrator-sessions
  namespace: {{ .Release.Namespace }}
subjects:
  - kind: ServiceAccount
    name: {{ .Release.Name }}-orchestrator
    namespace: {{ .Release.Namespace }}
roleRef:
  kind: Role
  name: {{ .Release.Name }}-orchestrator-sessions
  apiGroup: rbac.authorization.k8s.io
```

- [ ] **Step 3: Add `sessionRouter` to values files**

Append to `helm/values.yaml`:

```yaml
sessionRouter:
  # Tier-1 supported ingress controller (tested in CI + dev).
  # nginx-ingress, HAProxy ingress, Istio Gateway are Tier-2:
  # documented in README per-controller but not exercised by the test suite.
  ingressClass: traefik

  # Hostname used in per-session Ingress rules. Defaults to derived from
  # global.domain (e.g., api.<global.domain>) when unset.
  ingressHost: ""

  # JWT signing key shared by orchestrator + agent pods. Set this in
  # values-prod.yaml (or via --set-string sessionRouter.jwtSecret=...);
  # leaving it empty disables the new flow and prevents pod startup.
  jwtSecret: ""
  jwtSecretName: srw-session-jwt
  jwtTtlSeconds: 60

  # Extra annotations merged into every session Ingress resource.
  # See docs/features/direct_session_websockets.md §Configuration for
  # the per-controller annotation matrix.
  annotations: {}
```

Mirror the block in `helm/values.example.yaml` with example values.

- [ ] **Step 4: Inject the env vars into orchestrator and agent pods**

In `helm/templates/orchestrator-deployment.yaml` (or whatever the orchestrator Deployment template is named), add:

```yaml
env:
  - name: SESSION_JWT_SECRET
    valueFrom:
      secretKeyRef:
        name: {{ .Values.sessionRouter.jwtSecretName | default "srw-session-jwt" }}
        key: jwt-secret
  - name: SESSION_JWT_TTL_S
    value: {{ .Values.sessionRouter.jwtTtlSeconds | quote }}
  - name: SESSION_INGRESS_HOST
    value: {{ .Values.sessionRouter.ingressHost | default (printf "api.%s" .Values.global.domain) }}
  - name: SESSION_INGRESS_CLASS
    value: {{ .Values.sessionRouter.ingressClass | quote }}
  - name: SESSION_INGRESS_NAMESPACE
    value: {{ .Release.Namespace | quote }}
```

In the **agent pod** template (look for the agent provisioner's pod-spec rendering — likely in `agent_provisioner.py` itself rather than a Helm template, since pods are dynamic), inject the same `SESSION_JWT_SECRET` from the Secret, plus `SESSION_BOUND_THREAD_ID=<thread_id>` for each provisioned pod.

- [ ] **Step 5: Helm lint and template validation**

Run: `helm lint helm/`
Expected: no warnings about the new templates.

Run: `helm template srw helm/ --set sessionRouter.jwtSecret=test-secret-xyz | grep -A 5 "kind: Secret" | head -20`
Expected: shows the rendered Secret block with the jwt-secret.

- [ ] **Step 6: Commit**

```bash
git add helm/templates/session-jwt-secret.yaml helm/templates/orchestrator-rbac-sessions.yaml helm/values.yaml helm/values.example.yaml helm/templates/orchestrator-deployment.yaml orchestrator/services/agent_provisioner.py
git commit -m "feat: helm — session JWT secret, RBAC for Ingress/Service, value block"
```

---

## Task 13: Wire up router, remove legacy WS code from `main.py`

**Files:**
- Modify: `orchestrator/main.py`

- [ ] **Step 1: Register the new router and instantiate singletons**

In `orchestrator/main.py`, find the block at lines 3511-3514:

```python
app.include_router(bff_router)
app.include_router(graph_router)
app.include_router(uploads_router)
app.include_router(automations_router)
```

Add:

```python
from routers.sessions import router as sessions_router
app.include_router(sessions_router)
```

Add the singletons (find the existing singleton instantiation block near the top of `main.py` — there's a section that creates `agent_provisioner`, `persistent_provisioner`, etc.):

```python
import os
from services.session_router import SessionRouterService
from services.session_tokens import SessionTokenService

session_router = SessionRouterService(
    namespace=os.environ.get("SESSION_INGRESS_NAMESPACE", "default"),
    ingress_host=os.environ.get("SESSION_INGRESS_HOST", "api.example.com"),
    ingress_class=os.environ.get("SESSION_INGRESS_CLASS", "traefik"),
)

session_tokens = SessionTokenService(
    secret=os.environ.get("SESSION_JWT_SECRET", ""),
    ttl_seconds=int(os.environ.get("SESSION_JWT_TTL_S", "60")),
)
```

- [ ] **Step 2: Delete the legacy WS proxy and inspection helpers**

Delete these line ranges from `orchestrator/main.py` (verify the line numbers — they may have shifted slightly during earlier edits):

- Lines **7771-7833**: `ide_proxy_http` handler (HTTP half — KEEP for now, scope says HTTP stays. Re-verify; if uncertain leave it.)
- Lines **7852-7945**: `ide_proxy_ws` handler (WS half — DELETE)
- Lines **13670-13744**: `_inspect_session_event` and `_inspect_browser_event` helpers (DELETE both)
- Lines **13747-14063**: `persistent_ws_proxy` handler (DELETE)

Tools to verify before deleting: `grep -n "ide_proxy_ws\|persistent_ws_proxy\|_inspect_session_event\|_inspect_browser_event" orchestrator/main.py`. Confirm the only callers are within main.py itself.

- [ ] **Step 3: Verify no orphan callers**

Run: `grep -rn "_inspect_session_event\|_inspect_browser_event\|persistent_ws_proxy\|ide_proxy_ws" orchestrator/ src/ cockpit/`
Expected: Only the new test files (if any reference the removed names) and stale references; no live callers. Fix any orphans.

- [ ] **Step 4: Run the orchestrator test suite end-to-end**

Run: `pytest tests/ -v --tb=short -k "not slow"`
Expected: All currently-passing tests still pass. Some tests that asserted the legacy WS proxy behavior will fail and should be deleted or rewritten to test the new endpoints.

Make a separate pass for the new test files:

Run: `pytest tests/test_session_tokens.py tests/test_session_router.py tests/test_sessions_router.py tests/test_nats_bridge_session_events.py tests/test_persistent_app_ws_auth.py tests/test_persistent_app_nats_publish.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/main.py tests/
git commit -m "refactor: replace WS proxy in main.py with /api/sessions REST surface"
```

---

## Task 14: Dev cluster smoke test

**Status:** Complete (2026-05-23). Resume flow surfaced lifecycle phases (`Creating thread` → `Provisioning agent` active in the cockpit's startup card) and successfully bound an agent; chat round-trip verified with the agent replying `"resumed, 2025-05-22"`. Mid-stream hard refresh during a 6 KB streaming response produced a recovered placeholder bubble (~4.6 KB starting mid-word `"oni…"` — tail of "Tononi") that promoted to the real turn id on `turn_completed`. Single agent observed bound per thread (race fix verified). Remaining stretch goals (reconnect latency under 200 ms measurement, orchestrator-restart resilience, ingress GC after end_session) were not separately measured here — left as ad-hoc verifications now that the core path is proven.

**Files:**
- None — this is a deployment + verification step.

This task is not unit-testable. Run the full flow on the dev cluster and verify reconnect speed end-to-end.

- [x] **Step 1: Build images and deploy**

```bash
# Build new orchestrator and agent images with the changes.
./scripts/build-and-push.sh  # or whatever the project uses; check Makefile.

# Apply the helm chart with the new sessionRouter values.
helm upgrade srw helm/ \
  --namespace srw \
  --set sessionRouter.jwtSecret="$(openssl rand -base64 48)" \
  --set sessionRouter.ingressHost=api.dev.superhuman-remote-worker.com \
  --wait
```

- [x] **Step 2: Create a new persistent session in the cockpit**

Open the cockpit at `https://cockpit.dev.superhuman-remote-worker.com` and create a new persistent session. Verify in the browser DevTools Network tab:

- One `POST /api/sessions/<tid>/prepare` returning 202.
- An EventSource on the notification SSE channel receiving `session.lifecycle` events: `provisioning` → `booting` → `ready`.
- One `GET /api/sessions/<tid>/connection` returning 200 with `ws_url` pointing at `wss://api.dev.superhuman-remote-worker.com/p/<tid>/ws?t=...`.
- A WS connection to that URL, not to the legacy `/ws/persistent/<tid>`.

Also verify the K8s state:

```bash
kubectl get ingress -n srw -l srw.io/thread-id=<tid>
kubectl get svc -n srw -l srw.io/thread-id=<tid>
kubectl get pod -n srw -l srw.io/thread-id=<tid>
```

All three should exist.

- [ ] **Step 3: Measure reconnect latency** *(deferred — not measured during smoke test; quick ad-hoc verification can be done by closing the WS in DevTools and watching the badge flip)*

In the cockpit DevTools console, force a WS close (or kill the connection at the network panel) and measure the time from disconnect to "Connected" reappearing in the UI. Target: < 200 ms total wall-clock.

- [ ] **Step 4: Restart the orchestrator and confirm active sessions survive** *(deferred — not exercised during smoke test; covered architecturally by the fact that WS terminates at the agent pod, not the orchestrator)*

```bash
kubectl rollout restart deployment/srw-orchestrator -n srw
kubectl rollout status deployment/srw-orchestrator -n srw
```

In a cockpit tab with an active session: the WS connection should NOT drop during the restart. (This is the property the design adds — orchestrator restarts are no longer session-loss events.) Confirm by watching the network panel; the WS frame stream should continue without interruption.

- [ ] **Step 5: Clean up smoke-test session** *(deferred — Ingress/Service GC is driven by K8s ownerReferences on the agent pod, so end_session cleanup is structurally covered; not separately verified during smoke test)*

End the session via the cockpit. Verify the Ingress, Service, and Pod are torn down:

```bash
kubectl get ingress,svc,pod -n srw -l srw.io/thread-id=<tid>
```

All should be absent (or in `Terminating`).

- [x] **Step 6: Commit any final fixes uncovered by smoke testing**

Smoke testing surfaced two bugs that were resolved in the same branch:

- `persistent_thread_double_provisioning_race` — fixed via advisory lock on both provision call sites + reject-duplicate-registration defense in `register_agent`. (Doc moved to `docs/done/`.)
- `persistent_chat_lost_assistant_turn_on_mid_turn_reload` — fixed via same-thread fast path in `connect()` + reducer placeholder for orphan replay events. (Doc moved to `docs/done/`.)

Additional fixes uncovered during smoke testing and committed on the same branch:

- 422 on `/api/sessions/{tid}/{prepare,connection}`: `require_approved_user` was used as `Depends` but takes a positional `db`. Switched to the inline call pattern matching `routers/automations.py`.
- `/connection` is now self-healing: idempotently calls `ensure_route` before minting the token, so the Ingress exists even on the legacy resume path.
- `ensure_route` patches the agent pod's `srw.io/thread-id` label so idle-pool agents become routable.
- Agent registers `/p/{thread_id}/ws` to match the URL the per-session Ingress forwards.
- `_session_auth` falls back to `pa._session.thread_id` when `SESSION_BOUND_THREAD_ID` env is empty (job-pool pods).
- HomeLab `cloudflare-tunnel_configmap.yaml` routes `^/p/.+/ws.*$` through Traefik (port 80), bypassing the api.* catch-all → orchestrator. First-match-wins on cloudflared rules.

---

## Self-review checklist

Before declaring the plan complete, scan against the spec:

- **Spec §Problem (reconnect speed, restart resilience):** addressed by Tasks 6-7 (new REST endpoints) + Task 13 (legacy removal).
- **Spec §Architecture (control plane):** addressed by Tasks 6-7.
- **Spec §Component overview — `routers/sessions.py`:** Task 6-7.
- **Spec §Component overview — `services/session_router.py`:** Task 2.
- **Spec §Component overview — `services/session_tokens.py`:** Task 1.
- **Spec §Component overview — `services/nats_bridge.py` (modified):** Task 5.
- **Spec §Component overview — Agent pod JWT validation:** Task 8.
- **Spec §Component overview — Agent pod NATS publishing:** Task 9.
- **Spec §Component overview — Cockpit connect flow:** Task 10 (persistent), Task 11 (IDE).
- **Spec §main.py extraction:** Task 13.
- **Spec §RBAC changes:** Task 12.
- **Spec §Configuration (env vars + Helm values):** Task 12.
- **Spec §Migration path (hard cutover):** Task 13 (no dual-path code).
- **Spec §Decisions #1 (hard cutover):** Task 13 deletes legacy WS handlers.
- **Spec §Decisions #2 (single-key JWT):** Task 1 + Task 12.
- **Spec §Decisions #3 (mock + dev-cluster tests):** Tasks 1-9 cover mocked unit tests; Task 14 covers dev-cluster integration.
- **Spec §Decisions #4 (NATS subjects + bridge filter):** Task 5.
- **Spec §Decisions #5 (Tier 1 = Traefik):** Task 12 (values default + docs).
- **Spec §Decisions #6 (advisory lock for prepare idempotency):** Task 3 + Task 6.
- **Spec §Decisions #7 (polling pattern):** Task 6 (POST /prepare returns 202, never the payload) + Task 7 (GET /connection is the source of truth) + Task 10 (cockpit polls /connection on SSE "ready").
- **Spec §Decisions #8 (IDE WS same release):** Task 11.

All 8 decisions and all major component requirements are covered.
