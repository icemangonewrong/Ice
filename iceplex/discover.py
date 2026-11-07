"""Discover: browse everything (not just your library) through Seerr, open any title, actor or director,
and request with one click.

Everything comes from the Seerr you already sign in to (it gets its catalogue from The Movie Database).
IMDb scores come from IMDb's own ratings file (imdb.py), matched by IMDb ID.
"""
from __future__ import annotations

import datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import imdb, theme
from .config import cache_dir
from .pages import NoWheelCombo, button, imdb_badge, loading_widget, message_widget, primary
from .plex import Item
from .util import run_bg
from .widgets import (Backdrop, CardRow, ClickRow, FlowLayout, ImageBox, LinkLabel, ScrollPage, Spinner,
                      centered, label)

AVAILABLE, PARTIAL, PENDING, PROCESSING = 5, 4, 2, 3


def status_of(r: dict) -> Optional[str]:
    st = r.get("status")
    if st in (AVAILABLE, PARTIAL):
        return "lib"
    if st in (PENDING, PROCESSING):
        return "req"
    return None


def kind_word(kind: str) -> str:
    return "Movie" if kind == "movie" else "TV show"


# ---------------------------------------------------------------------------
# IMDb id + age rating for list entries (lists don't include them), fetched in the background and kept on disk
# ---------------------------------------------------------------------------

class Extras(QObject):
    ready = Signal(str)        # "movie:123"

    def __init__(self):
        super().__init__()
        self.path = cache_dir() / "seerr_extras.json"
        try:
            self.cache: dict = json.loads(self.path.read_text())
        except Exception:
            self.cache = {}
        self.pending: set = set()
        self.pool = ThreadPoolExecutor(4, thread_name_prefix="seerr-extra")
        self._dirty = False
        self._lock = threading.Lock()
        self.saver = QTimer(self)
        self.saver.setInterval(5000)
        self.saver.timeout.connect(self._save)
        self.saver.start()

    @staticmethod
    def key(r: dict) -> str:
        return f"{r.get('kind')}:{r.get('id')}"

    def get(self, r: dict) -> Optional[dict]:
        return self.cache.get(self.key(r))

    def want(self, sr, r: dict):
        k = self.key(r)
        e = self.cache.get(k)
        # IMDb ids never change; an empty age rating (not rated yet) is looked up again after a few days.
        if e and (e.get("cert") or time.time() - e.get("t", 0) < 3 * 86400):
            return
        if k in self.pending or not r.get("id"):
            return
        self.pending.add(k)
        kind, tid = r["kind"], r["id"]

        def job():
            try:
                x = sr.extra(kind, tid)
                with self._lock:
                    self.cache[k] = {"imdb_id": x.get("imdb_id"), "cert": x.get("cert") or "", "t": time.time()}
                    self._dirty = True
                self.ready.emit(k)
            except Exception as e:  # noqa: BLE001
                print("seerr extra:", e)
            finally:
                self.pending.discard(k)
        self.pool.submit(job)

    def put(self, r: dict, imdb_id, cert):
        with self._lock:
            self.cache[self.key(r)] = {"imdb_id": imdb_id, "cert": cert or "", "t": time.time()}
            self._dirty = True

    def _save(self):
        if not self._dirty:
            return
        with self._lock:
            self._dirty = False
            data = json.dumps(self.cache)
        try:
            self.path.write_text(data)
        except OSError:
            pass


_extras: Optional[Extras] = None


def extras() -> Extras:
    global _extras
    if _extras is None:
        _extras = Extras()
    return _extras


# ---------------------------------------------------------------------------
# Opening things
# ---------------------------------------------------------------------------

def open_result(ctx, r: dict):
    """In your library: open it there (to play). Otherwise its title page."""
    if r.get("status") == AVAILABLE and r.get("rating_key"):
        ctx.open(Item({"ratingKey": str(r["rating_key"]), "type": "movie" if r["kind"] == "movie" else "show",
                       "title": r.get("title")}))
    else:
        ctx.push(TitlePage(ctx, r))


def open_person(ctx, person_id: Optional[int], name: str):
    sr = ctx.seerr()
    if not sr:
        return
    if person_id:
        ctx.push(PersonPage(ctx, person_id, name))
        return
    run_bg(lambda: sr.find_person(name), lambda pid: pid and ctx.push(PersonPage(ctx, pid, name)))


def open_list(ctx, title: str, kinds: list[str], filters: dict, sorts: bool = True):
    ctx.push(GridPage(ctx, title, kinds, filters, sorts))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

