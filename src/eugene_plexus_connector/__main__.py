"""CLI entry point: `python -m eugene_plexus_connector`."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app
from .settings import load_settings


def main() -> None:
    settings = load_settings()
    app = create_app(settings=settings)
    # Bind port: prefer the env var threaded in by the watchdog supervisor;
    # fall back to the canonical connector port for standalone dev runs.
    port = int(os.environ.get("EUGENE_PLEXUS_CONNECTOR_BIND_PORT", "8085"))
    uvicorn.run(app, host=settings.bind_host, port=port, log_level="info")


if __name__ == "__main__":
    main()
