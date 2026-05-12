"""Outbound client to the orchestrator's /v1/chat.

Posts inbound platform messages as `ChatRequest` and returns the
blended assistant response. Adapters use this via the `send_to_
orchestrator` hook so they don't need to know about the orchestrator's
URL or the bearer-token threading.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._generated.orchestrator_models import ChatRequest, ChatResponse


class OrchestratorClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 180.0,
        service_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = (
            {"Authorization": f"Bearer {service_token}"} if service_token else None
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers=headers,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """POST /v1/chat. Raises `httpx.HTTPStatusError` on 4xx/5xx."""
        payload: dict[str, Any] = request.model_dump(mode="json", exclude_none=True)
        response = await self._client.post("/v1/chat", json=payload)
        response.raise_for_status()
        return ChatResponse.model_validate(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()
