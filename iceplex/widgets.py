"""Reusable pieces: spinner, poster cards, rows, grids, backdrop."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget, QWidgetItem)

from . import theme
from .plex import Item
from .util import image_loader


# ---------------------------------------------------------------------------
# Orange spinning ring
# ---------------------------------------------------------------------------

class Spinner(QWidget):
    def __init__(self, size: int = 40, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def _tick(self):
        self.angle = (self.angle + 7) % 360
        self.update()

    def showEvent(self, e):
        self.timer.start(16)

    def hideEvent(self, e):
        self.timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        lw = max(3.0, w * 0.11)
        r = QRectF(lw / 2, lw / 2, w - lw, w - lw)
        pen = QPen(QColor(255, 140, 0, 50), lw)
        p.setPen(pen)
        p.drawEllipse(r)
        pen.setColor(QColor(theme.ORANGE))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(-self.angle * 16), int(100 * 16))


def centered(widget: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.addStretch()
    lay.addWidget(widget)
    lay.addStretch()
    return w


def label(text: str = "", name: Optional[str] = None, wrap: bool = False, size: Optional[float] = None,
          bold: bool = False) -> QLabel:
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    if wrap:
        l.setWordWrap(True)
    if size or bold:
        f = l.font()
        if size:
            f.setPointSizeF(size)
        if bold:
            f.setBold(True)
        l.setFont(f)
    l.setTextInteractionFlags(Qt.NoTextInteraction)
    return l


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def rounded(pix: QPixmap, w: int, h: int, radius: float, dpr: float) -> QPixmap:
    """Scales to fill w x h (cropping), with rounded corners."""
    W, H = int(w * dpr), int(h * dpr)
    scaled = pix.scaled(W, H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(W, H)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, W, H), radius * dpr, radius * dpr)
    p.setClipPath(path)
    p.drawPixmap(-(scaled.width() - W) // 2, -(scaled.height() - H) // 2, scaled)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


class ImageBox(QWidget):
    """Rounded image that loads from a URL, with progress bar / checkmark overlays."""

    def __init__(self, w: int, h: int, radius: float = 8, parent=None):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.radius = radius
        self.pix: Optional[QPixmap] = None
        self.url: Optional[str] = None
        self.progress: Optional[float] = None
        self.check = False
        self.highlight = False
        self.placeholder = "film"

    def set_url(self, url: Optional[str]):
        if url == self.url:
            return
        self.url = url
        self.pix = None
        self.update()
        if url:
            image_loader().load(url, self._loaded(url))

    def _loaded(self, url):
        def cb(pix):
            if self.url == url:
                self.pix = rounded(pix, self.width(), self.height(), self.radius, self.devicePixelRatioF())
                self.update()
        return cb

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        if self.pix:
            p.drawPixmap(0, 0, self.pix)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.CARD))
            p.drawRoundedRect(r, self.radius, self.radius)
        if self.progress is not None:
            bh = 4
            path = QPainterPath()
            path.addRoundedRect(r, self.radius, self.radius)
            p.setClipPath(path)
            p.fillRect(QRectF(0, r.height() - bh, r.width(), bh), QColor(0, 0, 0, 170))
            p.fillRect(QRectF(0, r.height() - bh, r.width() * self.progress, bh), QColor(theme.ORANGE))
            p.setClipping(False)
        if self.check:
            d = 24
            cr = QRectF(r.width() - d - 6, 6, d, d)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ORANGE))
            p.drawEllipse(cr)
            pen = QPen(QColor("white"), 2.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(cr.left() + d * .26, cr.top() + d * .52, cr.left() + d * .44, cr.top() + d * .70)
            p.drawLine(cr.left() + d * .44, cr.top() + d * .70, cr.left() + d * .76, cr.top() + d * .32)
        if self.highlight:
            p.setPen(QPen(QColor(theme.ORANGE), 2.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(1.25, 1.25, -1.25, -1.25), self.radius, self.radius)


# ---------------------------------------------------------------------------
# Poster card (click -> open info page)
# ---------------------------------------------------------------------------

class PosterCard(QWidget):
    clicked = Signal(object)

    def __init__(self, item: Item, client, width: int = 150, wide: bool = False, title: Optional[str] = None,
                 subtitle: Optional[str] = None, image_path: Optional[str] = None, current: bool = False):
        super().__init__()
        self.item = item
        self.setCursor(Qt.PointingHandCursor)
        h = int(width * 9 / 16) if wide else int(width * 1.5)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.img = ImageBox(width, h)
        path = image_path if image_path is not None else (item.get("thumb") if wide else item.poster_path)
        self.img.set_url(client.image_url(path, width * 2, h * 2))
        self.img.progress = item.progress
        self.img.check = item.show_check
        self.img.highlight = current
        lay.addWidget(self.img)
        t = label(title if title is not None else item.display_title)
        t.setFont(theme.font(10, QFont.DemiBold))
        t.setFixedWidth(width)
        t.setText(t.fontMetrics().elidedText(t.text(), Qt.ElideRight, width))
        lay.addWidget(t)
        sub = subtitle if subtitle is not None else item.display_subtitle
        if sub:
            s = label(sub, "Muted")
            s.setFont(theme.font(9))
            s.setFixedWidth(width)
            s.setText(s.fontMetrics().elidedText(sub, Qt.ElideRight, width))
            lay.addWidget(s)
        lay.addStretch()
        self.setFixedWidth(width)

    def enterEvent(self, e):
        if not self.img.highlight:
            self.img.highlight = True
            self._hover = True
            self.img.update()

    def leaveEvent(self, e):
        if getattr(self, "_hover", False):
            self.img.highlight = False
            self._hover = False
            self.img.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.item)


# ---------------------------------------------------------------------------
# Horizontal row of cards with arrow buttons
# ---------------------------------------------------------------------------

class _HScroll(QScrollArea):
    def wheelEvent(self, e):
        d = e.angleDelta()
        if abs(d.x()) > abs(d.y()) or e.modifiers() & Qt.ShiftModifier:
            amount = d.x() if abs(d.x()) > abs(d.y()) else d.y()
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - amount)
            e.accept()
        else:
            e.ignore()  # let the page scroll vertically


class CardRow(QWidget):
    def __init__(self, title: str, cards: list[QWidget], parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(32, 0, 24, 0)
        t = label(title, "H2")
        head.addWidget(t)
        head.addStretch()
        self.left = QToolButton()
        self.left.setIcon(theme.icon("left", theme.TEXT, 22))
        self.right = QToolButton()
        self.right.setIcon(theme.icon("right", theme.TEXT, 22))
        for b in (self.left, self.right):
            b.setIconSize(QSize(22, 22))
            b.setCursor(Qt.PointingHandCursor)
            head.addWidget(b)
        v.addLayout(head)

        self.scroll = _HScroll()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("Page")
        h = QHBoxLayout(inner)
        h.setContentsMargins(32, 0, 32, 0)
        h.setSpacing(16)
        for c in cards:
            h.addWidget(c, 0, Qt.AlignTop)
        h.addStretch()
        self.scroll.setWidget(inner)
        tallest = max((c.sizeHint().height() for c in cards), default=100)
        self.scroll.setFixedHeight(tallest + 6)
        v.addWidget(self.scroll)

        self.left.clicked.connect(lambda: self._page(-1))
        self.right.clicked.connect(lambda: self._page(1))
        self.anim = None

    def _page(self, direction: int):
        bar = self.scroll.horizontalScrollBar()
        target = max(0, min(bar.maximum(), bar.value() + direction * int(self.scroll.width() * 0.8)))
        self.anim = QPropertyAnimation(bar, b"value", self)
        self.anim.setDuration(300)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.setStartValue(bar.value())
        self.anim.setEndValue(target)
        self.anim.start()

    def scroll_to(self, widget: QWidget):
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(widget, 200, 0))


# ---------------------------------------------------------------------------
# Flow layout (wrapping grid of cards)
# ---------------------------------------------------------------------------

class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing=18):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        sp = self._spacing
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line_h > 0:
                x = r.x()
                y += line_h + sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + sp
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# ---------------------------------------------------------------------------
# Scrollable page base
# ---------------------------------------------------------------------------

class ScrollPage(QScrollArea):
    """A vertically scrolling page. Put content into self.body (a QVBoxLayout)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("Page")
        self.body = QVBoxLayout(self.inner)
        self.body.setContentsMargins(0, 0, 0, 40)
        self.body.setSpacing(28)
        self.setWidget(self.inner)

    def clear(self):
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
            elif it.layout():
                _clear_layout(it.layout())

    def padded(self, widget: QWidget, h: int = 32) -> QWidget:
        wrap = QWidget()
        l = QHBoxLayout(wrap)
        l.setContentsMargins(h, 0, h, 0)
        l.addWidget(widget, 1)
        if widget.maximumWidth() < 10000:
            l.addStretch()   # keep width-limited text left-aligned instead of centered
        return wrap


