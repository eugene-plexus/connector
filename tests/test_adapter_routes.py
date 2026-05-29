"""HTTP tests for the /v1/adapters CRUD surface.

Real `DiscordAdapter.start()` would call discord.py's websocket; we
monkeypatch it to a no-op so the route layer can be exercised without
network. The rest of the lifecycle (persistence, registry state,
status surface) is real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eugene_plexus_connector.adapters.discord_adapter import DiscordAdapter


@pytest.fixture(autouse=True)
def _stub_discord_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `DiscordAdapter.start` / `stop` with no-op coroutines —
    the adapter is still constructed (we want the entry validation
    path), but no network connection is attempted.

    We also stub `status()` to return a 'connected' snapshot so the
    route's status surface returns predictable values."""

    async def _noop_start(self: DiscordAdapter) -> None:
        self._status = "connected"

    async def _noop_stop(self: DiscordAdapter) -> None:
        self._status = "disconnected"

    monkeypatch.setattr(DiscordAdapter, "start", _noop_start)
    monkeypatch.setattr(DiscordAdapter, "stop", _noop_stop)


def test_list_adapters_starts_empty(client: TestClient) -> None:
    response = client.get("/v1/adapters")
    assert response.status_code == 200
    assert response.json() == {"adapters": []}


def test_create_adapter_persists_and_starts(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    payload = make_discord_adapter_entry()
    response = client.post("/v1/adapters", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["entry"]["name"] == "discord-1"
    assert body["entry"]["kind"] == "discord"
    assert body["status"] == "connected"  # stubbed start = connected

    # Listing shows the persisted adapter.
    list_response = client.get("/v1/adapters")
    assert list_response.status_code == 200
    names = [a["entry"]["name"] for a in list_response.json()["adapters"]]
    assert names == ["discord-1"]


def test_create_adapter_rejects_duplicate_name(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    payload = make_discord_adapter_entry()
    assert client.post("/v1/adapters", json=payload).status_code == 201
    second = client.post("/v1/adapters", json=payload)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]["detail"]


def test_create_adapter_rejects_missing_bot_token(client: TestClient) -> None:
    """Adapter-specific config validation kicks in inside
    `from_entry` — the route surfaces it as a 400."""
    response = client.post(
        "/v1/adapters",
        json={
            "name": "discord-1",
            "kind": "discord",
            "adapterConfig": {},  # botToken missing
            "enabled": True,
        },
    )
    assert response.status_code == 400, response.text
    assert "botToken" in response.json()["detail"]["detail"]


def test_get_adapter_returns_404_for_unknown(client: TestClient) -> None:
    response = client.get("/v1/adapters/nope")
    assert response.status_code == 404


def test_patch_adapter_replaces_entry_and_restarts(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    create = client.post("/v1/adapters", json=make_discord_adapter_entry(bot_token="t1"))
    assert create.status_code == 201

    # Update with a new bot token + channel allowlist.
    updated = {
        "name": "discord-1",
        "kind": "discord",
        "adapterConfig": {
            "botToken": "t2",
            "channelAllowlist": "100,200",
        },
        "enabled": True,
    }
    response = client.patch("/v1/adapters/discord-1", json=updated)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry"]["adapterConfig"]["channelAllowlist"] == "100,200"


def test_patch_adapter_404_for_unknown(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    response = client.patch("/v1/adapters/missing", json=make_discord_adapter_entry(name="missing"))
    assert response.status_code == 404


def test_patch_adapter_rejects_name_mismatch(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    client.post("/v1/adapters", json=make_discord_adapter_entry())
    response = client.patch(
        "/v1/adapters/discord-1",
        json=make_discord_adapter_entry(name="different-name"),
    )
    assert response.status_code == 400


def test_delete_adapter_stops_and_removes(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    client.post("/v1/adapters", json=make_discord_adapter_entry())
    delete = client.delete("/v1/adapters/discord-1")
    assert delete.status_code == 204

    list_response = client.get("/v1/adapters")
    assert list_response.json() == {"adapters": []}


def test_get_adapter_config_schema_returns_discord_fields(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    client.post("/v1/adapters", json=make_discord_adapter_entry())
    response = client.get("/v1/adapters/discord-1/config/schema")
    assert response.status_code == 200, response.text
    body = response.json()
    keys = {f["key"] for f in body["fields"]}
    assert keys == {"botToken", "channelAllowlist", "channelContextLimit"}


def test_disabled_adapter_is_persisted_but_not_started(client: TestClient) -> None:
    from tests.conftest import make_discord_adapter_entry

    payload = make_discord_adapter_entry()
    payload["enabled"] = False
    response = client.post("/v1/adapters", json=payload)
    assert response.status_code == 201
    body = response.json()
    # Persisted but not running → status "disabled".
    assert body["status"] == "disabled"
