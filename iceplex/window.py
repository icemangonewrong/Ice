"""Main window: sidebar, page navigation, and the player."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget, QApplication, QLineEdit)

from . import theme
from .config import Settings
from .pages import (HomePage, LibrariesPage, SearchPage, ServerPage, SettingsPage, SignInPage, detail_page,
                    resource_path)
from .player import PlayerPage
from .plex import Item, PlexClient
from .widgets import label


class Context(QObject):
    """Shared by every page: the Plex client, settings, and navigation."""
    data_changed = Signal()

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.settings = Settings()
        self.client = PlexClient(self.settings["clientIdentifier"])
        self.client.token = self.settings["token"]
        self.client.server = self.settings["server"]
        self.client.on_server_changed = lambda s: self.settings.__setitem__("server", s)
        self.sections: list[dict] = []

    # -- account / server -------------------------------------------------
    def set_token(self, token):
        self.settings["token"] = token
        self.client.token = token

    def set_server(self, server):
        self.settings["server"] = server
        self.client.server = server
        self.sections = []

    def server_name(self) -> str:
        return (self.settings["server"] or {}).get("name", "Home")

    def forget_server(self):
        self.settings["server"] = None
        self.client.server = None
        self.sections = []
        self.window.route()

    def sign_out(self):
        self.settings["token"] = None
        self.settings["server"] = None
        self.client.token = None
        self.client.server = None
        self.sections = []
        self.window.route()

    # -- libraries ------------------------------------------------------------
    def is_excluded(self, item: Item) -> bool:
        sec = item.section_id
        return bool(sec) and sec in set(self.settings["excludedSections"] or [])

    def libraries_changed(self):
        self.window.libraries_changed()

    # -- navigation -------------------------------------------------------------
    def open(self, item: Item):
        self.window.push(detail_page(self, item))

    def push(self, page: QWidget):
        self.window.push(page)

    def play(self, item: Item, from_start: bool = False):
        self.window.play(item, from_start)


class NavButton(QPushButton):
    def __init__(self, text, icon_name):
        super().__init__("   " + text)
        self.setObjectName("Nav")
        self.setCheckable(True)
        self.icon_name = icon_name
        self.setIconSize(QSize(22, 22))
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.toggled.connect(self._recolor)
        self._recolor(False)

    def _recolor(self, on):
        self.setIcon(theme.icon(self.icon_name, theme.ORANGE if on else theme.MUTED, 22))


class MainWindow(QMainWindow):
    TABS = [("Home", "home"), ("Libraries", "grid"), ("Search", "search"), ("Settings", "gear")]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IcePlex")
        self.setWindowIcon(QIcon(resource_path("icon.ico")))
        self.setMinimumSize(980, 640)
        self.ctx = Context(self)
        self.ctx.data_changed.connect(self._data_changed)

        self.outer = QStackedWidget()          # [setup pages | main ui | player]
        self.setCentralWidget(self.outer)

        # Main UI: sidebar + content stack
        self.main = QWidget()
        self.main.setObjectName("Root")
        h = QHBoxLayout(self.main)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(220)
        sv = QVBoxLayout(self.sidebar)
        sv.setContentsMargins(14, 22, 14, 18)
        sv.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        pix = QPixmap(resource_path("logo.png"))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand.addWidget(logo)
        name = label("IcePlex", "H2")
        brand.addWidget(name)
        brand.addStretch()
        sv.addLayout(brand)
        sv.addSpacing(22)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons = []
        for i, (text, ic) in enumerate(self.TABS):
            b = NavButton(text, ic)
            self.nav.addButton(b, i)
            self.nav_buttons.append(b)
            sv.addWidget(b)
        self.nav.idClicked.connect(self.switch_tab)
        sv.addStretch()
        self.server_lbl = label("", "Muted")
        sv.addWidget(self.server_lbl)
        h.addWidget(self.sidebar)

        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        cv.addWidget(self.stack)
        self.back_btn = QToolButton(content)
        self.back_btn.setIcon(theme.icon("back", "white", 26))
        self.back_btn.setIconSize(QSize(26, 26))
        self.back_btn.setToolTip("Back (Alt+←)")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.setFixedSize(44, 44)
        self.back_btn.setStyleSheet("QToolButton { background: rgba(20,20,26,0.75); border-radius: 22px; }"
                                    "QToolButton:hover { background: rgba(60,60,70,0.9); }")
        self.back_btn.clicked.connect(self.back)
        self.back_btn.move(16, 16)
        self.back_btn.hide()
        h.addWidget(content, 1)

        self.player = PlayerPage(self.ctx)
        self.player.closed.connect(self._player_closed)
        self.player.toggle_fullscreen.connect(self.toggle_fullscreen)

        self.setup_holder = QWidget()
        QVBoxLayout(self.setup_holder).setContentsMargins(0, 0, 0, 0)

        self.outer.addWidget(self.setup_holder)
        self.outer.addWidget(self.main)
        self.outer.addWidget(self.player)

        self.history: list[QWidget] = []
        self.tab_roots: dict[int, QWidget] = {}
        self.current_tab = 0
        self._was_maximized = False
        self._fs_by_player = False

        QShortcut(QKeySequence("Alt+Left"), self, activated=self.back)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: self.switch_tab(2))
        QApplication.instance().installEventFilter(self)

        self._restore_geometry()
        self.route()

    # ------------------------------------------------------------------ routing

    def route(self):
        """Sign-in -> server picker -> main UI, depending on what we have."""
        self._reset_main()
        lay = self.setup_holder.layout()
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not self.ctx.client.token:
            lay.addWidget(SignInPage(self.ctx, self.route))
            self.outer.setCurrentWidget(self.setup_holder)
        elif not self.ctx.client.server:
            lay.addWidget(ServerPage(self.ctx, self.route))
            self.outer.setCurrentWidget(self.setup_holder)
        else:
            self.server_lbl.setText(f"Server: {self.ctx.server_name()}")
            self.outer.setCurrentWidget(self.main)
            self.switch_tab(0)

    def _reset_main(self):
        for w in self.history:
            self.stack.removeWidget(w)
            w.deleteLater()
        self.history = []
        for w in self.tab_roots.values():
            self.stack.removeWidget(w)
            w.deleteLater()
        self.tab_roots = {}

    # ------------------------------------------------------------------ tabs & history

    def switch_tab(self, i: int):
        # Leaving detail pages behind when switching tabs, like a phone app.
        for w in self.history:
            self.stack.removeWidget(w)
            w.deleteLater()
        self.history = []
        self.current_tab = i
        self.nav_buttons[i].setChecked(True)
        if i not in self.tab_roots:
            page = [HomePage, LibrariesPage, SearchPage, SettingsPage][i](self.ctx)
            self.tab_roots[i] = page
            self.stack.addWidget(page)
        elif hasattr(self.tab_roots[i], "refresh") and i in (1, 3):
            self.tab_roots[i].refresh()
        page = self.tab_roots[i]
        self.stack.setCurrentWidget(page)
        if hasattr(page, "focus"):
            page.focus()
        self._update_back()

    def push(self, page: QWidget):
        self.history.append(page)
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self._update_back()

    def back(self):
        if self.outer.currentWidget() is not self.main or not self.history:
            return
        page = self.history.pop()
        self.stack.removeWidget(page)
        page.deleteLater()
        self.stack.setCurrentWidget(self.history[-1] if self.history else self.tab_roots[self.current_tab])
        self._update_back()

    def _update_back(self):
        self.back_btn.setVisible(bool(self.history))
        self.back_btn.raise_()

    def current_page(self) -> Optional[QWidget]:
        return self.stack.currentWidget()

    # ------------------------------------------------------------------ data refresh

    def _data_changed(self):
        page = self.current_page()
        if page is not None and hasattr(page, "refresh"):
            page.refresh()
        home = self.tab_roots.get(0)
        if home is not None and home is not page:
            home.refresh()

    def libraries_changed(self):
        for i in (0, 1):
            p = self.tab_roots.get(i)
            if p is not None:
                p.refresh()

    # ------------------------------------------------------------------ player

    def play(self, item: Item, from_start: bool = False):
        self.outer.setCurrentWidget(self.player)
        self.player.start(item, from_start)

    def _player_closed(self):
        if self._fs_by_player and self.isFullScreen():
            self.toggle_fullscreen()
        self.outer.setCurrentWidget(self.main)
        self.player.set_fullscreen_icon(self.isFullScreen())

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._fs_by_player = False
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self._fs_by_player = self.outer.currentWidget() is self.player
            self.showFullScreen()
        self.player.set_fullscreen_icon(self.isFullScreen())

    # ------------------------------------------------------------------ events

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonRelease and e.button() == Qt.BackButton:
            self.back()
            return True
        if t == QEvent.KeyPress and e.key() == Qt.Key_Backspace and self.outer.currentWidget() is self.main:
            if not isinstance(QApplication.focusWidget(), QLineEdit):
                self.back()
                return True
        return super().eventFilter(obj, e)

    def _restore_geometry(self):
        g = self.ctx.settings["geometry"]
        if g:
            try:
                self.restoreGeometry(QByteArray.fromBase64(g.encode()))
            except Exception:
                pass
        else:
            self.resize(1360, 860)

    def show_initial(self):
        if self.ctx.settings["maximized"]:
            self.showMaximized()
        else:
            self.show()

    def closeEvent(self, e):
        if not self.isFullScreen():
            self.ctx.settings["maximized"] = self.isMaximized()
            if not self.isMaximized():
                self.ctx.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.player.shutdown()
        super().closeEvent(e)
