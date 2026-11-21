"""Main window + settings dialog. All state shown here is the real state reported by the core (no fake animation)."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QPlainTextEdit,
                               QPushButton, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QVBoxLayout,
                               QWidget)

from core.app import App
from core.config import load_personality
from core.paths import PERSONALITY_FILE
from voice.audio import list_input_sources, list_output_sinks
from . import styles
from .components import ActivityLog, ConfirmBar, LevelBar, Orb, Pill, Transcript

log = logging.getLogger("meimei.gui")


class Bridge(QObject):
    """Thread-safe adapter: bus events (any thread) -> Qt signal (delivered on the GUI thread)."""
    event = Signal(str, dict)

    def __init__(self, bus):
        super().__init__()
        bus.on("*", lambda e, d: self.event.emit(e, d))


def tray_icon(color: str = "#38e1ff") -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(8, 8, 48, 48)
    p.setBrush(QColor("#070b14"))
    p.drawEllipse(24, 24, 16, 16)
    p.end()
    return QIcon(pm)


class SettingsDialog(QDialog):
    def __init__(self, app: App, parent=None):
        super().__init__(parent)
        self.app, self.s = app, app.settings
        self.setWindowTitle("Mei Mei — Settings")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # ---- Voice
        v = QWidget(); f = QFormLayout(v)
        self.mic = QComboBox(); self.mic.addItem("System default", None)
        for src in list_input_sources():
            self.mic.addItem(src["description"] + ("  (default)" if src["default"] else ""), src["name"])
        cur = self.s.get("audio.input_source")
        idx = self.mic.findData(cur)
        self.mic.setCurrentIndex(max(0, idx))
        f.addRow("Microphone", self.mic)
        self.sink = QComboBox(); self.sink.addItem("System default", None)
        for k in list_output_sinks():
            self.sink.addItem(k["description"], k["name"])
        self.sink.setCurrentIndex(max(0, self.sink.findData(self.s.get("audio.output_sink"))))
        f.addRow("Speaker output", self.sink)
        self.sens = QSlider(Qt.Horizontal); self.sens.setRange(0, 100); self.sens.setValue(int(self.s.get("wake_word.sensitivity", 0.6) * 100))
        f.addRow("Wake sensitivity", self.sens)
        self.gain = QSlider(Qt.Horizontal); self.gain.setRange(10, 400); self.gain.setValue(int(self.s.get("audio.input_gain", 1.0) * 100))
        f.addRow("Mic gain", self.gain)
        self.wake_on = QCheckBox("Always-on wake word (“Hey Mei Mei”)"); self.wake_on.setChecked(bool(self.s.get("wake_word.enabled", True)))
        f.addRow(self.wake_on)
        self.bare = QCheckBox("Also accept just “Mei Mei” at the start"); self.bare.setChecked(bool(self.s.get("wake_word.allow_bare_name", True)))
        f.addRow(self.bare)
        self.chime = QCheckBox("Activation sound"); self.chime.setChecked(bool(self.s.get("wake_word.activation_sound", True)))
        f.addRow(self.chime)
        self.aec = QCheckBox("Echo cancellation (PipeWire WebRTC AEC — helps with speakers; restart to apply)")
        self.aec.setChecked(bool(self.s.get("audio.echo_cancel", False)))
        f.addRow(self.aec)
        self.barge = QCheckBox("Allow interrupting her while she speaks"); self.barge.setChecked(bool(self.s.get("voice.barge_in", True)))
        f.addRow(self.barge)
        self.follow = QSpinBox(); self.follow.setRange(0, 30); self.follow.setValue(int(self.s.get("voice.followup_window_s", 0)))
        self.follow.setSuffix(" s  (0 = off)")
        f.addRow("Follow-up window", self.follow)
        self.end_sil = QSpinBox(); self.end_sil.setRange(300, 2500); self.end_sil.setSingleStep(100)
        self.end_sil.setValue(int(self.s.get("vad.end_silence_ms", 800))); self.end_sil.setSuffix(" ms")
        f.addRow("End-of-speech silence", self.end_sil)
        tabs.addTab(v, "Voice")

        # ---- Speech
        t = QWidget(); f = QFormLayout(t)
        self.tts_on = QCheckBox("Speak responses"); self.tts_on.setChecked(bool(self.s.get("tts.enabled", True)))
        f.addRow(self.tts_on)
        self.voice = QLineEdit(self.s.get("tts.voice", ""))
        f.addRow("Piper voice id", self.voice)
        self.speed = QSlider(Qt.Horizontal); self.speed.setRange(60, 170); self.speed.setValue(int(self.s.get("tts.speed", 1.0) * 100))
        f.addRow("Speaking speed", self.speed)
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(10, 150); self.vol.setValue(int(self.s.get("tts.volume", 0.9) * 100))
        f.addRow("Volume", self.vol)
        f.addRow(QLabel("<span style='color:#7d8fb3'>Voice changes apply after restart (a new voice is downloaded automatically).</span>"))
        tabs.addTab(t, "Speech")

        # ---- Claude
        c = QWidget(); f = QFormLayout(c)
        self.model = QLineEdit(self.s.get("claude.model", "")); f.addRow("Model", self.model)
        self.effort = QComboBox(); self.effort.addItems(["low", "medium", "high"])
        self.effort.setCurrentText(self.s.get("claude.effort", "low")); f.addRow("Effort", self.effort)
        self.web = QCheckBox("Allow web search/fetch tools"); self.web.setChecked(bool(self.s.get("claude.allow_web_tools", True)))
        f.addRow(self.web)
        f.addRow(QLabel("<span style='color:#7d8fb3'>Applies to the next new session (Clear Context or restart).</span>"))
        tabs.addTab(c, "Claude")

        # ---- Personality
        pw = QWidget(); pl = QVBoxLayout(pw)
        self.persona = QPlainTextEdit(load_personality())
        pl.addWidget(self.persona)
        pl.addWidget(QLabel(f"<span style='color:#7d8fb3'>Saved to {PERSONALITY_FILE.name}. Re-applied on every restart and context clear.</span>"))
        tabs.addTab(pw, "Personality")

        # ---- Memory
        mw = QWidget(); ml = QVBoxLayout(mw)
        self.mem_list = QListWidget(); ml.addWidget(self.mem_list)
        row = QHBoxLayout()
        self.mem_key = QLineEdit(); self.mem_key.setPlaceholderText("key (e.g. favourite editor)")
        self.mem_val = QLineEdit(); self.mem_val.setPlaceholderText("value")
        add = QPushButton("Save"); add.clicked.connect(self._mem_add)
        dele = QPushButton("Delete selected"); dele.setObjectName("danger"); dele.clicked.connect(self._mem_del)
        row.addWidget(self.mem_key); row.addWidget(self.mem_val); row.addWidget(add)
        ml.addLayout(row); ml.addWidget(dele)
        ml.addWidget(QLabel("<span style='color:#7d8fb3'>Permanent memory survives “Clear context”. Only explicit saves land here.</span>"))
        tabs.addTab(mw, "Memory")
        self._mem_refresh()

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _mem_refresh(self):
        self.mem_list.clear()
        for m in self.app.memory.all_memory():
            self.mem_list.addItem(f"{m['key']}  →  {m['value']}")

    def _mem_add(self):
        if self.mem_key.text().strip() and self.mem_val.text().strip():
            self.app.memory.remember(self.mem_key.text(), self.mem_val.text())
            self.mem_key.clear(); self.mem_val.clear(); self._mem_refresh()

    def _mem_del(self):
        it = self.mem_list.currentItem()
        if it:
            self.app.memory.forget(it.text().split("  →  ")[0]); self._mem_refresh()

    def _save(self):
        s = self.s
        s.set("audio.input_source", self.mic.currentData(), save=False)
        s.set("audio.output_sink", self.sink.currentData(), save=False)
        s.set("wake_word.sensitivity", self.sens.value() / 100, save=False)
        s.set("audio.input_gain", self.gain.value() / 100, save=False)
        s.set("wake_word.allow_bare_name", self.bare.isChecked(), save=False)
        s.set("wake_word.activation_sound", self.chime.isChecked(), save=False)
        s.set("voice.barge_in", self.barge.isChecked(), save=False)
        s.set("audio.echo_cancel", self.aec.isChecked(), save=False)
        s.set("voice.followup_window_s", self.follow.value(), save=False)
        s.set("vad.end_silence_ms", self.end_sil.value(), save=False)
        s.set("tts.enabled", self.tts_on.isChecked(), save=False)
        s.set("tts.voice", self.voice.text().strip(), save=False)
        s.set("tts.speed", self.speed.value() / 100, save=False)
        s.set("tts.volume", self.vol.value() / 100, save=False)
        s.set("claude.model", self.model.text().strip(), save=False)
        s.set("claude.effort", self.effort.currentText(), save=False)
        s.set("claude.allow_web_tools", self.web.isChecked(), save=False)
        s.save()
        if self.persona.toPlainText().strip() != load_personality():
            PERSONALITY_FILE.write_text(self.persona.toPlainText().strip() + "\n")
        if self.app.voice:
            self.app.voice.apply_settings()
            (self.app.voice.enable if self.wake_on.isChecked() else self.app.voice.disable)()
        self.accept()


class MainWindow(QMainWindow):
    clear_finished = Signal()          # emitted from the assistant thread; delivered on the GUI thread

    def __init__(self, app: App, bridge: Bridge):
        super().__init__()
        self.app = app
        self.setWindowTitle("Mei Mei")
        self.resize(1120, 760)
        self.state = "IDLE"
        self.really_quit = False
        root = QWidget(); root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root); outer.setContentsMargins(16, 16, 16, 12); outer.setSpacing(14)

        # ---------------- left: brand, orb, status, controls
        left = QFrame(); left.setObjectName("panel"); left.setFixedWidth(380)
        ll = QVBoxLayout(left); ll.setContentsMargins(18, 16, 18, 16); ll.setSpacing(8)
        brand = QLabel("MEI MEI"); brand.setObjectName("brand"); brand.setAlignment(Qt.AlignCenter)
        sub = QLabel("PERSONAL AI ASSISTANT"); sub.setObjectName("sub"); sub.setAlignment(Qt.AlignCenter)
        ll.addWidget(brand); ll.addWidget(sub)
        self.orb = Orb(); ll.addWidget(self.orb, 1)
        self.state_lbl = QLabel("IDLE"); self.state_lbl.setObjectName("state"); self.state_lbl.setAlignment(Qt.AlignCenter)
        self.caption = QLabel("Say “Hey Mei Mei”"); self.caption.setObjectName("caption"); self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        ll.addWidget(self.state_lbl); ll.addWidget(self.caption)
        self.mic_bar = LevelBar(); ll.addWidget(self.mic_bar)

        pills = QVBoxLayout(); pills.setSpacing(5)
        self.p_mic, self.p_wake, self.p_claude, self.p_sess, self.p_ctx = Pill(), Pill(), Pill(), Pill(), Pill()
        for p in (self.p_mic, self.p_wake, self.p_claude, self.p_sess, self.p_ctx):
            pills.addWidget(p)
        ll.addLayout(pills)

        self.btn_ptt = QPushButton("Hold to talk  (fallback)")
        self.btn_ptt.pressed.connect(lambda: self.app.voice and self.app.voice.ptt_begin())
        self.btn_ptt.released.connect(lambda: self.app.voice and self.app.voice.ptt_end())
        self.btn_listen = QPushButton("Pause listening"); self.btn_listen.clicked.connect(self._toggle_listen)
        self.btn_stop = QPushButton("Stop speaking"); self.btn_stop.clicked.connect(self._stop_speaking)
        self.btn_clear = QPushButton("CLEAR CONTEXT"); self.btn_clear.setObjectName("danger"); self.btn_clear.clicked.connect(self._clear)
        self.btn_settings = QPushButton("Settings"); self.btn_settings.clicked.connect(self._settings)
        row1 = QHBoxLayout(); row1.addWidget(self.btn_listen); row1.addWidget(self.btn_stop)
        row2 = QHBoxLayout(); row2.addWidget(self.btn_clear); row2.addWidget(self.btn_settings)
        ll.addWidget(self.btn_ptt); ll.addLayout(row1); ll.addLayout(row2)
        outer.addWidget(left)

        # ---------------- right: transcript, confirm, input, activity
        right = QVBoxLayout(); right.setSpacing(10)
        split = QSplitter(Qt.Vertical)
        tp = QFrame(); tp.setObjectName("panel"); tl = QVBoxLayout(tp); tl.setContentsMargins(10, 10, 10, 10)
        self.transcript = Transcript(); tl.addWidget(self.transcript)
        self.confirm = ConfirmBar(); self.confirm.decided.connect(lambda i, ok: self.app.perms.resolve(i, ok)); tl.addWidget(self.confirm)
        inrow = QHBoxLayout()
        self.input = QLineEdit(); self.input.setPlaceholderText("Type to Mei Mei…  (Enter to send)")
        self.input.returnPressed.connect(self._send_text)
        send = QPushButton("Send"); send.setObjectName("primary"); send.clicked.connect(self._send_text)
        inrow.addWidget(self.input, 1); inrow.addWidget(send); tl.addLayout(inrow)
        split.addWidget(tp)
        ap = QFrame(); ap.setObjectName("panel"); al = QVBoxLayout(ap); al.setContentsMargins(10, 8, 10, 10)
        hdr = QLabel("ACTIVITY LOG"); hdr.setObjectName("sub"); al.addWidget(hdr)
        self.activity = ActivityLog(); al.addWidget(self.activity)
        split.addWidget(ap)
        split.setSizes([520, 190])
        right.addWidget(split)
        outer.addLayout(right, 1)

        bridge.event.connect(self._on_event)
        self.clear_finished.connect(lambda: self.btn_clear.setEnabled(True))
        self._init_tray()
        self._claude = "starting"; self._wake = "loading"; self._mic = "stopped"; self._mic_detail = ""
        self._refresh_pills()
        t = QTimer(self); t.timeout.connect(self._refresh_pills); t.start(1000)
        self._load_history()

    # ---------------------------------------------------------------- tray / close behaviour
    def _init_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(tray_icon(), self)
        m = QMenu()
        for text, fn in (("Show Mei Mei", self.showNormal), ("Pause / resume listening", self._toggle_listen),
                         ("Clear context", self._clear), ("Quit", self._quit)):
            a = QAction(text, self); a.triggered.connect(fn); m.addAction(a)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self._tray_timer = QTimer(self); self._tray_timer.timeout.connect(self._tray_tick); self._tray_timer.start(100)

    def _update_tray_color(self):
        pass  # tray now mirrors the orb (colour + animation) via _tray_tick

    def _tray_tick(self):
        if not getattr(self, "tray", None):
            return
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtGui import QRegion
        o = self.orb
        src = QPixmap(o.size()); src.fill(Qt.transparent)
        o.render(src, QPoint(), QRegion(), QWidget.RenderFlags(QWidget.DrawChildren))   # no window background
        side = int(min(o.width(), o.height()) * 0.78)
        crop = src.copy(QRect((o.width() - side) // 2, (o.height() - side) // 2, side, side))
        pm = crop.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.tray.setIcon(QIcon(pm))

    def closeEvent(self, e):
        if self.tray and not self.really_quit:
            self.hide(); e.ignore()      # keep listening in the background
        else:
            self._quit(); e.accept()

    def _quit(self):
        self.really_quit = True
        self.app.shutdown()
        QApplication.quit()

    # ---------------------------------------------------------------- actions
    def _send_text(self):
        t = self.input.text().strip()
        if t:
            self.input.clear()
            self.app.assistant.submit(t, source="text")

    def _clear(self):
        self.btn_clear.setEnabled(False)
        fut = self.app.assistant.clear_context()
        fut.add_done_callback(lambda f: self.clear_finished.emit())

    def _stop_speaking(self):
        self.app.tts.stop()
        self.app.assistant.interrupt()

    def _toggle_listen(self):
        v = self.app.voice
        if not v:
            return
        (v.resume if (v.paused or not v.enabled) else v.pause)()

    def _settings(self):
        SettingsDialog(self.app, self).exec()

    def _load_history(self):
        for m in self.app.memory.history(self.app.sessions.session_id, 30):
            self.transcript.add("user" if m["role"] == "user" else "assistant", m["text"])
        if self.app.memory.history(self.app.sessions.session_id, 1):
            self.transcript.add("system", "session restored after restart")

    # ---------------------------------------------------------------- events
    def _on_event(self, ev: str, d: dict):
        if ev == "state":
            self.state = d["state"]
            self.orb.set_state(self.state)
            self.state_lbl.setText(self.state)
            self.state_lbl.setStyleSheet(f"color: {styles.STATE_COLORS[self.state]};")
        elif ev == "audio_level":
            self.orb.mic_level, self.orb.out_level = d["mic"], d["out"]
            self.mic_bar.set_value(d["mic"])
        elif ev == "user_text":
            self.transcript.add("user", d["text"])
            self.caption.setText(f"“{d['text'][:90]}”")
        elif ev == "assistant_text":
            self.transcript.add("assistant", d["text"])
        elif ev == "heard":
            self.caption.setText(f"Heard: “{d['text'][:90]}”")
        elif ev == "wake_detected":
            self.caption.setText("Listening…")
        elif ev == "activity":
            self.activity.add(d["level"], d["text"])
        elif ev == "confirm_request":
            self.confirm.show_request(d["id"], d["summary"], d["reason"])
            self.showNormal(); self.raise_()
        elif ev == "confirm_done":
            if not self.app.perms.has_pending():
                self.confirm.hide()
        elif ev == "wake_status":
            self._wake, self._mic, self._mic_detail = d["state"], d["mic"], d.get("mic_detail", "")
            self.orb.active = d["state"] in ("listening", "capturing")
            v = self.app.voice
            self.btn_listen.setText("Resume listening" if v and (v.paused or not v.enabled) else "Pause listening")
            self._refresh_pills()
        elif ev == "mic_status":
            self._mic, self._mic_detail = d["status"], d.get("detail", "")
            self._refresh_pills()
        elif ev == "claude_status":
            self._claude = d["status"]
            self._refresh_pills()
        elif ev in ("session_changed", "context_usage"):
            self._refresh_pills()
        elif ev == "transcript_cleared":
            self.transcript.clear()
            self.transcript.add("system", "context cleared — new conversation")
            self.caption.setText("Fresh start")
        elif ev == "show_window":
            self.showNormal(); self.raise_(); self.activateWindow()
        elif ev == "quit_requested":
            self.really_quit = True
            QApplication.quit()

    def _refresh_pills(self):
        G, Y, R, D = "#3dffb5", "#ffb84d", "#ff6b7a", styles.DIM
        mic_col = {"active": G, "paused": Y, "reconnecting": R, "no-device": R, "stopped": D}.get(self._mic, D)
        self.p_mic.set_status(f"MIC · {self._mic}" + (f" · {self._mic_detail[:34]}" if self._mic_detail else ""), mic_col)
        w_col = {"listening": G, "capturing": G, "paused": Y, "disabled": D, "no-mic": R, "loading": Y, "error": R}.get(self._wake, D)
        label = {"listening": "armed — say “Hey Mei Mei”", "capturing": "capturing your request", "paused": "paused",
                 "disabled": "disabled", "no-mic": "waiting for microphone", "loading": "loading models…", "error": "error"}.get(self._wake, self._wake)
        self.p_wake.set_status(f"WAKE WORD · {label}", w_col)
        self._update_tray_color()
        c_col = {"connected": G, "connecting": Y, "starting": Y, "error": R}.get(self._claude, D)
        self.p_claude.set_status(f"CLAUDE · {self._claude}", c_col)
        s = self.app.sessions.state
        self.p_sess.set_status(f"SESSION · {s.session_id[:8]}  gen {s.generation}", D)
        pct = self.app.assistant.last_context_pct if self.app.assistant else None
        self.p_ctx.set_status("CONTEXT · " + ("—" if pct is None else f"{pct:.0f}% used"),
                              R if (pct or 0) > 85 else Y if (pct or 0) > 70 else D)
