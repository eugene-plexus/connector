"""FastAPI app factory + lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI

from . import __version__
from ._generated.orchestrator_models import (
    ChannelContextEntry as ChatChannelContext,
)
from ._generated.orchestrator_models import (
    ChatRequest,
)
from ._generated.orchestrator_models import (
    MessageSource as ChatMessageSource,
)
from .adapters import AdapterHooks, OrchestratorRequest, PendingLinkPayload
from .auth_state import AuthState, load_auth_state
from .clients import IdentityClient, OrchestratorClient
from .config import ConfigStore
from .dependencies import require_authorized, require_operator
from .routes import config as config_routes
from .routes import health as health_routes
from .settings import Settings, load_settings
from .store import AdapterRegistry, AdaptersStore

log = logging.getLogger(__name__)


def _build_hooks(
    *,
    orchestrator_client: OrchestratorClient,
    identity_client: IdentityClient,
) -> AdapterHooks:
    """Build the per-adapter hook bundle.

    The hooks close over the outbound clients so adapter code never
    sees raw httpx — keeps auth-header threading in one place and
    lets tests substitute fakes for end-to-end coverage.
    """

    async def resolve_person(platform: str, account_id: str) -> UUID | None:
        try:
            return await identity_client.resolve_person_by_alias(
                platform=platform, account_id=account_id
            )
        except httpx.HTTPError as e:
            log.warning(
                "identity service unreachable while resolving %s:%s: %s "
                "(treating as unknown user)",
                platform,
                account_id,
                e,
            )
            return None

    async def file_pending_link(payload: PendingLinkPayload) -> bool:
        try:
            return await identity_client.file_pending_link(
                platform=payload.platform,
                account_id=payload.account_id,
                handle=payload.handle,
                display_name=payload.display_name,
                avatar_url=payload.avatar_url,
                triggering_message=payload.triggering_message,
                adapter_private=payload.adapter_private,
            )
        except httpx.HTTPError as e:
            log.warning(
                "identity service unreachable while filing pending link "
                "for %s:%s: %s",
                payload.platform,
                payload.account_id,
                e,
            )
            return False

    async def send_to_orchestrator(req: OrchestratorRequest) -> str:
        # Both MessageSource / ChannelContextEntry exist in distinct
        # generated modules (common.yaml-side and orchestrator.yaml-
        # side) with identical wire shapes. Round-trip through dict to
        # bridge — keeps the two model namespaces decoupled.
        chat_request = ChatRequest(
            message=req.content,
            personId=req.person_id,
            conversationId=req.conversation_id,
            source=ChatMessageSource.model_validate(
                req.source.model_dump(exclude_none=True)
            ),
            channelContext=(
                [
                    ChatChannelContext.model_validate(
                        e.model_dump(exclude_none=True)
                    )
                    for e in req.channel_context
                ]
                if req.channel_context
                else None
            ),
        )
        response = await orchestrator_client.chat(chat_request)
        return response.message.content

    return AdapterHooks(
        resolve_person=resolve_person,
        file_pending_link=file_pending_link,
        send_to_orchestrator=send_to_orchestrator,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    config_store = ConfigStore(settings.config_file)
    if settings.safe_mode:
        log.warning(
            "starting in SAFE MODE (EUGENE_PLEXUS_CONNECTOR_SAFE_MODE=1); "
            "ignoring %s and running on defaults. No adapters started; fix "
            "config via /v1/config, then restart without the env var.",
            settings.config_file,
        )
    else:
        config_store.load()
    app.state.config_store = config_store
    app.state.safe_mode = settings.safe_mode

    if not hasattr(app.state, "auth_state"):
        app.state.auth_state = load_auth_state(
            signing_key_b64=settings.auth_signing_key,
            service_token=settings.service_token,
            master_key_b64=settings.master_key,
        )
    auth_state: AuthState = app.state.auth_state

    # Outbound clients. Tests can pre-populate `app.state.orchestrator_client`
    # / `app.state.identity_client` to inject fakes.
    if not hasattr(app.state, "orchestrator_client"):
        app.state.orchestrator_client = OrchestratorClient(
            base_url=str(config_store.get("orchestratorUrl") or "http://127.0.0.1:8080"),
            service_token=auth_state.service_token,
        )
        owns_orchestrator_client = True
    else:
        owns_orchestrator_client = False
    if not hasattr(app.state, "identity_client"):
        app.state.identity_client = IdentityClient(
            base_url=str(config_store.get("identityUrl") or "http://127.0.0.1:8084"),
            service_token=auth_state.service_token,
        )
        owns_identity_client = True
    else:
        owns_identity_client = False

    # Adapter persistence + runtime registry.
    adapters_store = AdaptersStore(settings.adapters_file)
    if not settings.safe_mode:
        try:
            adapters_store.load()
        except Exception as e:
            log.error(
                "failed to load %s (%s); starting with no adapters. Fix "
                "via the /v1/adapters endpoints and restart.",
                settings.adapters_file,
                e,
            )
    app.state.adapters_store = adapters_store

    registry = AdapterRegistry()
    app.state.adapter_registry = registry

    hooks = _build_hooks(
        orchestrator_client=app.state.orchestrator_client,
        identity_client=app.state.identity_client,
    )
    app.state.adapter_hooks = hooks

    # Auto-start every enabled adapter from the persisted list. Failures
    # are logged but don't block startup — the route layer surfaces
    # them via AdapterStatus.lastError.
    if not settings.safe_mode:
        for entry in adapters_store.list():
            if entry.enabled is False:
                continue
            try:
                await registry.start(entry=entry, hooks=hooks)
            except Exception:
                log.exception("failed to auto-start adapter %r at boot", entry.name)

    try:
        yield
    finally:
        await registry.stop_all()
        if owns_orchestrator_client:
            await app.state.orchestrator_client.aclose()
        if owns_identity_client:
            await app.state.identity_client.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    app = FastAPI(
        title="Eugene Plexus — connector",
        description=(
            "Bridges external chat platforms to the orchestrator. "
            "v0.2 ships the Discord adapter; slack/matrix/telegram/gmail "
            "are v0.3+ drop-ins."
        ),
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = settings

    # Health stays unauthenticated.
    app.include_router(health_routes.router)

    # Operator-only: config edits + adapter mutations.
    operator_only = [Depends(require_operator)]
    app.include_router(config_routes.router, dependencies=operator_only)

    # Adapter routes: mix of operator (mutations) and authorized (reads).
    # Per-route deps are applied inside the router so the granular
    # rules are visible alongside the handlers.
    from .routes import adapters as adapter_routes
    authorized = [Depends(require_authorized)]
    app.include_router(adapter_routes.router, dependencies=authorized)

    return app
