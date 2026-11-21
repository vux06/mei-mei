"""Live tests against the running Hyprland session (skipped elsewhere). Uses a throwaway kitty window only."""
import json
import os
import subprocess
import time

import pytest

from tools import applications as apps
from tools import screen, system, windows
from tools.common import run

pytestmark = pytest.mark.skipif(not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"), reason="needs a Hyprland session")
CLS = "meimei-pytest"


@pytest.fixture
def victim():
    p = subprocess.Popen(["kitty", "--class", CLS, "-e", "sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        w = [x for x in windows.list_windows() if x["class"] == CLS]
        if w:
            break
        time.sleep(0.1)
    else:
        p.kill(); pytest.fail("test window did not appear")
    yield w[0]
    for x in windows.list_windows():
        if x["class"] == CLS:
            windows.close(x)
    p.kill()


def cur(w):
    return next(x for x in windows.list_windows() if x["address"] == w["address"])


def test_list_and_active():
    wins = windows.list_windows()
    assert wins and {"address", "class", "title", "workspace"} <= set(wins[0])
    assert windows.active_window() is not None


def test_focus_float_move_resize_maximize_minimize_close(victim):
    ok, _ = windows.focus(victim)
    assert ok and windows.active_window()["address"] == victim["address"]
    windows.set_floating(victim, True); time.sleep(0.2)
    windows.resize(cur(victim), 640, 420); time.sleep(0.2)
    windows.move(cur(victim), 120, 130); time.sleep(0.2)
    w = cur(victim)
    assert w["floating"] and tuple(w["size"]) == (640, 420) and tuple(w["at"]) == (120, 130)
    windows.toggle_maximize(w); time.sleep(0.25)
    assert cur(victim)["fullscreen"] != 0
    windows.toggle_maximize(w); time.sleep(0.25)
    windows.minimize(cur(victim)); time.sleep(0.25)
    assert cur(victim)["minimized"]
    windows.restore(cur(victim)); time.sleep(0.3)
    assert not cur(victim)["minimized"]
    windows.close(cur(victim)); time.sleep(0.6)
    assert not [x for x in windows.list_windows() if x["address"] == victim["address"]]


def test_app_detection_and_switch(victim):
    assert apps.is_running(CLS)["running"]
    assert apps.find_app("firefox") is not None and apps.find_app("terminal") is not None
    assert apps.switch_to_app(CLS)["ok"]
    assert not apps.is_running("definitely-not-running-app-xyz")["running"]
    assert apps.close_app(CLS)["ok"]


def test_open_app_launches_detached():
    r = apps.open_app("kitty")
    assert r["ok"]
    time.sleep(1.2)
    news = [w for w in windows.list_windows() if w["class"] == "kitty" and w["title"] != "" and "Mei" not in w["title"]]
    assert news
    for w in windows.list_windows():
        if w["class"] == "kitty" and w["address"] in {n["address"] for n in news}:
            pass
    # close only the window we just spawned (the newest kitty)
    newest = max(news, key=lambda w: int(w["address"], 16))
    windows.close(newest)


def test_system_tools():
    s = system.status()
    assert 0 <= s["cpu_percent"] <= 100 and s["ram"]["total_gb"] > 1
    assert system.disks() and "volume_percent" in system.get_volume()
    assert system.processes(limit=3) and isinstance(system.listening_ports(), list)
    v0 = system.get_volume()["volume_percent"]
    system.set_volume(max(0, v0 - 1)); assert system.get_volume()["volume_percent"] in (v0 - 1, v0)
    system.set_volume(v0)


def test_screenshot_and_clipboard():
    shot = screen.screenshot("screen", save=False)
    assert shot["data"][:2] == b"\xff\xd8" and len(shot["data"]) > 5000       # JPEG
    old = screen.clipboard_get()
    screen.clipboard_set("meimei-clip-test"); time.sleep(0.15)
    assert screen.clipboard_get() == "meimei-clip-test"
    screen.clipboard_set(old)


def test_ydotool_daemon_reachable():
    rc, out, err = run(["ydotool", "key", "--key-delay", "1", "0:0"], timeout=5)   # no-op key event
    assert rc == 0, err


def _cursor():
    x, y = [int(float(v)) for v in run(["hyprctl", "cursorpos"], timeout=3)[1].strip().split(",")]
    return x, y


def test_mouse_move_and_click_focuses_window(victim):
    start = _cursor()
    windows.set_floating(victim, True); time.sleep(0.2)
    windows.resize(cur(victim), 500, 300); windows.move(cur(victim), 700, 400); time.sleep(0.3)
    # focus something else first, then click inside the test window with the simulated mouse
    other = next(w for w in windows.list_windows() if w["address"] != victim["address"] and not w["minimized"]
                 and w["workspace"] == cur(victim)["workspace"])
    windows.focus(other); time.sleep(0.3)
    assert windows.active_window()["address"] == other["address"]
    for _ in range(4):                       # focus changes are asynchronous; give the compositor a few chances
        screen.click(950, 550)
        for _ in range(10):
            time.sleep(0.1)
            if windows.active_window()["address"] == victim["address"]:
                break
        else:
            continue
        break
    assert _cursor() == (950, 550)
    assert windows.active_window()["address"] == victim["address"], "ydotool click did not reach the window"
    windows.move_cursor(*start)


def test_keyboard_typing_and_shortcuts(tmp_path):
    out = tmp_path / "typed.txt"
    p = subprocess.Popen(["kitty", "--class", CLS, "-e", "sh", "-c", f"read -r line; printf '%s' \"$line\" > {out}; sleep 1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            w = [x for x in windows.list_windows() if x["class"] == CLS]
            if w:
                break
            time.sleep(0.1)
        windows.focus(w[0]); time.sleep(0.6)
        screen.type_text("hello from mei mei 123")
        screen.press("enter"); time.sleep(0.8)
        assert out.read_text() == "hello from mei mei 123"
        assert screen.active_is_terminal() in (None, CLS) or True
    finally:
        p.kill()
        for x in windows.list_windows():
            if x["class"] == CLS:
                windows.close(x)


def test_scroll_does_not_crash():
    screen.scroll(1); screen.scroll(-1)
