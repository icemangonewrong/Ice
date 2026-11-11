"""Preloading (the next episode near the end of one, and whatever info page is open).

Near the end of an episode, the first ~20 seconds of the next one (plus the last few MB of the file,
where some formats keep their index) are downloaded into memory. When the next episode starts, mpv
reads it through a tiny local server on 127.0.0.1: the preloaded part comes straight from memory, so
playback starts at once, and everything after it is passed through from the Plex server as usual.
"""
from __future__ import annotations

import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import requests

PRELOAD_SECS = 20          # how much of the next episode to fetch
START_WHEN_LEFT = 300      # start preloading with this many seconds left (5 minutes)
MIN_CUSHION = 60           # ...but only while the current episode has this much buffered ahead
HEAD_MIN = 4_000_000
HEAD_MAX = 80_000_000      # cap (very high bitrate files get fewer seconds)
TAIL_BYTES = 4_000_000
CHUNK = 256 * 1024


class Preload:
    """Pieces of one video file held in memory: (start byte, data) ranges, downloaded in order."""

    def __init__(self, key: str, url: str, size: Optional[int], bitrate_kbps: Optional[int],
                 seconds: float = PRELOAD_SECS, at_seconds: float = 0.0, duration: float = 0.0):
        self.key, self.url, self.size = key, url, size
        bps = (bitrate_kbps or 8000) * 1000 / 8
        want = int(min(max(bps * seconds * 1.1, HEAD_MIN), HEAD_MAX))
        plan: list[tuple[int, int]] = []
        if at_seconds > 1 and size and duration > 0:
            # Resuming partway: the file's start (headers), then a window around the resume point
            # (its byte position is estimated from the average bitrate, so leave some room before it).
            plan.append((0, 2_000_000))
            est = int(size * at_seconds / duration)
            before = int(bps * 10)
            plan.append((max(est - before, 0), want + before))
        else:
            plan.append((0, want))
        if size:
            plan.append((size - TAIL_BYTES, TAIL_BYTES))
            plan = [(a, min(n, size - a)) for a, n in plan if a < size]
        self.plan = plan
        self.parts: list[list] = []          # [start, bytearray], in plan order
        self.head_want = plan[0][1]
        self.allowed = threading.Event()     # cleared = pause (current episode needs the bandwidth)
        self.cancelled = threading.Event()
        self.done = False
        self.token = secrets.token_hex(8)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self.cancelled.set()
        self.allowed.set()

    def head_len(self) -> int:
        return len(self.parts[0][1]) if self.parts else 0

    def part_at(self, pos: int):
        """(start, data) of the downloaded piece containing pos, or None."""
        for st, buf in list(self.parts):
            if st <= pos < st + len(buf):
                return st, buf
        return None

    def next_start(self, pos: int) -> Optional[int]:
        """Where the next downloaded piece after pos begins."""
        later = [st for st, buf in list(self.parts) if st > pos and len(buf)]
        return min(later) if later else None

    # -- download ---------------------------------------------------------------

    def _wait_allowed(self) -> bool:
        while not self.allowed.wait(0.5):
            if self.cancelled.is_set():
                return False
        return not self.cancelled.is_set()

    def _run(self):
        try:
            for i, (a, n) in enumerate(self.plan):
                if not self._wait_allowed():
                    return
                if self.size and i > 0 and any(st <= a < st + len(b) for st, b in self.parts):
                    continue
                buf = bytearray()
                self.parts.append([a, buf])
                with requests.get(self.url, headers={"Range": f"bytes={a}-{a + n - 1}"},
                                  stream=True, timeout=(10, 30)) as r:
                    if r.status_code not in (200, 206):
                        return
                    if r.status_code == 200 and a > 0:
                        return
                    m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
                    if m:
                        self.size = int(m.group(1))
                    elif r.status_code == 200 and r.headers.get("Content-Length"):
                        self.size = int(r.headers["Content-Length"])
                    for chunk in r.iter_content(CHUNK):
                        if not self._wait_allowed():
                            return
                        buf += chunk[:n - len(buf)]
                        if len(buf) >= n:
                            break
            self.done = True
        except Exception as e:  # noqa: BLE001  (a failed preload just means a normal start)
            print("preload:", e)


