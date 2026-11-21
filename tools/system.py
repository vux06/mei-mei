"""System information and controls: CPU/RAM/disk/battery/network/ports/processes, volume, media, brightness."""
from __future__ import annotations

import re
import time

import psutil

from .common import run


def status() -> dict:
    vm = psutil.virtual_memory()
    d = {"cpu_percent": psutil.cpu_percent(interval=0.4), "cpu_cores": psutil.cpu_count(),
         "load_avg": [round(x, 2) for x in psutil.getloadavg()],
         "ram": {"used_gb": round(vm.used / 2**30, 1), "total_gb": round(vm.total / 2**30, 1), "percent": vm.percent},
         "swap_percent": psutil.swap_memory().percent,
         "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1)}
    temps = getattr(psutil, "sensors_temperatures", lambda: {})()
    for name, entries in temps.items():
        if entries:
            d.setdefault("temps_c", {})[name] = round(entries[0].current)
            if len(d["temps_c"]) >= 3:
                break
    rc, out, _ = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                      "--format=csv,noheader,nounits"], timeout=5)
    if rc == 0 and out.strip():
        n, u, mu, mt, t = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        d["gpu"] = {"name": n, "util_percent": int(u), "vram_used_mb": int(mu), "vram_total_mb": int(mt), "temp_c": int(t)}
    return d


def disks() -> list[dict]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay") or p.mountpoint.startswith(("/snap", "/boot/efi")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        out.append({"mount": p.mountpoint, "device": p.device, "fs": p.fstype,
                    "used_gb": round(u.used / 2**30, 1), "total_gb": round(u.total / 2**30, 1), "percent": u.percent})
    return out


def battery() -> dict:
    b = psutil.sensors_battery()
    if b is None:
        return {"present": False}
    return {"present": True, "percent": round(b.percent), "plugged_in": b.power_plugged,
            "minutes_left": None if b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) else round(b.secsleft / 60)}


def network() -> dict:
    rc, out, _ = run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION,DEVICE", "device"], timeout=5)
    devs = [dict(zip(("type", "state", "connection", "device"), ln.split(":"))) for ln in out.splitlines() if ln] if rc == 0 else []
    addrs = {n: [a.address for a in al if a.family.name == "AF_INET"] for n, al in psutil.net_if_addrs().items() if n != "lo"}
    rc2, _, _ = run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], timeout=4)
    return {"internet_reachable": rc2 == 0, "devices": devs, "ipv4": {k: v for k, v in addrs.items() if v}}


def processes(sort: str = "memory", limit: int = 10) -> list[dict]:
    for p in psutil.process_iter(["cpu_percent"]):   # first call primes the per-process CPU counters
        pass
    time.sleep(0.4)
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
        i = p.info
        procs.append({"pid": i["pid"], "name": i["name"], "cpu": round(i["cpu_percent"] or 0, 1),
                      "mem": round(i["memory_percent"] or 0, 1)})
    key = "cpu" if sort.startswith("c") else "mem"
    return sorted(procs, key=lambda x: x[key], reverse=True)[:limit]


def listening_ports() -> list[dict]:
    rc, out, _ = run(["ss", "-H", "-tulnp"], timeout=5)
    res = []
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5:
            proc = re.search(r'users:\(\("([^"]+)",pid=(\d+)', ln)
            res.append({"proto": parts[0], "local": parts[4], "process": proc.group(1) if proc else "?"})
    return res


# ---- audio / media / brightness ----
def get_volume() -> dict:
    rc, out, _ = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=4)
    m = re.search(r"([\d.]+)", out)
    return {"volume_percent": round(float(m.group(1)) * 100) if m else None, "muted": "MUTED" in out}


def set_volume(percent: int) -> dict:
    percent = max(0, min(150, int(percent)))
    run(["wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", f"{percent}%"], timeout=4)
    return get_volume()


def change_volume(delta: int) -> dict:
    cur = get_volume().get("volume_percent") or 0
    return set_volume(cur + delta)


def set_mute(mute: bool | None) -> dict:
    arg = "toggle" if mute is None else ("1" if mute else "0")
    run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", arg], timeout=4)
    return get_volume()


def media(action: str) -> str:
    amap = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}
    if action == "status":
        return run(["playerctl", "metadata", "--format", "{{status}}: {{artist}} - {{title}}"], timeout=4)[1].strip() or "no player"
    rc, _, err = run(["playerctl", amap.get(action, "play-pause")], timeout=4)
    return "ok" if rc == 0 else (err.strip() or "no media player running")


def brightness(percent: int | None) -> str:
    if percent is None:
        rc, out, _ = run(["brightnessctl", "-m"], timeout=4)
        return out.strip()
    rc, _, err = run(["brightnessctl", "set", f"{max(1, min(100, int(percent)))}%"], timeout=4)
    return "ok" if rc == 0 else err.strip()
