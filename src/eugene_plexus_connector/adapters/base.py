"""Adapter Protocol + registry.

An `Adapter` is the platform-specific runtime: discord.py for the
`discord` kind, slack-sdk for `slack`, etc. The connector's core
machinery (CRUD routes, lifecycle, status surface) is platform-agnostic
and talks to adapters through this Protocol only.

Adapter lifecycle:

  1. `build_adapter(entry, hooks)` constructs from the persisted
     `AdapterEntry` plus a set of `AdapterHooks` (callbacks the
     adapter uses to reach back into the connector — resolving a
     platform user to a personId, filing a pending link, posting
     to the orchestrator).
  2. `await adapter.start()` — connects to the platform. Runs in
     the background until `stop()` is called.
  3. `await adapter.stop()` — graceful shutdown of the platform
     connection.
  4. `adapter.status()` — current operational state (snapshot).
  5. `await adapter.test()` — non-destructive platform reachability
     check (e.g. "fetch my own bot user record"). Used by the UI's
     Test Connection button.

Hooks return values from the connector's outbound clients
(orchestrator chat, identity link/lookup). Adapters never call the
HTTP clients directly — keeps the auth header threading in one
place and lets tests substitute fakes for end-to-end coverage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from .._generated.common_models import (
        AdapterEntry,
        AdapterKind,
        ChannelContextEntry,
        ConfigField,
        MessageSource,
    )


# -------- Hook signatures --------


# Resolve a platform user to a personId. Returns None if the user is
# unknown (adapter then files a pending link).
ResolvePersonHook = Callable[[str, str], Awaitable[UUID | None]]
"""(platform: str, account_id: str) -> personId | None"""


# File a PendingIdentityLink with the identity component.
# Returns True on success or 409 (already pending), False otherwise.
FilePendingLinkHook = Callable[
    [
        "PendingLinkPayload",
    ],
    Awaitable[bool],
]


# Send a message to the orchestrator's POST /v1/chat. Returns the
# orchestrator's response text (the blended reply) or raises on transport
# failure. The adapter is responsible for delivering this back to its
# platform.
SendToOrchestratorHook = Callable[
    [
        "OrchestratorRequest",
    ],
    Awaitable[str],
]


@dataclass(frozen=True)
class PendingLinkPayload:
    """Fields the adapter needs to file a pending link.

    Maps 1:1 to `PendingIdentityLink` in the spec; broken out as a
    dataclass so adapter code doesn't have to import the generated
    Pydantic types directly.
    """

    platform: str
    account_id: str
    handle: str | None
    display_name: str | None
    avatar_url: str | None
    triggering_message: str
    adapter_private: dict[str, Any] | None = None


@dataclass(frozen=True)
class OrchestratorRequest:
    """Fields the adapter sends to the orchestrator's /v1/chat.

    The adapter has already resolved `person_id` (either from a
    PlatformAlias or as the operator default if the platform user is
    the operator on the local UI). `conversation_id` is the adapter's
    stable mapping from a platform channel/thread to an Eugene
    conversation; omit on first contact to let the orchestrator mint
    a new id.
    """

    person_id: UUID
    content: str
    conversation_id: UUID | None
    source: MessageSource
    channel_context: list[ChannelContextEntry] | None


# -------- Status snapshot --------


@dataclass(frozen=True)
class PlatformIdentity:
    """Adapter-supplied summary of the platform identity it's
    authenticated as (e.g. Discord bot user id + username). Surfaced
    in the UI's adapter list so the operator can verify they're
    authenticated as the right bot."""

    platform_account_id: str
    display_name: str
    extras: dict[str, Any] | None = None


@dataclass(frozen=True)
class AdapterStatusSnapshot:
    """Snapshot of an adapter's current operational state. Returned by
    `Adapter.status()` and surfaced in `AdapterStatus.status` over the
    wire (the matching enum lives in connector.yaml).
    """

    status: str  # starting / connected / disconnected / rate_limited / error / disabled
    connected_at: str | None  # ISO-8601 timestamp
    last_error: str | None
    platform_identity: PlatformIdentity | None


@dataclass(frozen=True)
class TestResult:
    """Result of `Adapter.test()` — what `/v1/adapters/{name}/test` returns."""

    ok: bool
    detail: str


class AdapterError(Exception):
    """Raised by adapters for typed failures. `status_code` is the HTTP
    status to map to; `detail` is the operator-facing message."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# -------- Hooks bundle --------


@dataclass(frozen=True)
class AdapterHooks:
    """Callbacks the connector passes to each adapter at construction.

    All async — adapters run inside the asyncio event loop. Holding
    these as a frozen dataclass means the adapter doesn't need a
    reference to the wider connector app state, which keeps adapter
    code testable in isolation.
    """

    resolve_person: ResolvePersonHook
    file_pending_link: FilePendingLinkHook
    send_to_orchestrator: SendToOrchestratorHook


# -------- The Adapter Protocol --------


class Adapter(Protocol):
    """The interface every platform adapter implements."""

    name: str
    """Operator-supplied label from `AdapterEntry.name`."""

    @classmethod
    def field_specs(cls) -> list[ConfigField]:
        """Adapter-specific config fields, rendered by the UI inside
        the adapter's settings panel. Each field validates the
        `AdapterEntry.adapterConfig` map at PATCH time."""
        ...

    async def start(self) -> None:
        """Connect to the platform. Returns after the connection
        attempt is initiated; long-running platform loops should
        spawn their own background task and return promptly."""
        ...

    async def stop(self) -> None:
        """Disconnect from the platform. Idempotent — safe to call
        on an already-stopped adapter."""
        ...

    def status(self) -> AdapterStatusSnapshot:
        """Return the current operational state. Synchronous because
        the route layer hits this on every GET — must be cheap."""
        ...

    async def test(self) -> TestResult:
        """Non-destructive platform reachability check. Used by the
        UI's Test Connection button."""
        ...


# -------- Registry --------


def build_adapter(
    entry: AdapterEntry, hooks: AdapterHooks
) -> Adapter:
    """Construct an adapter instance from the persisted entry.

    Raises `AdapterError(status_code=400, ...)` if the kind isn't
    supported or the adapter-specific config fails validation.
    """
    # Lazy import so a missing optional adapter (e.g. discord.py not
    # installed) doesn't break the module import.
    from .discord_adapter import DiscordAdapter

    kind_str = entry.kind.value if hasattr(entry.kind, "value") else str(entry.kind)
    if kind_str == "discord":
        return DiscordAdapter.from_entry(entry, hooks)

    raise AdapterError(
        status_code=400,
        detail=(
            f"Unsupported adapter kind {kind_str!r}. v0.2 supports: discord."
        ),
    )


def field_specs_for(kind: AdapterKind | str) -> list[ConfigField]:
    """Return the `ConfigField` list for the given adapter kind. Used
    by `GET /v1/adapters/{name}/config/schema`."""
    from .discord_adapter import DiscordAdapter

    kind_str = kind.value if hasattr(kind, "value") else str(kind)
    if kind_str == "discord":
        return DiscordAdapter.field_specs()

    raise AdapterError(
        status_code=400,
        detail=f"Unsupported adapter kind {kind_str!r}.",
    )