class SeerrCard(QWidget):
    """Poster from Seerr: IMDb badge, in-library / requested mark, title, year · age rating."""
    clicked = Signal(object)

    def __init__(self, ctx, r: dict, width: int = 160):
        super().__init__()
        self.r = r
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.img = ImageBox(width, int(width * 1.5))
        self.img.set_url(r.get("poster"))
        self.img.status = status_of(r)
        lay.addWidget(self.img)
        t = label(r.get("title") or "")
        t.setFont(theme.font(10, QFont.DemiBold))
        t.setFixedWidth(width)
        t.setText(t.fontMetrics().elidedText(t.text(), Qt.ElideRight, width))
        lay.addWidget(t)
        self.sub = label("", "Muted")
        self.sub.setFont(theme.font(9))
        self.sub.setFixedWidth(width)
        lay.addWidget(self.sub)
        lay.addStretch()
        self.setFixedWidth(width)
        self._apply()
        ex = extras()
        ex.ready.connect(self._ready)
        sr = ctx.seerr()
        if sr:
            ex.want(sr, r)

    def _ready(self, key):
        if key == Extras.key(self.r):
            self._apply()

    def _apply(self):
        e = extras().get(self.r) or {}
        self.img.rating = imdb.rating(e.get("imdb_id"))
        bits = [self.r.get("year") or "", e.get("cert") or kind_word(self.r["kind"])]
        if self.r.get("character") or self.r.get("job"):
            bits = [self.r.get("character") or self.r.get("job")]
        self.sub.setText("  ·  ".join(b for b in bits if b))
        self.img.update()

    def enterEvent(self, e):
        self.img.highlight = True
        self.img.update()

    def leaveEvent(self, e):
        self.img.highlight = False
        self.img.update()

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.r)


