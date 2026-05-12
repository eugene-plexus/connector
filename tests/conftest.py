"""Pytest fixtures shared across the connector test suite.

Tests bypass discord.py entirely: a `StubAdapter` plays the role of a
platform adapter, and `FakeOrchestratorClient` / `FakeIdentityClient`
play the role of the outbound HTTP clients. The connector's HTTP
surface, persistence, lifecycle, and routes are all exercised with
these doubles.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eugene_plexus_connector._generated.common_models import (
    AdapterKind,
)
from eugene_plexus_connector._generated.orchestrator_models import (
    ChatRequest,
    ChatResponse,
    Decision,
    Message,
    NTLevel,
    NTState,
    PassRecord,
    Role,
)
from eugene_plexus_connector.adapters.base import (
    AdapterHooks,
    AdapterStatusSnapshot,
    PlatformIdentity,
    TestResult,
)
from eugene_plexus_connector.app import create_app
from eugene_plexus_connector.settings import Settings

# ---------------------------------------------------------------------------
# Stub clients (no httpx calls)
# ---------------------------------------------------------------------------


class FakeOrchestratorClient:
    def __init__(self) -> None:
        self.calls: list[ChatRequest] = []
        self.canned_reply: str = "blended reply"

    @property
    def base_url(self) -> str:
        return "in-process"

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        from datetime import UTC, datetime
        from uuid import uuid4

        _neutral = NTLevel(level=0.5, baseline=0.5, decay=0.0)
        nt_state = NTState(
            lastUpdated=datetime.now(UTC),
            dopamine=_neutral,
            serotonin=_neutral,
            norepinephrine=_neutral,
            gaba=_neutral,
            cortisol=_neutral,
            acetylcholine=_neutral,
        )
        return ChatResponse(
            conversationId=request.conversationId or uuid4(),
            message=Message(role=Role.assistant, content=self.canned_reply),
            passes=[
                PassRecord(
                    passIndex=0,
                    hemispheres=[],
                    callosum=__import__(
                        "eugene_plexus_connector._generated.orchestrator_models",
                        fromlist=["CallosumState"],
                    ).CallosumState(agreement=1.0, decision=Decision.terminate),
                )
            ],
            ntStateAtStart=nt_state,
            ntStateAtEnd=nt_state,
        )

    async def aclose(self) -> None:
        return None


class FakeIdentityClient:
    """Records pending-link filings and answers alias lookups from a dict."""

    def __init__(
        self,
        *,
        aliases: dict[tuple[str, str], UUID] | None = None,
    ) -> None:
        self._aliases: dict[tuple[str, str], UUID] = dict(aliases or {})
        self.pending_links: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return "in-process"

    async def list_persons(self) -> list[Any]:
        return []

    async def resolve_person_by_alias(
        self, *, platform: str, account_id: str
    ) -> UUID | None:
        return self._aliases.get((platform, account_id))

    async def file_pending_link(
        self,
        *,
        platform: str,
        account_id: str,
        handle: str | None,
        display_name: str | None,
        avatar_url: str | None,
        triggering_message: str,
        adapter_private: dict[str, Any] | None = None,
    ) -> bool:
        self.pending_links.append(
            {
                "platform": platform,
                "account_id": account_id,
                "handle": handle,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "triggering_message": triggering_message,
                "adapter_private": adapter_private,
            }
        )
        return True

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Stub adapter (no discord.py)
# ---------------------------------------------------------------------------


class StubAdapter:
    """In-process Adapter implementation. Tests construct one directly
    rather than going through `build_adapter` — that registry path
    only knows about the real discord kind."""

    def __init__(
        self,
        *,
        name: str,
        hooks: AdapterHooks,
        platform_identity: PlatformIdentity | None = None,
        test_ok: bool = True,
    ) -> None:
        self.name = name
        self._hooks = hooks
        self._status = "disabled"
        self._platform_identity = platform_identity
        self._test_ok = test_ok
        self.start_calls = 0
        self.stop_calls = 0

    @classmethod
    def field_specs(cls) -> list[Any]:
        return []

    async def start(self) -> None:
        self.start_calls += 1
        self._status = "connected"

    async def stop(self) -> None:
        self.stop_calls += 1
        self._status = "disconnected"

    def status(self) -> AdapterStatusSnapshot:
        return AdapterStatusSnapshot(
            status=self._status,
            connected_at=None,
            last_error=None,
            platform_identity=self._platform_identity,
        )

    async def test(self) -> TestResult:
        return TestResult(ok=self._test_ok, detail="stub")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_file=tmp_path / "config.yaml",
        adapters_file=tmp_path / "adapters.yaml",
    )


@pytest.fixture
def orch_client() -> FakeOrchestratorClient:
    return FakeOrchestratorClient()


@pytest.fixture
def identity_client() -> FakeIdentityClient:
    return FakeIdentityClient()


@pytest.fixture
def app(
    settings: Settings,
    orch_client: FakeOrchestratorClient,
    identity_client: FakeIdentityClient,
) -> FastAPI:
    """Build an app with the FAKE outbound clients pre-injected.

    No adapters are pre-seeded — tests that need an adapter create
    one via `POST /v1/adapters` (which routes through the real
    `build_adapter` registry). Because the registry only knows about
    the `discord` kind, tests that need a stub adapter wire it in
    AFTER lifespan startup.
    """
    app = create_app(settings=settings)
    app.state.orchestrator_client = orch_client
    app.state.identity_client = identity_client
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_discord_adapter_entry(
    *, name: str = "discord-1", bot_token: str = "fake-token"
) -> dict[str, Any]:
    """Build the JSON payload for POST /v1/adapters with a discord kind."""
    return {
        "name": name,
        "kind": AdapterKind.discord.value,
        "adapterConfig": {"botToken": bot_token, "channelAllowlist": ""},
        "enabled": True,
    }
