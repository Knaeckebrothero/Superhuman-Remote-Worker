"""Telemetry decorator for main-cloud backends.

Two things the decorator does for every method call:

1. Opens an OpenTelemetry span named ``cloud_backend.{backend_id}.{op}``
   with attributes for ``op``, ``backend``, ``status``, and ``latency_ms``.
   Uses the no-op tracer when the OTel SDK is not installed, so the
   decorator is safe in dev and never requires the ops team to install
   an SDK/collector before the code runs.
2. Emits a single structured log line per call at ``DEBUG`` level
   (success) or ``WARNING`` (failure). Fields: ``backend``, ``op``,
   ``status``, ``latency_ms``, ``error_kind`` (on failure).

Applied manually — this file intentionally does not walk every class
attribute via metaclass magic. Explicit is friendlier to grep.

See §4.11 of ``docs/features/main_cloud_abstraction.md``.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

try:
    from opentelemetry import trace

    _tracer = trace.get_tracer("orchestrator.services.cloud")
    _HAS_OTEL = True
except Exception:  # pragma: no cover — defensive for environments without API
    _tracer = None
    _HAS_OTEL = False

from .errors import CloudBackendError

logger = logging.getLogger(__name__)

T = TypeVar("T")
AsyncMethod = Callable[..., Awaitable[T]]


def instrument_backend_op(op_name: str) -> Callable[[AsyncMethod], AsyncMethod]:
    """Decorator that emits a span + structured log line per call.

    Parameters
    ----------
    op_name:
        Short operation name, e.g. ``"ensure_project_folder"``. Used as
        the span name suffix and the ``op`` log field.
    """

    def decorator(method: AsyncMethod) -> AsyncMethod:
        @functools.wraps(method)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            backend_id = getattr(self, "backend_id", "unknown")
            span_name = f"cloud_backend.{backend_id}.{op_name}"
            start = time.monotonic()

            span_cm = (
                _tracer.start_as_current_span(span_name)
                if _HAS_OTEL and _tracer is not None
                else _NullContext()
            )

            with span_cm as span:  # type: ignore[arg-type]
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("cloud_backend.op", op_name)
                    span.set_attribute("cloud_backend.backend", backend_id)
                try:
                    result = await method(self, *args, **kwargs)
                except CloudBackendError as exc:
                    latency_ms = (time.monotonic() - start) * 1000
                    if span is not None and hasattr(span, "set_attribute"):
                        span.set_attribute("cloud_backend.status", "error")
                        span.set_attribute("cloud_backend.error_kind", exc.kind.value)
                        span.set_attribute("cloud_backend.latency_ms", latency_ms)
                    logger.warning(
                        "cloud op failed",
                        extra={
                            "backend": backend_id,
                            "op": op_name,
                            "status": "error",
                            "latency_ms": round(latency_ms, 2),
                            "error_kind": exc.kind.value,
                        },
                    )
                    raise
                except Exception:
                    latency_ms = (time.monotonic() - start) * 1000
                    if span is not None and hasattr(span, "set_attribute"):
                        span.set_attribute("cloud_backend.status", "unknown_error")
                        span.set_attribute("cloud_backend.latency_ms", latency_ms)
                    logger.warning(
                        "cloud op failed (unmapped)",
                        extra={
                            "backend": backend_id,
                            "op": op_name,
                            "status": "unknown_error",
                            "latency_ms": round(latency_ms, 2),
                        },
                    )
                    raise
                latency_ms = (time.monotonic() - start) * 1000
                if span is not None and hasattr(span, "set_attribute"):
                    span.set_attribute("cloud_backend.status", "ok")
                    span.set_attribute("cloud_backend.latency_ms", latency_ms)
                logger.debug(
                    "cloud op ok",
                    extra={
                        "backend": backend_id,
                        "op": op_name,
                        "status": "ok",
                        "latency_ms": round(latency_ms, 2),
                    },
                )
                return result

        return wrapper

    return decorator


class _NullContext:
    """Context manager that yields ``None`` — used when OTel is absent."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc_info: Any) -> None:
        return None
