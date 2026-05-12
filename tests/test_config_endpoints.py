"""Standard config trio tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_get_config_schema(client: TestClient) -> None:
    response = client.get("/v1/config/schema")
    assert response.status_code == 200
    body = response.json()
    assert body["component"] == "connector"
    keys = {f["key"] for f in body["fields"]}
    assert keys == {"orchestratorUrl", "identityUrl", "logLevel"}


def test_get_config_defaults(client: TestClient) -> None:
    response = client.get("/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["orchestratorUrl"] == "http://127.0.0.1:8080"
    assert body["identityUrl"] == "http://127.0.0.1:8084"
    assert body["logLevel"] == "INFO"


def test_patch_config_partial(client: TestClient) -> None:
    response = client.patch(
        "/v1/config",
        json={"orchestratorUrl": "http://example.invalid:9000", "logLevel": "DEBUG"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["applied"]) == {"orchestratorUrl", "logLevel"}
    assert body["requiresRestart"] is True
