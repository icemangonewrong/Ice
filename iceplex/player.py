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
import sys
import time
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QFont, QIcon, QLinearGradient, QOpenGLContext, QPainter, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QMenu, QSlider, QToolButton, QVBoxLayout, QWidget, QPushButton)

from . import audiodev, log, prefetch, theme
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
                           terminal="no", audio_client_name="Ice",
                           log_handler=self._mpv_log, loglevel="warn")
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
            "user-agent": "Ice",
            "sub-auto": "fuzzy",
            "audio-file-auto": "no",
            # English audio first, whatever the file marks as default (falls back to the default if none).
            "alang": "en,eng,english",
            "video-sync": "audio",
            # Resume at the nearest keyframe instead of decoding forward to the exact second: starts
            # much faster on big files. Seeks you make yourself are still exact (see seek()).
            "hr-seek": "no",
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
    def _mpv_log(level, component, message):
        log.info(f"mpv [{level}] {component}: {str(message).strip()}")

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
            reason = int(event.data.reason)
            log.info(f"mpv: file ended, reason {reason} ({ {0: 'end', 2: 'stopped', 3: 'quit', 4: 'ERROR', 5: 'replaced'}.get(reason, '?') })"
                     + (f", error {getattr(event.data, 'error', '')}" if reason == 4 else ""))
            self.mpv_event.emit(eid, reason)
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
# Scrub bar along the top edge of the control bar: played (orange), loaded (light), rest (dim)
# ---------------------------------------------------------------------------

class ScrubBar(QWidget):
    seek_requested = Signal(float)
    dragging_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.current = 0.0
        self.buffered = 0.0
        self.duration = 0.0
        self.markers: list[dict] = []
        self.drag: Optional[float] = None
        self.hover: Optional[float] = None
        self.tip = QLabel(parent)
        self.tip.setStyleSheet("background: rgba(20,20,26,0.95); border-radius: 6px; padding: 4px 9px;"
                               "font-weight: 600;")
        self.tip.hide()

    def set_state(self, current, buffered, duration):
        self.current, self.buffered, self.duration = current, buffered, duration
        self.update()

    def _time_at(self, x):
        return max(0.0, min(x / max(self.width(), 1), 1.0)) * self.duration

    def _x_for(self, t):
        if self.duration <= 0:
            return 0
        return self.width() * max(0.0, min(t / self.duration, 1.0))

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
        px = max(4, min(int(gp.x() - self.tip.width() / 2), self.tip.parentWidget().width() - self.tip.width() - 4))
        self.tip.move(px, gp.y() - self.tip.height() - 4)
        self.tip.show()
        self.tip.raise_()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        active = self.drag is not None or self.hover is not None
        th = 7 if active else 4
        y = self.height() / 2 - th / 2
        w = self.width()
        shown = self.drag if self.drag is not None else self.current
        px = self._x_for(shown)
        bx = max(self._x_for(self.current + self.buffered), px)
        p.setPen(Qt.NoPen)
        p.fillRect(QRectF(0, y, w, th), QColor(255, 255, 255, 55))
        p.fillRect(QRectF(0, y, bx, th), QColor(255, 255, 255, 120))
        p.fillRect(QRectF(0, y, px, th), QColor(theme.ORANGE))
        # little ticks where the intro / credits are
        for m in self.markers:
            p.fillRect(QRectF(self._x_for(m["start"]) - 1, y, 2, th), QColor(255, 255, 255, 170))
        if active:
            p.setBrush(QColor(theme.ORANGE))
            p.drawEllipse(QPointF(px, self.height() / 2), 8, 8)


def tool(icon_name: str, size: int = 26, tip: str = "", pad: int = 8) -> QToolButton:
    b = QToolButton()
    b.setIcon(theme.icon(icon_name, "white", size))
    b.setIconSize(QSize(size, size))
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    b.setStyleSheet(f"QToolButton {{ border-radius: {(size + 2 * pad) // 2}px; padding: {pad}px; }}"
                    "QToolButton:hover { background: rgba(255,255,255,0.12); }"
                    "QToolButton:disabled { background: transparent; }")
    if tip:
        b.setToolTip(tip)
    return b


def divider() -> QWidget:
    d = QWidget()
    d.setFixedSize(2, 34)
    d.setStyleSheet("background: rgba(255,255,255,0.22); border-radius: 1px;")
    return d


class Pill(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: rgba(10,10,14,0.85); border-radius: 14px; padding: 7px 16px;"
                           "font-weight: 600; font-size: 10.5pt;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()


def overlay_btn_style() -> str:
    return ("QPushButton { background: rgba(20,20,26,0.85); border: 1.5px solid rgba(255,255,255,0.8);"
            " border-radius: 8px; padding: 10px 22px; font-size: 11.5pt; font-weight: 700; color: white; }"
            f"QPushButton:hover {{ background: {theme.accent_rgba(0.95)}; border-color: {theme.ORANGE};"
            f" color: {theme.on_accent_text()}; }}")


def volume_style() -> str:
    return ("QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.25); border-radius: 2px; }"
            f"QSlider::sub-page:horizontal {{ background: {theme.ORANGE}; border-radius: 2px; }}"
            "QSlider::handle:horizontal { background: white; width: 12px; height: 12px; margin: -4px 0;"
            " border-radius: 6px; }")


# ---------------------------------------------------------------------------
# Up Next card
# ---------------------------------------------------------------------------

class UpNextCard(QFrame):
    play_now = Signal()
    cancel = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("UpNext")
        self.setStyleSheet("QFrame#UpNext { background: rgba(18,18,22,0.94); border: 1px solid rgba(255,255,255,0.15);"
                           " border-radius: 12px; }")
        self.setFixedWidth(340)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(4)
        self.head = label("UP NEXT", "Accent")
        self.head.setFont(theme.font(9.5, QFont.Bold))
        v.addWidget(self.head)
        self.title = label("", bold=True)
        self.title.setFont(theme.font(12.5, QFont.Bold))
        v.addWidget(self.title)
        self.sub = label("", "Muted")
        v.addWidget(self.sub)
        v.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.play_btn = QPushButton("Play now")
        self.play_btn.setObjectName("Primary")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.NoFocus)
        self.play_btn.clicked.connect(self.play_now.emit)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setFocusPolicy(Qt.NoFocus)
        self.cancel_btn.clicked.connect(self.cancel.emit)
        row.addWidget(self.play_btn, 1)
        row.addWidget(self.cancel_btn)
        v.addLayout(row)
        self.hide()

    def show_for(self, nxt: Item, seconds_left: int):
        self.title.setText(nxt.title or "Next episode")
        self.sub.setText(nxt.episode_code)
        self.set_countdown(seconds_left)
        self.adjustSize()
        self.show()
        self.raise_()

    def set_countdown(self, secs: int):
        self.head.setText(f"UP NEXT · PLAYING IN {max(secs, 0)}")


# ---------------------------------------------------------------------------
# Small popup with − / + for audio delay and subtitle timing
# ---------------------------------------------------------------------------

SUB_LANGS = [("English", "en"), ("Spanish", "es"), ("French", "fr"), ("German", "de"), ("Italian", "it"),
             ("Portuguese", "pt"), ("Dutch", "nl"), ("Swedish", "sv"), ("Norwegian", "no"), ("Danish", "da"),
             ("Finnish", "fi"), ("Polish", "pl"), ("Russian", "ru"), ("Ukrainian", "uk"), ("Czech", "cs"),
             ("Hungarian", "hu"), ("Romanian", "ro"), ("Greek", "el"), ("Turkish", "tr"), ("Arabic", "ar"),
             ("Hebrew", "he"), ("Hindi", "hi"), ("Japanese", "ja"), ("Korean", "ko"), ("Chinese", "zh"),
             ("Vietnamese", "vi"), ("Thai", "th"), ("Indonesian", "id")]


