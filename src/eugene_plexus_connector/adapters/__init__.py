"""Platform adapter registry.

Adapters are constructed by `build_adapter(entry, hooks)` based on the
operator's `AdapterEntry.kind`. v0.2 supports `discord`; future versions
add slack, matrix, telegram, gmail, etc.

The `Adapter` Protocol decouples platform-specific runtime (discord.py
websocket loop, Slack RTM, etc.) from the connector core — the
registry handles all generic lifecycle (start, stop, test, status) and
each adapter implementation only contains its platform code.
"""

from .base import (
    Adapter,
    AdapterError,
    AdapterHooks,
    AdapterStatusSnapshot,
    OrchestratorRequest,
    PendingLinkPayload,
    PlatformIdentity,
    TestResult,
    build_adapter,
    field_specs_for,
)

__all__ = [
    "Adapter",
    "AdapterError",
    "AdapterHooks",
    "AdapterStatusSnapshot",
    "OrchestratorRequest",
    "PendingLinkPayload",
    "PlatformIdentity",
    "TestResult",
    "build_adapter",
    "field_specs_for",
]