def _clear_layout(layout):
    while layout.count():
        it = layout.takeAt(0)
        if it.widget():
            it.widget().deleteLater()
        elif it.layout():
            _clear_layout(it.layout())


# ---------------------------------------------------------------------------
# Wide backdrop that fades into the page
# ---------------------------------------------------------------------------

class Backdrop(QWidget):
    def __init__(self, height: int = 380, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.src: Optional[QPixmap] = None
        self.url = None

    def set_url(self, url):
        if url == self.url:
            return
        self.url = url
        if url:
            image_loader().load(url, self._loaded(url))

    def _loaded(self, url):
        def cb(pix):
            if self.url == url:
                self.src = pix
                self.update()
        return cb

    def paintEvent(self, e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        if self.src:
            dpr = self.devicePixelRatioF()
            scaled = self.src.scaled(int(w * dpr), int(h * dpr), Qt.KeepAspectRatioByExpanding,
                                     Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            sw, sh = scaled.width() / dpr, scaled.height() / dpr
            p.drawPixmap(QRectF(-(sw - w) / 2, -(sh - h) * 0.3, sw, sh), scaled, QRectF(scaled.rect()))
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(11, 11, 14, 90))
        g.setColorAt(0.45, QColor(11, 11, 14, 60))
        g.setColorAt(1.0, QColor(11, 11, 14, 255))
        p.fillRect(self.rect(), QBrush(g))
        side = QLinearGradient(0, 0, w, 0)
        side.setColorAt(0.0, QColor(11, 11, 14, 200))
        side.setColorAt(0.55, QColor(11, 11, 14, 0))
        p.fillRect(self.rect(), QBrush(side))


class ClickRow(QFrame):
    """A full-width clickable row (episode lists, server list)."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("ClickRow")
        self.setStyleSheet(
            f"QFrame#ClickRow {{ background: {theme.CARD}; border-radius: 10px; }}"
            f"QFrame#ClickRow:hover {{ background: #26262e; }}")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
