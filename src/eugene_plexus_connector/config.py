"""Runtime configuration: standard Eugene Plexus config trio.

The connector's runtime config is **connector-level** — orchestrator
URL, identity URL, log level. Adapter-specific config (Discord bot
tokens, channel allowlists, etc.) lives under each adapter's entry in
the persisted adapters list, NOT here.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from ._generated.common_models import (
    ComponentKind,
    ConfigDocument,
    ConfigField,
    ConfigFieldError,
    ConfigSchema,
    ConfigUpdateRequest,
    ConfigUpdateResult,
    ConfigValueType,
)

CATEGORY_LABELS: dict[str, str] = {
    "topology": "Topology",
    "logging": "Logging",
}

FIELDS: list[ConfigField] = [
    ConfigField(
        key="orchestratorUrl",
        label="Orchestrator",
        description=(
            "Which `eugene-plexus/orchestrator` instance the connector "
            "POSTs every inbound platform message to as a `ChatRequest`. "
            "Sourced from the watchdog topology."
        ),
        category="topology",
        valueType=ConfigValueType.url,
        componentKindHint=ComponentKind.orchestrator,
        default="http://127.0.0.1:8080",
        required=True,
        requiresRestart=True,
    ),
    ConfigField(
        key="identityUrl",
        label="Identity",
        description=(
            "Which `eugene-plexus/identity` instance adapters call to "
            "resolve known platform users to `personId` and to file "
            "PendingIdentityLink for unknown ones. Sourced from the "
            "watchdog topology."
        ),
        category="topology",
        valueType=ConfigValueType.url,
        componentKindHint=ComponentKind.identity,
        default="http://127.0.0.1:8084",
        required=True,
        requiresRestart=True,
    ),
    ConfigField(
        key="logLevel",
        label="Log level",
        description=(
            "How chatty the connector's terminal output is. `DEBUG` "
            "prints every adapter event; `INFO` is the normal "
            "operating level; `WARNING` and `ERROR` go progressively "
            "quieter."
        ),
        category="logging",
        valueType=ConfigValueType.enum,
        default="INFO",
        enumValues=["DEBUG", "INFO", "WARNING", "ERROR"],
        requiresRestart=True,
    ),
]

_FIELDS_BY_KEY: dict[str, ConfigField] = {f.key: f for f in FIELDS}


def as_schema() -> ConfigSchema:
    return ConfigSchema(
        component="connector",
        fields=list(FIELDS),
        categories=CATEGORY_LABELS,
    )


def _defaults() -> dict[str, Any]:
    return {f.key: f.default for f in FIELDS if f.default is not None}


def _validate_value(field: ConfigField, value: Any) -> str | None:
    if value is None:
        return None
    vt = field.valueType
    if vt == ConfigValueType.enum:
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        allowed = field.enumValues or []
        if value not in allowed:
            return f"must be one of {allowed}"
        return None
    if vt in (ConfigValueType.string, ConfigValueType.url, ConfigValueType.file_path):
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"
        return None
    return f"unsupported valueType: {vt}"


class ConfigStore:
    """File-backed config store. Thread-safe single-writer model."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._values: dict[str, Any] = _defaults()

    def load(self) -> None:
        with self._lock:
            if self._path.exists():
                raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"{self._path} must be a YAML mapping at the root"
                    )
                merged = _defaults()
                for k, v in raw.items():
                    if k in _FIELDS_BY_KEY:
                        merged[k] = v
                self._values = merged
            else:
                self._values = _defaults()
                self._write_locked()

    def as_document(self) -> ConfigDocument:
        with self._lock:
            return ConfigDocument.model_validate(dict(self._values))

    def apply_patch(self, request: ConfigUpdateRequest) -> ConfigUpdateResult:
        applied: list[str] = []
        rejected: list[ConfigFieldError] = []
        pending_restart: list[str] = []
        patch: dict[str, Any] = request.model_dump()

        with self._lock:
            for key, new_value in patch.items():
                field = _FIELDS_BY_KEY.get(key)
                if field is None:
                    rejected.append(ConfigFieldError(key=key, message="unknown field"))
                    continue
                err = _validate_value(field, new_value)
                if err is not None:
                    rejected.append(ConfigFieldError(key=key, message=err))
                    continue
                if new_value is None and field.default is not None:
                    self._values[key] = field.default
                else:
                    self._values[key] = new_value
                applied.append(key)
                if field.requiresRestart:
                    pending_restart.append(key)
            if applied:
                self._write_locked()
            return ConfigUpdateResult(
                applied=applied,
                rejected=rejected,
                requiresRestart=bool(pending_restart),
                pendingRestart=sorted(pending_restart),
            )

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values.get(key)

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(self._values, f, sort_keys=True, default_flow_style=False)
