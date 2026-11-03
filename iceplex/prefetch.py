"""Next-episode preload.

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
    def __init__(self, key: str, url: str, size: Optional[int], bitrate_kbps: Optional[int]):
        self.key, self.url, self.size = key, url, size
        bps = (bitrate_kbps or 8000) * 1000 / 8
        self.head_want = int(min(max(bps * PRELOAD_SECS * 1.1, HEAD_MIN), HEAD_MAX))
        if size:
            self.head_want = min(self.head_want, size)
        self.head = bytearray()
        self.tail = b""
        self.tail_start: Optional[int] = None
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
        return len(self.head)

    # -- download ---------------------------------------------------------------

    def _wait_allowed(self) -> bool:
        while not self.allowed.wait(0.5):
            if self.cancelled.is_set():
                return False
        return not self.cancelled.is_set()

    def _run(self):
        try:
            if not self._wait_allowed():
                return
            with requests.get(self.url, headers={"Range": f"bytes=0-{self.head_want - 1}"},
                              stream=True, timeout=(10, 30)) as r:
                if r.status_code not in (200, 206):
                    return
                m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
                if m:
                    self.size = int(m.group(1))
                elif r.status_code == 200 and r.headers.get("Content-Length"):
                    self.size = int(r.headers["Content-Length"])
                for chunk in r.iter_content(CHUNK):
                    if not self._wait_allowed():
                        return
                    room = self.head_want - len(self.head)
                    self.head += chunk[:room]
                    if len(self.head) >= self.head_want:
                        break
            if self.size and self.size > len(self.head) + TAIL_BYTES and self._wait_allowed():
                start = self.size - TAIL_BYTES
                r = requests.get(self.url, headers={"Range": f"bytes={start}-{self.size - 1}"}, timeout=(10, 30))
                if r.status_code == 206 and len(r.content) == TAIL_BYTES:
                    self.tail, self.tail_start = r.content, start
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
        try:
            self._send(p, a, b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            print("preload serve:", e)

    def _send(self, p: Preload, a: int, b: int):
        head = p.head            # what's downloaded right now
        hl = len(head)
        tail, ts = p.tail, p.tail_start
        pos = a
        upstream = None
        # Open the connection for whatever comes after the preloaded part while that part is sent.
        if pos < hl and b >= hl:
            up_end = (ts - 1) if (ts is not None and b >= ts) else b
            if up_end >= hl:
                upstream = _Opener(p.url, hl, up_end)
        while pos <= b:
            if pos < hl:
                end = min(hl, b + 1)
                for i in range(pos, end, CHUNK):
                    self.wfile.write(head[i:min(i + CHUNK, end)])
                pos = end
            elif ts is not None and pos >= ts:
                self.wfile.write(tail[pos - ts:b + 1 - ts])
                pos = b + 1
            else:
                up_end = (ts - 1) if (ts is not None and b >= ts) else b
                r = upstream.get() if (upstream and upstream.start == pos) else _open(p.url, pos, up_end)
                upstream = None
                with r:
                    for chunk in r.iter_content(CHUNK):
                        self.wfile.write(chunk)
                        pos += len(chunk)
                        if pos > up_end:
                            break
                if pos <= up_end:
                    return   # server cut off; mpv reconnects for the rest
        if upstream:
            upstream.close()


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


def url_for(p: Preload) -> Optional[str]:
    with _lock:
        if _preloads.get(p.token) is not p:
            return None
    return f"http://127.0.0.1:{_ensure_server().server_address[1]}/ice/{p.token}"
