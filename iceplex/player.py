"""Full-window video player built on mpv (true direct play of every format, MKV included).

Video is drawn by mpv into an OpenGL widget, so the controls can float on top of it.
mpv keeps minutes of video cached on disk ahead of where you're watching, and reconnects
on its own if the connection drops, so a flaky hotspot doesn't stop playback.
"""
from __future__ import annotations

import ctypes
import locale
import os
import shutil
import time
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QFont, QIcon, QLinearGradient, QOpenGLContext, QPainter, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QSlider, QToolButton, QVBoxLayout,
                               QWidget, QPushButton)

from . import theme
from .config import cache_dir
from .plex import Item
from .util import run_bg, time_str
from .widgets import Spinner, label

import mpv  # noqa: E402  (needs libmpv on PATH; main.py takes care of that)

END_EOF = 0
END_ERROR = 4


# ---------------------------------------------------------------------------
# mpv drawing into an OpenGL widget
# ---------------------------------------------------------------------------

class MpvWidget(QOpenGLWidget):
    frame_ready = Signal()
    mpv_event = Signal(int, int)        # event id, end-file reason

    def __init__(self, parent=None):
        super().__init__(parent)
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.mpv = mpv.MPV(vo="libmpv", keep_open="yes", idle="yes", osc="no", osd_level="0",
                           input_default_bindings="no", input_vo_keyboard="no", ytdl="no",
                           terminal="no", audio_client_name="IcePlex")
        opts = {
            "hwdec": "auto-safe",
            "cache": "yes",
            "cache-on-disk": "yes",
            "demuxer-cache-dir": str(cache_dir() / "stream"),
            "demuxer-max-back-bytes": "300MiB",
            "cache-pause-initial": "no",
            "cache-pause-wait": "2",
            "network-timeout": "20",
            "stream-lavf-o": "reconnect=1,reconnect_streamed=1,reconnect_on_network_error=1,reconnect_delay_max=30",
            "user-agent": "IcePlex",
            "sub-auto": "fuzzy",
            "audio-file-auto": "no",
            "video-sync": "audio",
        }
        os.makedirs(opts["demuxer-cache-dir"], exist_ok=True)
        for k, v in opts.items():
            try:
                self.mpv[k] = v
            except Exception as e:  # noqa: BLE001  (older mpv may lack an option; skip it)
                print(f"mpv option {k} not set: {e}")
        self.ctx = None
        self._proc = mpv.MpvGlGetProcAddressFn(self._get_proc)
        self.frame_ready.connect(self.update, Qt.QueuedConnection)
        self.mpv.register_event_callback(self._on_event)
        self.setMouseTracking(True)

    @staticmethod
    def _get_proc(_ctx, name):
        glctx = QOpenGLContext.currentContext()
        if glctx is None:
            return 0
        addr = glctx.getProcAddress(name)
        return int(addr) if addr else 0

    def _on_event(self, event):
        eid = event.event_id.value
        if eid == mpv.MpvEventID.END_FILE:
            self.mpv_event.emit(eid, int(event.data.reason))
        elif eid in (mpv.MpvEventID.FILE_LOADED, mpv.MpvEventID.PLAYBACK_RESTART):
            self.mpv_event.emit(eid, -1)

    def initializeGL(self):
        # Software OpenGL (no proper GPU driver, remote desktop, VMs) draws black with mpv's default
        # high-precision buffers; plain 8-bit buffers work everywhere.
        try:
            renderer = (QOpenGLContext.currentContext().functions().glGetString(0x1F01) or "").lower()
        except Exception:
            renderer = ""
        self.renderer = renderer
        if any(k in renderer for k in ("llvmpipe", "softpipe", "swrast", "basic render", "gdi generic")):
            try:
                self.mpv["fbo-format"] = "rgba8"
            except Exception:
                pass
        self.ctx = mpv.MpvRenderContext(self.mpv, "opengl", opengl_init_params={"get_proc_address": self._proc})
        self.ctx.update_cb = self.frame_ready.emit

    def paintGL(self):
        if not self.ctx:
            return
        r = self.devicePixelRatioF()
        self.ctx.render(flip_y=True, opengl_fbo={"w": int(self.width() * r), "h": int(self.height() * r),
                                                 "fbo": self.defaultFramebufferObject()})

    def shutdown(self):
        try:
            self.makeCurrent()
            if self.ctx:
                self.ctx.free()
                self.ctx = None
            self.doneCurrent()
        finally:
            self.mpv.terminate()