class SubtitleSearchPopup(QFrame):
    """Find subtitles online through the Plex server (it searches OpenSubtitles, like Plex's own apps)."""

    def __init__(self, parent, client, item_key: str, language: str, on_pick, on_language):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("SubSearch")
        self.setStyleSheet("QFrame#SubSearch { background: #1d1d23; border: 1px solid #2a2a32; border-radius: 12px; }"
                           "QListWidget { background: #15151a; border: 1px solid #2a2a32; border-radius: 8px; }"
                           "QListWidget::item { padding: 7px 6px; }"
                           f"QListWidget::item:selected {{ background: {theme.ORANGE}; color: black; }}")
        self.client, self.key, self.on_pick, self.on_language = client, item_key, on_pick, on_language
        self.alive = True
        self.destroyed.connect(lambda *_: setattr(self, "alive", False))
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(8)
        v.addWidget(label("Search for subtitles", "H3"))
        n = label("Your Plex server looks them up online (OpenSubtitles) and saves the one you pick with the video.",
                  "Muted", wrap=True)
        n.setMaximumWidth(420)
        v.addWidget(n)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.lang = QComboBox()
        self.lang.setFocusPolicy(Qt.StrongFocus)
        for name, code in SUB_LANGS:
            self.lang.addItem(name, code)
        i = self.lang.findData(language)
        self.lang.setCurrentIndex(i if i >= 0 else 0)
        self.lang.currentIndexChanged.connect(lambda *_: self.search())
        row.addWidget(self.lang, 1)
        self.hi = QComboBox()
        for text, val in (("Regular", 0), ("Hearing impaired (SDH)", 1)):
            self.hi.addItem(text, val)
        self.hi.currentIndexChanged.connect(lambda *_: self.search())
        row.addWidget(self.hi, 1)
        v.addLayout(row)
        self.list = QListWidget()
        self.list.setMinimumSize(420, 260)
        self.list.setCursor(Qt.PointingHandCursor)
        self.list.itemActivated.connect(self._picked)
        self.list.itemClicked.connect(lambda it: self.get_btn.setEnabled(it.data(Qt.UserRole) is not None))
        v.addWidget(self.list)
        self.status = label("", "Muted", wrap=True)
        v.addWidget(self.status)
        self.get_btn = QPushButton("Download and use")
        self.get_btn.setObjectName("Primary")
        self.get_btn.setCursor(Qt.PointingHandCursor)
        self.get_btn.setEnabled(False)
        self.get_btn.clicked.connect(lambda: self.list.currentItem() and self._picked(self.list.currentItem()))
        v.addWidget(self.get_btn)
        self.search()

    def search(self):
        code = self.lang.currentData()
        hi = self.hi.currentData()
        self.on_language(code)
        self.list.clear()
        self.get_btn.setEnabled(False)
        self.status.setText("Searching…")
        token = object()
        self._token = token

        def done(results):
            if not self.alive or self._token is not token:
                return
            self.list.clear()
            if not results:
                self.status.setText("Nothing found in that language.")
                return
            self.status.setText(f"{len(results)} found · double-click one to use it.")
            for r in results:
                bits = [b for b in (r["provider"], r["format"], "SDH" if r["hi"] else "", "Forced" if r["forced"] else "") if b]
                if r["score"] is not None:
                    bits.append(f"match {r['score']}")
                text = ("★ " if r["perfect"] else "") + r["title"] + ("\n" + "  ·  ".join(bits) if bits else "")
                it = QListWidgetItem(text)
                it.setData(Qt.UserRole, r)
                self.list.addItem(it)
            self.list.setCurrentRow(0)
            self.get_btn.setEnabled(True)

        def failed(e):
            if self.alive and self._token is token:
                self.status.setText(f"Couldn't search: {e}")

        run_bg(lambda: self.client.search_subtitles(self.key, code, hi), done, failed)

    def _picked(self, it):
        r = it.data(Qt.UserRole) if it else None
        if r:
            self.on_pick(r)
            self.close()


class AdjustPopup(QFrame):
    def __init__(self, parent, title: str, note: str, value_ms: int, minus_text: str, plus_text: str,
                 describe, on_change, step: int = 50):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("Adjust")
        self.setStyleSheet("QFrame#Adjust { background: #1d1d23; border: 1px solid #2a2a32; border-radius: 12px; }")
        self.value, self.step, self.describe, self.on_change = value_ms, step, describe, on_change
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(6)
        t = label(title, "H3")
        v.addWidget(t)
        n = label(note, "Muted", wrap=True)
        n.setMaximumWidth(300)
        v.addWidget(n)
        self.val = label("")
        self.val.setFont(theme.font(16, QFont.Bold))
        self.val.setAlignment(Qt.AlignCenter)
        v.addSpacing(4)
        v.addWidget(self.val)
        self.desc = label("", "Muted")
        self.desc.setAlignment(Qt.AlignCenter)
        v.addWidget(self.desc)
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, delta in ((minus_text, -1), (plus_text, 1)):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(120)
            b.clicked.connect(lambda _=False, d=delta: self._bump(d))
            row.addWidget(b)
        v.addLayout(row)
        reset = QPushButton("Reset to 0")
        reset.setCursor(Qt.PointingHandCursor)
        reset.clicked.connect(lambda: self._set(0))
        v.addWidget(reset)
        self._render()

    def _bump(self, d):
        self._set(self.value + d * self.step)

    def _set(self, v):
        self.value = max(-5000, min(5000, int(v)))
        self.on_change(self.value)
        self._render()

    def _render(self):
        self.val.setText(f"{self.value:+d} ms" if self.value else "0 ms")
        self.desc.setText(self.describe(self.value))


# ---------------------------------------------------------------------------
# Player page
# ---------------------------------------------------------------------------

BAR_H = 112
MINI_H = 100            # height of the mini player along the bottom of the app
UP_NEXT_SECS = 10


