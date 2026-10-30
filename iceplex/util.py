"""Background tasks, image loading and small formatting helpers."""
from __future__ import annotations

import traceback
from collections import OrderedDict
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Qt
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkRequest, QNetworkReply

from .config import cache_dir


# ---------------------------------------------------------------------------
# Background work: run a function off the UI thread, get the result back on it.
# ---------------------------------------------------------------------------

class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn, bridge):
        super().__init__()
        self.fn = fn
        self.bridge = bridge

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.bridge.failed.emit(e)
        else:
            self.bridge.done.emit(result)


_pool = QThreadPool()
_pool.setMaxThreadCount(8)
_live: set = set()


def run_bg(fn: Callable, on_done: Optional[Callable] = None, on_error: Optional[Callable] = None):
    """Runs fn() on a worker thread. on_done(result) / on_error(exc) run on the UI thread."""
    bridge = _Bridge()
    _live.add(bridge)

    def finish(cb, value):
        _live.discard(bridge)
        bridge.deleteLater()
        if cb:
            try:
                cb(value)
            except RuntimeError:
                pass  # the page that asked was closed in the meantime

    bridge.done.connect(lambda r: finish(on_done, r), Qt.QueuedConnection)
    bridge.failed.connect(lambda e: finish(on_error, e), Qt.QueuedConnection)
    _pool.start(_Task(fn, bridge))


# ---------------------------------------------------------------------------
# Images (posters, backdrops) with memory + disk cache
# ---------------------------------------------------------------------------

class ImageLoader(QObject):
    def __init__(self):
        super().__init__()
        self.nam = QNetworkAccessManager(self)
        disk = QNetworkDiskCache(self)
        disk.setCacheDirectory(str(cache_dir() / "images"))
        disk.setMaximumCacheSize(500 * 1024 * 1024)
        self.nam.setCache(disk)
        self.mem: OrderedDict[str, QPixmap] = OrderedDict()
        self.waiting: dict[str, list[Callable]] = {}

    def load(self, url: Optional[str], callback: Callable[[QPixmap], None]):
        if not url:
            return
        if url in self.mem:
            self.mem.move_to_end(url)
            callback(self.mem[url])
            return
        if url in self.waiting:
            self.waiting[url].append(callback)
            return
        self.waiting[url] = [callback]
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferCache)
        reply = self.nam.get(req)
        reply.finished.connect(lambda r=reply, u=url: self._finished(r, u))

    def _finished(self, reply: QNetworkReply, url: str):
        callbacks = self.waiting.pop(url, [])
        pix = None
        if reply.error() == QNetworkReply.NoError:
            img = QImage()
            if img.loadFromData(reply.readAll()):
                pix = QPixmap.fromImage(img)
        reply.deleteLater()
        if pix is None:
            return
        self.mem[url] = pix
        while len(self.mem) > 600:
            self.mem.popitem(last=False)
        for cb in callbacks:
            try:
                cb(pix)
            except RuntimeError:
                pass  # widget already gone


images: Optional[ImageLoader] = None


def image_loader() -> ImageLoader:
    global images
    if images is None:
        images = ImageLoader()
    return images


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def time_str(seconds: float) -> str:
    if not seconds or seconds != seconds or seconds < 0:
        return "0:00"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def runtime_str(ms: int, round_up: bool = False) -> str:
    mins = max((int(ms) + 59999) // 60000 if round_up else int(ms) // 60000, 0)
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


def size_str(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""
