"""Ice's log file, for tracking down playback problems.

Everything goes to %LOCALAPPDATA%\\Ice\\logs\\ice.log (older runs roll over to ice.log.1 … ice.log.4), including
anything the app prints and any crash. Settings → "Open log folder" opens it.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
from pathlib import Path

_log = logging.getLogger("ice")
_ready = False

# The Plex token must never reach the log (the log gets shared when tracking down a problem).
_TOKEN = re.compile(r"""(X-Plex-Token(?:=|%3D|['"]?:\s*['"]?))[^&\s"'<>]+""", re.IGNORECASE)


def scrub(text: str) -> str:
    return _TOKEN.sub(r"\1***", text)


class _Scrubbed(logging.Formatter):
    """Formats a line (crash tracebacks included), then blanks out any Plex token in it."""

    def format(self, record):
        return scrub(super().format(record))


def log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = Path(os.environ.get("ICEPLEX_LOGS") or base / "Ice" / "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    return log_dir() / "ice.log"


class _Lines:
    """Stands in for stdout/stderr (a windowed exe has none), sending each printed line to the log."""

    def __init__(self, level, keep=None):
        self.level, self.keep, self.buf = level, keep, ""

    def write(self, s):
        if self.keep:
            try:
                self.keep.write(s)
            except Exception:  # noqa: BLE001
                pass
        self.buf += str(s)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                _log.log(self.level, line.rstrip())
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup():
    global _ready
    if _ready:
        return
    _ready = True
    _log.setLevel(logging.DEBUG)
    try:
        h = logging.handlers.RotatingFileHandler(log_file(), maxBytes=3_000_000, backupCount=4, encoding="utf-8")
        h.setFormatter(_Scrubbed("%(asctime)s.%(msecs)03d  %(message)s", "%Y-%m-%d %H:%M:%S"))
        _log.addHandler(h)
    except Exception:  # noqa: BLE001  (no log is better than no app)
        return
    sys.stdout = _Lines(logging.INFO, sys.stdout)
    sys.stderr = _Lines(logging.WARNING, sys.stderr)

    def crash(t, v, tb):
        _log.error("CRASH", exc_info=(t, v, tb))
    sys.excepthook = crash
    threading.excepthook = lambda a: _log.error(f"CRASH in thread {a.thread.name if a.thread else '?'}",
                                                exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    try:
        from .version import BUILD
    except Exception:  # noqa: BLE001
        BUILD = "dev"
    _log.info("=" * 70)
    _log.info(f"Ice build {BUILD} starting  ·  {platform.platform()}  ·  Python {platform.python_version()}")


def info(msg: str):
    _log.info(msg)


def warn(msg: str):
    _log.warning("WARNING " + msg)
