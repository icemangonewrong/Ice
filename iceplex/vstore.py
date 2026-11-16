"""Downloaded video kept on disk until it's watched.

Everything the player downloads passes through the local relay (prefetch.py), which also saves it here in
4 MB pieces, one folder per video. Close the player and open it again (even after restarting Ice) and the
part already downloaded comes straight off the disk: no waiting and no data used twice. A video's pieces
are deleted once it's watched, and when the total goes over the limit in Settings the videos played
longest ago are deleted first.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from .config import cache_dir

PIECE = 4 * 1024 * 1024

_lock = threading.Lock()
_limit_gb = 10.0
_total: Optional[int] = None      # bytes on disk (counted on first use)
_playing: set[str] = set()        # never deleted while open
_since_check = 0


def root() -> Path:
    d = cache_dir() / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_limit(gb: float):
    global _limit_gb
    _limit_gb = float(gb)
    threading.Thread(target=trim, daemon=True).start()


def enabled() -> bool:
    return _limit_gb > 0


def _name(key: str, size: int) -> str:
    return f"{key}-{size}"     # a new file for the same title (upgraded copy) gets its own folder


def _count() -> int:
    global _total
    if _total is None:
        n = 0
        for f in root().glob("*/*.p"):
            try:
                n += f.stat().st_size
            except OSError:
                pass
        _total = n
    return _total


class Store:
    """One video's saved pieces."""

    def __init__(self, key: str, size: int):
        self.key, self.size = key, size
        self.dir = root() / _name(key, size)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.utime(self.dir)            # "played recently": deleted last
        except OSError:
            pass
        self.have = {int(f.stem) for f in self.dir.glob("*.p")}

    def _piece_len(self, i: int) -> int:
        return min(PIECE, self.size - i * PIECE)

    def has(self, pos: int) -> bool:
        return pos // PIECE in self.have

    def next_saved(self, pos: int) -> Optional[int]:
        """Start byte of the next saved piece after pos."""
        later = [i for i in self.have if i * PIECE > pos]
        return min(later) * PIECE if later else None

    def read(self, pos: int, end: int) -> Optional[bytes]:
        """Bytes pos..end-1 (within one piece), or None if that piece isn't saved (or was deleted)."""
        i = pos // PIECE
        try:
            with open(self.dir / f"{i}.p", "rb") as f:
                f.seek(pos - i * PIECE)
                return f.read(min(end, (i + 1) * PIECE) - pos)
        except OSError:
            self.have.discard(i)
            return None

    def writer(self) -> "Writer":
        return Writer(self)

    def _save(self, i: int, data: bytes):
        global _total, _since_check
        if i in self.have or not enabled():
            return
        tmp = self.dir / f"{i}.tmp"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.dir / f"{i}.p")
        except OSError as e:
            print(f"video store: couldn't save ({e})")
            return
        self.have.add(i)
        with _lock:
            _total = _count() + len(data)
            _since_check += 1
            check = _since_check >= 16 or _total > _limit_gb * 1e9
            if check:
                _since_check = 0
        if check:
            threading.Thread(target=trim, daemon=True).start()


class Writer:
    """Collects bytes streamed from the server into whole pieces and saves each one when it's complete."""

    def __init__(self, store: Store):
        self.s = store
        self.start = -1
        self.buf = bytearray()

    def feed(self, pos: int, data: bytes):
        if not enabled():
            return
        while data:
            if self.start >= 0 and pos == self.start + len(self.buf):
                pass
            elif pos % PIECE == 0:
                self.start, self.buf = pos, bytearray()
            else:
                # Not at a piece boundary: skip to the next one (the part before it isn't kept).
                skip = PIECE - pos % PIECE
                if skip >= len(data):
                    self.start = -1
                    return
                pos, data = pos + skip, data[skip:]
                self.start, self.buf = pos, bytearray()
            i = self.start // PIECE
            want = self.s._piece_len(i) - len(self.buf)
            take, data = data[:want], data[want:]
            self.buf += take
            pos += len(take)
            if len(self.buf) == self.s._piece_len(i):
                self.s._save(i, bytes(self.buf))
                self.start, self.buf = -1, bytearray()


_stores: dict[str, Store] = {}


def get(key: str, size: int) -> Store:
    with _lock:
        s = _stores.get(_name(key, size))
        if s is None or not s.dir.exists():
            s = _stores[_name(key, size)] = Store(key, size)
    return s


def opened(key: str):
    _playing.clear()      # one player: the video it now shows is the only one in use
    _playing.add(key)


def closed(key: str):
    _playing.discard(key)


def drop(key: str):
    """Watched: delete what was saved for it."""
    global _total
    for d in root().glob(f"{key}-*"):
        shutil.rmtree(d, ignore_errors=True)
        print(f"video store: watched, deleted the saved copy of {key}")
    with _lock:
        for n in [n for n in _stores if n.startswith(f"{key}-")]:
            del _stores[n]
        _total = None


def trim():
    """Over the limit: delete the videos played longest ago until it fits (never the one playing)."""
    global _total
    limit = _limit_gb * 1e9
    with _lock:
        _total = None
        total = _count()
    if total <= limit:
        return
    dirs = sorted((d for d in root().iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs:
        if total <= limit * 0.95:
            break
        key = d.name.rsplit("-", 1)[0]
        if key in _playing:
            continue
        n = sum(f.stat().st_size for f in d.glob("*.p") if f.exists())
        shutil.rmtree(d, ignore_errors=True)
        total -= n
        print(f"video store: over {_limit_gb:g} GB, deleted {key} ({n / 1e9:.1f} GB, played longest ago)")
        with _lock:
            _stores.pop(d.name, None)
    with _lock:
        _total = None


def usage_gb() -> float:
    with _lock:
        return _count() / 1e9


def clear():
    global _total
    shutil.rmtree(root(), ignore_errors=True)
    with _lock:
        _stores.clear()
        _total = 0
    time.sleep(0)