class CastCard(QFrame):
    """Actor card: photo on top, name and role below (opens their page)."""
    clicked = Signal()

    def __init__(self, name: str, role: str, photo: Optional[str], width: int = 132):
        super().__init__()
        self.setObjectName("CastCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(width)
        self.setStyleSheet(f"QFrame#CastCard {{ background: {theme.PANEL}; border: 1px solid {theme.LINE};"
                           f" border-radius: 12px; }} QFrame#CastCard:hover {{ border-color: {theme.ORANGE}; }}")
        self.setFixedHeight(int(width * 1.2) + 72)   # same size whatever the name length (rows line up)
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 8)
        v.setSpacing(3)
        img = ImageBox(width - 2, int(width * 1.2), 11)
        img.placeholder = "person"
        img.set_url(photo)
        v.addWidget(img)
        n = label(name)
        n.setFont(theme.font(9.5, QFont.Bold))
        n.setContentsMargins(9, 4, 9, 0)
        n.setWordWrap(True)
        n.setMaximumHeight(n.fontMetrics().lineSpacing() * 2 + 6)
        v.addWidget(n)
        if role:
            r = label("", "Muted")
            r.setFont(theme.font(8.5))
            r.setContentsMargins(9, 0, 9, 0)
            r.setText(r.fontMetrics().elidedText(role, Qt.ElideRight, width - 20))
            v.addWidget(r)
        v.addStretch()

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class Tile(QFrame):
    """Streaming service / network / genre button."""
    clicked = Signal()

    def __init__(self, text: str, logo: Optional[str] = None, height: int = 64):
        super().__init__()
        self.setObjectName("Tile")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(height)
        self.setStyleSheet(f"QFrame#Tile {{ background: {theme.CARD}; border: 1px solid {theme.LINE};"
                           f" border-radius: 10px; }} QFrame#Tile:hover {{ border-color: {theme.ORANGE}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 14, 8)
        h.setSpacing(10)
        if logo:
            img = ImageBox(height - 22, height - 22, 8)
            img.set_url(logo)
            h.addWidget(img)
        t = label(text)
        t.setFont(theme.font(10.5, QFont.Bold))
        h.addWidget(t, 1)

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


def chip(text: str, checked: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setCheckable(True)
    b.setChecked(checked)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton {{ background: {theme.CARD}; border: 1px solid {theme.LINE}; border-radius: 16px;"
        f" padding: 6px 16px; font-weight: 600; }}"
        f"QPushButton:hover {{ border-color: {theme.ORANGE}; }}"
        f"QPushButton:checked {{ background: {theme.ORANGE}; color: {theme.on_accent_text()};"
        f" border-color: {theme.ORANGE}; }}")
    return b


def cards_row(ctx, title: str, results: list[dict], on_title: Optional[Callable] = None, width: int = 160):
    cards = []
    for r in results:
        c = SeerrCard(ctx, r, width)
        c.clicked.connect(lambda r, ctx=ctx: open_result(ctx, r))
        cards.append(c)
    return CardRow(title, cards, on_title=on_title)


def cast_row(ctx, people: list[dict], title: str = "Cast") -> Optional[QWidget]:
    """people: [{id (TMDB, or None), name, role, photo}]."""
    if not people:
        return None
    cards = []
    for p in people:
        c = CastCard(p["name"], p.get("role") or "", p.get("photo"))
        c.clicked.connect(lambda p=p: open_person(ctx, p.get("id"), p["name"]))
        cards.append(c)
    return CardRow(title, cards)


def plex_cast(item: Item) -> list[dict]:
    """Cast from a Plex item, for cast_row (Plex gives names and photos; Seerr finds the person by name)."""
    return [{"id": None, "name": r.get("tag", ""), "role": r.get("role") or "", "photo": r.get("thumb")}
            for r in (item.get("Role") or [])[:20] if r.get("tag")]


def flow_box(widgets: list[QWidget], spacing: int = 10, margin: int = 32) -> QWidget:
    w = QWidget()
    f = FlowLayout(w, spacing=spacing)
    f.setContentsMargins(margin, 0, margin, 0)
    for x in widgets:
        f.addWidget(x)
    return w


def section_title(text: str) -> QLabel:
    t = label(text, "H2")
    t.setContentsMargins(32, 0, 0, 0)
    return t


def needs_seerr(page: ScrollPage, ctx) -> bool:
    if ctx.seerr():
        return False
    page.body.addWidget(message_widget("Discover uses your Seerr.\nSet its address in Settings → Requests (Seerr)."))
    page.body.addStretch()
    return True


# ---------------------------------------------------------------------------
# Discover tab
# ---------------------------------------------------------------------------

class DiscoverPage(ScrollPage):
    title = "Discover"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.body.setContentsMargins(0, 28, 0, 40)
        self.kind = "all"
        self.data: dict = {}
        self._built = False
        self.refresh()

    def refresh(self):
        if self._built:
            self._render()     # e.g. IMDb ratings just arrived
            return
        self.clear()
        if needs_seerr(self, self.ctx):
            return
        self._built = True
        head = QHBoxLayout()
        head.setContentsMargins(32, 0, 32, 0)
        head.setSpacing(8)
        head.addWidget(label("Discover", "H1"))
        head.addSpacing(18)
        self.chips = {}
        for key, text in (("all", "All"), ("movie", "Movies"), ("tv", "Shows")):
            b = chip(text, key == "all")
            b.clicked.connect(lambda _=False, k=key: self._set_kind(k))
            self.chips[key] = b
            head.addWidget(b)
        wl = chip("Watchlist")
        wl.setCheckable(False)
        wl.clicked.connect(lambda: self.ctx.push(GridPage(self.ctx, "Your Plex watchlist", ["all"], {"_watchlist": 1},
                                                          sorts=False)))
        head.addWidget(wl)
        head.addStretch()
        hw = QWidget()
        hw.setLayout(head)
        self.body.addWidget(hw)
        self.rows_box = QWidget()
        self.rows = QVBoxLayout(self.rows_box)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(28)
        self.body.addWidget(self.rows_box)
        self.body.addStretch()
        self._load()

    ROWS = [
        ("trending", "Trending this week", "all"),
        ("pop_movie", "Popular movies", "movie"),
        ("pop_tv", "Popular shows", "tv"),
        ("up_movie", "Coming soon · Movies", "movie"),
        ("up_tv", "Coming soon · Shows", "tv"),
    ]

    def _load(self):
        sr = self.ctx.seerr()
        jobs = {
            "trending": lambda: sr.trending()[0],
            "pop_movie": lambda: sr.popular("movie")[0],
            "pop_tv": lambda: sr.popular("tv")[0],
            "up_movie": lambda: sr.upcoming("movie")[0],
            "up_tv": lambda: sr.upcoming("tv")[0],
            "services": lambda: sr.services("movie"),
            "genres": lambda: (sr.genres("movie"), sr.genres("tv")),
        }
        for k, fn in jobs.items():
            run_bg(fn, lambda res, k=k: self._got(k, res), lambda e, k=k: self._got(k, e))
        self._render()

    def _got(self, key, res):
        self.data[key] = res
        self._render()

    def _set_kind(self, k):
        self.kind = k
        for key, b in self.chips.items():
            b.setChecked(key == k)
        self._render()

    def _see_all(self, key):
        title = dict((k, t) for k, t, _ in self.ROWS)[key]
        if key == "trending":
            return lambda: open_list(self.ctx, title, ["all"], {"_trending": 1}, sorts=False)
        kind = "movie" if key.endswith("movie") else "tv"
        if key.startswith("up"):
            return lambda: open_list(self.ctx, title, [kind], {"_upcoming": 1}, sorts=False)
        return lambda: open_list(self.ctx, title, [kind], {})

    def _render(self):
        sb = self.verticalScrollBar().value()
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        errors = []
        for key, title, kind in self.ROWS:
            if self.kind != "all" and kind not in ("all", self.kind):
                continue
            res = self.data.get(key)
            if res is None:
                self.rows.addWidget(self._placeholder(title))
                continue
            if isinstance(res, Exception):
                errors.append(str(res))
                continue
            items = [r for r in res if self.kind == "all" or r["kind"] == self.kind]
            if items:
                self.rows.addWidget(cards_row(self.ctx, title, items, on_title=self._see_all(key)))
        if errors and len(errors) == len([1 for _k, _t, kd in self.ROWS if self.kind == "all" or kd in ("all", self.kind)]):
            self.rows.addWidget(message_widget(f"Couldn't reach Seerr.\n{errors[0]}"))
        self._add_services()
        self._add_networks()
        self._add_genres()
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(sb))

    def _placeholder(self, title):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(section_title(title))
        sp = QHBoxLayout()
        sp.setContentsMargins(32, 30, 0, 30)
        sp.addWidget(Spinner(30))
        sp.addStretch()
        v.addLayout(sp)
        return w

    def _kinds(self):
        return ["movie", "tv"] if self.kind == "all" else [self.kind]

    def _add_services(self):
        svc = self.data.get("services")
        if not svc or isinstance(svc, Exception):
            return
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        v.addWidget(section_title("Streaming services"))
        tiles = []
        for s in svc:
            t = Tile(s["name"], s.get("logo"))
            t.setFixedWidth(200)
            t.clicked.connect(lambda s=s: open_list(self.ctx, s["name"], self._kinds(),
                                                    {"watchProviders": s["id"], "watchRegion": "US"}))
            tiles.append(t)
        v.addWidget(flow_box(tiles))
        self.rows.addWidget(box)

    def _add_networks(self):
        from .seerr import NETWORKS, STUDIOS
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        groups = []
        if self.kind in ("all", "tv"):
            groups.append(("Networks", NETWORKS, "tv", "network"))
        if self.kind in ("all", "movie"):
            groups.append(("Studios", STUDIOS, "movie", "studio"))
        for title, lst, kind, param in groups:
            v.addWidget(section_title(title))
            chips = []
            for name, nid in lst:
                b = chip(name)
                b.setCheckable(False)
                b.clicked.connect(lambda _=False, name=name, nid=nid, kind=kind, param=param:
                                  open_list(self.ctx, name, [kind], {param: nid}))
                chips.append(b)
            v.addWidget(flow_box(chips, 8))
            v.addSpacing(8)
        self.rows.addWidget(box)

    def _add_genres(self):
        g = self.data.get("genres")
        if not g or isinstance(g, Exception):
            return
        movie, tv = g
        by_name: dict = {}
        for kind, lst in (("movie", movie), ("tv", tv)):
            if self.kind in ("all", kind):
                for x in lst:
                    by_name.setdefault(x["name"], {})[kind] = x["id"]
        if not by_name:
            return
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        v.addWidget(section_title("Genres"))
        tiles = []
        for name in sorted(by_name):
            ids = by_name[name]
            t = Tile(name, height=48)
            t.setFixedWidth(170)
            t.clicked.connect(lambda name=name, ids=ids: open_list(self.ctx, name, list(ids), {"_genre": ids}))
            tiles.append(t)
        v.addWidget(flow_box(tiles))
        self.rows.addWidget(box)


# ---------------------------------------------------------------------------
# "See all" / service / network / studio / genre grid, with sort and Movies/Shows switch
# ---------------------------------------------------------------------------

class GridPage(ScrollPage):
    SORTS = [("Popular", "popularity.desc"), ("Newest", "date.desc"), ("Top rated", "vote_average.desc")]

    def __init__(self, ctx, title: str, kinds: list[str], filters: dict, sorts: bool = True):
        super().__init__()
        self.ctx, self.title, self.filters = ctx, title, filters
        self.kinds = kinds
        self.kind = kinds[0]
        self.body.setContentsMargins(32, 76, 32, 40)
        self.body.setSpacing(20)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label(title, "H1"))
        head.addSpacing(18)
        self.chips = {}
        if len(kinds) > 1:
            for k in kinds:
                b = chip("Movies" if k == "movie" else "Shows", k == self.kind)
                b.clicked.connect(lambda _=False, k=k: self._set_kind(k))
                self.chips[k] = b
                head.addWidget(b)
        head.addStretch()
        self.sort = None
        if sorts:
            self.sort = NoWheelCombo()
            for name, _ in self.SORTS:
                self.sort.addItem(name)
            self.sort.currentIndexChanged.connect(self.reload)
            head.addWidget(self.sort)
        self.body.addLayout(head)
        self.msg = label("", "Muted")
        self.msg.hide()
        self.body.addWidget(self.msg)
        self.grid = QWidget()
        self.flow = FlowLayout(self.grid, spacing=20)
        self.body.addWidget(self.grid)
        self.spinner = Spinner(36)
        self.body.addWidget(centered(self.spinner))
        self.body.addStretch()
        self._gen = 0
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.reload()

    def _set_kind(self, k):
        self.kind = k
        for key, b in self.chips.items():
            b.setChecked(key == k)
        self.reload()

    def reload(self):
        self._gen += 1
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.page, self.pages, self.loading = 0, None, False
        self.seen: set = set()
        self.msg.hide()
        self.load_more()

    def _fetch(self, page: int):
        sr = self.ctx.seerr()
        f, kind = self.filters, self.kind
        if "_trending" in f:
            return sr.trending(page)
        if "_watchlist" in f:
            return sr.watchlist(page)
        if "_upcoming" in f:
            return sr.upcoming(kind, page)
        params = {k: v for k, v in f.items() if not k.startswith("_")}
        if "_genre" in f:
            params["genre"] = f["_genre"].get(kind)
        sort = self.SORTS[self.sort.currentIndex()][1] if self.sort else "popularity.desc"
        today = datetime.date.today().isoformat()
        if sort == "date.desc":
            sort = "primary_release_date.desc" if kind == "movie" else "first_air_date.desc"
            params["primaryReleaseDateLte" if kind == "movie" else "firstAirDateLte"] = today
        elif sort == "vote_average.desc":
            params["voteCountGte"] = 300
        params["sortBy"] = sort
        return sr.popular(kind, page, **params)

    def _scrolled(self, v):
        if v > self.verticalScrollBar().maximum() - 900:
            self.load_more()

    def load_more(self):
        if self.loading or (self.pages is not None and self.page >= self.pages) or not self.ctx.seerr():
            return
        self.loading = True
        self.spinner.show()
        gen, page = self._gen, self.page + 1
        run_bg(lambda: self._fetch(page), lambda r: self._got(gen, page, r), lambda e: self._failed(gen, e))

    def _failed(self, gen, e):
        if gen != self._gen:
            return
        self.loading = False
        self.spinner.hide()
        self.msg.setText(f"Couldn't load from Seerr: {e}")
        self.msg.show()

    def _got(self, gen, page, res):
        if gen != self._gen:
            return
        results, pages = res
        self.loading = False
        self.spinner.hide()
        self.page, self.pages = page, min(pages, 30)
        for r in results:
            k = (r["kind"], r["id"])
            if k in self.seen:
                continue
            self.seen.add(k)
            c = SeerrCard(self.ctx, r)
            c.clicked.connect(lambda r: open_result(self.ctx, r))
            self.flow.addWidget(c)
        if not self.seen:
            self.msg.setText("Nothing here.")
            self.msg.show()
        self.grid.updateGeometry()
        QTimer.singleShot(50, lambda: self._scrolled(self.verticalScrollBar().value()))


# ---------------------------------------------------------------------------
# Title page (anything, in your library or not)
# ---------------------------------------------------------------------------

def outline_badge(text: str) -> QLabel:
    b = QLabel(text)
    b.setFont(theme.font(9.5, QFont.Bold))
    b.setStyleSheet(f"color: #d8d8e0; border: 1px solid #6a6a76; border-radius: 4px; padding: 1px 6px;")
    return b


def dark_badge(text: str) -> QLabel:
    b = QLabel(text)
    b.setFont(theme.font(10, QFont.Bold))
    b.setStyleSheet(f"background: {theme.CARD}; border-radius: 4px; padding: 2px 8px;")
    return b


def link_line(ctx, name: str, links: list[tuple[str, Callable]]) -> Optional[QWidget]:
    if not links:
        return None
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    n = label(name + "  ", "Muted")
    n.setFixedWidth(110)
    h.addWidget(n)
    for i, (text, fn) in enumerate(links):
        l = AccentLink(text + ("," if i < len(links) - 1 else ""))
        l.clicked.connect(fn)
        h.addWidget(l)
    h.addStretch()
    return w


class AccentLink(LinkLabel):
    """Name in the accent colour that opens something (underlines on hover)."""

    def __init__(self, text):
        super().__init__(text)
        self._style(False)

    def _style(self, on):
        f = self.font()
        f.setUnderline(on)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)
        self.setStyleSheet(f"color: {theme.ORANGE};")

    def enterEvent(self, e):
        self._style(True)

    def leaveEvent(self, e):
        self._style(False)