# ---------------------------------------------------------------------------
# Scrub bar: played (orange), loaded ahead (light), rest (dim)
# ---------------------------------------------------------------------------

class ScrubBar(QWidget):
    seek_requested = Signal(float)
    dragging_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.current = 0.0
        self.buffered = 0.0
        self.duration = 0.0
        self.drag: Optional[float] = None
        self.hover: Optional[float] = None
        self.tip = QLabel(parent)
        self.tip.setStyleSheet("background: rgba(20,20,26,0.92); border-radius: 6px; padding: 3px 8px;"
                               "font-weight: 600;")
        self.tip.hide()

    def set_state(self, current, buffered, duration):
        self.current, self.buffered, self.duration = current, buffered, duration
        self.update()

    def _time_at(self, x):
        w = max(self.width() - 16, 1)
        return max(0.0, min((x - 8) / w, 1.0)) * self.duration

    def _x_for(self, t):
        if self.duration <= 0:
            return 8
        return 8 + (self.width() - 16) * max(0.0, min(t / self.duration, 1.0))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.duration > 0:
            self.drag = self._time_at(e.position().x())
            self.dragging_changed.emit(True)
            self.update()

    def mouseMoveEvent(self, e):
        t = self._time_at(e.position().x())
        if self.drag is not None:
            self.drag = t
        self.hover = t
        self._show_tip(e.position().x(), t)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.drag is not None:
            t = self.drag
            self.drag = None
            self.dragging_changed.emit(False)
            self.seek_requested.emit(t)
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.tip.hide()
        self.update()

    def _show_tip(self, x, t):
        if self.duration <= 0:
            return
        self.tip.setText(time_str(t))
        self.tip.adjustSize()
        gp = self.mapTo(self.tip.parentWidget(), QPointF(x, 0).toPoint())
        self.tip.move(int(gp.x() - self.tip.width() / 2), gp.y() - self.tip.height() - 6)
        self.tip.show()
        self.tip.raise_()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        active = self.drag is not None or self.hover is not None
        th = 6 if active else 4
        y = self.height() / 2 - th / 2
        left, right = 8, self.width() - 8
        shown = self.drag if self.drag is not None else self.current
        px = self._x_for(shown)
        bx = max(self._x_for(self.current + self.buffered), px)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 55))
        p.drawRoundedRect(QRectF(left, y, right - left, th), th / 2, th / 2)
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawRoundedRect(QRectF(left, y, bx - left, th), th / 2, th / 2)
        p.setBrush(QColor(theme.ORANGE))
        p.drawRoundedRect(QRectF(left, y, px - left, th), th / 2, th / 2)
        r = 8 if active else 6
        p.drawEllipse(QPointF(px, self.height() / 2), r, r)


# ---------------------------------------------------------------------------
# Gradient strips behind the controls
# ---------------------------------------------------------------------------

class Shade(QWidget):
    def __init__(self, top: bool, parent=None):
        super().__init__(parent)
        self.top = top
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        dark, clear = QColor(0, 0, 0, 190), QColor(0, 0, 0, 0)
        g.setColorAt(0, dark if self.top else clear)
        g.setColorAt(1, clear if self.top else dark)
        p.fillRect(self.rect(), g)


def tool(icon_name: str, size: int = 26, tip: str = "") -> QToolButton:
    b = QToolButton()
    b.setIcon(theme.icon(icon_name, "white", size))
    b.setIconSize(QSize(size, size))
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if tip:
        b.setToolTip(tip)
    return b


