"""Colors, fonts, stylesheet and the vector icons used across the app."""
from __future__ import annotations

import math
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap

ORANGE = "#ff8c00"
ORANGE_HOVER = "#ffa333"
BG = "#0b0b0e"
PANEL = "#131318"
CARD = "#1c1c22"
LINE = "#2a2a32"
TEXT = "#f2f2f5"
MUTED = "#9a9aa6"

FONT_FAMILY = "Segoe UI" if sys.platform == "win32" else "Noto Sans"


def font(size: float, weight: QFont.Weight = QFont.Normal) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


STYLESHEET = f"""
* {{
    font-family: "{FONT_FAMILY}";
    color: {TEXT};
}}
QMainWindow, QWidget#Root, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget#Page {{
    background: {BG};
}}
QWidget#Sidebar {{
    background: {PANEL};
    border-right: 1px solid {LINE};
}}
QWidget#TopBar {{
    background: {BG};
}}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Accent {{ color: {ORANGE}; font-weight: 600; }}
QLabel#H1 {{ font-size: 26pt; font-weight: 700; }}
QLabel#H2 {{ font-size: 15pt; font-weight: 700; }}
QLabel#H3 {{ font-size: 11.5pt; font-weight: 600; }}

QPushButton {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 10.5pt;
    font-weight: 600;
}}
QPushButton:hover {{ background: #26262e; border-color: #3a3a44; }}
QPushButton:pressed {{ background: #1a1a20; }}
QPushButton:disabled {{ color: #666; }}
QPushButton#Primary {{
    background: {ORANGE};
    border: none;
    color: #111;
    padding: 10px 26px;
    font-size: 11.5pt;
    font-weight: 700;
}}
QPushButton#Primary:hover {{ background: {ORANGE_HOVER}; }}
QPushButton#Watched {{ color: {ORANGE}; border-color: {ORANGE}; }}

QPushButton#Nav {{
    background: transparent;
    border: none;
    border-radius: 8px;
    text-align: left;
    padding: 10px 14px;
    font-size: 11pt;
    font-weight: 600;
    color: {MUTED};
}}
QPushButton#Nav:hover {{ background: #1e1e25; color: {TEXT}; }}
QPushButton#Nav:checked {{ background: #2a1d0c; color: {ORANGE}; }}

QToolButton {{
    background: transparent;
    border: none;
    border-radius: 18px;
    padding: 6px;
}}
QToolButton:hover {{ background: rgba(255,255,255,0.10); }}
QToolButton::menu-indicator {{ image: none; }}

QLineEdit {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 12pt;
    selection-background-color: {ORANGE};
}}
QLineEdit:focus {{ border-color: {ORANGE}; }}

QComboBox {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 7px 12px;
    min-width: 120px;
    font-size: 10.5pt;
}}
QComboBox:hover {{ border-color: #3a3a44; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {LINE};
    selection-background-color: #2a1d0c;
    selection-color: {ORANGE};
    outline: none;
}}

QCheckBox {{ spacing: 10px; font-size: 11pt; }}
QCheckBox::indicator {{
    width: 20px; height: 20px; border-radius: 5px;
    border: 2px solid #4a4a55; background: {CARD};
}}
QCheckBox::indicator:checked {{ background: {ORANGE}; border-color: {ORANGE}; }}

QMenu {{
    background: #1d1d23;
    border: 1px solid {LINE};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 8px 28px 8px 14px; border-radius: 6px; font-size: 10.5pt; }}
QMenu::item:selected {{ background: #2a1d0c; color: {ORANGE}; }}
QMenu::item:disabled {{ color: {MUTED}; font-weight: 700; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 6px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #33333c; border-radius: 4px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #4a4a55; }}
QScrollBar:horizontal {{ background: transparent; height: 0px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    background: {CARD};
    border-radius: 10px;
    margin: 4px 0;
    padding: 14px;
}}
QListWidget::item:hover {{ background: #26262e; }}
QListWidget::item:selected {{ background: #2a1d0c; color: {ORANGE}; }}

QToolTip {{
    background: #1d1d23; color: {TEXT}; border: 1px solid {LINE}; padding: 4px 8px;
}}
"""


def full_stylesheet(assets_dir: str) -> str:
    """Stylesheet plus the rules that need image files (checkbox tick, dropdown arrow)."""
    d = assets_dir.replace("\\", "/")
    return STYLESHEET + f"""
QCheckBox::indicator:checked {{ image: url("{d}/check.png"); }}
QComboBox::down-arrow {{ image: url("{d}/down.png"); width: 14px; height: 14px; }}
"""


# ---------------------------------------------------------------------------
# Vector icons (drawn, so they're crisp at any size and need no image files)
# ---------------------------------------------------------------------------

def _pen(color, w):
    p = QPen(QColor(color), w)
    p.setCapStyle(Qt.RoundCap)
    p.setJoinStyle(Qt.RoundJoin)
    return p


def _draw(name: str, p: QPainter, s: float, color: str):
    c = QColor(color)
    lw = s * 0.09
    if name == "play":
        path = QPainterPath()
        path.moveTo(s * 0.30, s * 0.18)
        path.lineTo(s * 0.82, s * 0.50)
        path.lineTo(s * 0.30, s * 0.82)
        path.closeSubpath()
        p.setPen(_pen(color, s * 0.06))
        p.setBrush(c)
        p.drawPath(path)
    elif name == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.18, s * 0.17, s * 0.64), s * 0.04, s * 0.04)
        p.drawRoundedRect(QRectF(s * 0.59, s * 0.18, s * 0.17, s * 0.64), s * 0.04, s * 0.04)
    elif name in ("back10", "fwd10"):
        fwd = name == "fwd10"
        p.setPen(_pen(color, s * 0.075))
        p.setBrush(Qt.NoBrush)
        R = s * 0.36
        p.drawArc(QRectF(s * .5 - R, s * .5 - R, 2 * R, 2 * R), 110 * 16, 320 * 16)
        # arrow head where the arc ends at the top, pointing the way time moves
        ang = math.radians(70 if fwd else 110)
        ax, ay = s * .5 + R * math.cos(ang), s * .5 - R * math.sin(ang)
        d = s * 0.10
        tip = 1 if fwd else -1
        path = QPainterPath()
        path.moveTo(ax - tip * d * 0.6, ay - d)
        path.lineTo(ax + tip * d * 0.6, ay)
        path.lineTo(ax - tip * d * 0.6, ay + d)
        p.drawPath(path)
        f = QFont(FONT_FAMILY)
        f.setPixelSize(int(s * 0.30))
        f.setBold(True)
        p.setFont(f)
        p.setPen(c)
        p.drawText(QRectF(0, s * 0.03, s, s), Qt.AlignCenter, "10")
    elif name == "close":
        p.setPen(_pen(color, lw))
        p.drawLine(QPointF(s * .27, s * .27), QPointF(s * .73, s * .73))
        p.drawLine(QPointF(s * .73, s * .27), QPointF(s * .27, s * .73))
    elif name == "back":
        p.setPen(_pen(color, lw))
        p.drawLine(QPointF(s * .60, s * .22), QPointF(s * .32, s * .50))
        p.drawLine(QPointF(s * .32, s * .50), QPointF(s * .60, s * .78))
    elif name in ("left", "right"):
        p.setPen(_pen(color, lw))
        if name == "left":
            p.drawLine(QPointF(s * .58, s * .25), QPointF(s * .36, s * .50))
            p.drawLine(QPointF(s * .36, s * .50), QPointF(s * .58, s * .75))
        else:
            p.drawLine(QPointF(s * .42, s * .25), QPointF(s * .64, s * .50))
            p.drawLine(QPointF(s * .64, s * .50), QPointF(s * .42, s * .75))
    elif name in ("fullscreen", "unfullscreen"):
        p.setPen(_pen(color, lw * 0.85))
        a, b, k = s * 0.2, s * 0.8, s * 0.2
        corners = [(a, a, 1, 1), (b, a, -1, 1), (a, b, 1, -1), (b, b, -1, -1)]
        for x, y, dx, dy in corners:
            if name == "unfullscreen":
                x, y = x + dx * k, y + dy * k
                dx, dy = -dx, -dy
            p.drawLine(QPointF(x, y), QPointF(x + dx * k, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * k))
    elif name in ("volume", "mute"):
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        path.moveTo(s * .14, s * .38)
        path.lineTo(s * .30, s * .38)
        path.lineTo(s * .50, s * .20)
        path.lineTo(s * .50, s * .80)
        path.lineTo(s * .30, s * .62)
        path.lineTo(s * .14, s * .62)
        path.closeSubpath()
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(color, lw * 0.75))
        if name == "volume":
            p.drawArc(QRectF(s * .40, s * .32, s * .24, s * .36), -50 * 16, 100 * 16)
            p.drawArc(QRectF(s * .38, s * .20, s * .42, s * .60), -50 * 16, 100 * 16)
        else:
            p.drawLine(QPointF(s * .62, s * .38), QPointF(s * .84, s * .62))
            p.drawLine(QPointF(s * .84, s * .38), QPointF(s * .62, s * .62))
    elif name == "tracks":
        p.setPen(_pen(color, lw * 0.8))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(s * .14, s * .22, s * .72, s * .50), s * .1, s * .1)
        p.drawLine(QPointF(s * .28, s * .42), QPointF(s * .52, s * .42))
        p.drawLine(QPointF(s * .58, s * .42), QPointF(s * .72, s * .42))
        p.drawLine(QPointF(s * .28, s * .56), QPointF(s * .42, s * .56))
        p.drawLine(QPointF(s * .48, s * .56), QPointF(s * .72, s * .56))
    elif name == "home":
        p.setPen(_pen(color, lw * 0.8))
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(s * .16, s * .46)
        path.lineTo(s * .50, s * .16)
        path.lineTo(s * .84, s * .46)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(s * .26, s * .42, s * .48, s * .42), s * .05, s * .05)
    elif name == "grid":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        for x in (0.18, 0.54):
            for y in (0.18, 0.54):
                p.drawRoundedRect(QRectF(s * x, s * y, s * .28, s * .28), s * .06, s * .06)
    elif name == "search":
        p.setPen(_pen(color, lw * 0.9))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(s * .16, s * .16, s * .46, s * .46))
        p.drawLine(QPointF(s * .56, s * .56), QPointF(s * .82, s * .82))
    elif name == "gear":
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        path = QPainterPath()
        cx = cy = s / 2
        teeth = 8
        for i in range(teeth * 2):
            ang = math.pi * 2 * i / (teeth * 2)
            r = s * (0.40 if i % 2 == 0 else 0.31)
            for da in (-0.17, 0.17):
                a = ang + da
                pt = QPointF(cx + r * math.cos(a), cy + r * math.sin(a))
                if i == 0 and da < 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
        path.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(QPointF(cx, cy), s * .13, s * .13)
        p.drawPath(path.subtracted(hole))
    elif name == "check":
        p.setPen(_pen(color, lw))
        p.drawLine(QPointF(s * .22, s * .52), QPointF(s * .42, s * .72))
        p.drawLine(QPointF(s * .42, s * .72), QPointF(s * .78, s * .30))
    elif name == "restart":
        p.setPen(_pen(color, lw * 0.85))
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(s * .22, s * .22), QPointF(s * .22, s * .78))
        path = QPainterPath()
        path.moveTo(s * .80, s * .20)
        path.lineTo(s * .36, s * .50)
        path.lineTo(s * .80, s * .80)
        path.closeSubpath()
        p.setBrush(c)
        p.drawPath(path)
    elif name == "signal_off":
        p.setPen(_pen(color, lw * 0.8))
        p.setBrush(Qt.NoBrush)
        for i, r in enumerate((0.12, 0.24, 0.36)):
            p.drawArc(QRectF(s * (.5 - r), s * (.62 - r), s * 2 * r, s * 2 * r), 45 * 16, 90 * 16)
        p.drawLine(QPointF(s * .18, s * .18), QPointF(s * .82, s * .82))


def icon(name: str, color: str = TEXT, size: int = 24) -> QIcon:
    ico = QIcon()
    for scale in (1, 2):
        px = QPixmap(size * scale, size * scale)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        _draw(name, p, size * scale, color)
        p.end()
        px.setDevicePixelRatio(scale)
        ico.addPixmap(px)
    return ico
