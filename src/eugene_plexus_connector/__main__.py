"""CLI entry point: `python -m eugene_plexus_connector`."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    # See orchestrator/hemisphere-driver __main__ for the rationale.
    # uvicorn only configures its own loggers; basicConfig with
    # force=True gives our application warnings/info a timestamp +
    # level + logger name so the watchdog's combined log is scannable.
    # Connector doesn't expose a logLevel config field yet — hardcode
    # INFO; v0.3 wiring to a config field is a small follow-up if
    # operators want noisier debugging.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    settings = load_settings()
    app = create_app(settings=settings)
    # Bind port: prefer the env var threaded in by the watchdog supervisor;
    # fall back to the canonical connector port for standalone dev runs.
    port = int(os.environ.get("EUGENE_PLEXUS_CONNECTOR_BIND_PORT", "8085"))
    uvicorn.run(app, host=settings.bind_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
