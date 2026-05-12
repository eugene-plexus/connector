"""Adapter list persistence + the live registry of running adapters.

`AdaptersStore` is the YAML-backed source of truth for the operator's
configured adapter entries. `AdapterRegistry` holds the actually-
running adapter instances at runtime.

These are split because:
  - the persistence concern (what the operator declared) is separate
    from the runtime concern (which connections are live)
  - safe-mode boots the persistence path but skips the registry
    (no platform connections attempted)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from pathlib import Path

import yaml

from ._generated.common_models import AdapterEntry
from .adapters import (
    Adapter,
    AdapterError,
    AdapterHooks,
    AdapterStatusSnapshot,
    build_adapter,
)

log = logging.getLogger(__name__)


class AdaptersStore:
    """YAML-backed persistence of the adapter entries."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, AdapterEntry] = OrderedDict()

    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._entries = OrderedDict()
                return
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or []
            if not isinstance(raw, list):
                raise ValueError(
                    f"{self._path}: expected YAML list of adapter entries, got {type(raw).__name__}"
                )
            entries: OrderedDict[str, AdapterEntry] = OrderedDict()
            for raw_entry in raw:
                entry = AdapterEntry.model_validate(raw_entry)
                entries[entry.name] = entry
            self._entries = entries

    def list(self) -> list[AdapterEntry]:
        with self._lock:
            return list(self._entries.values())

    def get(self, name: str) -> AdapterEntry | None:
        with self._lock:
            return self._entries.get(name)

    def add(self, entry: AdapterEntry) -> None:
        with self._lock:
            if entry.name in self._entries:
                raise AdapterError(
                    status_code=409,
                    detail=f"adapter named {entry.name!r} already exists",
                )
            self._entries[entry.name] = entry
            self._write_locked()

    def update(self, entry: AdapterEntry) -> None:
        with self._lock:
            if entry.name not in self._entries:
                raise AdapterError(
                    status_code=404,
                    detail=f"no adapter named {entry.name!r}",
                )
            self._entries[entry.name] = entry
            self._write_locked()

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self._entries:
                return False
            del self._entries[name]
            self._write_locked()
            return True

    def _write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [e.model_dump(mode="json", exclude_none=True) for e in self._entries.values()]
        with self._path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(rows, f, sort_keys=False, default_flow_style=False)


class AdapterRegistry:
    """Holds the live `Adapter` instances. Operations are async because
    starting / stopping platform connections is async."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._lock = asyncio.Lock()

    async def start(
        self, *, entry: AdapterEntry, hooks: AdapterHooks
    ) -> Adapter:
        async with self._lock:
            if entry.name in self._adapters:
                # Idempotent: starting an already-running adapter is a
                # no-op. Restart goes through `stop()` then `start()`.
                return self._adapters[entry.name]
            adapter = build_adapter(entry, hooks)
            await adapter.start()
            self._adapters[entry.name] = adapter
            return adapter

    async def stop(self, name: str) -> bool:
        async with self._lock:
            adapter = self._adapters.pop(name, None)
        if adapter is None:
            return False
        try:
            await adapter.stop()
        except Exception:
            log.exception("adapter %r raised during stop", name)
        return True

    async def stop_all(self) -> None:
        async with self._lock:
            names = list(self._adapters.keys())
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for name, adapter in zip(names, adapters, strict=True):
            try:
                await adapter.stop()
            except Exception:
                log.exception("adapter %r raised during stop_all", name)

    def get(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def status(self, name: str) -> AdapterStatusSnapshot | None:
        adapter = self._adapters.get(name)
        if adapter is None:
            return None
        return adapter.status()

    def names(self) -> list[str]:
        return list(self._adapters.keys())