class TitlePage(ScrollPage):
    def __init__(self, ctx, r: dict):
        super().__init__()
        self.ctx, self.r = ctx, dict(r)
        self.title = r.get("title") or ""
        self.body.setSpacing(20)
        self.body.addWidget(loading_widget())
        self.seasons: list = []
        sr = ctx.seerr()
        if not sr:
            self.clear()
            needs_seerr(self, ctx)
            return
        kind, tid = r["kind"], r["id"]

        def fetch():
            info = sr.title(kind, tid)
            scores = sr.scores(kind, tid)
            try:
                recs = sr.recommendations(kind, tid)
            except Exception:  # noqa: BLE001
                recs = []
            return info, scores, recs

        run_bg(fetch, self._show, self._fail)

    def _fail(self, e):
        self.clear()
        self.body.addWidget(message_widget(f"Couldn't load from Seerr.\n{e}"))
        self.body.addStretch()

    def _show(self, res):
        info, scores, recs = res
        self.info = info
        extras().put(info, info.get("imdb_id"), info.get("cert"))
        r = self.r
        self.clear()
        # Header: backdrop, poster, title, meta
        bd = Backdrop(420)
        bd.set_url(info.get("backdrop"))
        h = QHBoxLayout(bd)
        h.setContentsMargins(32, 70, 32, 18)
        h.setSpacing(28)
        img = ImageBox(200, 300, 10)
        img.set_url(info.get("poster") or r.get("poster"))
        img.status = status_of(info)
        h.addWidget(img, 0, Qt.AlignBottom)
        col = QVBoxLayout()
        col.setSpacing(8)
        col.addStretch()
        col.addWidget(label(info.get("tagline") or kind_word(info["kind"]).upper(), "Accent"))
        t = label(info.get("title") or r.get("title") or "", "H1", wrap=True)
        t.setMaximumWidth(900)
        col.addWidget(t)
        bits = [info.get("year")]
        if info["kind"] == "movie" and info.get("runtime"):
            m = int(info["runtime"])
            bits.append(f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m")
        if info["kind"] == "tv" and info.get("num_seasons"):
            n = info["num_seasons"]
            bits.append("1 season" if n == 1 else f"{n} seasons")
        bits += info.get("genres", [])[:3]
        col.addWidget(label("  ·  ".join(b for b in bits if b), "Muted"))
        badges = QHBoxLayout()
        badges.setSpacing(8)
        if info.get("cert"):
            badges.addWidget(outline_badge(info["cert"]))
        score = imdb.rating(info.get("imdb_id")) or scores.get("imdb")
        if score:
            badges.addWidget(imdb_badge(score))
        if scores.get("rt") is not None:
            badges.addWidget(dark_badge(f"Rotten Tomatoes  {scores['rt']}%"))
        if info.get("vote"):
            badges.addWidget(dark_badge(f"TMDB  {float(info['vote']):.1f}"))
        badges.addStretch()
        col.addLayout(badges)
        h.addLayout(col, 1)
        self.body.addWidget(bd)

        # Buttons
        row = QHBoxLayout()
        row.setContentsMargins(32, 0, 32, 0)
        row.setSpacing(12)
        st = info.get("status")
        if st in (AVAILABLE, PARTIAL) and info.get("rating_key"):
            p = primary("▶   Open in your library")
            p.clicked.connect(lambda: self.ctx.open(Item({"ratingKey": str(info["rating_key"]),
                                                          "type": "movie" if info["kind"] == "movie" else "show",
                                                          "title": info.get("title")})))
            row.addWidget(p)
        self.req_btn = None
        if info["kind"] == "movie":
            if st in (PENDING, PROCESSING):
                row.addWidget(label("✓  Requested", "Accent"))
            elif st != AVAILABLE:
                self.req_btn = primary("＋   Request")
                self.req_btn.clicked.connect(lambda: self._request(None))
                row.addWidget(self.req_btn)
        else:
            open_seasons = [s for s in info.get("seasons", []) if s["status"] not in (AVAILABLE, PENDING, PROCESSING)]
            if open_seasons:
                self.req_btn = primary("＋   Request")
                self.req_btn.clicked.connect(lambda: self._request([n for n, c in self.seasons if c.isChecked()]))
                row.addWidget(self.req_btn)
            elif info.get("seasons"):
                row.addWidget(label("✓  Every season is in your library or requested", "Accent"))
        if info.get("trailer"):
            tb = button("▶   Trailer")
            tb.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info["trailer"])))
            row.addWidget(tb)
        self.req_note = label("", "Muted")
        row.addWidget(self.req_note)
        row.addStretch()
        wrap = QWidget()
        wrap.setLayout(row)
        self.body.addWidget(wrap)

        # Seasons (shows): pick which to request
        if info["kind"] == "tv" and info.get("seasons"):
            from .seerr import STATUS_TEXT
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(32, 0, 32, 0)
            v.setSpacing(4)
            flow = []
            for s in info["seasons"]:
                stx = s["status"]
                cb = QCheckBox(f"{s['name']}  ·  {s['episodes']} ep"
                               + (f"  ·  {STATUS_TEXT[stx]}" if stx in STATUS_TEXT else ""))
                cb.setChecked(stx not in (AVAILABLE, PENDING, PROCESSING))
                if stx in (AVAILABLE, PENDING, PROCESSING):
                    cb.setEnabled(False)
                else:
                    self.seasons.append((s["number"], cb))
                cb.setMinimumWidth(240)
                flow.append(cb)
            fw = flow_box(flow, 6, 0)
            v.addWidget(fw)
            self.body.addWidget(box)

        # Cast right under the title (cards with photos)
        cr = cast_row(self.ctx, info.get("cast") or [])
        if cr:
            self.body.addWidget(cr)

        if info.get("overview"):
            s = label(info["overview"], wrap=True)
            s.setFont(theme.font(11))
            s.setMaximumWidth(980)
            s.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.body.addWidget(self.padded(s))

        # Credits
        credits = QWidget()
        cv = QVBoxLayout(credits)
        cv.setContentsMargins(32, 0, 32, 0)
        cv.setSpacing(6)
        ppl = lambda lst: [(p["name"], lambda p=p: open_person(self.ctx, p["id"], p["name"])) for p in lst]
        lines = [link_line(self.ctx, "Director" if len(info.get("directors") or []) < 2 else "Directors",
                           ppl(info.get("directors") or [])),
                 link_line(self.ctx, "Created by", ppl(info.get("creators") or [])),
                 link_line(self.ctx, "Writers", ppl(info.get("writers") or []))]
        if info.get("network"):
            n = info["network"]
            lines.append(link_line(self.ctx, "Network", [(n["name"], lambda: open_list(
                self.ctx, n["name"], ["tv"], {"network": n["id"]}))]))
        if info.get("studio") and info["kind"] == "movie":
            s = info["studio"]
            lines.append(link_line(self.ctx, "Studio", [(s["name"], lambda: open_list(
                self.ctx, s["name"], ["movie"], {"studio": s["id"]}))]))
        if info.get("providers"):
            lines.append(link_line(self.ctx, "Streaming on", [
                (p["name"], lambda p=p: open_list(self.ctx, p["name"], [info["kind"], "tv" if info["kind"] == "movie"
                                                                         else "movie"],
                                                  {"watchProviders": p["id"], "watchRegion": "US"}))
                for p in info["providers"][:4]]))
        for ln in lines:
            if ln:
                cv.addWidget(ln)
        self.body.addWidget(credits)

        # Collection (one row; the movies are on its own page)
        col_info = info.get("collection")
        if col_info:
            cr = ClickRow()
            cr.setMaximumWidth(620)
            hh = QHBoxLayout(cr)
            hh.setContentsMargins(12, 10, 16, 10)
            hh.setSpacing(14)
            ci = ImageBox(40, 60, 5)
            ci.set_url(col_info.get("poster"))
            hh.addWidget(ci)
            tv_ = QVBoxLayout()
            tv_.setSpacing(2)
            tv_.addWidget(label(f"Part of the {col_info['name'].removesuffix(' Collection')} collection", "H3"))
            tv_.addWidget(label("See every movie in it", "Muted"))
            hh.addLayout(tv_, 1)
            arrow = QLabel()
            arrow.setPixmap(theme.icon("right", theme.MUTED, 20).pixmap(20, 20))
            hh.addWidget(arrow)
            cr.clicked.connect(lambda: self.ctx.push(CollectionPage(self.ctx, col_info["id"], col_info["name"])))
            self.body.addWidget(self.padded(cr))

        if recs:
            self.body.addWidget(cards_row(self.ctx, "More like this", recs[:20]))
        self.body.addStretch()

    def _request(self, seasons):
        sr = self.ctx.seerr()
        info = self.info
        if info["kind"] == "tv" and not seasons:
            self.req_note.setText("Pick at least one season.")
            return
        btn = self.req_btn
        btn.setEnabled(False)
        btn.setText("Requesting…")

        def done(_):
            btn.setText("✓  Requested")
            for _n, c in self.seasons:
                c.setEnabled(False)
            self.req_note.setText("")

        def failed(e):
            btn.setEnabled(True)
            btn.setText("＋   Request")
            self.req_note.setText(f"Couldn't request: {e}")

        run_bg(lambda: sr.request(info["kind"], info["id"], seasons), done, failed)


