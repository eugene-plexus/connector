"""Adapter CRUD + status + test + adapter-config-schema routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from .._generated.common_models import (
    AdapterEntry,
    ConfigSchema,
    ConfigTestRequest,
    ConfigTestResult,
    Problem,
)
from ..adapters import AdapterError, field_specs_for
from ..adapters.base import AdapterHooks
from ..dependencies import require_operator
from ..store import AdapterRegistry, AdaptersStore

router = APIRouter(tags=["adapters"])


def _ctx(request: Request) -> tuple[AdaptersStore, AdapterRegistry, AdapterHooks]:
    return (
        request.app.state.adapters_store,
        request.app.state.adapter_registry,
        request.app.state.adapter_hooks,
    )


def _problem(status_code: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=Problem(
            type=f"https://github.com/eugene-plexus/connector#{title.replace(' ', '-').lower()}",
            title=title,
            status=status_code,
            detail=detail,
            component="connector",
        ).model_dump(exclude_none=True),
    )


def _status_payload(entry: AdapterEntry, registry: AdapterRegistry) -> dict[str, Any]:
    """Render an `AdapterStatus` for a single adapter.

    Returns a dict so the route can serialize platformIdentity
    (a dataclass on our side) as the open-shape `additionalProperties:
    true` object the spec declares.
    """
    snapshot = registry.status(entry.name)
    if snapshot is None:
        # Persisted but not running (disabled, or in safe mode).
        if entry.enabled is False:
            return {
                "entry": entry.model_dump(mode="json", exclude_none=True),
                "status": "disabled",
            }
        return {
            "entry": entry.model_dump(mode="json", exclude_none=True),
            "status": "disconnected",
        }
    out: dict[str, Any] = {
        "entry": entry.model_dump(mode="json", exclude_none=True),
        "status": snapshot.status,
    }
    if snapshot.connected_at is not None:
        out["connectedAt"] = snapshot.connected_at
    if snapshot.last_error is not None:
        out["lastError"] = snapshot.last_error
    if snapshot.platform_identity is not None:
        pi = snapshot.platform_identity
        pi_dict: dict[str, Any] = {
            "platformAccountId": pi.platform_account_id,
            "displayName": pi.display_name,
        }
        if pi.extras:
            pi_dict.update(pi.extras)
        out["platformIdentity"] = pi_dict
    return out


@router.get("/v1/adapters")
async def list_adapters(request: Request) -> dict[str, Any]:
    store, registry, _ = _ctx(request)
    return {
        "adapters": [_status_payload(e, registry) for e in store.list()],
    }


@router.get("/v1/adapters/{name}")
async def get_adapter(request: Request, name: str) -> dict[str, Any]:
    store, registry, _ = _ctx(request)
    entry = store.get(name)
    if entry is None:
        raise _problem(404, "adapter not found", f"no adapter named {name!r}")
    return _status_payload(entry, registry)


@router.post(
    "/v1/adapters",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
async def create_adapter(request: Request, body: AdapterEntry) -> dict[str, Any]:
    store, registry, hooks = _ctx(request)
    try:
        store.add(body)
    except AdapterError as e:
        raise _problem(e.status_code, "adapter conflict", e.detail) from e

    # Auto-start unless explicitly disabled. Failures here are logged
    # into AdapterStatus.lastError so the operator can see them in the
    # UI without watching server logs.
    if body.enabled is not False:
        try:
            await registry.start(entry=body, hooks=hooks)
        except AdapterError as e:
            raise _problem(e.status_code, "adapter start failed", e.detail) from e
        except Exception as e:
            raise _problem(500, "adapter start failed", f"unexpected error: {e!r}") from e
    return _status_payload(body, registry)


@router.patch(
    "/v1/adapters/{name}",
    dependencies=[Depends(require_operator)],
)
async def update_adapter(request: Request, name: str, body: AdapterEntry) -> dict[str, Any]:
    store, registry, hooks = _ctx(request)
    if body.name != name:
        raise _problem(
            400,
            "adapter name mismatch",
            f"URL name {name!r} does not match body name {body.name!r}",
        )
    try:
        store.update(body)
    except AdapterError as e:
        raise _problem(e.status_code, "adapter not found", e.detail) from e

    # Restart the adapter so config changes (bot token, allowlist) take
    # effect. Simpler + more predictable than diffing the entry.
    await registry.stop(name)
    if body.enabled is not False:
        try:
            await registry.start(entry=body, hooks=hooks)
        except AdapterError as e:
            raise _problem(e.status_code, "adapter restart failed", e.detail) from e
        except Exception as e:
            raise _problem(500, "adapter restart failed", f"unexpected error: {e!r}") from e
    return _status_payload(body, registry)


@router.delete(
    "/v1/adapters/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_operator)],
)
async def delete_adapter(request: Request, name: str) -> Response:
    store, registry, _ = _ctx(request)
    if store.get(name) is None:
        raise _problem(404, "adapter not found", f"no adapter named {name!r}")
    await registry.stop(name)
    store.remove(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v1/adapters/{name}/config/schema", response_model=ConfigSchema)
async def get_adapter_config_schema(request: Request, name: str) -> ConfigSchema:
    store, _, _ = _ctx(request)
    entry = store.get(name)
    if entry is None:
        raise _problem(404, "adapter not found", f"no adapter named {name!r}")
    try:
        fields = field_specs_for(entry.kind)
    except AdapterError as e:
        raise _problem(e.status_code, "adapter config schema unavailable", e.detail) from e
    return ConfigSchema(
        component=f"connector:{name}",
        fields=fields,
        categories={"auth": "Authentication", "behavior": "Behavior"},
    )


@router.post(
    "/v1/adapters/{name}/test",
    dependencies=[Depends(require_operator)],
)
async def test_adapter(
    request: Request,
    name: str,
    body: ConfigTestRequest | None = None,
) -> ConfigTestResult:
    _, registry, _ = _ctx(request)
    adapter = registry.get(name)
    if adapter is None:
        raise _problem(
            404,
            "adapter not running",
            f"adapter {name!r} is not currently running; start it (or check "
            "for boot errors via GET /v1/adapters/{name}) before testing.",
        )
    import time

    started = time.monotonic()
    result = await adapter.test()
    latency_ms = int((time.monotonic() - started) * 1000)
    return ConfigTestResult(
        ok=result.ok,
        component=f"connector:{name}",
        latencyMs=latency_ms,
        summary=result.detail if result.ok else None,
        error=None if result.ok else result.detail,
    )