class Pill(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: rgba(10,10,14,0.80); border-radius: 14px; padding: 7px 16px;"
                           "font-weight: 600; font-size: 10.5pt;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()


# ---------------------------------------------------------------------------
# Player page
# ---------------------------------------------------------------------------

class PlayerPage(QWidget):
    closed = Signal()
    toggle_fullscreen = Signal()

    HIDE_AFTER_MS = 3000

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setObjectName("Player")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#Player { background: black; }")

        self.video = MpvWidget(self)
        self.video.mpv_event.connect(self._mpv_event)
        m = self.video.mpv
        m.volume = int(ctx.settings["volume"] or 100)
        m.mute = bool(ctx.settings["muted"])

        # --- overlay pieces -------------------------------------------------
        self.top_shade = Shade(True, self)
        self.bottom_shade = Shade(False, self)

        self.top = QWidget(self)
        th = QHBoxLayout(self.top)
        th.setContentsMargins(18, 14, 18, 0)
        th.setSpacing(10)
        self.close_btn = tool("back", 28, "Close player (Esc)")
        self.close_btn.clicked.connect(self.stop)
        th.addWidget(self.close_btn)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.title = label("", bold=True)
        self.title.setFont(theme.font(14, QFont.Bold))
        self.subtitle = label("", "Muted")
        tcol.addWidget(self.title)
        tcol.addWidget(self.subtitle)
        th.addLayout(tcol)
        th.addStretch()
        self.tracks_btn = tool("tracks", 28, "Audio & subtitles")
        self.tracks_btn.clicked.connect(self._show_tracks)
        th.addWidget(self.tracks_btn)
        self.fs_btn = tool("fullscreen", 26, "Full screen (F)")
        self.fs_btn.clicked.connect(self.toggle_fullscreen.emit)
        th.addWidget(self.fs_btn)

        self.center = QWidget(self)
        ch = QHBoxLayout(self.center)
        ch.setSpacing(54)
        ch.setContentsMargins(0, 0, 0, 0)
        self.back_btn = tool("back10", 46, "Back 10 seconds (←)")
        self.play_btn = tool("pause", 62, "Play / pause (Space)")
        self.fwd_btn = tool("fwd10", 46, "Forward 10 seconds (→)")
        for b in (self.back_btn, self.play_btn, self.fwd_btn):
            b.setStyleSheet("QToolButton { border-radius: 40px; padding: 10px; }"
                            "QToolButton:hover { background: rgba(255,255,255,0.12); }")
            ch.addWidget(b)
        self.back_btn.clicked.connect(lambda: self.skip(-10))
        self.fwd_btn.clicked.connect(lambda: self.skip(10))
        self.play_btn.clicked.connect(self.toggle_pause)

        self.bottom = QWidget(self)
        bv = QVBoxLayout(self.bottom)
        bv.setContentsMargins(22, 0, 22, 14)
        bv.setSpacing(2)
        self.scrub = ScrubBar(self)
        self.scrub.seek_requested.connect(self.seek)
        self.scrub.dragging_changed.connect(self._dragging)
        bv.addWidget(self.scrub)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.time_lbl = label("0:00")
        self.time_lbl.setFont(theme.font(10.5, QFont.DemiBold))
        row.addWidget(self.time_lbl)
        row.addStretch()
        self.loaded_lbl = label("", "Muted")
        row.addWidget(self.loaded_lbl)
        row.addStretch()
        self.mute_btn = tool("volume", 22, "Mute (M)")
        self.mute_btn.clicked.connect(self.toggle_mute)
        row.addWidget(self.mute_btn)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(m.volume))
        self.vol.setFixedWidth(110)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setStyleSheet(
            "QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.25); border-radius: 2px; }"
            f"QSlider::sub-page:horizontal {{ background: {theme.ORANGE}; border-radius: 2px; }}"
            "QSlider::handle:horizontal { background: white; width: 12px; height: 12px; margin: -4px 0;"
            " border-radius: 6px; }")
        self.vol.valueChanged.connect(self._volume_changed)
        row.addWidget(self.vol)
        row.addSpacing(10)
        self.remain_lbl = label("-0:00")
        self.remain_lbl.setFont(theme.font(10.5, QFont.DemiBold))
        row.addWidget(self.remain_lbl)
        bv.addLayout(row)

        self.spinner = Spinner(64, self)
        self.status = Pill(self)
        self.signal_pill = Pill(self)

        self.error_box = QWidget(self)
        ev = QVBoxLayout(self.error_box)
        self.error_lbl = label("", wrap=True)
        self.error_lbl.setAlignment(Qt.AlignCenter)
        self.error_lbl.setFont(theme.font(12, QFont.DemiBold))
        ev.addWidget(self.error_lbl)
        eb = QPushButton("Close")
        eb.setObjectName("Primary")
        eb.clicked.connect(self.stop)
        ev.addWidget(eb, 0, Qt.AlignCenter)
        self.error_box.setStyleSheet("QWidget { background: transparent; }")
        self.error_box.hide()

        self.controls = [self.top_shade, self.bottom_shade, self.top, self.center, self.bottom]

        # --- timers ---------------------------------------------------------
        self.poll = QTimer(self)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._tick)
        self.report_timer = QTimer(self)
        self.report_timer.setInterval(10000)
        self.report_timer.timeout.connect(self._report)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._auto_hide)
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.setInterval(QApplication.doubleClickInterval())
        self.click_timer.timeout.connect(self.toggle_pause)
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry)

        # --- state ----------------------------------------------------------
        self.item: Optional[Item] = None
        self.active = False
        self.cur = 0.0
        self.dur = 0.0
        self.buffered = 0.0
        self.readahead = 600.0
        self.dragging = False
        self.controls_shown = True
        self.has_played = False
        self.failures = 0
        self.last_data = time.monotonic()
        self.offline = False
        self.reloading = False
        self.finishing = False
        self.load_started = 0.0

        for w in self.findChildren(QWidget):
            w.setMouseTracking(True)
        self.video.installEventFilter(self)

    # ------------------------------------------------------------------ layout

    def resizeEvent(self, e):
        w, h = self.width(), self.height()
        self.video.setGeometry(0, 0, w, h)
        self.top_shade.setGeometry(0, 0, w, 150)
        self.bottom_shade.setGeometry(0, h - 190, w, 190)
        self.top.setGeometry(0, 0, w, 90)
        self.center.adjustSize()
        cs = self.center.sizeHint()
        self.center.setGeometry((w - cs.width()) // 2, (h - cs.height()) // 2, cs.width(), cs.height())
        self.bottom.setGeometry(0, h - 84, w, 84)
        self.spinner.move((w - self.spinner.width()) // 2, (h - self.spinner.height()) // 2)
        self._place_pills()
        self.error_box.setGeometry(w // 2 - 260, h // 2 - 80, 520, 160)

    def _place_pills(self):
        w, h = self.width(), self.height()
        for pill, y in ((self.status, h // 2 + 64), (self.signal_pill, 96 if self.controls_shown else 18)):
            pill.adjustSize()
            pill.move((w - pill.width()) // 2, y)

    # ------------------------------------------------------------------ start / stop

    def start(self, item: Item, from_start: bool = False):
        """Plays a movie or episode (or a show's next episode)."""
        self.active = True
        self.error_box.hide()
        self.status.hide()
        self.signal_pill.hide()
        self.spinner.show()
        self.spinner.raise_()
        self.title.setText(item.display_title if item.type != "episode" else item.title)
        self.subtitle.setText("")
        self.show_controls()
        self.setFocus()
        client = self.ctx.client

        def prepare():
            target = item
            if target.type == "show":
                target = client.next_episode(target.key)
                if not target:
                    raise Exception("No episodes found.")
            if not target.part.get("key"):
                target = client.metadata(target.key) or target
            return target

        run_bg(prepare, lambda t: self._load(t, from_start), lambda e: self._fatal(str(e)))

    def _load(self, item: Item, from_start: bool):
        if not self.active:
            return
        url = self.ctx.client.direct_url(item)
        if not url:
            self._fatal("Couldn't find the file for this video.")
            return
        self.item = item
        self.dur = item.duration_ms / 1000
        resume = 0.0 if from_start else item.offset_ms / 1000
        if self.dur and resume > self.dur - 120:
            resume = 0.0
        self.cur = resume
        self.has_played = False
        self.failures = 0
        self.finishing = False
        self.offline = False
        self.last_data = time.monotonic()
        if item.type == "episode":
            self.title.setText(item.display_title)
            self.subtitle.setText(f"{item.episode_code}  ·  {item.title}")
        else:
            self.title.setText(item.display_title)
            self.subtitle.setText(str(item.get("year") or ""))
        self._configure_cache(item)
        self._open(resume)
        self.poll.start()
        self.report_timer.start()

    def _configure_cache(self, item: Item):
        minutes = int(self.ctx.settings["bufferMinutes"] or 10)
        secs = minutes * 60
        kbps = item.media.get("bitrate")
        size = item.part.get("size")
        if kbps:
            bps = kbps * 1000 / 8
        elif size and self.dur:
            bps = size / self.dur
        else:
            bps = 2_500_000
        want = int(bps * secs * 1.2)
        try:
            free = shutil.disk_usage(str(cache_dir())).free
            want = min(want, max(free - 2_000_000_000, 150_000_000))
        except Exception:
            pass
        want = max(min(want, 12_000_000_000), 150_000_000)
        self.readahead = secs
        m = self.video.mpv
        for k, v in (("demuxer-max-bytes", str(want)), ("demuxer-readahead-secs", str(secs)),
                     ("cache-secs", str(secs))):
            try:
                m[k] = v
            except Exception as e:  # noqa: BLE001
                print(f"mpv option {k}: {e}")

    def _open(self, at: float):
        m = self.video.mpv
        self.load_started = time.monotonic()
        url = self.ctx.client.direct_url(self.item)
        opts = {"start": f"{at:.3f}"} if at > 1 else {}
        m.loadfile(url, **opts)
        m.pause = False
        self._set_play_icon(True)

    def stop(self):
        if not self.active:
            return
        self.active = False
        self.poll.stop()
        self.report_timer.stop()
        self.retry_timer.stop()
        self.hide_timer.stop()
        item, t, d = self.item, self.cur, self.dur
        try:
            self.video.mpv.command("stop")
        except Exception:
            pass
        self.item = None
        self.spinner.hide()
        self.setCursor(Qt.ArrowCursor)
        if item:
            client = self.ctx.client

            def report():
                client.report_timeline(item, "stopped", int(t * 1000), int(d * 1000))
                if d and t / d > 0.9:
                    client.mark_watched(item.key)
            run_bg(report, lambda _: self.ctx.data_changed.emit())
        self.closed.emit()

    def shutdown(self):
        """App is closing."""
        if self.active:
            item, t, d = self.item, self.cur, self.dur
            if item:
                try:
                    self.ctx.client.report_timeline(item, "stopped", int(t * 1000), int(d * 1000))
                except Exception:
                    pass
        self.active = False
        self.video.shutdown()

    def _fatal(self, msg: str):
        self.spinner.hide()
        self.status.hide()
        self.error_lbl.setText(msg)
        self.error_box.show()
        self.error_box.raise_()
        self.show_controls()

    # ------------------------------------------------------------------ mpv events / polling

    def _mpv_event(self, eid: int, reason: int):
        if not self.active:
            return
        if eid == mpv.MpvEventID.END_FILE and reason == END_ERROR:
            # Couldn't open/read (no signal, server hiccup). Try again shortly.
            self._schedule_retry()
        elif eid == mpv.MpvEventID.PLAYBACK_RESTART:
            self.reloading = False

    def _schedule_retry(self):
        if not self.has_played:
            self.failures += 1
            if self.failures >= 4:
                self._fatal("This video couldn't be played.\nCheck your connection to the Plex server.")
                return
        self.reloading = True
        self.retry_timer.start(3000)

    def _retry(self):
        if self.active and self.item:
            self._open(self.cur)

    def _prop(self, name, default=None):
        try:
            v = self.video.mpv._get_property(name)
            return default if v is None else v
        except Exception:
            return default

    def _tick(self):
        if not self.active or not self.item:
            return
        m = self.video.mpv
        t = self._prop("time-pos")
        if t is not None and not self.reloading:
            self.cur = float(t)
        d = self._prop("duration")
        if d:
            self.dur = float(d)
        paused = bool(self._prop("pause", False))
        pfc = bool(self._prop("paused-for-cache", False))
        seeking = bool(self._prop("seeking", False))
        idle = bool(self._prop("core-idle", False))
        eof = bool(self._prop("eof-reached", False))
        state = self._prop("demuxer-cache-state", {}) or {}

        cache_end = state.get("cache-end")
        self.buffered = max(0.0, float(cache_end) - self.cur) if cache_end is not None else 0.0
        # "eof" is also set when the connection dies, so only trust it if we really reached the end.
        stream_done = bool(state.get("eof")) and cache_end is not None and self.dur > 0 \
            and float(cache_end) >= self.dur - 3

        # Is data still arriving? (mpv's speed reading freezes when data stops, so watch the
        # downloaded range grow instead.) If not, and the cache isn't simply full, we've lost signal.
        now = time.monotonic()
        full = self.buffered >= self.readahead - 15
        progress = (state.get("fw-bytes"), cache_end)
        if progress != getattr(self, "_last_progress", None) or stream_done or full or paused:
            self.last_data = now
        self._last_progress = progress
        self.offline = (now - self.last_data) > 6

        playing_now = t is not None and not paused and not idle and not pfc
        if playing_now and not seeking:
            self.has_played = True
            self.failures = 0

        buffering = (pfc or seeking or (not paused and idle and not eof) or self.reloading or t is None)
        self.spinner.setVisible(buffering)
        if buffering:
            self.spinner.raise_()
        self.buffering = buffering

        if self.offline and buffering:
            self.status.setText("No signal — will pick up where it left off")
            self.status.show()
            self.signal_pill.hide()
        elif self.offline:
            self.status.hide()
            self.signal_pill.setText(f"No signal · playing from {time_str(self.buffered)} saved")
            self.signal_pill.show()
        else:
            self.status.hide()
            self.signal_pill.hide()
        self._place_pills()

        # Reached the end of what was downloaded before the real end (connection gave out).
        if eof and not self.finishing:
            if self.dur and self.cur < self.dur - 20:
                if not self.reloading:
                    self._schedule_retry()
            else:
                self._finished()

        self._set_play_icon(not paused, buffering)
        if not self.dragging:
            self.scrub.set_state(self.cur, self.buffered, self.dur)
        self.time_lbl.setText(time_str(self.cur))
        self.remain_lbl.setText("-" + time_str(max(self.dur - self.cur, 0)))
        self.loaded_lbl.setText(f"{time_str(self.buffered)} loaded" if self.buffered > 1 else "")

    def _report(self):
        if not self.active or not self.item or self.offline:
            return
        item, t, d = self.item, self.cur, self.dur
        st = "paused" if self._prop("pause", False) else "playing"
        run_bg(lambda: self.ctx.client.report_timeline(item, st, int(t * 1000), int(d * 1000)))

    def _finished(self):
        self.finishing = True
        item, d = self.item, self.dur
        client = self.ctx.client
        self.spinner.show()

        def wrap_up():
            client.report_timeline(item, "stopped", int(d * 1000), int(d * 1000))
            client.mark_watched(item.key)
            return client.episode_after(item)

        def next_up(nxt):
            if not self.active:
                return
            self.ctx.data_changed.emit()
            if nxt:
                self._load(nxt, from_start=True)
            else:
                self.item = None  # already reported
                self.stop()

        run_bg(wrap_up, next_up, lambda e: self.stop())

    # ------------------------------------------------------------------ controls

    def toggle_pause(self):
        if not self.active:
            return
        m = self.video.mpv
        m.pause = not m.pause
        self._set_play_icon(not m.pause)
        self._report()
        self.show_controls()

    def skip(self, secs: float):
        self.seek(self.cur + secs)

    def seek(self, t: float):
        if not self.active or not self.item:
            return
        t = max(0.0, min(t, self.dur - 2 if self.dur else t))
        self.cur = t
        try:
            self.video.mpv.command("seek", f"{t:.3f}", "absolute")
        except Exception:
            pass
        self.scrub.set_state(self.cur, self.buffered, self.dur)
        self.show_controls()

    def _dragging(self, on):
        self.dragging = on
        if on:
            self.hide_timer.stop()
        else:
            self.show_controls()

    def _volume_changed(self, v):
        m = self.video.mpv
        m.volume = v
        if v > 0 and m.mute:
            m.mute = False
        self.ctx.settings["volume"] = v
        self._update_mute_icon()

    def change_volume(self, delta):
        self.vol.setValue(max(0, min(100, self.vol.value() + delta)))
        self.show_controls()

    def toggle_mute(self):
        m = self.video.mpv
        m.mute = not m.mute
        self.ctx.settings["muted"] = bool(m.mute)
        self._update_mute_icon()
        self.show_controls()

    def _update_mute_icon(self):
        muted = bool(self.video.mpv.mute) or self.vol.value() == 0
        self.mute_btn.setIcon(theme.icon("mute" if muted else "volume", "white", 22))

    def _set_play_icon(self, playing: bool, buffering: bool = False):
        # While loading, the spinner takes the play button's spot.
        name = "none" if buffering else ("pause" if playing else "play")
        if getattr(self, "_icon_state", None) != name:
            self._icon_state = name
            self.play_btn.setIcon(QIcon() if buffering else theme.icon(name, "white", 62))

    def set_fullscreen_icon(self, fs: bool):
        self.fs_btn.setIcon(theme.icon("unfullscreen" if fs else "fullscreen", "white", 26))

    def _show_tracks(self):
        tracks = self._prop("track-list", []) or []
        menu = QMenu(self)
        audio = [t for t in tracks if t.get("type") == "audio"]
        subs = [t for t in tracks if t.get("type") == "sub"]

        def name(t):
            bits = [t.get("title"), (t.get("lang") or "").upper() or None, (t.get("codec") or "").upper() or None]
            ch = t.get("demux-channel-count")
            if ch:
                bits.append({1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch} ch"))
            if t.get("forced"):
                bits.append("Forced")
            return "  ·  ".join(b for b in bits if b) or f"Track {t.get('id')}"

        def add_section(title, items, prop, allow_off):
            hdr = menu.addAction(title)
            hdr.setEnabled(False)
            group = QActionGroup(menu)
            if allow_off:
                a = menu.addAction("Off")
                a.setCheckable(True)
                a.setChecked(not any(t.get("selected") for t in items))
                a.triggered.connect(lambda: self._set_track(prop, "no"))
                group.addAction(a)
            for t in items:
                a = menu.addAction(name(t))
                a.setCheckable(True)
                a.setChecked(bool(t.get("selected")))
                a.triggered.connect(lambda _=False, i=t.get("id"): self._set_track(prop, i))
                group.addAction(a)

        if audio:
            add_section("Audio", audio, "aid", False)
        if subs or True:
            if audio:
                menu.addSeparator()
            add_section("Subtitles", subs, "sid", True)
        self.hide_timer.stop()
        menu.exec(self.tracks_btn.mapToGlobal(self.tracks_btn.rect().bottomLeft()))
        self.show_controls()

    def _set_track(self, prop, value):
        try:
            self.video.mpv[prop] = value
        except Exception:
            pass

    # ------------------------------------------------------------------ show / hide controls

    def show_controls(self):
        if not self.controls_shown:
            self.controls_shown = True
            for w in self.controls:
                w.show()
            for w in self.controls:
                w.raise_()
            self.spinner.raise_()
            self.status.raise_()
            self.signal_pill.raise_()
            self.error_box.raise_()
            self._place_pills()
        self.unsetCursor()
        self.video.unsetCursor()
        self.hide_timer.start(self.HIDE_AFTER_MS)

    def _auto_hide(self):
        if not self.active or self.dragging or self.error_box.isVisible():
            return
        if self._prop("pause", False):
            return
        over = QApplication.widgetAt(QCursor.pos())
        if over is not None and over is not self.video and any(
                over is c or c.isAncestorOf(over) for c in (self.top, self.bottom, self.center)):
            self.hide_timer.start(self.HIDE_AFTER_MS)
            return
        self.controls_shown = False
        for w in self.controls:
            w.hide()
        self.scrub.tip.hide()
        self.setCursor(Qt.BlankCursor)
        self.video.setCursor(Qt.BlankCursor)
        self._place_pills()

    # ------------------------------------------------------------------ input

    def eventFilter(self, obj, e):
        if obj is self.video:
            t = e.type()
            if t == QEvent.MouseMove:
                self.show_controls()
            elif t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
                self.click_timer.start()
            elif t == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
                self.click_timer.stop()
                self.toggle_fullscreen.emit()
                return True
            elif t == QEvent.Wheel:
                self.change_volume(5 if e.angleDelta().y() > 0 else -5)
                return True
        return super().eventFilter(obj, e)

    def mouseMoveEvent(self, e):
        self.show_controls()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Space, Qt.Key_K, Qt.Key_MediaPlay, Qt.Key_MediaTogglePlayPause, Qt.Key_MediaPause):
            self.toggle_pause()
        elif k in (Qt.Key_Left, Qt.Key_J):
            self.skip(-10)
        elif k in (Qt.Key_Right, Qt.Key_L):
            self.skip(10)
        elif k == Qt.Key_Up:
            self.change_volume(5)
        elif k == Qt.Key_Down:
            self.change_volume(-5)
        elif k == Qt.Key_M:
            self.toggle_mute()
        elif k in (Qt.Key_F, Qt.Key_F11):
            self.toggle_fullscreen.emit()
        elif k in (Qt.Key_Escape, Qt.Key_Back):
            if self.window().isFullScreen():
                self.toggle_fullscreen.emit()
            else:
                self.stop()
        elif k == Qt.Key_MediaStop:
            self.stop()
        else:
            super().keyPressEvent(e)