# ---------------------------------------------------------------------------
# Actor / director page
# ---------------------------------------------------------------------------

def _nice_date(s: str) -> str:
    try:
        d = datetime.date.fromisoformat(s[:10])
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except Exception:  # noqa: BLE001
        return s


class CreditRow(ClickRow):
    """One line of a filmography: year, title, role · age rating, IMDb, in-library mark."""

    def __init__(self, ctx, r: dict):
        super().__init__()
        self.r = r
        self.setMaximumWidth(1000)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(14)
        y = label(r.get("year") or "—", "Muted")
        y.setFixedWidth(44)
        h.addWidget(y)
        img = ImageBox(36, 54, 4)
        img.set_url(r.get("poster"))
        h.addWidget(img)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = label(r.get("title") or "")
        t.setFont(theme.font(10.5, QFont.Bold))
        col.addWidget(t)
        self.sub = label("", "Muted")
        self.sub.setFont(theme.font(9))
        col.addWidget(self.sub)
        h.addLayout(col, 1)
        self.badge = QLabel()
        self.badge.setFont(theme.font(9, QFont.Bold))
        self.badge.setStyleSheet("background: #f5c518; color: black; border-radius: 4px; padding: 1px 6px;")
        self.badge.hide()
        h.addWidget(self.badge, 0, Qt.AlignVCenter)
        st = status_of(r)
        mark = label({"lib": "✓ In library", "req": "Requested"}.get(st, "＋ Request"))
        mark.setFont(theme.font(9, QFont.Bold))
        mark.setStyleSheet({"lib": "color: #4cd07a;", "req": "color: #f0b040;"}.get(st, f"color: {theme.MUTED};"))
        mark.setFixedWidth(96)
        mark.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(mark)
        self.clicked.connect(lambda: open_result(ctx, r))
        self._apply()
        extras().ready.connect(self._ready)
        sr = ctx.seerr()
        if sr:
            extras().want(sr, r)

    def _ready(self, key):
        if key == Extras.key(self.r):
            self._apply()

    def _apply(self):
        e = extras().get(self.r) or {}
        bits = [self.r.get("role") or "", kind_word(self.r["kind"]) if self.r["kind"] == "tv" else "",
                e.get("cert") or ""]
        if self.r["kind"] == "tv" and self.r.get("episodes"):
            bits.append(f"{self.r['episodes']} ep")
        self.sub.setText("  ·  ".join(b for b in bits if b))
        sc = imdb.rating(e.get("imdb_id"))
        if sc is not None:
            self.badge.setText(f"IMDb {sc:.1f}")
            self.badge.show()


