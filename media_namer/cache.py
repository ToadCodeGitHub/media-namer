"""Remembers AI descriptions so re-scanning a folder doesn't pay for them twice.

Keyed by file size + modified time (unchanged by renames), plus the backend,
model, and context used, so changing any of those asks again.
"""
from __future__ import annotations

import hashlib
import json
import os


class DescriptionCache:
    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, str] = {}
        self._dirty = False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(size: int, mtime_ns: int, backend: str, model: str, context: str) -> str:
        ctx = hashlib.sha1(context.strip().encode()).hexdigest()[:10]
        return f"{size}|{mtime_ns}|{backend}|{model}|{ctx}"

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        if value and self._data.get(key) != value:
            self._data[key] = value
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)
        self._dirty = False
