"""Settings saved between launches (in %APPDATA%\\IcePlex on Windows)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .plex import new_client_id

BUFFER_CHOICES = [2, 5, 10, 20, 30]


def app_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = Path(os.environ.get("ICEPLEX_HOME") or base / "IcePlex")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = Path(os.environ.get("ICEPLEX_CACHE") or base / "IcePlex")
    d.mkdir(parents=True, exist_ok=True)
    return d


class Settings:
    _defaults = {
        "clientIdentifier": None,
        "token": None,
        "server": None,
        "excludedSections": [],
        "bufferMinutes": 10,
        "volume": 100,
        "muted": False,
        "geometry": None,
        "maximized": False,
    }

    def __init__(self):
        self.path = app_dir() / "settings.json"
        self.data = dict(self._defaults)
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            pass
        if not self.data.get("clientIdentifier"):
            self.data["clientIdentifier"] = new_client_id()
            self.save()
        if self.data.get("bufferMinutes") not in BUFFER_CHOICES:
            self.data["bufferMinutes"] = 10

    def __getitem__(self, k):
        return self.data.get(k)

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass
