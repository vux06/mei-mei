"""Custom widgets: animated assistant orb, level meter, transcript, activity log, confirmation bar."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout,
                               QWidget)

from . import styles


class Orb(QWidget):
    """Central assistant indicator. Motion is driven by the real state and the real mic/speaker levels."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(260, 260)
        self.state = "IDLE"
        self.mic_level = 0.0
        self.out_level = 0.0
        self.active = True          # False when the mic is not actually listening (paused/disabled)
        self._phase = 0.0
        self._sm_level = 0.0
        self._color = QColor(styles.STATE_COLORS["IDLE"])
        self._t0 = time.time()
        t = QTimer(self)
        t.timeout.connect(self._tick)
        t.start(33)

    def set_state(self, s: str) -> None:
        self.state = s

    def _tick(self) -> None:
        self._phase = time.time() - self._t0
        target = QColor(styles.STATE_COLORS.get(self.state, "#2fb8d6"))
        if not self.active and self.state == "IDLE":
            target = QColor("#4a5878")
        c = self._color
        self._color = QColor(int(c.red() + (target.red() - c.red()) * 0.18), int(c.green() + (target.green() - c.green()) * 0.18),
                             int(c.blue() + (target.blue() - c.blue()) * 0.18))
        lvl = self.out_level if self.state == "SPEAKING" else self.mic_level
        self._sm_level += (lvl - self._sm_level) * 0.35
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) * 0.30
        col = self._color
        ph, st = self._phase, self.state
        breathe = 0.5 + 0.5 * math.sin(ph * (1.2 if st == "IDLE" else 3.0))
        lvl = self._sm_level
        scale = 1.0 + (0.05 * breathe if st == "IDLE" else 0.0) + min(0.35, lvl * 0.9)

        # outer glow
        g = QRadialGradient(QPointF(cx, cy), r * 2.3)
        a = int(60 + 60 * breathe + 90 * lvl)
        g.setColorAt(0.0, QColor(col.red(), col.green(), col.blue(), min(200, a)))
        g.setColorAt(0.5, QColor(col.red(), col.green(), col.blue(), 22))
        g.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), r * 2.3, r * 2.3)

        # reactive rings
        n = 3
        for i in range(n):
            k = ((ph * (0.35 if st != "LISTENING" else 0.7) + i / n) % 1.0)
            rr = r * (1.05 + k * (0.9 + lvl * 1.2))
            pen = QPen(QColor(col.red(), col.green(), col.blue(), int(110 * (1 - k))), 1.6)
            if st in ("IDLE",) and not self.active:
                pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), rr, rr)

        # state-specific rotating elements
        if st in ("PROCESSING", "EXECUTING"):
            n_arcs = 3 if st == "PROCESSING" else 6
            for i in range(n_arcs):
                ang = (ph * (140 if st == "PROCESSING" else -220) + i * 360 / n_arcs) % 360
                pen = QPen(QColor(col.red(), col.green(), col.blue(), 230), 3.2 if st == "PROCESSING" else 5)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                rect = QRectF(cx - r * 1.25, cy - r * 1.25, r * 2.5, r * 2.5)
                p.drawArc(rect, int(-ang * 16), int((50 if st == "PROCESSING" else 4) * 16))
        if st == "ERROR":
            flash = 0.5 + 0.5 * math.sin(ph * 12)
            p.setPen(QPen(QColor(255, 77, 94, int(120 + 120 * flash)), 3))
            p.drawEllipse(QPointF(cx, cy), r * 1.4, r * 1.4)

        # core sphere
        rc = r * scale
        core = QRadialGradient(QPointF(cx - rc * 0.3, cy - rc * 0.35), rc * 1.4)
        core.setColorAt(0.0, QColor(255, 255, 255, 235))
        core.setColorAt(0.25, col.lighter(130))
        core.setColorAt(1.0, col.darker(260))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(cx, cy), rc, rc)

        # waveform ring (speaking / listening)
        if st in ("SPEAKING", "LISTENING"):
            pen = QPen(QColor(255, 255, 255, 170), 1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            pts = 90
            path = []
            for i in range(pts + 1):
                a2 = 2 * math.pi * i / pts
                amp = (math.sin(a2 * 7 + ph * 9) * 0.5 + math.sin(a2 * 3 - ph * 5) * 0.5) * (4 + 34 * lvl)
                rr = rc * 1.12 + amp
                path.append(QPointF(cx + math.cos(a2) * rr, cy + math.sin(a2) * rr))
            for i in range(len(path) - 1):
                p.drawLine(path[i], path[i + 1])
        p.end()


class LevelBar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(8)
        self.value = 0.0
        self.color = QColor(styles.ACCENT)

    def set_value(self, v: float) -> None:
        self.value = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(styles.BORDER))
        p.drawRoundedRect(self.rect(), 4, 4)
        p.setBrush(self.color)
        w = int(self.width() * self.value)
        p.drawRoundedRect(QRectF(0, 0, w, self.height()), 4, 4)
        p.end()


