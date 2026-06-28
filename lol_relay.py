"""
LOL Client Tool — Ready-Up Relay Server
=======================================
A tiny, dependency-free HTTP relay so separate copies of the LOL Client Tool
can share "ready" state across PCs. Run this on a server reachable by every
party member (LAN IP, or public IP with a forwarded port).

Storage is in-memory only and keyed by opaque hashes the clients send — the
relay never sees summoner names or IDs. Entries older than TTL are pruned.

Usage:
    LOL_Relay.exe                 # listens on 0.0.0.0:8777
    LOL_Relay.exe 9000            # custom port

Endpoints (used by the tool):
    GET  /ping                    -> {"ok": true}
    POST /ready  {party, member, ready:bool}
    GET  /party?id=<party>        -> {"ready": [member, ...]}
"""
import os
import sys
import json
import time
import socket
import threading
import subprocess
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

RELAY_VERSION = "1.6.5"
GITHUB_REPO   = "Naieter/LoL-Client-Automation"

DEFAULT_PORT = 8777
# A member is "present" (has the tool running) if it heartbeated within FRESH
# seconds; tools heartbeat every ~2s, so this drops members who close the tool.
FRESH = 20

_lock = threading.Lock()
_data: dict = {}   # party -> { member -> {"r": ready_bool, "t": timestamp} }


def _disable_quick_edit():
    """Turn off the Windows console 'QuickEdit' mode so a stray click/selection
    in the relay window can't freeze the process (which would hang request
    handling on its next print)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
            k.SetConsoleMode(
                h, (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS)
    except Exception:
        pass


def _ts():
    return time.strftime("%H:%M:%S")


def _public_ip():
    """This server's public (internet-facing) IP, or None if it can't be found."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            ip = r.read().decode().strip()
            return ip or None
    except Exception:
        return None


def _lan_ip():
    """This server's LAN IP (for party members on the same network)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj=None):
        payload = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            self._send(200, {"ok": True})
            return
        if u.path == "/party":
            pid = (parse_qs(u.query).get("id") or [""])[0]
            now = time.time()
            with _lock:
                members = _data.get(pid, {})
                # "present" = heartbeated recently (tool running); "ready" = those
                # who are also marked ready.
                present = [m for m, v in members.items() if now - v["t"] < FRESH]
                ready   = [m for m, v in members.items()
                           if now - v["t"] < FRESH and v["r"]]
            self._send(200, {"present": present, "ready": ready})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/ready":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        party  = str(body.get("party", ""))
        member = str(body.get("member", ""))
        ready  = bool(body.get("ready", True))
        if not party or not member:
            self._send(400, {"error": "party and member required"})
            return
        now = time.time()
        with _lock:
            p = _data.setdefault(party, {})
            # Always record presence (timestamp); the ready flag may be off.
            p[member] = {"r": ready, "t": now}
            present = sum(1 for v in p.values() if now - v["t"] < FRESH)
            rdy     = sum(1 for v in p.values() if now - v["t"] < FRESH and v["r"])
        print(f"[{_ts()}] {'ready' if ready else 'unready'}  party={party[:8]}…  "
              f"({rdy}/{present} ready)")
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass  # suppress default per-request logging


def _prune_loop():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            for party in list(_data.keys()):
                members = _data[party]
                for m in list(members.keys()):
                    if now - members[m]["t"] > FRESH:
                        del members[m]
                if not members:
                    del _data[party]


# ── Self-update (mirrors the client tool) ──────────────────────────────────────
def _ver(v):
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def _check_and_update(idle_required=False):
    """Check GitHub for a newer LOL_Relay.exe and, if found, download + swap +
    relaunch. When idle_required is True, skip if any party is currently active
    (so periodic checks don't interrupt an in-progress ready-up)."""
    if not getattr(sys, "frozen", False):
        return  # only the packaged exe self-updates
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "LOL-Relay"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        tag = data.get("tag_name", "")
        if _ver(tag) <= _ver(RELAY_VERSION):
            return
        dl = next((a["browser_download_url"] for a in data.get("assets", [])
                   if a.get("name", "").lower() == "lol_relay.exe"), None)
        if not dl:
            return
        if idle_required:
            with _lock:
                if _data:
                    print(f"[{_ts()}] update v{tag.lstrip('v')} available — "
                          "waiting until idle to apply.")
                    return
        print("=" * 56)
        print(f"  Update available: v{tag.lstrip('v')} "
              f"(running v{RELAY_VERSION}). Downloading…")
        _do_update(dl)
    except Exception as e:
        print(f"  Update check failed: {e}")


def _do_update(dl_url):
    exe = Path(sys.executable)
    tmp = exe.with_name("LOL_Relay_update.exe")
    bat = exe.with_name("relay_update.bat")
    log = exe.with_name("relay_update_log.txt")
    try:
        req = urllib.request.Request(dl_url, headers={"User-Agent": "LOL-Relay"})
        with urllib.request.urlopen(req, timeout=180) as r:
            blob = r.read()
        with open(tmp, "wb") as f:
            f.write(blob)
        pid = os.getpid()
        # Wait for this process to exit, swap with retries, relaunch. Uses ping
        # for delays (timeout needs a console) and explorer to relaunch cleanly.
        bat.write_text(
            "@echo off\r\n"
            "setlocal enableextensions enabledelayedexpansion\r\n"
            f'set "EXE={exe}"\r\n'
            f'set "NEW={tmp}"\r\n'
            f'set "LOG={log}"\r\n'
            'echo === relay update === > "%LOG%"\r\n'
            ":waitexit\r\n"
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 ( ping -n 2 127.0.0.1 >nul & goto waitexit )\r\n"
            "ping -n 3 127.0.0.1 >nul\r\n"
            "set /a tries=0\r\n"
            ":swap\r\n"
            'move /y "%NEW%" "%EXE%" >>"%LOG%" 2>&1\r\n'
            'if exist "%NEW%" ( set /a tries+=1 & '
            'if !tries! lss 20 ( ping -n 2 127.0.0.1 >nul & goto swap ) )\r\n'
            "ping -n 2 127.0.0.1 >nul\r\n"
            'explorer.exe "%EXE%"\r\n'
            'del "%~f0"\r\n',
            encoding="ascii",
        )
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000)
        print("  Downloaded — restarting to apply update…")
        time.sleep(0.7)
        os._exit(0)
    except Exception as e:
        print(f"  Update failed: {e}")
        try:
            tmp.unlink()
        except Exception:
            pass


def _update_loop():
    """Periodically check for updates, applying only while idle."""
    while True:
        time.sleep(6 * 3600)   # every 6 hours
        _check_and_update(idle_required=True)


def main():
    _disable_quick_edit()   # don't let a console click freeze request handling
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    # Self-update on startup (the relay is idle here), then periodically.
    _check_and_update()
    threading.Thread(target=_update_loop, daemon=True).start()
    threading.Thread(target=_prune_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 56)
    print(f"  LOL Client Tool — Ready-Up Relay  v{RELAY_VERSION}")
    print(f"  Listening on  0.0.0.0:{port}")
    print("  Set each tool's Relay URL to:")
    pub = _public_ip()
    lan = _lan_ip()
    if pub:
        print(f"      remote:        http://{pub}:{port}")
    if lan:
        print(f"      same network:  http://{lan}:{port}")
    if not pub and not lan:
        print(f"      http://<this-server-ip>:{port}")
    if pub:
        print("  (remote members also need TCP "
              f"{port} forwarded on the router to this server)")
    print("  Keep this window open. Ctrl+C to stop.")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        srv.shutdown()


if __name__ == "__main__":
    main()
