"""Startup-time settings, sourced from environment variables.

Distinct from runtime *config* (see `config.py`), which is editable via
`PATCH /v1/config`. These settings only control bootstrap.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EUGENE_PLEXUS_CONNECTOR_",
        env_file=None,
        case_sensitive=False,
    )

    config_file: Path = Path("config.yaml")
    """Where the runtime config is persisted. PATCH /v1/config writes here."""

    adapters_file: Path = Path("adapters.yaml")
    """Where the adapter list is persisted. Operator can edit by hand
    when the UI isn't reachable; the connector reloads it on restart."""

    bind_host: str = "127.0.0.1"
    """Network interface to bind. Override to 0.0.0.0 for tailnet exposure."""

    safe_mode: bool = False
    """If true, skip loading the persisted config file at startup and run on
    built-in defaults. Adapters list is also skipped — no platform
    connections attempted. Set by the watchdog via
    EUGENE_PLEXUS_CONNECTOR_SAFE_MODE=1 when a previous boot failed.
    PATCH /v1/config still writes to `config_file` normally so the
    operator's repair survives the next non-safe-mode boot."""

    auth_signing_key: str | None = None
    """Base64-encoded 32-byte HMAC signing key, supplied by the watchdog
    at spawn time (EUGENE_PLEXUS_CONNECTOR_AUTH_SIGNING_KEY). When
    absent the component runs unauthenticated — dev / standalone path
    only."""

    service_token: str | None = None
    """Long-lived service JWT for outbound calls to peer components
    (EUGENE_PLEXUS_CONNECTOR_SERVICE_TOKEN). The connector POSTs every
    inbound platform message to the orchestrator's /v1/chat with this
    bearer attached, and files PendingIdentityLink to identity with
    the same."""

    master_key: str | None = None
    """Base64-encoded 32-byte secretbox key
    (EUGENE_PLEXUS_CONNECTOR_MASTER_KEY). Reserved for v0.2.x sealing
    of adapter-config bot tokens at rest; currently captured but not
    consumed."""

    watchdog_url: str = "http://127.0.0.1:8079"
    """Watchdog endpoint used to auto-resolve peer URLs
    (orchestratorUrl, identityUrl) when not explicitly set in config.
    The watchdog is the source of truth for body-component topology;
    duplicating URLs in every component's config is the OpenClaw-style
    trap. Override with EUGENE_PLEXUS_CONNECTOR_WATCHDOG_URL on
    networked deployments where the watchdog isn't on the loopback."""


def load_settings() -> Settings:
    return Settings()