# ---------------------------------------------------------------------------
# Local server mpv reads from
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_preloads: dict[str, Preload] = {}
_server: Optional[ThreadingHTTPServer] = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        token = self.path.strip("/").split("/")[-1]
        with _lock:
            p = _preloads.get(token)
        if not p or not p.size:
            self.send_error(404)
            return
        size = p.size
        rng = self.headers.get("Range")
        a, b = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    a = int(m.group(1))
                    if m.group(2):
                        b = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    a = max(0, size - int(m.group(2)))
        if a >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.end_headers()
        if head_only:
            return
        sent_all = False
        try:
            sent_all = self._send(p, a, b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            print(f"preload serve: lost the server at byte {a}: {e}")
        if not sent_all:
            # Hang up so the player notices at once and reconnects, instead of waiting on this connection for
            # bytes that will never come.
            self.close_connection = True

    def _send(self, p: Preload, a: int, b: int):
        self._up = None
        try:
            return self._send_range(p, a, b)
        finally:
            if self._up:
                self._up.close()   # mpv hung up before using it

    def _send_range(self, p: Preload, a: int, b: int):
        pos = a
        upstream = None
        while pos <= b:
            part = p.part_at(pos)
            if part:
                st, buf = part
                end = min(st + len(buf), b + 1)
                if end <= b and upstream is None and p.part_at(end) is None:
                    # Open the connection for what follows while this piece is sent.
                    nxt = p.next_start(end)
                    upstream = self._up = _Opener(p.url, end, min(b, nxt - 1) if nxt else b)
                for i in range(pos, end, CHUNK):
                    self.wfile.write(buf[i - st:min(i + CHUNK, end) - st])
                pos = end
                continue
            nxt = p.next_start(pos)
            up_end = min(b, nxt - 1) if nxt else b
            if upstream and upstream.start == pos:
                r = upstream.get()
            else:
                if upstream:
                    upstream.close()
                r = _open(p.url, pos, up_end)
            upstream = self._up = None
            with r:
                for chunk in r.iter_content(CHUNK):
                    take = chunk[:up_end + 1 - pos]
                    self.wfile.write(take)
                    pos += len(take)
                    if pos > up_end:
                        break
            if pos <= up_end:
                print(f"preload serve: server cut off at byte {pos}")
                return False   # mpv reconnects for the rest
        return True


def _open(url, start, end):
    r = requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=(10, 30))
    if r.status_code not in (200, 206):
        r.close()
        raise IOError(f"server returned {r.status_code}")
    return r


class _Opener:
    """Opens an upstream connection in the background."""

    def __init__(self, url, start, end):
        self.start = start
        self._r = None
        self._err = None
        self._t = threading.Thread(target=self._go, args=(url, start, end), daemon=True)
        self._t.start()

    def _go(self, url, start, end):
        try:
            self._r = _open(url, start, end)
        except Exception as e:  # noqa: BLE001
            self._err = e

    def get(self):
        self._t.join()
        if self._err:
            raise self._err
        return self._r

    def close(self):
        def _c():
            self._t.join()
            if self._r is not None:
                self._r.close()
        threading.Thread(target=_c, daemon=True).start()


def _ensure_server() -> ThreadingHTTPServer:
    global _server
    if _server is None:
        ThreadingHTTPServer.daemon_threads = True
        _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return _server


def register(p: Preload) -> str:
    srv = _ensure_server()
    with _lock:
        _preloads[p.token] = p
    return f"http://127.0.0.1:{srv.server_address[1]}/ice/{p.token}"


def keep_only(keys: set[str]):
    """Forget preloads for anything except these items."""
    with _lock:
        for t, p in list(_preloads.items()):
            if p.key not in keys:
                p.cancel()
                del _preloads[t]


def find(key: str) -> Optional[Preload]:
    """The most recent preload for this video, if any."""
    with _lock:
        found = [p for p in _preloads.values() if p.key == key and not p.cancelled.is_set()]
    return found[-1] if found else None


def url_for(p: Preload) -> Optional[str]:
    with _lock:
        if _preloads.get(p.token) is not p:
            return None
    return f"http://127.0.0.1:{_ensure_server().server_address[1]}/ice/{p.token}"
