"""IMDb ratings for everything, from IMDb's own free daily ratings file.

Plex only knows the IMDb score when a library's ratings source is set to IMDb, never for seasons, and often
leaves it out of lists. IMDb publishes every title's rating (movies, shows, episodes) as one file updated daily
(https://datasets.imdbws.com/title.ratings.tsv.gz, free for personal use). Ice downloads it about once a day and
looks titles up by IMDb ID (tt1234567), which Plex and Seerr both give.

Kept compact: two sorted arrays (ids, ratings*10), about 8 MB in memory, saved to disk so later launches load
it in a fraction of a second.
"""
from __future__ import annotations

import array
import bisect
import gzip
import io
import os
import re
import threading
import time
from typing import Callable, Optional

import requests

from .config import cache_dir

URL = os.environ.get("ICE_IMDB_URL") or "https://datasets.imdbws.com/title.ratings.tsv.gz"
MAX_AGE = 24 * 3600          # refresh about once a day
RETRY_AFTER = 3600           # after a failed download

_ids = array.array("I")
_vals = array.array("B")
_lock = threading.Lock()
_started = False
_listeners: list[Callable[[], None]] = []


def _paths():
    d = cache_dir() / "imdb"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ids.bin", d / "ratings.bin", d / "stamp"


def _id_num(imdb_id) -> Optional[int]:
    m = re.match(r"tt0*(\d+)$", str(imdb_id or "").strip())
    return int(m.group(1)) if m else None


def rating(imdb_id) -> Optional[float]:
    """IMDb score for tt1234567, or None (not loaded yet, or no rating)."""
    n = _id_num(imdb_id)
    if n is None:
        return None
    ids, vals = _ids, _vals
    i = bisect.bisect_left(ids, n)
    if i < len(ids) and ids[i] == n:
        return vals[i] / 10
    return None


def loaded() -> bool:
    return len(_ids) > 0


def on_ready(fn: Callable[[], None]):
    """fn() is called (from a worker thread) whenever fresh ratings have been loaded."""
    _listeners.append(fn)


def _notify():
    for fn in list(_listeners):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print("imdb listener:", e)


def _load_saved() -> bool:
    global _ids, _vals
    ip, vp, _ = _paths()
    try:
        ids, vals = array.array("I"), array.array("B")
        with open(ip, "rb") as f:
            ids.frombytes(f.read())
        with open(vp, "rb") as f:
            vals.frombytes(f.read())
        if len(ids) != len(vals) or not ids:
            return False
        with _lock:
            _ids, _vals = ids, vals
        return True
    except OSError:
        return False


def _download():
    global _ids, _vals
    ip, vp, sp = _paths()
    r = requests.get(URL, timeout=(15, 120), headers={"User-Agent": "Ice"})
    r.raise_for_status()
    ids, vals = array.array("I"), array.array("B")
    in_order, last = True, -1
    with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8", newline="") as f:
        next(f, None)   # header: tconst  averageRating  numVotes
        for line in f:
            p = line.split("\t")
            if len(p) < 2 or not p[0].startswith("tt"):
                continue
            try:
                n = int(p[0][2:])
                v = int(round(float(p[1]) * 10))
            except ValueError:
                continue
            if n <= last:
                in_order = False
            last = n
            ids.append(n)
            vals.append(max(0, min(v, 100)))
    if not ids:
        raise IOError("empty ratings file")
    if not in_order:   # IMDb's file is sorted already; just in case it ever isn't
        order = sorted(range(len(ids)), key=ids.__getitem__)
        ids = array.array("I", (ids[i] for i in order))
        vals = array.array("B", (vals[i] for i in order))
    for path, arr in ((ip, ids), (vp, vals)):
        tmp = str(path) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(arr.tobytes())
        os.replace(tmp, path)
    sp.write_text(str(time.time()))
    with _lock:
        _ids, _vals = ids, vals


def _age() -> float:
    try:
        return time.time() - float(_paths()[2].read_text().strip())
    except (OSError, ValueError):
        return float("inf")


def _worker():
    if _load_saved():
        _notify()
    while True:
        age = _age()
        if age >= MAX_AGE or not loaded():
            try:
                _download()
                _notify()
                wait = MAX_AGE
            except Exception as e:  # noqa: BLE001
                print("imdb ratings download:", e)
                wait = RETRY_AFTER
        else:
            wait = MAX_AGE - age
        time.sleep(max(60.0, wait))


def start():
    """Load the saved ratings and keep them fresh in the background."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True, name="imdb-ratings").start()
