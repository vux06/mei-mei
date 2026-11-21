#!/usr/bin/env python3
"""Control a running Mei Mei (standard library only — works with any python3, no venv needed).

  meimei_ctl.py status
  meimei_ctl.py clear            # identify active session -> clear exactly that one -> verify
  meimei_ctl.py listen pause|resume|enable|disable
  meimei_ctl.py ask "open firefox" | stop | show | quit
"""
import json
import os
import socket
import sys

RUNTIME = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/meimei-{os.getuid()}", "meimei")
SOCK = os.environ.get("MEIMEI_SOCKET", os.path.join(RUNTIME, "ctl.sock"))


def rpc(req: dict, timeout: float = 60) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        print("Mei Mei is not running (no control socket). Start it with ./run.sh", file=sys.stderr)
        sys.exit(2)
    s.sendall((json.dumps(req) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    return json.loads(data.decode())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "status":
        print(json.dumps(rpc({"cmd": "status"}), indent=2))
        return 0
    if cmd == "clear":
        before = rpc({"cmd": "status"})
        old = before["session_id"]
        print(f"Active session: {old} (generation {before['generation']})")
        res = rpc({"cmd": "clear", "expect_session_id": old}, timeout=90)
        if not res.get("ok"):
            print(f"FAILED: {res.get('error')}", file=sys.stderr)
            return 1
        after = rpc({"cmd": "status"})
        if after["session_id"] == old or after["session_id"] != res["new"]:
            print("FAILED: the active session did not change — context was NOT cleared", file=sys.stderr)
            return 1
        print(f"Context cleared.\n  archived : {old}\n  new      : {res['new']}  (fresh session, not resumed)"
              f"\n  persona  : re-applied\n  memory   : permanent memory untouched")
        return 0
    if cmd == "listen" and len(argv) > 1:
        r = rpc({"cmd": "listen", "action": argv[1]})
        print(json.dumps(r))
        return 0 if r.get("ok") else 1
    if cmd == "ask" and len(argv) > 1:
        return 0 if rpc({"cmd": "ask", "text": " ".join(argv[1:])}).get("ok") else 1
    if cmd in ("stop", "show", "quit"):
        r = rpc({"cmd": {"stop": "stop_speaking"}.get(cmd, cmd)})
        return 0 if r.get("ok") else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