class PersonPage(ScrollPage):
    SHOW = 40

    def __init__(self, ctx, person_id: int, name: str = ""):
        super().__init__()
        self.ctx, self.pid = ctx, person_id
        self.title = name
        self.body.setSpacing(22)
        self.body.setContentsMargins(0, 76, 0, 40)
        self.body.addWidget(loading_widget())
        sr = ctx.seerr()
        if not sr:
            self.clear()
            needs_seerr(self, ctx)
            return
        run_bg(lambda: sr.person(person_id), self._show, self._fail)

    def _fail(self, e):
        self.clear()
        self.body.addWidget(message_widget(f"Couldn't load from Seerr.\n{e}"))
        self.body.addStretch()

    def _show(self, p):
        self.p = p
        self.clear()
        top = QHBoxLayout()
        top.setContentsMargins(32, 0, 32, 0)
        top.setSpacing(24)
        img = ImageBox(160, 200, 14)
        img.set_url(p.get("photo"))
        top.addWidget(img, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(label(p["name"], "H1"))
        dept = {"Acting": "Actor", "Directing": "Director", "Writing": "Writer", "Production": "Producer"}.get(
            p.get("dept"), p.get("dept") or "")
        if dept:
            col.addWidget(label(dept, "Accent"))
        born = []
        if p.get("birthday"):
            born.append("Born " + _nice_date(p["birthday"]))
        if p.get("place"):
            born.append(p["place"])
        if p.get("deathday"):
            born.append("Died " + _nice_date(p["deathday"]))
        if born:
            col.addWidget(label("  ·  ".join(born), "Muted"))
        bio = p.get("bio") or ""
        if bio:
            short = bio if len(bio) < 520 else bio[:500].rsplit(" ", 1)[0] + "…"
            self.bio = label(short, wrap=True)
            self.bio.setFont(theme.font(10.5))
            self.bio.setMaximumWidth(900)
            col.addSpacing(6)
            col.addWidget(self.bio)
            if short != bio:
                more = LinkLabel("More", "Accent")
                more.clicked.connect(lambda: (self.bio.setText(bio), more.hide()))
                col.addWidget(more, 0, Qt.AlignLeft)
        col.addStretch()
        top.addLayout(col, 1)
        tw = QWidget()
        tw.setLayout(top)
        self.body.addWidget(tw)

        if p.get("known"):
            self.body.addWidget(cards_row(self.ctx, "Known for", p["known"]))

        groups = p.get("groups") or []
        if groups:
            head = QHBoxLayout()
            head.setContentsMargins(32, 0, 32, 0)
            head.setSpacing(8)
            head.addWidget(label("Filmography", "H2"))
            head.addSpacing(14)
            self.tabs = {}
            for i, (g, lst) in enumerate(groups):
                b = chip(f"{g}  {len(lst)}", i == 0)
                b.clicked.connect(lambda _=False, g=g: self._show_group(g))
                self.tabs[g] = b
                head.addWidget(b)
            head.addStretch()
            hw = QWidget()
            hw.setLayout(head)
            self.body.addWidget(hw)
            self.list_box = QWidget()
            self.list = QVBoxLayout(self.list_box)
            self.list.setContentsMargins(32, 0, 32, 0)
            self.list.setSpacing(6)
            self.body.addWidget(self.list_box)
            self._show_group(groups[0][0])
        self.body.addStretch()

    def _show_group(self, g, show_all=False):
        for k, b in self.tabs.items():
            b.setChecked(k == g)
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        lst = dict(self.p["groups"])[g]
        shown = lst if show_all else lst[:self.SHOW]
        for r in shown:
            self.list.addWidget(CreditRow(self.ctx, r))
        if len(shown) < len(lst):
            b = button(f"Show all {len(lst)}")
            b.setMaximumWidth(220)
            b.clicked.connect(lambda: self._show_group(g, True))
            self.list.addWidget(b)


# ---------------------------------------------------------------------------
# Collection page
# ---------------------------------------------------------------------------

class CollectionPage(ScrollPage):
    def __init__(self, ctx, collection_id: int, name: str):
        super().__init__()
        self.ctx = ctx
        self.title = name
        self.body.setSpacing(20)
        self.body.addWidget(loading_widget())
        sr = ctx.seerr()
        if not sr:
            self.clear()
            needs_seerr(self, ctx)
            return
        run_bg(lambda: sr.collection(collection_id), self._show,
               lambda e: (self.clear(), self.body.addWidget(message_widget(f"Couldn't load from Seerr.\n{e}"))))

    def _show(self, c):
        self.clear()
        bd = Backdrop(320)
        bd.set_url(c.get("backdrop"))
        v = QVBoxLayout(bd)
        v.setContentsMargins(32, 70, 32, 18)
        v.addStretch()
        have = sum(1 for p in c["parts"] if p.get("status") == AVAILABLE)
        v.addWidget(label(f"{len(c['parts'])} movies  ·  {have} in your library", "Accent"))
        t = label(c["name"], "H1", wrap=True)
        v.addWidget(t)
        self.body.addWidget(bd)
        if c.get("overview"):
            s = label(c["overview"], wrap=True)
            s.setFont(theme.font(11))
            s.setMaximumWidth(980)
            self.body.addWidget(self.padded(s))
        cards = []
        for r in c["parts"]:
            card = SeerrCard(self.ctx, r)
            card.clicked.connect(lambda r: open_result(self.ctx, r))
            cards.append(card)
        self.body.addWidget(flow_box(cards, 20))
        self.body.addStretch()
