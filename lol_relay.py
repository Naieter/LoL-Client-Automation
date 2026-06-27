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
import sys
import json
import time
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DEFAULT_PORT = 8777
TTL = 600  # seconds — ready entries older than this are ignored & pruned

_lock = threading.Lock()
_data: dict = {}   # party -> { member -> last_seen_timestamp }


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
                ready = [m for m, t in members.items() if now - t < TTL]
            self._send(200, {"ready": ready})
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
        with _lock:
            p = _data.setdefault(party, {})
            if ready:
                p[member] = time.time()
            else:
                p.pop(member, None)
            count = len(p)
        print(f"[{_ts()}] {'ready' if ready else 'unready'}  party={party[:8]}…  "
              f"({count} ready in party)")
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass  # suppress default per-request logging


def _prune_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with _lock:
            for party in list(_data.keys()):
                members = _data[party]
                for m in list(members.keys()):
                    if now - members[m] > TTL:
                        del members[m]
                if not members:
                    del _data[party]


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    threading.Thread(target=_prune_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 56)
    print("  LOL Client Tool — Ready-Up Relay")
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
