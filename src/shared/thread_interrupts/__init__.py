"""Exact-turn durable interrupt inbox primitives (stateless-agents S2)."""

from .queries import (
    InterruptInputConsumption,
    InterruptReceipt,
    InterruptRequest,
    consume_applied_interrupt_input_idle,
    consume_applied_interrupt_input_live,
    fetch_interrupt_receipt,
    fetch_next_interrupt_request,
    fetch_next_stale_interrupt_request,
    fetch_stale_interrupt_requests,
    finalize_interrupt_request,
    interrupt_receipt_result,
    owner_fence_current,
    owner_fence_current_for_update,
)

__all__ = [
    "InterruptInputConsumption",
    "InterruptReceipt",
    "InterruptRequest",
    "consume_applied_interrupt_input_idle",
    "consume_applied_interrupt_input_live",
    "fetch_interrupt_receipt",
    "fetch_next_interrupt_request",
    "fetch_next_stale_interrupt_request",
    "fetch_stale_interrupt_requests",
    "finalize_interrupt_request",
    "interrupt_receipt_result",
    "owner_fence_current",
    "owner_fence_current_for_update",
]
