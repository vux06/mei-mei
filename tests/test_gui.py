"""GUI interaction tests (offscreen Qt, isolated data dir). Drives the real widgets and the real core."""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="meimei-gui-")
os.environ.update(QT_QPA_PLATFORM="offscreen", MEIMEI_DATA_DIR=TMP + "/data", MEIMEI_SOCKET=TMP + "/ctl.sock",
                  XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000"))
import json  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.app import App  # noqa: E402
from gui.main_window import Bridge, MainWindow, SettingsDialog  # noqa: E402
from gui.styles import QSS  # noqa: E402


@pytest.fixture(scope="module")
def env():
    qt = QApplication.instance() or QApplication([])
    qt.setStyleSheet(QSS)
    app = App(voice=False)
    app.start()
    win = MainWindow(app, Bridge(app.bus))
    win.show()
    yield qt, app, win
    app.shutdown()


def pump(qt, seconds=0.3, until=None, timeout=60):
    t0 = time.time()
    while time.time() - t0 < (timeout if until else seconds):
        qt.processEvents()
        if until and until():
            return True
        time.sleep(0.02)
    return until() if until else True


def test_all_main_screen_elements_present(env):
    qt, app, win = env
    assert win.orb and win.transcript and win.input and win.activity and win.confirm
    assert win.btn_ptt.text() and win.btn_clear.text() == "CLEAR CONTEXT" and win.btn_settings.text() == "Settings"
    assert win.p_mic and win.p_wake and win.p_claude and win.p_sess and win.p_ctx


@pytest.mark.parametrize("state", ["IDLE", "LISTENING", "PROCESSING", "EXECUTING", "SPEAKING", "ERROR"])
def test_visual_states(env, state):
    qt, app, win = env
    app.bus.emit("state", state=state)
    pump(qt, 0.15)
    assert win.state_lbl.text() == state and win.orb.state == state
    img = win.orb.grab().toImage()
    assert img.width() > 100
    if os.environ.get("MEIMEI_SHOTS"):
        win.grab().save(f"{os.environ['MEIMEI_SHOTS']}/gui_{state}.png")


def test_status_pills_show_real_status(env):
    qt, app, win = env
    app.bus.emit("mic_status", status="reconnecting", detail="microphone lost")
    app.bus.emit("wake_status", state="listening", enabled=True, paused=False, mic="active", mic_detail="Test mic", stats={})
    pump(qt, 0.2)
    assert "active" in win.p_mic.text() and "armed" in win.p_wake.text()
    app.bus.emit("wake_status", state="paused", enabled=True, paused=True, mic="paused", mic_detail="", stats={})
    app.bus.emit("claude_status", status="error")
    pump(qt, 0.2)
    assert "paused" in win.p_wake.text() and "error" in win.p_claude.text()
    app.bus.emit("wake_status", state="disabled", enabled=False, paused=False, mic="stopped", mic_detail="", stats={})
    pump(qt, 0.2)
    assert "disabled" in win.p_wake.text()
    app.bus.emit("claude_status", status="connected")


def test_text_input_round_trip_with_claude(env):
    qt, app, win = env
    win.input.setText("Reply with exactly the word: pong")
    win.input.returnPressed.emit()
    assert pump(qt, until=lambda: "MEI MEI" in win.transcript.toPlainText() and "pong" in win.transcript.toPlainText().split("MEI MEI")[-1].lower(), timeout=90)
    assert "YOU" in win.transcript.toPlainText() and "MEI MEI" in win.transcript.toPlainText()
    assert win.input.text() == ""
    assert pump(qt, until=lambda: "%" in win.p_ctx.text(), timeout=30), win.p_ctx.text()


def test_clear_context_button(env):
    qt, app, win = env
    before = app.sessions.session_id
    win.btn_clear.click()
    assert pump(qt, until=lambda: app.sessions.session_id != before, timeout=40)
    assert pump(qt, until=lambda: "cleared" in win.transcript.toPlainText().lower(), timeout=10)
    assert win.p_sess.text().split()[2][:8] == app.sessions.session_id[:8] or app.sessions.session_id[:8] in win.p_sess.text()
    assert pump(qt, until=lambda: win.btn_clear.isEnabled(), timeout=40)


def test_confirmation_buttons_resolve_requests(env):
    qt, app, win = env
    import asyncio
    for button, expect in ((win.confirm.allow, True), (win.confirm.deny, False)):
        fut = asyncio.run_coroutine_threadsafe(app.perms.gate("delete_path", "delete test.txt", __import__("core.permissions", fromlist=["Level"]).Level.CONFIRM), app.assistant.loop)
        assert pump(qt, until=lambda: win.confirm.isVisible(), timeout=10)
        assert "delete test.txt" in win.confirm.label.text()
        button.click()
        pump(qt, 0.5)
        if expect:
            assert fut.result(5) is None
        else:
            with pytest.raises(Exception):
                fut.result(5)
        assert pump(qt, until=lambda: not win.confirm.isVisible(), timeout=5)


def test_pause_resume_button_without_voice(env):
    qt, app, win = env
    win.btn_listen.click()          # no voice pipeline in this test: must not crash
    pump(qt, 0.1)


def test_settings_dialog_saves_and_personality_file_roundtrip(env):
    qt, app, win = env
    d = SettingsDialog(app, win)
    d.sens.setValue(77); d.speed.setValue(120); d.barge.setChecked(False)
    d.mem_key.setText("test key"); d.mem_val.setText("test value"); d._mem_add()
    assert any("test key" in d.mem_list.item(i).text() for i in range(d.mem_list.count()))
    persona_before = d.persona.toPlainText()
    d._save()
    assert app.settings.get("wake_word.sensitivity") == 0.77 and app.settings.get("tts.speed") == 1.2
    assert app.settings.get("voice.barge_in") is False
    assert app.memory.recall("test key")
    # restore the user's real settings file values we touched
    app.settings.set("wake_word.sensitivity", 0.6); app.settings.set("tts.speed", 1.0); app.settings.set("voice.barge_in", True)
    from core.paths import PERSONALITY_FILE
    assert PERSONALITY_FILE.read_text().strip() == persona_before.strip()