class Pill(QLabel):
    def __init__(self, text=""):
        super().__init__(text)
        self.setObjectName("pill")

    def set_status(self, text: str, color: str = styles.DIM) -> None:
        self.setText(text)
        self.setStyleSheet(f"color: {color}; border-color: {color}55;")


class Transcript(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(False)
        self.document().setDefaultStyleSheet("p{margin:0}")

    def add(self, role: str, text: str) -> None:
        text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")).replace("\n", "<br>")
        ts = time.strftime("%H:%M")
        if role == "user":
            html = (f'<table width="100%"><tr><td width="22%"></td><td align="right" style="background:#13304a;'
                    f'border-radius:12px;padding:8px 12px"><span style="color:#7d8fb3;font-size:10px">YOU · {ts}</span><br>{text}</td></tr></table>')
        elif role == "assistant":
            html = (f'<table width="100%"><tr><td style="background:#2a1830;border-radius:12px;padding:8px 12px">'
                    f'<span style="color:#ff5fc8;font-size:10px">MEI MEI · {ts}</span><br>{text}</td><td width="22%"></td></tr></table>')
        else:
            html = f'<p align="center" style="color:#7d8fb3;font-size:11px">— {text} —</p>'
        self.append(html + "<p></p>")
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


class ActivityLog(QPlainTextEdit):
    COLORS = {"info": "#7d8fb3", "tool": "#ffb84d", "warn": "#ffd27a", "error": "#ff6b7a", "deny": "#ff6b7a", "user": "#38e1ff"}

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(600)
        self.setFont(QFont("monospace", 10))

    def add(self, level: str, text: str) -> None:
        c = self.COLORS.get(level, styles.DIM)
        t = text.replace("&", "&amp;").replace("<", "&lt;")
        self.appendHtml(f'<span style="color:#4a5878">{time.strftime("%H:%M:%S")}</span> '
                        f'<span style="color:{c}">{level.upper():5}</span> {t}')


class ConfirmBar(QFrame):
    decided = Signal(int, bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("confirm")
        self.req_id = 0
        lay = QHBoxLayout(self)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.allow = QPushButton("Allow")
        self.allow.setObjectName("allow")
        self.deny = QPushButton("Deny")
        self.deny.setObjectName("deny")
        lay.addWidget(self.label, 1)
        lay.addWidget(self.allow)
        lay.addWidget(self.deny)
        self.allow.clicked.connect(lambda: self.decided.emit(self.req_id, True))
        self.deny.clicked.connect(lambda: self.decided.emit(self.req_id, False))
        self.hide()

    def show_request(self, req_id: int, summary: str, reason: str) -> None:
        self.req_id = req_id
        self.label.setText(f"<b>Confirm:</b> {summary}<br><span style='color:#c9a15a'>{reason} — or just say “yes” / “no”</span>")
        self.show()
