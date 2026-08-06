"""Typed, bounded inventory collectors for infrastructure metering."""

from .contracts import (
    CollectorLimits,
    InventoryError,
    InventoryScope,
    InventorySnapshot,
    KubernetesApiFailure,
    KubernetesListPage,
    KubernetesWatchEvent,
    RecoverableItemError,
    StagedInventoryItem,
    WatchEventType,
    WatchObservation,
    WatchOutcome,
    WatchQueueOverflow,
    normalized_payload,
)
from .kubernetes import KubernetesCollectionEngine, inventory_item_digest

__all__ = [
    "CollectorLimits",
    "InventoryError",
    "InventoryScope",
    "InventorySnapshot",
    "KubernetesApiFailure",
    "KubernetesCollectionEngine",
    "KubernetesListPage",
    "KubernetesWatchEvent",
    "RecoverableItemError",
    "StagedInventoryItem",
    "WatchEventType",
    "WatchObservation",
    "WatchOutcome",
    "WatchQueueOverflow",
    "inventory_item_digest",
    "normalized_payload",
]
