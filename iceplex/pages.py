"""All the screens except the player."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import theme
from .config import BUFFER_CHOICES
from .plex import Item
from .util import run_bg, runtime_str, size_str
from .widgets import (Backdrop, CardRow, ClickRow, FlowLayout, ImageBox, PosterCard, ScrollPage, Spinner,
                      centered, label)


def resource_path(name: str) -> str:
    import os
    import sys
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, "assets", name)


def primary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("Primary")
    b.setCursor(Qt.PointingHandCursor)
    return b


def button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    return b


def loading_widget(text: str = "") -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 80, 0, 80)
    v.addWidget(centered(Spinner(44)))
    if text:
        l = label(text, "Muted")
        l.setAlignment(Qt.AlignCenter)
        v.addWidget(l)
    return w


def message_widget(text: str) -> QWidget:
    l = label(text, "Muted", wrap=True)
    l.setAlignment(Qt.AlignCenter)
    l.setContentsMargins(32, 60, 32, 60)
    return l


# ===========================================================================
# Sign in
# ===========================================================================

class SignInPage(QWidget):
    def __init__(self, ctx, on_done):
        super().__init__()
        self.ctx, self.on_done = ctx, on_done
        self.pin = None
        self.deadline = 0
        v = QVBoxLayout(self)
        v.addStretch(2)
        logo = QLabel()
        pix = QPixmap(resource_path("logo.png"))
        if not pix.isNull():
            from .widgets import rounded
            logo.setPixmap(rounded(pix, 140, 140, 30, self.devicePixelRatioF()))
        logo.setAlignment(Qt.AlignCenter)
        v.addWidget(logo)
        t = label("Ice", "H1")
        t.setAlignment(Qt.AlignCenter)
        v.addWidget(t)
        self.info = label("Sign in with your Plex account. Your browser will open — approve the sign-in, "
                          "then come back here.", "Muted", wrap=True)
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setMaximumWidth(460)
        v.addWidget(centered(self.info))
        self.btn = primary("Sign in with Plex")
        self.btn.setMinimumWidth(260)
        self.btn.clicked.connect(self.start)
        v.addSpacing(10)
        v.addWidget(centered(self.btn))
        self.spinner = Spinner(28)
        self.spinner.hide()
        v.addWidget(centered(self.spinner))
        self.err = label("", wrap=True)
        self.err.setStyleSheet("color: #ff6b6b;")
        self.err.setAlignment(Qt.AlignCenter)
        v.addWidget(self.err)
        v.addStretch(3)
        self.poll = QTimer(self)
        self.poll.setInterval(2000)
        self.poll.timeout.connect(self._check)

    def start(self):
        self.err.setText("")
        self.btn.setEnabled(False)
        self.btn.setText("Waiting for Plex…")
        self.spinner.show()
        run_bg(self.ctx.client.create_pin, self._got_pin, self._fail)

    def _got_pin(self, pin):
        self.pin = pin
        self.deadline = time.time() + 300
        QDesktopServices.openUrl(QUrl(self.ctx.client.auth_url(pin)))
        self.poll.start()

    def _check(self):
        if not self.pin:
            return
        if time.time() > self.deadline:
            self.poll.stop()
            self._fail(Exception("Sign-in timed out. Try again."))
            return
        pid = self.pin["id"]
        run_bg(lambda: self.ctx.client.check_pin(pid), self._checked)

    def _checked(self, pin):
        tok = pin.get("authToken")
        if tok and self.poll.isActive():
            self.poll.stop()
            self.ctx.set_token(tok)
            self.on_done()

    def _fail(self, e):
        self.poll.stop()
        self.spinner.hide()
        self.btn.setEnabled(True)
        self.btn.setText("Sign in with Plex")
        self.err.setText(str(e))


# ===========================================================================
# Server picker
# ===========================================================================

class ServerPage(ScrollPage):
    def __init__(self, ctx, on_done):
        super().__init__()
        self.ctx, self.on_done = ctx, on_done
        self.body.setContentsMargins(0, 60, 0, 40)
        self.load()

    def load(self):
        self.clear()
        t = label("Choose your server", "H1")
        t.setAlignment(Qt.AlignCenter)
        self.body.addWidget(t)
        self.body.addWidget(loading_widget("Finding your servers…"))
        self.body.addStretch()
        run_bg(self.ctx.client.servers, self._show, self._error)

    def _error(self, e):
        self.clear()
        self.body.addWidget(message_widget(f"Couldn't get your servers: {e}"))
        row = QHBoxLayout()
        row.addStretch()
        retry = button("Try again")
        retry.clicked.connect(self.load)
        out = button("Sign out")
        out.clicked.connect(self.ctx.sign_out)
        row.addWidget(retry)
        row.addWidget(out)
        row.addStretch()
        self.body.addLayout(row)
        self.body.addStretch()

    def _show(self, servers):
        self.clear()
        t = label("Choose your server", "H1")
        t.setAlignment(Qt.AlignCenter)
        self.body.addWidget(t)
        box = QWidget()
        box.setMaximumWidth(560)
        v = QVBoxLayout(box)
        v.setSpacing(10)
        if not servers:
            v.addWidget(message_widget("No servers found on this Plex account."))
        for s in servers:
            row = ClickRow()
            h = QHBoxLayout(row)
            h.setContentsMargins(18, 14, 18, 14)
            col = QVBoxLayout()
            col.addWidget(label(s.get("name", "Server"), "H3"))
            col.addWidget(label("Your server" if s.get("owned") else "Shared with you", "Muted"))
            h.addLayout(col)
            h.addStretch()
            spin = Spinner(22)
            spin.hide()
            h.addWidget(spin)
            row.clicked.connect(lambda s=s, sp=spin: self._connect(s, sp))
            v.addWidget(row)
        self.err = label("", wrap=True)
        self.err.setStyleSheet("color: #ff6b6b;")
        v.addWidget(self.err)
        out = button("Sign out")
        out.clicked.connect(self.ctx.sign_out)
        v.addWidget(out, 0, Qt.AlignLeft)
        self.body.addWidget(centered(box))
        self.body.addStretch()

    def _connect(self, server, spin):
        spin.show()
        self.err.setText("")

        def done(saved):
            spin.hide()
            self.ctx.set_server(saved)
            self.on_done()

        def fail(e):
            spin.hide()
            self.err.setText(str(e))

        run_bg(lambda: self.ctx.client.connect(server), done, fail)


# ===========================================================================
# Home
# ===========================================================================

class HomePage(ScrollPage):
    title = "Home"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.body.setContentsMargins(0, 28, 0, 40)
        self._gen = 0
        self.refresh()

    def refresh(self):
        self._gen += 1
        gen = self._gen
        if self.body.count() == 0:
            self.body.addWidget(loading_widget())

        def fetch():
            c = self.ctx.client
            sections = c.sections()
            cw = c.continue_watching()
            excluded = set(self.ctx.settings["excludedSections"] or [])
            recent = []
            for s in sections:
                if s["key"] in excluded:
                    continue
                try:
                    recent.append((s, c.recently_added(s["key"])))
                except Exception:
                    recent.append((s, []))
            return sections, cw, recent

        run_bg(fetch, lambda r: self._show(gen, *r), lambda e: self._error(gen, e))

    def _error(self, gen, e):
        if gen != self._gen:
            return
        self.clear()
        self.body.addWidget(message_widget(f"Couldn't load from your server.\n{e}"))
        b = button("Try again")
        b.clicked.connect(self.refresh)
        self.body.addWidget(centered(b))
        self.body.addStretch()

    def _show(self, gen, sections, cw, recent):
        if gen != self._gen:
            return
        self.ctx.sections = sections
        sb = self.verticalScrollBar().value()
        self.clear()
        head = label(self.ctx.server_name(), "H1")
        head.setContentsMargins(32, 0, 0, 0)
        self.body.addWidget(head)
        cw = [i for i in cw if not self.ctx.is_excluded(i)]
        if cw:
            self.body.addWidget(self._row("Continue Watching", cw, 190))
        else:
            self.body.addWidget(self.padded(label("Nothing in progress.", "Muted")))
        for s, items in recent:
            if items:
                self.body.addWidget(self._row(f"Recently Added · {s['title']}", items, 160))
        self.body.addStretch()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(sb))

    def _row(self, title, items, width):
        cards = []
        for it in items:
            c = PosterCard(it, self.ctx.client, width)
            c.clicked.connect(self.ctx.open)
            cards.append(c)
        return CardRow(title, cards)


# ===========================================================================
# Libraries
# ===========================================================================

class LibrariesPage(ScrollPage):
    title = "Libraries"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.body.setContentsMargins(32, 28, 32, 40)
        self.refresh()

    def refresh(self):
        def fetch():
            return self.ctx.client.sections()
        if not self.ctx.sections:
            self.clear()
            self.body.addWidget(loading_widget())
        run_bg(fetch, self._show, lambda e: None)

    def _show(self, sections):
        self.ctx.sections = sections
        self.clear()
        self.body.addWidget(label("Libraries", "H1"))
        vis = [s for s in sections if s["key"] not in set(self.ctx.settings["excludedSections"] or [])]
        if not vis:
            self.body.addWidget(message_widget("All libraries are hidden. Change that in Settings."))
        grid = QWidget()
        flow = FlowLayout(grid, spacing=16)
        for s in vis:
            row = ClickRow()
            row.setFixedSize(300, 110)
            v = QVBoxLayout(row)
            v.setContentsMargins(22, 16, 22, 16)
            kind = "TV Shows" if s.get("type") == "show" else "Movies"
            v.addWidget(label(s["title"], "H2"))
            v.addWidget(label(kind, "Muted"))
            row.clicked.connect(lambda s=s: self.ctx.push(LibraryGridPage(self.ctx, s)))
            flow.addWidget(row)
        self.body.addWidget(grid)
        self.body.addStretch()


class LibraryGridPage(ScrollPage):
    SORTS = [("Title", "titleSort"), ("Recently added", "addedAt:desc"),
             ("Release date", "originallyAvailableAt:desc"), ("Recently watched", "lastViewedAt:desc")]
    PAGE = 60

    def __init__(self, ctx, section: dict):
        super().__init__()
        self.ctx, self.section = ctx, section
        self.title = section["title"]
        self.body.setContentsMargins(32, 76, 32, 40)   # room for the back button
        self.body.setSpacing(20)
        head = QHBoxLayout()
        head.addWidget(label(section["title"], "H1"))
        head.addStretch()
        self.sort = QComboBox()
        for name, _ in self.SORTS:
            self.sort.addItem(name)
        self.unwatched = QCheckBox("Unwatched only")
        head.addWidget(self.unwatched)
        head.addSpacing(16)
        head.addWidget(self.sort)
        self.body.addLayout(head)
        self.grid = QWidget()
        self.flow = FlowLayout(self.grid, spacing=20)
        self.body.addWidget(self.grid)
        self.spinner = Spinner(36)
        self.body.addWidget(centered(self.spinner))
        self.body.addStretch()
        self.items: list[Item] = []
        self.total: Optional[int] = None
        self.loading = False
        self._gen = 0
        self.sort.currentIndexChanged.connect(self.reload)
        self.unwatched.toggled.connect(self.reload)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.reload()

    def reload(self):
        self._gen += 1
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.items, self.total, self.loading = [], None, False
        self.load_more()

    def refresh(self):
        # Keep the scroll position; just update progress/checkmarks by reloading the loaded pages.
        pass

    def _scrolled(self, v):
        bar = self.verticalScrollBar()
        if v > bar.maximum() - 900:
            self.load_more()

    def load_more(self):
        if self.loading or (self.total is not None and len(self.items) >= self.total):
            return
        self.loading = True
        self.spinner.show()
        gen = self._gen
        sort = self.SORTS[self.sort.currentIndex()][1]
        unwatched = self.unwatched.isChecked()
        start = len(self.items)
        key = self.section["key"]
        run_bg(lambda: self.ctx.client.items(key, sort, unwatched, start, self.PAGE),
               lambda r: self._got(gen, *r), lambda e: self._got(gen, [], len(self.items)))

    def _got(self, gen, items, total):
        if gen != self._gen:
            return
        self.loading = False
        self.spinner.hide()
        self.items.extend(items)
        self.total = total if total is not None else (len(self.items) if len(items) < self.PAGE else None)
        for it in items:
            c = PosterCard(it, self.ctx.client, 160)
            c.clicked.connect(self.ctx.open)
            self.flow.addWidget(c)
        self.grid.updateGeometry()
        # Fill the screen if the first page didn't.
        QTimer.singleShot(50, lambda: self._scrolled(self.verticalScrollBar().value()))


# ===========================================================================
# Search
# ===========================================================================

class SearchPage(ScrollPage):
    title = "Search"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.body.setContentsMargins(32, 28, 32, 40)
        self.body.addWidget(label("Search", "H1"))
        self.box = QLineEdit()
        self.box.setPlaceholderText("Movies, shows and episodes")
        self.box.setClearButtonEnabled(True)
        self.box.setMaximumWidth(640)
        self.body.addWidget(self.box)
        self.status = QWidget()
        sl = QHBoxLayout(self.status)
        sl.setContentsMargins(0, 0, 0, 0)
        self.spinner = Spinner(28)
        sl.addWidget(self.spinner)
        self.msg = label("", "Muted")
        sl.addWidget(self.msg)
        sl.addStretch()
        self.spinner.hide()
        self.body.addWidget(self.status)
        self.grid = QWidget()
        self.flow = FlowLayout(self.grid, spacing=20)
        self.body.addWidget(self.grid)
        self.body.addStretch()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.search)
        self.box.textChanged.connect(lambda _: self.timer.start())
        self._gen = 0

    def focus(self):
        self.box.setFocus()

    def search(self):
        text = self.box.text().strip()
        self._gen += 1
        gen = self._gen
        if len(text) < 2:
            self._fill([])
            self.msg.setText("")
            return
        self.spinner.show()
        self.msg.setText("")
        run_bg(lambda: self.ctx.client.search(text), lambda r: self._got(gen, r), lambda e: self._got(gen, []))

    def _got(self, gen, items):
        if gen != self._gen:
            return
        self.spinner.hide()
        items = [i for i in items if not self.ctx.is_excluded(i)]
        self.msg.setText("" if items else "No results")
        self._fill(items)

    def _fill(self, items):
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for it in items:
            if it.type == "episode":
                c = PosterCard(it, self.ctx.client, 160, title=it.title,
                               subtitle=f"{it.display_title} · {it.episode_code}")
            else:
                sub = {"show": "TV show", "movie": str(it.get("year") or "Movie")}.get(it.type, "")
                c = PosterCard(it, self.ctx.client, 160, subtitle=sub)
            c.clicked.connect(self.ctx.open)
            self.flow.addWidget(c)


# ===========================================================================
# Settings
# ===========================================================================

class SettingsPage(ScrollPage):
    title = "Settings"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.body.setContentsMargins(32, 28, 32, 40)
        self.body.setSpacing(14)
        self.refresh()

    def section(self, title, note=None):
        self.body.addSpacing(14)
        self.body.addWidget(label(title, "H2"))
        if note:
            n = label(note, "Muted", wrap=True)
            n.setMaximumWidth(720)
            self.body.addWidget(n)

    def refresh(self):
        self.clear()
        s = self.ctx.settings
        self.body.addWidget(label("Settings", "H1"))

        self.section("Server")
        server = s["server"] or {}
        self.body.addWidget(label(f"Connected to {server.get('name', '—')}"))
        url = label(server.get("baseURL", ""), "Muted")
        url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(url)
        b = button("Change server")
        b.clicked.connect(self.ctx.forget_server)
        self.body.addWidget(b, 0, Qt.AlignLeft)

        self.section("Accent color", "Used for buttons, progress bars, the loading circle and highlights.")
        sw = QHBoxLayout()
        sw.setSpacing(10)
        cur = (s["accent"] or theme.ACCENTS[0][1]).lower()
        for name, color in theme.ACCENTS:
            b = QToolButton()
            b.setFixedSize(38, 38)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(name)
            on = color.lower() == cur
            hover = "white" if on else "rgba(255,255,255,0.55)"
            b.setStyleSheet(f"QToolButton {{ background: {color}; border-radius: 19px;"
                            f" border: 3px solid {'white' if on else 'transparent'}; }}"
                            f"QToolButton:hover {{ border-color: {hover}; }}")
            b.clicked.connect(lambda _=False, c=color: self._set_accent(c))
            sw.addWidget(b)
        sw.addSpacing(10)
        sw.addWidget(label(next((n for n, c in theme.ACCENTS if c.lower() == cur), "Custom"), "Muted"))
        sw.addStretch()
        self.body.addLayout(sw)

        self.section("Libraries", "Hidden libraries are left out of Home, Continue Watching, Libraries and Search.")
        excluded = set(s["excludedSections"] or [])
        if not self.ctx.sections:
            self.body.addWidget(label("Loading…", "Muted"))
            run_bg(self.ctx.client.sections, self._got_sections, lambda e: None)
        for sec in self.ctx.sections:
            cb = QCheckBox(sec["title"])
            cb.setChecked(sec["key"] not in excluded)
            cb.toggled.connect(lambda on, k=sec["key"]: self._toggle(k, on))
            self.body.addWidget(cb)

        self.section("Buffering", "How much video to download ahead so playback keeps going through a weak or "
                                  "dropped connection (like a phone hotspot on the road). It's kept on disk, not "
                                  "in memory. It also keeps loading while paused, so you can pause and let it fill.")
        combo = QComboBox()
        for m in BUFFER_CHOICES:
            combo.addItem(f"{m} minutes", m)
        combo.setCurrentIndex(BUFFER_CHOICES.index(s["bufferMinutes"]))
        combo.currentIndexChanged.connect(lambda i: self._set_buffer(combo.itemData(i)))
        row = QHBoxLayout()
        row.addWidget(label("Load ahead"))
        row.addSpacing(12)
        row.addWidget(combo)
        row.addStretch()
        self.body.addLayout(row)

        self.section("Player")
        tap = QCheckBox("Pause when the video is clicked")
        tap.setChecked(bool(s["tapToPause"]))
        tap.toggled.connect(lambda on: self.ctx.settings.__setitem__("tapToPause", on))
        self.body.addWidget(tap)
        note = label("When this is off, clicking the video just shows or hides the control bar.", "Muted", wrap=True)
        self.body.addWidget(note)

        delays = s["audioDelays"] or {}
        self.section("Audio delay", "Set from the player's audio delay button. Each audio device keeps its own "
                                    "setting, and it's applied automatically when that device is connected.")
        if not delays:
            self.body.addWidget(label("No devices adjusted yet.", "Muted"))
        for dev, ms in sorted(delays.items()):
            row = QHBoxLayout()
            row.addWidget(label(dev))
            row.addSpacing(16)
            row.addWidget(label(f"{int(ms):+d} ms", "Muted"))
            row.addSpacing(16)
            rb = button("Reset")
            rb.clicked.connect(lambda _=False, d=dev: self._reset_delay(d))
            row.addWidget(rb)
            row.addStretch()
            self.body.addLayout(row)

        self.section("Player shortcuts")
        keys = [("Space / K", "Play / pause"), ("← / J", "Back 10 seconds"), ("→ / L", "Forward 10 seconds"),
                ("Shift+N", "Next episode"), ("Shift+P", "Restart / previous episode"), ("S", "Skip intro / credits"),
                ("↑ / ↓", "Volume"), ("M", "Mute"), ("F / double-click", "Full screen"),
                ("Esc", "Pause and minimize the player")]
        g = QGridLayout()
        g.setHorizontalSpacing(24)
        for i, (k, d) in enumerate(keys):
            kl = label(k)
            kl.setFont(theme.font(10.5, QFont.DemiBold))
            g.addWidget(kl, i, 0)
            g.addWidget(label(d, "Muted"), i, 1)
        g.setColumnStretch(2, 1)
        self.body.addLayout(g)

        self.section("Account")
        out = button("Sign out")
        out.clicked.connect(self.ctx.sign_out)
        self.body.addWidget(out, 0, Qt.AlignLeft)
        self.body.addStretch()

    def _got_sections(self, sections):
        self.ctx.sections = sections
        self.refresh()

    def _toggle(self, key, on):
        ex = set(self.ctx.settings["excludedSections"] or [])
        if on:
            ex.discard(key)
        else:
            ex.add(key)
        self.ctx.settings["excludedSections"] = sorted(ex)
        self.ctx.libraries_changed()

    def _reset_delay(self, dev):
        d = dict(self.ctx.settings["audioDelays"] or {})
        d.pop(dev, None)
        self.ctx.settings["audioDelays"] = d
        self.refresh()

    def _set_accent(self, color):
        self.ctx.set_accent(color)
        QTimer.singleShot(0, self.refresh)

    def _set_buffer(self, minutes):
        self.ctx.settings["bufferMinutes"] = minutes


# ===========================================================================
# Info pages
# ===========================================================================

def meta_line(item: Item, seasons: int = 0) -> str:
    parts = []
    if item.get("year"):
        parts.append(str(item.get("year")))
    if item.get("contentRating"):
        parts.append(item.get("contentRating"))
    if item.type == "show":
        if seasons:
            parts.append("1 season" if seasons == 1 else f"{seasons} seasons")
    elif item.duration_ms:
        parts.append(runtime_str(item.duration_ms))
    score = item.get("audienceRating") or item.get("rating")
    if score:
        parts.append(f"★ {float(score):.1f}")
    return "  ·  ".join(parts)


class DetailBase(ScrollPage):
    """Backdrop with poster + title, then whatever the subclass adds."""

    def __init__(self, ctx, item: Item):
        super().__init__()
        self.ctx, self.item = ctx, item
        self.full: Optional[Item] = None
        self.title = item.display_title
        self.body.setSpacing(22)
        self.body.addWidget(loading_widget())
        self.refresh()

    @property
    def d(self) -> Item:
        return self.full or self.item

    def header(self, backdrop_path, poster_path, top_line, title, sub_lines, poster=True) -> Backdrop:
        bd = Backdrop(420)
        bd.set_url(self.ctx.client.image_url(backdrop_path, 1920, 1080))
        h = QHBoxLayout(bd)
        h.setContentsMargins(32, 70, 32, 18)
        h.setSpacing(28)
        if poster:
            img = ImageBox(200, 300, 10)
            img.set_url(self.ctx.client.image_url(poster_path, 400, 600))
            h.addWidget(img, 0, Qt.AlignBottom)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addStretch()
        if top_line:
            col.addWidget(label(top_line, "Accent"))
        t = label(title, "H1", wrap=True)
        t.setMaximumWidth(900)
        col.addWidget(t)
        for s in sub_lines:
            if s:
                col.addWidget(label(s, "Muted"))
        h.addLayout(col, 1)
        return bd

    def watched_button(self, item: Item) -> QPushButton:
        watched = item.show_check
        b = button("✓  Watched" if watched else "Mark as watched")
        if watched:
            b.setObjectName("Watched")

        def toggle():
            b.setEnabled(False)
            b.setText("…")
            key = item.key
            fn = self.ctx.client.mark_unwatched if watched else self.ctx.client.mark_watched
            run_bg(lambda: fn(key), lambda _: self.ctx.data_changed.emit())
        b.clicked.connect(toggle)
        return b

    def summary(self, text):
        if text:
            s = label(text, wrap=True)
            s.setFont(theme.font(11))
            s.setMaximumWidth(980)
            s.setTextInteractionFlags(Qt.TextSelectableByMouse)
            return self.padded(s, 32)
        return None

    def credits(self, item: Item) -> Optional[QWidget]:
        directors = item.tags("Director")
        roles = (item.get("Role") or [])[:12]
        if not directors and not roles:
            return None
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        if directors:
            v.addWidget(label("Directed by", "Muted"))
            v.addWidget(label(", ".join(directors)))
            v.addSpacing(8)
        if roles:
            v.addWidget(label("Cast", "H3"))
            g = QGridLayout()
            g.setHorizontalSpacing(18)
            g.setVerticalSpacing(4)
            for i, r in enumerate(roles):
                g.addWidget(label(r.get("tag", "")), i, 0)
                g.addWidget(label(r.get("role") or "", "Muted"), i, 1)
            g.setColumnStretch(2, 1)
            v.addLayout(g)
        v.addStretch()
        return w

    def file_info(self, item: Item) -> Optional[QWidget]:
        m = item.media
        if not m:
            return None
        res = (m.get("videoResolution") or "").lower()
        res = {"4k": "4K", "sd": "SD"}.get(res, (res if res.endswith("p") or not res else res + "p"))
        hdr = m.get("videoDynamicRange")
        ch = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(m.get("audioChannels"),
                                                             f"{m.get('audioChannels')} ch" if m.get("audioChannels") else None)
        rows = [
            ("Video", [res, hdr if hdr and hdr != "SDR" else None, (m.get("videoCodec") or "").upper()]),
            ("Audio", [(m.get("audioCodec") or "").upper(), ch]),
            ("File", [(m.get("container") or "").upper(),
                      size_str(item.part["size"]) if item.part.get("size") else None,
                      f"{m['bitrate'] / 1000:.1f} Mbps" if m.get("bitrate") else None]),
        ]
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(label("File", "H3"))
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(4)
        r = 0
        for name, vals in rows:
            text = "  ·  ".join(x for x in vals if x)
            if text:
                g.addWidget(label(name, "Muted"), r, 0)
                g.addWidget(label(text), r, 1)
                r += 1
        g.setColumnStretch(2, 1)
        v.addLayout(g)
        v.addStretch()
        return w

    def two_columns(self, left: Optional[QWidget], right: Optional[QWidget]) -> Optional[QWidget]:
        if not left and not right:
            return None
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(32, 0, 32, 0)
        h.setSpacing(60)
        if left:
            h.addWidget(left, 3, Qt.AlignTop)
        if right:
            h.addWidget(right, 2, Qt.AlignTop)
        h.addStretch()
        return w

    def add(self, w):
        if w is not None:
            self.body.addWidget(w)


class PlayableDetailPage(DetailBase):
    """Movie or episode."""

    def refresh(self):
        key = self.item.key
        is_ep = self.item.type == "episode"

        def fetch():
            full = self.ctx.client.metadata(key)
            eps = []
            if is_ep and full and full.get("parentRatingKey"):
                eps = self.ctx.client.children(str(full.get("parentRatingKey")))
            return full, eps

        run_bg(fetch, self._show, lambda e: self._show((None, [])))

    def _show(self, result):
        full, eps = result
        if full:
            self.full = full
        d = self.d
        sb = self.verticalScrollBar().value()
        self.clear()
        is_ep = d.type == "episode"
        genres = "  ·  ".join(d.tags("Genre")[:4])
        if is_ep:
            sub = [f"Season {d.get('parentIndex')} · Episode {d.get('index')}"
                   if d.get("parentIndex") is not None else "", meta_line(d)]
            self.add(self.header(d.get("art") or d.get("grandparentArt") or d.get("thumb"),
                                 d.get("grandparentThumb"), d.get("grandparentTitle"), d.title, sub, poster=True))
        else:
            self.add(self.header(d.get("art"), d.get("thumb"), d.get("tagline") or "", d.title,
                                 [meta_line(d), genres]))

        # Play buttons + progress
        row = QHBoxLayout()
        row.setContentsMargins(32, 0, 32, 0)
        row.setSpacing(12)
        in_progress = d.progress is not None
        play = primary("▶   Resume" if in_progress else "▶   Play")
        play.clicked.connect(lambda: self.ctx.play(d))
        row.addWidget(play)
        if in_progress:
            fs = button("Play from start")
            fs.clicked.connect(lambda: self.ctx.play(d, from_start=True))
            row.addWidget(fs)
        row.addWidget(self.watched_button(d))
        if in_progress:
            row.addSpacing(16)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(int(d.progress * 1000))
            bar.setTextVisible(False)
            bar.setFixedSize(180, 6)
            bar.setStyleSheet(f"QProgressBar {{ background: #333; border: none; border-radius: 3px; }}"
                              f"QProgressBar::chunk {{ background: {theme.ORANGE}; border-radius: 3px; }}")
            row.addWidget(bar)
            row.addWidget(label(f"{runtime_str(max(d.duration_ms - d.offset_ms, 0), True)} left", "Muted"))
        row.addStretch()
        wrap = QWidget()
        wrap.setLayout(row)
        self.add(wrap)

        self.add(self.summary(d.get("summary")))
        self.add(self.two_columns(self.credits(d), self.file_info(d)))

        if is_ep and len(eps) > 1:
            cards, current = [], None
            for ep in eps:
                c = PosterCard(ep, self.ctx.client, 240, wide=True,
                               title=f"{ep.get('index')}. {ep.title}" if ep.get("index") is not None else ep.title,
                               subtitle=runtime_str(ep.duration_ms) if ep.duration_ms else "",
                               current=(ep.key == d.key))
                c.clicked.connect(self.ctx.open)
                cards.append(c)
                if ep.key == d.key:
                    current = c
            row = CardRow(f"Season {d.get('parentIndex')}", cards)
            self.add(row)
            if current:
                row.scroll_to(current)
        self.body.addStretch()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(sb))


class ShowDetailPage(DetailBase):
    def refresh(self):
        key = self.item.key

        def fetch():
            c = self.ctx.client
            full = c.metadata(key)
            seasons = [s for s in c.children(key) if s.type == "season"]
            nxt = c.next_episode(key)
            return full, seasons, nxt

        run_bg(fetch, self._show, lambda e: self._show((None, [], None)))

    def _show(self, result):
        full, seasons, nxt = result
        if full:
            self.full = full
        d = self.d
        sb = self.verticalScrollBar().value()
        self.clear()
        self.add(self.header(d.get("art"), d.get("thumb"), d.get("studio") or "", d.title,
                             [meta_line(d, len(seasons)), "  ·  ".join(d.tags("Genre")[:4])]))
        row = QHBoxLayout()
        row.setContentsMargins(32, 0, 32, 0)
        row.setSpacing(12)
        if nxt:
            resume = nxt.progress is not None
            text = ("▶   Resume " if resume else "▶   Play ") + nxt.episode_code
            p = primary(text)
            p.setToolTip(nxt.title)
            p.clicked.connect(lambda: self.ctx.play(nxt))
            row.addWidget(p)
            row.addWidget(label(nxt.title, "Muted"))
            row.addSpacing(8)
        row.addWidget(self.watched_button(d))
        row.addStretch()
        wrap = QWidget()
        wrap.setLayout(row)
        self.add(wrap)
        self.add(self.summary(d.get("summary")))
        if seasons:
            cards = []
            for s in seasons:
                n = s.get("leafCount")
                c = PosterCard(s, self.ctx.client, 160, title=s.title or "Season",
                               subtitle=f"{n} episodes" if n else "",
                               image_path=s.get("thumb") or d.get("thumb"))
                c.clicked.connect(self.ctx.open)
                cards.append(c)
            self.add(CardRow("Seasons", cards))
        self.add(self.two_columns(self.credits(d), None))
        self.body.addStretch()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(sb))


class SeasonDetailPage(DetailBase):
    def refresh(self):
        key = self.item.key

        def fetch():
            return self.ctx.client.metadata(key), self.ctx.client.children(key)

        run_bg(fetch, self._show, lambda e: self._show((None, [])))

    def _show(self, result):
        full, eps = result
        if full:
            self.full = full
        d = self.d
        self.title = f"{d.get('parentTitle', '')} · {d.title}"
        sb = self.verticalScrollBar().value()
        self.clear()
        self.add(self.header(d.get("art") or d.get("parentArt"), d.get("thumb") or d.get("parentThumb"),
                             d.get("parentTitle") or "", d.title or "Season", [f"{len(eps)} episodes"]))
        row = QHBoxLayout()
        row.setContentsMargins(32, 0, 32, 0)
        row.addWidget(self.watched_button(d))
        row.addStretch()
        wrap = QWidget()
        wrap.setLayout(row)
        self.add(wrap)
        self.add(self.summary(d.get("summary")))
        lst = QWidget()
        v = QVBoxLayout(lst)
        v.setContentsMargins(32, 0, 32, 0)
        v.setSpacing(10)
        for ep in eps:
            v.addWidget(self._episode_row(ep))
        self.add(lst)
        self.body.addStretch()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(sb))

    def _episode_row(self, ep: Item) -> QWidget:
        row = ClickRow()
        row.setMaximumWidth(1100)
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 12, 18, 12)
        h.setSpacing(18)
        img = ImageBox(224, 126, 8)
        img.set_url(self.ctx.client.image_url(ep.get("thumb"), 448, 252))
        img.progress = ep.progress
        h.addWidget(img)
        col = QVBoxLayout()
        col.setSpacing(4)
        num = f"{ep.get('index')}. " if ep.get("index") is not None else ""
        col.addWidget(label(num + ep.title, "H3"))
        meta = runtime_str(ep.duration_ms) if ep.duration_ms else ""
        if ep.progress is not None:
            meta += f"  ·  {runtime_str(max(ep.duration_ms - ep.offset_ms, 0), True)} left"
        col.addWidget(label(meta, "Muted"))
        if ep.get("summary"):
            s = label(ep.get("summary"), "Muted", wrap=True)
            s.setMaximumHeight(44)
            col.addWidget(s)
        col.addStretch()
        h.addLayout(col, 1)
        if ep.show_check:
            chk = QLabel()
            chk.setPixmap(theme.icon("check", theme.ORANGE, 26).pixmap(26, 26))
            h.addWidget(chk, 0, Qt.AlignTop)
        row.clicked.connect(lambda: self.ctx.open(ep))
        return row


def detail_page(ctx, item: Item) -> QWidget:
    if item.type == "show":
        return ShowDetailPage(ctx, item)
    if item.type == "season":
        return SeasonDetailPage(ctx, item)
    return PlayableDetailPage(ctx, item)