class PlayerPage(QWidget):
    closed = Signal()
    toggle_fullscreen = Signal()
    mode_changed = Signal(str)      # "full" or "mini"
    mirror_msg = Signal(str)        # second headphones status (from the copy thread)

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

        # --- close button (top left) ----------------------------------------
        self.close_btn = tool("down", 26, "Minimize player (Esc)", 9)
        self.close_btn.setParent(self)
        self.close_btn.setStyleSheet("QToolButton { background: rgba(18,18,22,0.75); border-radius: 22px; padding: 9px; }"
                                     "QToolButton:hover { background: rgba(60,60,70,0.95); }")
        self.close_btn.clicked.connect(self.minimize)

        # --- bottom bar ------------------------------------------------------
        self.bar = QFrame(self)
        self.bar.setObjectName("Bar")
        self.bar.setStyleSheet("QFrame#Bar { background: rgba(16,16,20,0.90); }")

        self.info = QWidget(self.bar)
        iv = QVBoxLayout(self.info)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(1)
        self.title = label("")
        self.title.setFont(theme.font(13, QFont.Bold))
        self.subtitle = label("", "Muted")
        self.time_lbl = label("", "Muted")
        self.time_lbl.setFont(theme.font(10, QFont.DemiBold))
        for w in (self.title, self.subtitle, self.time_lbl):
            iv.addWidget(w)

        self.center = QWidget(self.bar)
        ch = QHBoxLayout(self.center)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        self.prev_btn = tool("prev", 26, "Restart / previous episode (Shift+P)")
        self.back_btn = tool("back10", 34, "Back 10 seconds (←)")
        self.play_btn = tool("pause", 30, "Play / pause (Space)", 11)
        self.play_btn.setStyleSheet("QToolButton { background: rgba(255,255,255,0.14); border-radius: 26px; padding: 11px; }"
                                    "QToolButton:hover { background: rgba(255,255,255,0.24); }")
        self.fwd_btn = tool("fwd10", 34, "Forward 10 seconds (→)")
        self.next_btn = tool("next", 26, "Next episode (Shift+N)")
        # Previous / next sit apart from the main controls, behind a divider, so they're hard to hit by accident.
        ch.addWidget(self.prev_btn)
        ch.addSpacing(26)
        self.div_l = divider()
        ch.addWidget(self.div_l)
        ch.addSpacing(30)
        ch.addWidget(self.back_btn)
        ch.addSpacing(18)
        ch.addWidget(self.play_btn)
        ch.addSpacing(18)
        ch.addWidget(self.fwd_btn)
        ch.addSpacing(30)
        self.div_r = divider()
        ch.addWidget(self.div_r)
        ch.addSpacing(26)
        ch.addWidget(self.next_btn)
        # Mini player only: stop
        self.stop_wrap = QWidget()
        sw = QHBoxLayout(self.stop_wrap)
        sw.setContentsMargins(18, 0, 0, 0)
        self.stop_btn = tool("stop", 24, "Stop")
        self.stop_btn.clicked.connect(self.stop)
        sw.addWidget(self.stop_btn)
        ch.addWidget(self.stop_wrap)
        self.stop_wrap.hide()
        # Keep the play button dead center even on movies, where there's no Next button.
        for w in (self.next_btn, self.div_r):
            sp = w.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            w.setSizePolicy(sp)
        self.prev_btn.clicked.connect(self.previous)
        self.back_btn.clicked.connect(lambda: self.skip(-10))
        self.play_btn.clicked.connect(self.toggle_pause)
        self.fwd_btn.clicked.connect(lambda: self.skip(10))
        self.next_btn.clicked.connect(self.next_episode)

        # Close (X): sits on its own between Next and the right-hand buttons so it isn't hit by accident.
        self.x_btn = tool("close", 22, "Close the video")
        self.x_btn.setParent(self.bar)
        self.x_btn.clicked.connect(self.stop)

        self.right = QWidget(self.bar)
        rh = QHBoxLayout(self.right)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(6)
        self.tracks_btn = tool("tracks", 24, "Audio language & subtitles")
        self.tracks_btn.clicked.connect(self._show_tracks)
        self.delay_btn = tool("audiodelay", 24, "Audio delay (for Bluetooth lag)")
        self.delay_btn.clicked.connect(self._show_audio_delay)
        self.mute_btn = tool("volume", 22, "Mute (M)")
        self.mute_btn.clicked.connect(self.toggle_mute)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(int(m.volume))
        self.vol.setFixedWidth(90)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.setStyleSheet(volume_style())
        self.vol.valueChanged.connect(self._volume_changed)
        self.fs_btn = tool("fullscreen", 22, "Full screen (F)")
        self.fs_btn.clicked.connect(self.toggle_fullscreen.emit)
        self.expand_btn = tool("up", 26, "Open player")
        self.expand_btn.clicked.connect(self.expand)
        self.expand_btn.hide()
        for w in (self.tracks_btn, self.delay_btn, self.mute_btn, self.vol, self.fs_btn, self.expand_btn):
            rh.addWidget(w)

        self.scrub = ScrubBar(self)
        self.scrub.seek_requested.connect(self.seek)
        self.scrub.dragging_changed.connect(self._dragging)

        # --- floating pieces ----------------------------------------------
        self.skip_btn = QPushButton("Skip Intro", self)
        self.skip_btn.setStyleSheet(overlay_btn_style())
        self.skip_btn.setCursor(Qt.PointingHandCursor)
        self.skip_btn.setFocusPolicy(Qt.NoFocus)
        self.skip_btn.clicked.connect(self._skip_marker)
        self.skip_btn.hide()

        self.up_next = UpNextCard(self)
        self.up_next.play_now.connect(lambda: self._advance(user=True))
        self.up_next.cancel.connect(self._cancel_up_next)

        self.spinner = Spinner(64, self)
        self.status = Pill(self)
        self.signal_pill = Pill(self)
        self.toast = Pill(self)
        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.toast.hide)

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
        self.error_box.hide()

        self.controls = [self.close_btn, self.bar, self.scrub]

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
        self.click_timer.timeout.connect(self._single_click)
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self._retry)
        self.device_timer = QTimer(self)
        self.device_timer.setInterval(3000)
        self.device_timer.timeout.connect(self._check_audio_device)

        # --- state ----------------------------------------------------------
        self.item: Optional[Item] = None
        self.next_item: Optional[Item] = None
        self.markers: list[dict] = []
        self.pending_subs: list[dict] = []
        self.zoom_scale = 1.0
        self.preload: Optional[prefetch.Preload] = None
        self._preload_extras: Optional[tuple] = None   # (key, details, episode after it)
        self._preload_pending: Optional[str] = None
        self.file_loaded = False
        self.active = False
        self.cur = 0.0
        self.dur = 0.0
        self.buffered = 0.0
        self.readahead = 600.0
        self.dragging = False
        self.user_paused = False
        self._log_state = None
        self._beat_at = 0.0
        self._last_drops = 0
        self._stall_reset()
        self.controls_shown = True
        self.has_played = False
        self.failures = 0
        self.last_data = time.monotonic()
        self.offline = False
        self.reloading = False
        self.finishing = False
        self.switching = False
        self.up_next_cancelled = False
        self.up_next_deadline: Optional[float] = None
        self._up_next_left = float(UP_NEXT_SECS)
        self.skipped_markers: set = set()
        self.sub_delay_ms = 0
        self.audio_device = None
        self.buffering = False
        self.mode = "full"

        for w in self.findChildren(QWidget):
            w.setMouseTracking(True)
        self.video.installEventFilter(self)
        self.info.installEventFilter(self)

        # Playback info overlay (press I).
        self.stats = QLabel(self)
        self.stats.setStyleSheet("background: rgba(10,10,14,0.82); color: #e8e8e8; border-radius: 8px;"
                                 "padding: 10px 14px; font-family: Consolas, monospace; font-size: 12px;")
        self.stats.hide()

        # Button hints are drawn inside the player. Windows' own tooltips are separate little windows, and
        # one popping up over the video makes the whole screen flash.
        self.hint = QLabel(self)
        self.hint.setStyleSheet("background: rgba(20,20,26,0.95); color: white; border-radius: 6px;"
                                "padding: 4px 9px; font-weight: 600;")
        self.hint.hide()
        self._hint_for = None
        for w in self.findChildren(QWidget):
            if w is not self.hint and w is not self.video:
                w.installEventFilter(self)

    # ------------------------------------------------------------------ layout

    def resizeEvent(self, e):
        self._layout()

    def _layout(self):
        w, h = self.width(), self.height()
        if self.mode == "mini":
            self._layout_mini(w, h)
            return
        self.video.setGeometry(0, 0, w, h)
        self.close_btn.adjustSize()
        self.close_btn.move(18, 18)
        self.bar.setGeometry(0, h - BAR_H, w, BAR_H)
        self.scrub.setGeometry(0, h - BAR_H - self.scrub.height() // 2, w, self.scrub.height())
        # Narrow window: drop the volume slider (mute and ↑/↓ still work) to make room.
        self.vol.setVisible(w >= 1180)
        cs = self.center.sizeHint()
        rs = self.right.sizeHint()
        xs = self.x_btn.sizeHint()
        rx = w - rs.width() - 24
        cy = (BAR_H - cs.height()) // 2 + 6
        cx = (w - cs.width()) // 2
        # The X sits halfway between Next and the right-hand buttons. If the window is too narrow for that,
        # slide the middle buttons left so the X always has clear space on both sides.
        gap = 28
        cx = min(cx, rx - 2 * gap - xs.width() - cs.width())
        show_x = cx >= 150
        if not show_x:
            cx = (w - cs.width()) // 2
        self.x_btn.setVisible(show_x and self.mode == "full")
        self.center.setGeometry(cx, cy, cs.width(), cs.height())
        self.right.setGeometry(rx, (BAR_H - rs.height()) // 2 + 6, rs.width(), rs.height())
        mid = (cx + cs.width() + rx) // 2
        self.x_btn.setGeometry(mid - xs.width() // 2, (BAR_H - xs.height()) // 2 + 6, xs.width(), xs.height())
        left_w = max(100, cx - 24)
        self.info.setGeometry(28, 22, left_w - 28, BAR_H - 30)
        self.spinner.move((w - self.spinner.width()) // 2, (h - self.spinner.height()) // 2)
        self.error_box.setGeometry(w // 2 - 260, h // 2 - 80, 520, 160)
        self._place_floating()

    def _layout_mini(self, w, h):
        self.vol.show()
        top = self.scrub.height() // 2
        bh = h - top
        self.bar.setGeometry(0, top, w, bh)
        self.scrub.setGeometry(0, 0, w, self.scrub.height())
        th = bh - 16
        tw = int(th * 16 / 9)
        self.video.setGeometry(10, top + 8, tw, th)
        cs = self.center.sizeHint()
        self.center.setGeometry((w - cs.width()) // 2, (bh - cs.height()) // 2, cs.width(), cs.height())
        rs = self.right.sizeHint()
        self.right.setGeometry(w - rs.width() - 18, (bh - rs.height()) // 2, rs.width(), rs.height())
        x = 10 + tw + 16
        self.info.setGeometry(x, (bh - 66) // 2, max(self.center.x() - 24 - x, 80), 66)
        self._elide_info()
        sp = self.spinner
        sp.move(10 + (tw - sp.width()) // 2, top + 8 + (th - sp.height()) // 2)

    # ------------------------------------------------------------------ mini player

    def minimize(self):
        """Pause and shrink to the mini player along the bottom (Esc / back)."""
        if not self.active or self.mode == "mini":
            return
        log.info(f"Minimized at {time_str(self.cur)}")
        self.user_paused = True
        if not self._prop("pause", False):
            try:
                self.video.mpv.pause = True
            except Exception:
                pass
            self._set_play_icon(False)
            self._report()
        self.set_mode("mini")

    def expand(self):
        if self.active and self.mode != "full":
            self.set_mode("full")

    def set_mode(self, mode: str):
        self.mode = mode
        mini = mode == "mini"
        self.hide_timer.stop()
        for w in (self.tracks_btn, self.delay_btn, self.fs_btn, self.x_btn):
            w.setVisible(not mini)
        for w in (self.stop_wrap, self.expand_btn):
            w.setVisible(mini)
        self.title.setFont(theme.font(12 if mini else 13, QFont.Bold))
        if mini:
            for w in (self.close_btn, self.skip_btn, self.up_next, self.status, self.signal_pill, self.toast,
                      self.error_box):
                w.hide()
            self.scrub.tip.hide()
            self.setStyleSheet("QWidget#Player { background: transparent; }")
            self.video.raise_()
            self.spinner.raise_()
        else:
            self.setStyleSheet("QWidget#Player { background: black; }")
            self.video.lower()
        self.controls_shown = False
        self.show_controls()
        self.mode_changed.emit(mode)
        self._layout()
        if not mini:
            self.setFocus()

    def restyle(self):
        """The accent color changed."""
        self.vol.setStyleSheet(volume_style())
        self.skip_btn.setStyleSheet(overlay_btn_style())
        self.scrub.update()
        self.spinner.update()

    def _place_floating(self):
        if self.mode == "mini":
            return
        w, h = self.width(), self.height()
        above = h - BAR_H - 24 if self.controls_shown else h - 36
        self.skip_btn.adjustSize()
        self.skip_btn.move(w - self.skip_btn.width() - 28, above - self.skip_btn.height())
        self.up_next.adjustSize()
        self.up_next.move(w - self.up_next.width() - 28, above - self.up_next.height())
        for pill, y in ((self.status, h // 2 + 56), (self.signal_pill, 24), (self.toast, 76)):
            pill.adjustSize()
            pill.move((w - pill.width()) // 2, y)

    def _elide_info(self):
        for lbl in (self.title, self.subtitle):
            full = lbl.property("full") or lbl.text()
            lbl.setProperty("full", full)
            lbl.setText(lbl.fontMetrics().elidedText(full, Qt.ElideRight, max(self.info.width() - 4, 60)))

    def _set_info(self, title: str, sub: str):
        self.title.setProperty("full", title)
        self.subtitle.setProperty("full", sub)
        self.title.setText(title)
        self.subtitle.setText(sub)
        self._elide_info()

    # ------------------------------------------------------------------ start / stop

    def start(self, item: Item, from_start: bool = False):
        """Plays a movie or episode (or a show's next episode)."""
        if self.active and self.item:
            # Something else was playing (e.g. in the mini player): save its spot first.
            old, t, d = self.item, self.cur, self.dur
            client0 = self.ctx.client
            run_bg(lambda: client0.report_timeline(old, "stopped", int(t * 1000), int(d * 1000)),
                   lambda _: self.ctx.data_changed.emit())
            self.item = None
        self.active = True
        self.error_box.hide()
        self.status.hide()
        self.signal_pill.hide()
        self.spinner.show()
        self.spinner.raise_()
        self._set_info(item.display_title, "")
        self.show_controls()
        self.setFocus()
        client = self.ctx.client

        def prepare():
            target = item
            if target.type == "show":
                target = client.next_episode(target.key)
                if not target:
                    raise Exception("No episodes found.")
            # Always the latest details: the list this came from may be minutes old, and its resume point with it.
            try:
                target = client.metadata(target.key) or target
            except Exception as e:  # noqa: BLE001
                print("fresh details failed, using the list's:", e)
            return target

        run_bg(prepare, lambda t: self._load(t, from_start), lambda e: self._fatal(str(e)))

    def _load(self, item: Item, from_start: bool):
        if not self.active:
            return
        url = self.ctx.client.direct_url(item)
        if not url:
            self._fatal("Couldn't find the file for this video.")
            return
        if not self.item or self.item.key != item.key:
            self._reset_zoom_silently()
        self.item = item
        self.next_item = None
        self.markers = []
        self.scrub.markers = []
        self.pending_subs = []
        self.file_loaded = False
        self.skipped_markers = set()
        self.up_next_cancelled = False
        self.up_next_deadline = None
        self._up_next_left = float(UP_NEXT_SECS)
        self.up_next.hide()
        self.skip_btn.hide()
        self.sub_delay_ms = 0
        self._user_picked_sub = False
        self._user_picked_audio = False
        self._audio_checked_until = time.monotonic() + 20
        self._embedded_subs = 0
        self.switching = False
        self.user_paused = False
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
            self._set_info(item.display_title, f"{item.episode_code}  ·  {item.title}")
        else:
            self._set_info(item.display_title, str(item.get("year") or ""))
        is_ep = item.type == "episode"
        self._show_next(False)   # shown once we know there's a next episode
        self._layout()
        self._configure_cache(item)
        try:
            self.video.mpv["sub-delay"] = 0
        except Exception:
            pass
        self._open(resume)
        self._apply_audio_delay(force=True)
        self.poll.start()
        self.report_timer.start()
        self.device_timer.start()
        self._fetch_extras(item)
        self._stall_reset()
        self._log_state = None
        self._beat_at = time.monotonic() + 20
        self._last_drops = 0
        log.info(f"PLAY {item.display_title}" + (f" {item.episode_code} {item.title}" if is_ep else "")
                 + f"  (key {item.key}, from {time_str(resume)}, {item.media.get('videoResolution', '?')} "
                 f"{item.media.get('videoCodec', '?')}/{item.media.get('audioCodec', '?')}, "
                 f"{item.media.get('bitrate', '?')} kbps, {item.part.get('container', '?')}, buffer {int(self.readahead) // 60} min)")
        log.info(f"Server address: {self.ctx.client.address_line()}")

    def _fetch_extras(self, item: Item):
        """Markers, external subtitles and the next episode, fetched while the video starts."""
        client = self.ctx.client

        pre = self._preload_extras
        if pre and pre[0] == item.key:
            self._preload_extras = None
            got_early = (pre[1], pre[2])
        else:
            got_early = None

        def fetch():
            if got_early:
                return got_early
            full = None
            try:
                full = client.details(item.key)
            except Exception:
                pass
            nxt = None
            if item.type == "episode":
                try:
                    nxt = client.episode_after(item)
                except Exception:
                    pass
            return full, nxt

        def got(result):
            full, nxt = result
            if not self.active or self.item is None or self.item.key != item.key:
                return
            if full:
                self.markers = full.markers()
                self.scrub.markers = self.markers
                self.pending_subs = full.external_subtitles()
                self._embedded_subs = sum(1 for st in full.part.get("Stream") or []
                                          if st.get("streamType") == 3 and not st.get("key"))
                self._add_external_subs()
            self.next_item = nxt
            self._show_next(nxt is not None)
            if nxt:
                self.next_btn.setToolTip(f"Next episode: {nxt.episode_code} · {nxt.title} (Shift+N)")

        run_bg(fetch, got)

    def _show_next(self, on: bool):
        """The next-episode button is only there when there is a next episode."""
        for w in (self.next_btn, self.div_r):
            w.setVisible(on)
        self.next_btn.setEnabled(on)
        self._layout()

    def _add_external_subs(self):
        # Wait until the video is actually playing so fetching subtitle files never delays the start.
        if not self.file_loaded or not self.has_played or not self.pending_subs:
            return
        subs, self.pending_subs = self.pending_subs, []
        for s in subs:
            url = self.ctx.client.stream_url(s["key"])
            if not url:
                continue
            try:
                # "auto" = add it to the menu without switching to it; "select" if you picked it in Plex
                self.video.mpv.command_async("sub-add", url, "auto", s["title"], s["lang"])
            except Exception as e:  # noqa: BLE001
                print("sub-add failed:", e)

    def _configure_cache(self, item: Item):
        minutes = int(self.ctx.settings["bufferMinutes"] or 2)
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

    def _open(self, at: float, direct: bool = False):
        m = self.video.mpv
        self.file_loaded = False
        url = self.ctx.client.direct_url(self.item)
        p = self.preload
        if not (p and p.key == self.item.key):
            p = prefetch.find(self.item.key)    # warmed up from its info page
            if p:
                self.preload = p
        if not direct and p and p.key == self.item.key and p.head_len() > 0 and p.size:
            local = prefetch.url_for(p)
            if local:
                url = local
                p.cancel()   # use what's there; the rest streams from the server
        opts = {"start": f"{at:.3f}"} if at > 1 else {}
        # Subtitles start off (like Plex), whatever the file marks as default. Plex's own choice for this
        # video is applied once its details arrive. On a reopen, keep whatever was showing.
        opts["sid"] = self._reopen_sid()
        log.info(f"open {'local preload' if url.startswith('http://127.') else 'server'} at {time_str(at)}")
        m.loadfile(url, **opts)
        m.pause = False
        self.user_paused = False
        self._set_play_icon(True)

    # ------------------------------------------------------------------ next-episode preload

    def warm(self, item: Item):
        """An info page is open: fetch ~10 s from where Play would start, so it starts at once.
        Skipped while something else is playing, so it never takes that video's bandwidth."""
        if self.active and self.item and not self._prop("pause", True):
            return
        if not item or item.type not in ("movie", "episode") or not item.part.get("key"):
            return
        size = item.part.get("size")
        dur = item.duration_ms / 1000
        if not size or dur <= 0:
            return
        at = item.offset_ms / 1000
        if at > dur - 120:
            at = 0.0
        w = getattr(self, "_warm", None)
        if w and w.key == item.key and abs(getattr(w, "at", 0) - at) < 5 and not w.cancelled.is_set():
            return
        if w:
            w.cancel()
        url = self.ctx.client.direct_url(item)
        if not url:
            return
        p = prefetch.Preload(item.key, url, int(size), item.media.get("bitrate"), seconds=10,
                             at_seconds=at, duration=dur)
        p.at = at
        keep = {k for k in (self.item.key if self.item else None,
                            self.next_item.key if self.next_item else None, item.key) if k}
        prefetch.keep_only(keep)
        prefetch.register(p)
        p.allowed.set()
        p.start()
        self._warm = p

    def _preload_next(self, stream_done: bool):
        """With 5 minutes or less left, fetch the first ~20 s of the next episode so it starts at once.
        Only while the current episode has a comfortable cushion buffered, so it never stalls this one."""
        nxt = self.next_item
        keep = {k for k in (self.item.key if self.item else None, nxt.key if nxt else None) if k}
        p = self.preload
        if p and p.key not in keep:
            self.preload = None
            p = None
        left = self.dur - self.cur if self.dur else 0
        if not self.has_played or not self.dur or left > prefetch.START_WHEN_LEFT:
            return
        # Last 5 minutes: keep loading this episode all the way to its end, whatever the buffer setting.
        self._load_to_end(left)
        if not nxt:
            return
        if p is None or p.key != nxt.key:
            if self._preload_pending == nxt.key:
                return
            self._preload_pending = nxt.key
            client = self.ctx.client
            cur_key = self.item.key

            def prepare():
                full = client.details(nxt.key) or nxt
                after = None
                try:
                    after = client.episode_after(full)
                except Exception:
                    pass
                return full, after

            def ready(res):
                self._preload_pending = None
                full, after = res
                if not self.active or not self.item or self.item.key != cur_key or not self.next_item \
                        or self.next_item.key != nxt.key:
                    return
                url = client.direct_url(full)
                size = full.part.get("size")
                if not url or not size:
                    return
                self.next_item = full
                self._preload_extras = (full.key, full, after)
                pl = prefetch.Preload(full.key, url, int(size), full.media.get("bitrate"))
                prefetch.keep_only(keep)
                prefetch.register(pl)
                self.preload = pl
                pl.start()

            def failed(_e):
                self._preload_pending = None

            run_bg(prepare, ready, failed)
            return
        # The next episode only loads once this one is loaded to the end, so it never takes bandwidth from it.
        if stream_done or self.buffered >= max(left - 2, 0):
            p.allowed.set()
        else:
            p.allowed.clear()

    def _load_to_end(self, left: float):
        want = left + 60
        if want <= self.readahead:
            return
        self.readahead = want
        item = self.item
        kbps = item.media.get("bitrate") if item else None
        size = item.part.get("size") if item else None
        bps = kbps * 1000 / 8 if kbps else (size / self.dur if size and self.dur else 2_500_000)
        m = self.video.mpv
        for k, v in (("demuxer-readahead-secs", str(int(want))), ("cache-secs", str(int(want)))):
            try:
                m[k] = v
            except Exception:
                pass
        try:
            cur_max = int(m["demuxer-max-bytes"] or 0)
        except Exception:
            cur_max = 0
        need = int(bps * want * 1.2)
        if need > cur_max:
            try:
                m["demuxer-max-bytes"] = str(need)
            except Exception:
                pass

    def stop(self):
        if not self.active:
            return
        self.active = False
        log.info(f"STOP at {time_str(self.cur)}")
        for t in (self.poll, self.report_timer, self.retry_timer, self.hide_timer, self.device_timer):
            t.stop()
        item, t, d = self.item, self.cur, self.dur
        try:
            self.video.mpv.command("stop")
        except Exception:
            pass
        self._keep_awake(False)
        self.stats.hide()
        self.item = None
        self.next_item = None
        self.preload = None
        self._preload_extras = None
        prefetch.keep_only(set())
        self.spinner.hide()
        self.up_next.hide()
        self.skip_btn.hide()
        self.toast.hide()
        self.setCursor(Qt.ArrowCursor)
        if self.mode != "full":
            self.mode = "full"
            self.set_mode("full")
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
        log.info("App closing")
        self.video.shutdown()

    def _fatal(self, msg: str):
        log.warn("ERROR shown: " + msg.replace("\n", " "))
        if self.mode == "mini":
            self.set_mode("full")
        self.spinner.hide()
        self.status.hide()
        self.error_lbl.setText(msg)
        self.error_box.show()
        self.error_box.raise_()
        self.show_controls()

    # ------------------------------------------------------------------ previous / next

    def previous(self):
        """More than 5 seconds in: back to the start. Within the first 5 seconds: previous episode."""
        if not self.active or not self.item or self.switching:
            return
        if self.cur > 5 or self.item.type != "episode":
            self.seek(0)
            if self._prop("pause", False):
                self.toggle_pause()
            return
        item = self.item
        self.switching = True
        self.spinner.show()

        def got(prev):
            self.switching = False
            if not self.active:
                return
            if prev:
                self._switch_to(prev)
            else:
                self.spinner.hide()
                self.seek(0)
                self._toast("This is the first episode")
        run_bg(lambda: self.ctx.client.episode_before(item), got, lambda e: setattr(self, "switching", False))

    def next_episode(self):
        if self.active and self.next_item and not self.switching:
            self._switch_to(self.next_item)

    def _switch_to(self, other: Item, mark_current_watched: bool = False):
        """Leave the current episode (saving its spot) and start another one from the beginning."""
        item, t, d = self.item, self.cur, self.dur
        client = self.ctx.client

        def report():
            if mark_current_watched:
                client.report_timeline(item, "stopped", int(d * 1000), int(d * 1000))
                client.mark_watched(item.key)
            else:
                client.report_timeline(item, "stopped", int(t * 1000), int(d * 1000))
        if item:
            run_bg(report, lambda _: self.ctx.data_changed.emit())
        self.spinner.show()
        self._load(other, from_start=True)

    # ------------------------------------------------------------------ skip intro / credits / up next

    def _current_marker(self) -> Optional[dict]:
        for m in self.markers:
            if m["start"] - 0.5 <= self.cur < m["end"] - 1:
                return m
        return None

    def _update_markers(self):
        if self.finishing or self.switching:
            self.skip_btn.hide()
            return
        m = self._current_marker()
        has_next = self.next_item is not None
        # Up Next: when the credits start, or 30 seconds from the end if Plex hasn't marked credits.
        credits = next((x for x in self.markers if x["type"] == "credits" and x["end"] >= self.dur - 15), None)
        start_at = credits["start"] if credits else (self.dur - 30 if self.dur > 180 else None)
        in_up_next = (has_next and not self.up_next_cancelled and start_at is not None
                      and self.cur >= start_at and self.dur - self.cur > 1)
        if in_up_next:
            if self.up_next_deadline is None:
                self._up_next_left = float(UP_NEXT_SECS)
                self.up_next_deadline = time.monotonic() + UP_NEXT_SECS
            if self._prop("pause", False) or self.buffering:
                # don't count down while paused or loading
                self.up_next_deadline = time.monotonic() + self._up_next_left
            left = self.up_next_deadline - time.monotonic()
            self._up_next_left = max(left, 0)
            if self.mode == "mini":
                self.up_next.hide()
            elif not self.up_next.isVisible():
                self.up_next.show_for(self.next_item, int(left + 0.99))
                self._place_floating()
            else:
                self.up_next.set_countdown(int(left + 0.99))
            if left <= 0:
                self._advance(user=False)
                return
        else:
            if (self.up_next.isVisible() or self.up_next_deadline is not None) and \
                    not (start_at is not None and self.cur >= start_at):
                self.up_next.hide()
                self.up_next_deadline = None
                self.up_next_cancelled = False   # they rewound out of the credits

        show_skip = (m is not None and id(m) not in self.skipped_markers and not self.up_next.isVisible()
                     and self.mode == "full")
        if show_skip:
            self.skip_btn.setText("Skip Intro" if m["type"] == "intro" else "Skip Credits")
            self.skip_btn.setProperty("marker", id(m))
            if not self.skip_btn.isVisible():
                self.skip_btn.show()
                self.skip_btn.raise_()
                self._place_floating()
        else:
            self.skip_btn.hide()

    def _skip_marker(self):
        m = self._current_marker()
        if not m:
            return
        self.skipped_markers.add(id(m))
        self.skip_btn.hide()
        if m["type"] == "credits" and m["end"] >= self.dur - 15:
            if self.next_item:
                self._advance(user=True)
            else:
                self.seek(max(self.dur - 2, 0))
        else:
            self.seek(m["end"])

    def _cancel_up_next(self):
        self.up_next_cancelled = True
        self.up_next_deadline = None
        self.up_next.hide()

    def _advance(self, user: bool):
        if not self.next_item or self.switching:
            return
        self.up_next.hide()
        self.up_next_deadline = None
        self._switch_to(self.next_item, mark_current_watched=True)

    # ------------------------------------------------------------------ audio delay (per device) & subtitle timing

    def _delays(self) -> dict:
        return dict(self.ctx.settings["audioDelays"] or {})

    def _device_label(self) -> str:
        if not self.audio_device or self.audio_device == "Default":
            return "your current speakers"
        return self.audio_device

    def _apply_audio_delay(self, force=False):
        name = audiodev.default_output_name() or "Default"
        if name == self.audio_device and not force:
            return
        changed = self.audio_device is not None and name != self.audio_device
        self.audio_device = name
        ms = int(self._delays().get(name, 0))
        try:
            self.video.mpv["audio-delay"] = ms / 1000
        except Exception:
            pass
        if changed:
            log.info(f"Sound output changed to {name}")
        if changed and self.active:
            self._toast(f"Audio: {name}" + (f"  ·  delay {ms:+d} ms" if ms else ""))

    def _check_audio_device(self):
        if self.active:
            self._apply_audio_delay()

    def _show_audio_delay(self):
        self._apply_audio_delay()
        name = self._device_label()

        def changed(ms):
            d = self._delays()
            if ms:
                d[self.audio_device or "Default"] = ms
            else:
                d.pop(self.audio_device or "Default", None)
            self.ctx.settings["audioDelays"] = d
            try:
                self.video.mpv["audio-delay"] = ms / 1000
            except Exception:
                pass

        def describe(ms):
            if ms < 0:
                return "Sound plays earlier"
            if ms > 0:
                return "Sound plays later"
            return "No adjustment"

        pop = AdjustPopup(self, "Audio delay", f"For {name}. Saved for this device and used "
                          "automatically whenever it's connected.",
                          int(self._delays().get(self.audio_device or "Default", 0)),
                          "◀  Sound earlier", "Sound later  ▶", describe, changed)
        self._popup(pop, self.delay_btn)

    def _show_sub_timing(self):
        def changed(ms):
            self.sub_delay_ms = ms
            try:
                self.video.mpv["sub-delay"] = ms / 1000
            except Exception:
                pass

        def describe(ms):
            if ms < 0:
                return "Subtitles show earlier"
            if ms > 0:
                return "Subtitles show later"
            return "No adjustment"

        pop = AdjustPopup(self, "Subtitle timing", "For this video.", self.sub_delay_ms,
                          "◀  Earlier", "Later  ▶", describe, changed, step=100)
        self._popup(pop, self.tracks_btn)

    # ------------------------------------------------------------------ online subtitles

    def _show_sub_search(self):
        if not self.item:
            return
        lang = self.ctx.settings["subSearchLang"] or (locale.getdefaultlocale()[0] or "en")[:2].lower()

        def remember(code):
            if code:
                self.ctx.settings["subSearchLang"] = code

        pop = SubtitleSearchPopup(self, self.ctx.client, self.item.key, lang, self._download_sub, remember)
        self._popup(pop, self.tracks_btn)

    def _download_sub(self, r: dict):
        item = self.item
        if not item:
            return
        key = item.key
        self._toast("Downloading subtitles…", 20000)

        def known():
            full = self.ctx.client.details(key)
            return {s["key"] for s in full.external_subtitles()} if full else set()

        def fetch():
            before = known()
            self.ctx.client.download_subtitle(key, r["key"])
            # The server fetches it in the background; wait for it to show up with the video.
            for _ in range(20):
                time.sleep(1.5)
                full = self.ctx.client.details(key)
                if not full:
                    continue
                new = [s for s in full.external_subtitles() if s["key"] not in before]
                if new:
                    return new[-1]
            return None

        def done(sub):
            if not self.item or self.item.key != key:
                return
            if not sub:
                self._toast("The server didn't get those subtitles. Try another one.")
                return
            url = self.ctx.client.stream_url(sub["key"])
            self._user_picked_sub = True
            try:
                self.video.mpv.command_async("sub-add", url, "select", r.get("title") or sub["title"], sub["lang"])
                self.video.mpv["sub-visibility"] = True
                self._toast(f"Subtitles: {r.get('title') or sub['title']}")
            except Exception as e:  # noqa: BLE001
                self._toast(f"Couldn't load them: {e}")

        def failed(e):
            if self.item and self.item.key == key:
                self._toast(f"Couldn't download: {e}")

        run_bg(fetch, done, failed)

    # ------------------------------------------------------------------ zoom (+ / - keys)

    def _zoom_levels(self) -> list[float]:
        fill = 1.0
        try:
            vw, vh = self.video.mpv["dwidth"], self.video.mpv["dheight"]
            ww, wh = max(self.video.width(), 1), max(self.video.height(), 1)
            if vw and vh:
                va, wa = vw / vh, ww / wh
                fill = max(va / wa, wa / va)
        except Exception:
            pass
        steps = [1, 1 + (fill - 1) / 3, 1 + 2 * (fill - 1) / 3, fill] if fill > 1.02 else [1, 1.1, 1.2, 1.33]
        z = steps[-1]
        while z * 1.25 <= 4.001:
            z *= 1.25
            steps.append(z)
        if steps[-1] < 4:
            steps.append(4.0)
        return steps

    def zoom(self, direction: int):
        levels = self._zoom_levels()
        cur = self.zoom_scale
        if direction == 0:
            new = 1.0
        elif direction > 0:
            # Past the last step: back to the original size.
            new = next((z for z in levels if z > cur + 0.005), 1.0)
        else:
            new = next((z for z in reversed(levels) if z < cur - 0.005), 1.0)
        self.zoom_scale = new
        try:
            import math
            self.video.mpv["video-zoom"] = math.log2(new)
            self.video.mpv["video-pan-x"] = 0
            self.video.mpv["video-pan-y"] = 0
        except Exception:
            pass

    def _reset_zoom_silently(self):
        self.zoom_scale = 1.0
        try:
            self.video.mpv["video-zoom"] = 0
            self.video.mpv["video-pan-x"] = 0
            self.video.mpv["video-pan-y"] = 0
        except Exception:
            pass

    def _popup(self, pop: QWidget, anchor: QWidget):
        self.hide_timer.stop()
        pop.adjustSize()
        g = anchor.mapToGlobal(anchor.rect().topLeft())
        x = min(g.x() + anchor.width() - pop.width(), g.x())
        pop.move(max(x, 8), g.y() - pop.height() - 10)
        pop.destroyed.connect(lambda *_: self.show_controls())
        pop.setAttribute(Qt.WA_DeleteOnClose)
        pop.show()

    def _toast(self, text: str, ms: int = 3500):
        if self.mode == "mini":
            return
        self.toast.setText(text)
        self.toast.show()
        self.toast.raise_()
        self._place_floating()
        self.toast_timer.start(ms)

    # ------------------------------------------------------------------ mpv events / polling

    def _mpv_event(self, eid: int, reason: int):
        if not self.active:
            return
        if eid == mpv.MpvEventID.END_FILE and reason == END_ERROR:
            # Couldn't open/read (no signal, server hiccup). Try again shortly.
            self._schedule_retry()
        elif eid == mpv.MpvEventID.FILE_LOADED:
            self.file_loaded = True
            self._add_external_subs()
            if self.sub_delay_ms:
                try:
                    self.video.mpv["sub-delay"] = self.sub_delay_ms / 1000
                except Exception:
                    pass
        elif eid == mpv.MpvEventID.PLAYBACK_RESTART:
            self.reloading = False

    # ------------------------------------------------------------------ reconnecting
    # When the video can't be read (connection dropped, server hiccup, network outage) or playback freezes,
    # Ice reopens the stream itself at the spot where it stopped, directly from the Plex server, and keeps
    # trying (3 s, 5 s, 10 s, 20 s, then every 30 s) until it plays again. Same as closing the player and
    # pressing Resume, only automatic. Every step goes in the log, with the player's state, so we can see why.

    RETRY_WAITS = [3, 5, 10, 20, 30]
    FROZEN_WITH_DATA = 8.0     # not paused, video downloaded ahead, yet the picture doesn't move
    NO_DATA = 12.0             # waiting for video and nothing has arrived for this long
    SLOW_LIMIT = 90.0          # waiting for video this long even though bits trickle in

    def _schedule_retry(self, why: str = "couldn't read the video"):
        """Reopen at the current spot after a short wait (longer each time it fails)."""
        if self.retry_timer.isActive():
            return
        if not self.has_played:
            self.failures += 1
            if self.failures >= 4:
                self._fatal("This video couldn't be played.\nCheck your connection to the Plex server.")
                return
        wait = self.RETRY_WAITS[min(self._reconnects, len(self.RETRY_WAITS) - 1)]
        self._reconnects += 1
        self._stuck_since = self._stuck_since or time.monotonic()
        log.warn(f"RECONNECT #{self._reconnects} in {wait} s: {why} at {time_str(self.cur)}. {self._state_line()}")
        self.reloading = True
        self.retry_timer.start(wait * 1000)

    def _retry(self):
        if self.active and self.item:
            log.info(f"Reopening the stream at {time_str(self.cur)} (straight from the server, {self.ctx.client.address_line()})")
            self._last_reopen = time.monotonic()
            self._open(self.cur, direct=True)

    def _stall_reset(self):
        self._stall_pos = None
        self._stall_since = time.monotonic()
        self._reconnects = 0
        self._stuck_since = None
        self._last_reopen = 0.0

    def _watch_stall(self, t, paused, seeking, now):
        if t is not None and (not self.reloading or self.retry_timer.isActive()) and (self._stall_pos is None or abs(float(t) - self._stall_pos) > 0.05):
            if self._stall_pos is not None and self._stuck_since:
                log.info(f"Playing again at {time_str(float(t))} after {now - self._stuck_since:.0f} s"
                         + (f" and {self._reconnects} reconnect(s)" if self._reconnects else ""))
            if self.retry_timer.isActive():
                self.retry_timer.stop()      # it came back by itself: no need to reopen
                self.reloading = False
            self._stall_pos = float(t)
            self._stall_since = now
            self._reconnects = 0
            self._stuck_since = None
            return
        should_play = (self.has_played and not paused and not self.switching and not self.finishing
                       and not self.dragging and self.item is not None)
        if not should_play:
            self._stall_since = now
            return
        if self.retry_timer.isActive():
            return      # a reconnect is already on its way
        stuck = now - self._stall_since
        since_reopen = now - self._last_reopen
        no_data = now - self.last_data
        if self.reloading and since_reopen < 25:
            return      # give the reopen time to connect
        if self.reloading:
            self._schedule_retry("reopening hasn't started playing")
        elif self.buffered >= 3 and stuck > self.FROZEN_WITH_DATA:
            self._schedule_retry(f"FROZEN {stuck:.0f} s with {self.buffered:.0f} s of video downloaded")
        elif self.buffered < 3 and stuck > self.NO_DATA and no_data > self.NO_DATA:
            self._schedule_retry(f"no video data for {no_data:.0f} s (connection lost?)")
        elif self.buffered < 3 and stuck > self.SLOW_LIMIT:
            self._schedule_retry(f"waiting {stuck:.0f} s for video on a slow connection")

    # ------------------------------------------------------------------ log helpers

    def _state_line(self) -> str:
        g = self._prop
        cache = g("demuxer-cache-state", {}) or {}
        return (f"[pos {time_str(self.cur)} · loaded {self.buffered:.0f}s · "
                f"dl {(g('cache-speed') or 0) * 8 / 1e6:.1f} Mbps · paused {g('pause')} · waiting-for-data {g('paused-for-cache')} · "
                f"idle {g('core-idle')} · seeking {g('seeking')} · eof {g('eof-reached')} · cache-eof {cache.get('eof')} · "
                f"underrun {cache.get('underrun')} · audio {g('audio-codec-name')}/{self.audio_device} · "
                f"offline {self.offline}]")

    def _frames_line(self) -> str:
        g = self._prop
        scr = self.screen() or QApplication.primaryScreen()
        win = self.window()
        return (f"[video {g('container-fps') or 0:.3f} fps · screen {scr.refreshRate() if scr else 0:.2f} Hz "
                f"({scr.name() if scr else '?'}) · mpv display {g('display-fps') or 0:.2f} Hz · "
                f"dropped {g('frame-drop-count') or 0} · decoder dropped {g('decoder-frame-drop-count') or 0} · "
                f"delayed {g('vo-delayed-frame-count') or 0} · mistimed {g('mistimed-frame-count') or 0} · "
                f"av {(g('avsync') or 0) * 1000:+.0f} ms · decoder {g('hwdec-current') or 'software'} · "
                f"sync {g('video-sync')} · {'full screen' if win and win.isFullScreen() else 'window'} "
                f"{self.video.width()}x{self.video.height()} · gpu {getattr(self.video, 'renderer', '') or '?'}]")

    def _log_tick(self, paused, pfc, buffering, now):
        st = (bool(paused), bool(pfc), bool(buffering), bool(self.offline), self.mode)
        if st != self._log_state:
            if self._log_state is not None or st[2]:
                what = []
                if st[0]:
                    what.append("paused")
                if st[1]:
                    what.append("waiting for data")
                elif st[2]:
                    what.append("loading")
                if st[3]:
                    what.append("NO SIGNAL")
                log.info(f"state: {', '.join(what) or 'playing'} ({self.mode}) {self._state_line()}")
            self._log_state = st
        if self.has_played and now >= self._beat_at:
            self._beat_at = now + float(os.environ.get("ICE_BEAT_SECS", 60))
            drops = int(self._prop("frame-drop-count") or 0) + int(self._prop("vo-delayed-frame-count") or 0)
            log.info(f"status {self._state_line()} {self._frames_line()}"
                     + (f"  ← {drops - self._last_drops} more dropped/late frames this minute" if drops - self._last_drops > 5 else ""))
            self._last_drops = drops

    def _prop(self, name, default=None):
        try:
            v = self.video.mpv._get_property(name)
            return default if v is None else v
        except Exception:
            return default

    def _tick(self):
        if not self.active or not self.item:
            return
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

        self._watch_stall(t, paused, seeking, now)
        playing_now = t is not None and not paused and not idle and not pfc
        # Keep the screen on (no screen saver / display sleep) while a video plays; let it sleep when paused.
        self._keep_awake(not paused)
        if self.stats.isVisible():
            self._update_stats()
        if playing_now and not seeking:
            if not self.has_played:
                self.has_played = True
                log.info(f"Started playing at {time_str(self.cur)} {self._frames_line()}")
                self._add_external_subs()
            self.failures = 0

        buffering = (pfc or seeking or (not paused and idle and not eof) or self.reloading or t is None
                     or self.switching)
        self.spinner.setVisible(buffering)
        if buffering:
            self.spinner.raise_()
        self.buffering = buffering
        self._log_tick(paused, pfc, buffering, now)

        if (self.offline or self._reconnects) and buffering:
            self.status.setText("No signal — will pick up where it left off" if not self._reconnects else
                                f"Reconnecting… (try {self._reconnects}) — will pick up where it left off")
            self.status.show()
            self.signal_pill.hide()
        elif self.offline:
            self.status.hide()
            self.signal_pill.setText(f"No signal · playing from {time_str(self.buffered)} saved")
            self.signal_pill.show()
        else:
            self.status.hide()
            self.signal_pill.hide()
        if self.mode == "mini":
            self.status.hide()
            self.signal_pill.hide()

        # Reached the end of what was downloaded before the real end (connection gave out).
        if eof and not self.finishing:
            if self.dur and self.cur < self.dur - 20:
                if not self.reloading:
                    self._schedule_retry()
            else:
                self._finished()
                return

        self._keep_subs_off()
        self._prefer_english_audio()
        self._preload_next(stream_done)
        self._update_markers()
        self._set_play_icon(not paused, buffering)
        if not self.dragging:
            self.scrub.set_state(self.cur, self.buffered, self.dur)
        loaded = f"   ·   {time_str(self.buffered)} loaded" if self.buffered > 1 else ""
        self.time_lbl.setText(f"{time_str(self.cur)} / {time_str(self.dur)}{loaded}")
        self._place_floating()

    def _report(self):
        if not self.active or not self.item or self.offline:
            return
        item, t, d = self.item, self.cur, self.dur
        st = "paused" if self._prop("pause", False) else "playing"
        run_bg(lambda: self.ctx.client.report_timeline(item, st, int(t * 1000), int(d * 1000)))

    def _finished(self):
        """Reached the real end of the file."""
        self.finishing = True
        item, d = self.item, self.dur
        client = self.ctx.client
        go_next = self.next_item if not self.up_next_cancelled else None
        self.up_next.hide()

        def wrap_up():
            client.report_timeline(item, "stopped", int(d * 1000), int(d * 1000))
            client.mark_watched(item.key)
            if go_next is None and not self.up_next_cancelled:
                return client.episode_after(item)
            return go_next

        def next_up(nxt):
            if not self.active:
                return
            self.ctx.data_changed.emit()
            if nxt:
                self.spinner.show()
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
        self.user_paused = bool(m.pause)
        log.info(f"You {'paused' if m.pause else 'pressed play'} at {time_str(self.cur)}")
        self._set_play_icon(not m.pause)
        self._report()
        self.show_controls()

    def skip(self, secs: float):
        self.seek(self.cur + secs)

    def seek(self, t: float):
        if not self.active or not self.item:
            return
        t = max(0.0, min(t, self.dur - 2 if self.dur else t))
        log.info(f"Seek {time_str(self.cur)} → {time_str(t)}")
        self.cur = t
        try:
            self.video.mpv.command("seek", f"{t:.3f}", "absolute+exact")
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
        name = "pause" if playing else "play"
        if getattr(self, "_icon_state", None) != name:
            self._icon_state = name
            self.play_btn.setIcon(theme.icon(name, "white", 30))

    def set_fullscreen_icon(self, fs: bool):
        self.fs_btn.setIcon(theme.icon("unfullscreen" if fs else "fullscreen", "white", 22))

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
            if t.get("external"):
                bits.append("File")
            seen, out = set(), []
            for b in bits:
                if b and b.lower() not in seen:
                    seen.add(b.lower())
                    out.append(b)
            return "  ·  ".join(out) or f"Track {t.get('id')}"

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
            menu.addSeparator()
        add_section("Subtitles", subs, "sid", True)
        menu.addSeparator()
        find = menu.addAction("Search for subtitles…")
        find.triggered.connect(lambda: QTimer.singleShot(0, self._show_sub_search))
        timing = menu.addAction("Subtitle timing…" + (f"  ({self.sub_delay_ms:+d} ms)" if self.sub_delay_ms else ""))
        timing.triggered.connect(lambda: QTimer.singleShot(0, self._show_sub_timing))
        self.hide_timer.stop()
        menu.adjustSize()
        g = self.tracks_btn.mapToGlobal(self.tracks_btn.rect().topLeft())
        menu.exec(g - QPoint(menu.sizeHint().width() - self.tracks_btn.width(), menu.sizeHint().height() + 10))
        self.show_controls()

    _awake = False

    def _keep_awake(self, on: bool):
        """Windows: stop the display from sleeping / the screen saver while a video is playing."""
        if sys.platform != "win32" or on == PlayerPage._awake:
            return
        PlayerPage._awake = on
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0))
        except Exception:
            pass

    @staticmethod
    def video_playing_now() -> bool:
        return PlayerPage._awake

    def _update_stats(self):
        """Playback info (press I): what the player is doing, for tracking down stutters."""
        g = lambda k, d=None: self._prop(k, d)  # noqa: E731
        vp = g("video-params", {}) or {}
        out = g("video-out-params", {}) or {}
        fps = g("container-fps") or 0
        scr = self.screen() or QApplication.primaryScreen()
        disp = scr.refreshRate() if scr else 0
        cache = g("demuxer-cache-state", {}) or {}
        speed = (g("cache-speed") or 0) * 8 / 1e6
        lines = [
            f"Video  {vp.get('w', '?')}×{vp.get('h', '?')}  {g('video-codec') or g('video-format') or '?'}  {fps:.3f} fps",
            f"Audio  {g('audio-codec-name') or '?'}  {g('audio-params/channel-count') or '?'} ch",
            f"Decoder  {'hardware (' + str(g('hwdec-current')) + ')' if g('hwdec-current') not in (None, '', 'no') else 'software'}",
            f"Display  {disp:.2f} Hz  ·  output {out.get('w', '?')}×{out.get('h', '?')}",
            f"Dropped frames  {g('frame-drop-count') or 0} (player)  ·  {g('decoder-frame-drop-count') or 0} (decoder)",
            f"Delayed frames  {g('vo-delayed-frame-count') or 0}",
            f"Loaded ahead  {time_str(self.buffered)}  ·  {speed:.1f} Mbps download",
            f"Video bitrate  {(g('video-bitrate') or 0) / 1e6:.1f} Mbps",
            f"Graphics  {getattr(self.video, 'renderer', '') or '?'}",
            "Press I to hide",
        ]
        self.stats.setText("\n".join(lines))
        self.stats.adjustSize()
        self.stats.move(24, 24)

    def _keep_subs_off(self):
        """Subtitles stay off unless you turn one on in Ice. (Files can mark a track as default, and the
        player would show it; Plex doesn't.) Checked on every tick, so nothing can switch one on behind us."""
        if self._user_picked_sub or not self.file_loaded:
            return
        try:
            if self.video.mpv["sid"] not in (False, None, "no"):
                self.video.mpv["sid"] = "no"
        except Exception:
            pass

    def _reopen_sid(self) -> str:
        if not self.file_loaded and not self.has_played:
            return "no"
        try:
            cur = self.video.mpv["sid"]
        except Exception:
            cur = None
        if isinstance(cur, int) and cur <= getattr(self, "_embedded_subs", 0):
            return str(cur)
        return "no"

    def _prefer_english_audio(self):
        """Backup for alang: for the first seconds of a video, switch to an English audio track if the file
        started on another language and has one (unless you picked a track yourself)."""
        if self._user_picked_audio or not self.file_loaded or self._audio_checked_until < time.monotonic():
            return
        try:
            tracks = [t for t in self.video.mpv.track_list if t.get("type") == "audio"]
        except Exception:
            return
        def english(t):
            lang = (t.get("lang") or "").lower()
            title = (t.get("title") or "").lower()
            return lang in ("en", "eng", "english") or (not lang and "english" in title)
        cur = next((t for t in tracks if t.get("selected")), None)
        if cur is None or english(cur):
            return
        en = next((t for t in tracks if english(t) and "commentary" not in (t.get("title") or "").lower()), None)
        if en:
            try:
                self.video.mpv["aid"] = en["id"]
            except Exception:
                pass

    def _set_track(self, prop, value):
        if prop == "aid":
            self._user_picked_audio = True
        if prop == "sid":
            self._user_picked_sub = True
        try:
            self.video.mpv[prop] = value
        except Exception:
            pass

    # ------------------------------------------------------------------ show / hide controls

    def show_controls(self):
        if not self.controls_shown:
            self.controls_shown = True
            for w in self.controls:
                if w is self.close_btn and self.mode == "mini":
                    continue
                w.show()
                w.raise_()
            for w in (self.skip_btn, self.up_next, self.spinner, self.status, self.signal_pill, self.toast,
                      self.error_box):
                w.raise_()
            self._place_floating()
        if self.mode == "mini":
            # The picture sits on top of the bar in the mini player (so it's clickable and not dimmed).
            self.video.raise_()
            self.spinner.raise_()
        self.unsetCursor()
        self.video.unsetCursor()
        self.hide_timer.start(self.HIDE_AFTER_MS)

    def hide_controls(self):
        if self.error_box.isVisible() or self.mode == "mini":
            return
        self.hide_timer.stop()
        self.controls_shown = False
        for w in self.controls:
            w.hide()
        self._hide_hint()
        self.scrub.tip.hide()
        self.setCursor(Qt.BlankCursor)
        self.video.setCursor(Qt.BlankCursor)
        self._place_floating()

    def _auto_hide(self):
        if not self.active or self.dragging or self.error_box.isVisible() or self.mode == "mini":
            return
        if self._prop("pause", False):
            return
        over = QApplication.widgetAt(QCursor.pos())
        if over is not None and over is not self.video and any(
                over is c or c.isAncestorOf(over) for c in (self.bar, self.scrub, self.close_btn)):
            self.hide_timer.start(self.HIDE_AFTER_MS)
            return
        self.hide_controls()

    # ------------------------------------------------------------------ input

    def _single_click(self):
        """One click on the picture: pause/play if that's turned on in Settings, else show/hide the bar."""
        if self.ctx.settings["tapToPause"]:
            self.toggle_pause()
        elif self.controls_shown:
            self.hide_controls()
        else:
            self.show_controls()

    def _show_hint(self, w: QWidget):
        text = w.toolTip()
        if not text or not w.isVisible():
            return
        self._hint_for = w
        self.hint.setText(text)
        self.hint.adjustSize()
        top_left = w.mapTo(self, QPoint(0, 0))
        x = top_left.x() + (w.width() - self.hint.width()) // 2
        x = max(8, min(x, self.width() - self.hint.width() - 8))
        y = top_left.y() - self.hint.height() - 8
        if y < 8:
            y = top_left.y() + w.height() + 8
        self.hint.move(x, y)
        self.hint.show()
        self.hint.raise_()

    def _hide_hint(self, w=None):
        if w is None or w is self._hint_for:
            self.hint.hide()
            self._hint_for = None

    def eventFilter(self, obj, e):
        t0 = e.type()
        if t0 == QEvent.ToolTip and obj is not self.video:
            # Never let a native tooltip window appear over the video.
            if isinstance(obj, QWidget):
                self._show_hint(obj)
            return True
        if t0 in (QEvent.Leave, QEvent.Hide, QEvent.MouseButtonPress) and obj is self._hint_for:
            self._hide_hint(obj)
        if obj not in (self.video, self.info):
            return False
        if self.mode == "mini" and obj in (self.video, self.info):
            # Mini player: click the picture or the title to open the full player.
            if e.type() == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
                self.expand()
                return True
            if e.type() == QEvent.MouseButtonDblClick:
                return True
            if obj is self.video and e.type() == QEvent.Wheel:
                self.change_volume(5 if e.angleDelta().y() > 0 else -5)
                return True
            return False
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
        shift = bool(e.modifiers() & Qt.ShiftModifier)
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
        elif (k == Qt.Key_N and shift) or k == Qt.Key_MediaNext:
            self.next_episode()
        elif (k == Qt.Key_P and shift) or k == Qt.Key_MediaPrevious:
            self.previous()
        elif k == Qt.Key_S and self.skip_btn.isVisible():
            self._skip_marker()
        elif k in (Qt.Key_F, Qt.Key_F11):
            self.toggle_fullscreen.emit()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(1)
        elif k in (Qt.Key_Minus, Qt.Key_Underscore):
            self.zoom(-1)
        elif k == Qt.Key_0:
            self.zoom(0)
        elif k == Qt.Key_I:
            self.stats.setVisible(not self.stats.isVisible())
            if self.stats.isVisible():
                self._update_stats()
                self.stats.raise_()
        elif k in (Qt.Key_Escape, Qt.Key_Back):
            # Pause and drop to the mini player (it leaves full screen too).
            if self.mode == "full":
                self.minimize()
        elif k == Qt.Key_MediaStop:
            self.stop()
        else:
            super().keyPressEvent(e)
