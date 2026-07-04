#!/usr/bin/env python3
"""
LOL Client Tool  –  Role-Based Champion Selection  (Python rebuild)

Uses the LCU (League Client Update) local API to automate champion select.
Detects your assigned role each game and picks from your per-role priority list.

WARNING: Third-party automation tools may violate Riot Games' Terms of Service
and could result in account penalties. Use at your own risk.
"""

import sys, os, json, threading, time, hashlib, subprocess, re as _re, math as _math
import uuid as _uuid
import ctypes, ctypes.wintypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import defaultdict

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, scrolledtext, messagebox


# ── Auto-install missing dependencies ─────────────────────────────────────────
def _ensure_deps():
    import subprocess
    missing = []
    for pkg in ("requests", "psutil"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing: {', '.join(missing)} ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        )

_ensure_deps()
import requests, psutil, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Debug log ─────────────────────────────────────────────────────────────────
_DEBUG_LOG = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LOL_Client_TOOL" / "debug.log"

def _dbg(*args):
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(a) for a in args) + "\n")
    except Exception:
        pass


_mixer_ready = False
_mixer_lock  = threading.Lock()

def _play_sound(path: str, volume: float = 1.0):
    def _do():
        global _mixer_ready
        try:
            import pygame.mixer
            with _mixer_lock:
                if not _mixer_ready:
                    pygame.mixer.pre_init(44100, -16, 2, 2048)
                    pygame.mixer.init()
                    _mixer_ready = True
            snd = pygame.mixer.Sound(path)
            snd.set_volume(max(0.0, min(1.0, volume)))
            snd.play()
            time.sleep(snd.get_length() + 0.2)
        except Exception as exc:
            _dbg(f"_play_sound: {exc}")
    threading.Thread(target=_do, daemon=True).start()

def _delayed_play(path: str, cancel: threading.Event,
                  delay: float = 30.0, volume: float = 1.0):
    """Play `path` after `delay` seconds unless `cancel` is set first."""
    def _do():
        if cancel.wait(timeout=delay):
            return   # cancelled before timeout
        _play_sound(path, volume)
    threading.Thread(target=_do, daemon=True).start()


# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME    = "LOL Client Tool  –  Role-Based Pick"
APP_VERSION = "1.14.0"
GITHUB_REPO = "Naieter/LoL-Client-Automation"
CONFIG_DIR  = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LOL_Client_TOOL"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE    = CONFIG_DIR / "lol_tool.log"
DDRAGON_URL  = "https://ddragon.leagueoflegends.com"
READY_SOUND  = r"C:\Users\naiet\Downloads\Neeko_Original_R_0.ogg"

# LCU assignedPosition values
ROLES = ["top", "jungle", "middle", "bottom", "utility"]
ROLE_LABEL = {
    "top":     "Top",
    "jungle":  "Jungle",
    "middle":  "Mid",
    "bottom":  "ADC",
    "utility": "Support",
}

MAX_PRIORITY_ITEMS = 5   # pick/ban priority list cap; a cross-list drop that
                         # pushes a list past this bumps its 6th champion

# Champions allowed to appear more than once on a single team. These are never
# treated as "taken" when an ally already has one, and stay pickable/lockable no
# matter what — champion -3 is the only such champion.
DUP_ALLOWED_CHAMPS = frozenset({-3})

# Summoner-spell IDs:  Flash=4 Teleport=12 Smite=11 Ignite=14 Heal=7 Exhaust=3
# Fallback spell pairs used only when no meta data is available for the champ.
ROLE_SPELLS = {
    "top":     (4, 12),   # Flash + Teleport
    "jungle":  (4, 11),   # Flash + Smite
    "middle":  (4, 14),   # Flash + Ignite
    "bottom":  (4, 7),    # Flash + Heal
    "utility": (4, 14),   # Flash + Ignite
}

# LCU assignedPosition → op.gg position enum
OPGG_POSITION = {
    "top": "top", "jungle": "jungle", "middle": "mid",
    "bottom": "adc", "utility": "support",
}

# webRegion → a regional Riot host used to estimate ping (TCP RTT to :443)
REGION_HOST = {
    "na":   "na1.api.riotgames.com",   "euw":  "euw1.api.riotgames.com",
    "eune": "eun1.api.riotgames.com",  "eun":  "eun1.api.riotgames.com",
    "kr":   "kr.api.riotgames.com",    "br":   "br1.api.riotgames.com",
    "lan":  "la1.api.riotgames.com",   "las":  "la2.api.riotgames.com",
    "oce":  "oc1.api.riotgames.com",   "tr":   "tr1.api.riotgames.com",
    "ru":   "ru.api.riotgames.com",    "jp":   "jp1.api.riotgames.com",
}
DEFAULT_PING_HOST = "na1.api.riotgames.com"

META_BANS = {
    "top":     ["Darius", "Camille", "Fiora", "Renekton", "Garen"],
    "jungle":  ["Lee Sin", "Hecarim", "Nocturne", "Vi", "Briar"],
    "middle":  ["LeBlanc", "Fizz", "Zed", "Syndra", "Ahri"],
    "bottom":  ["Caitlyn", "Jinx", "Jhin", "Kai'Sa", "Miss Fortune"],
    "utility": ["Blitzcrank", "Thresh", "Nautilus", "Pyke", "Leona"],
}

DEFAULT_CONFIG = {
    "autoAccept":   True,
    "autoPick":     True,
    "autoPrePick":  True,
    "autoBan":      True,
    "autoRunes":    True,    # import the meta rune page on lock
    "autoSpells":   True,    # import the meta summoner spells on lock
    "autoItems":    True,    # import an op.gg item set once the pick is locked
    "pickDelay":    3000,    # lock this many ms before the pick phase ends
    "banDelay":     8000,    # ban this many ms before the ban phase ends
    "prePickDelay": 500,
    "acceptDelay":  0,       # wait before auto-accepting a found match (ms)
    "readyUpEnabled":  True,  # enable/disable the party ready-up feature
    "overlayRelX": None,       # saved overlay X offset from LCU left edge (None = auto)
    "overlayRelY": None,       # saved overlay Y offset from LCU top edge  (None = auto)
    "overlayPosVersion": "",   # version when position was saved; mismatch clears it
    "relayUrl":     "",      # ready-up relay server, e.g. http://192.168.1.50:8777
    "localApiPort": 0,      # Stream Deck REST API port (0 = disabled by default)
    "autoAcceptInvites": False, # auto-accept lobby invites from friends
    "inviteWhitelist":   [],    # restrict to these summoner names; empty = all friends
    "permaBans":    [],      # champion ids always banned first, regardless of role
    "roleChampions": {role: {"picks": [], "bans": []} for role in ROLES},
}


# ── Config helpers ────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            # Fill in any missing keys from DEFAULT_CONFIG
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            for role in ROLES:
                cfg.setdefault("roleChampions", {})
                if role not in cfg["roleChampions"]:
                    cfg["roleChampions"][role] = {"picks": [], "bans": []}
            # Clear saved overlay position on version change so the auto-
            # placement formula re-runs after an update.
            if cfg.get("overlayPosVersion") != APP_VERSION:
                cfg["overlayRelX"] = None
                cfg["overlayRelY"] = None
                cfg["overlayPosVersion"] = APP_VERSION
            return cfg
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy of defaults


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── Auto-update ───────────────────────────────────────────────────────────────
def _ver(v: str):
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def _client_asset_url(assets: list):
    """Pick the client exe from a release's assets — never the relay exe, even
    though both are .exe (otherwise the client could update itself to the relay)."""
    for a in assets:
        if a.get("name", "").lower() == "lol_client_tool.exe":
            return a["browser_download_url"]
    for a in assets:
        n = a.get("name", "").lower()
        if n.endswith(".exe") and "relay" not in n:
            return a["browser_download_url"]
    return None


def _update_check(root, log_fn):
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10, headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code != 200:
            return
        data = r.json()
        tag = data.get("tag_name", "")
        if _ver(tag) <= _ver(APP_VERSION):
            return
        dl_url = _client_asset_url(data.get("assets", []))
        if not dl_url:
            return
        root.after(0, lambda: _update_prompt(root, tag, dl_url, log_fn))
    except Exception:
        pass


def _update_prompt(root, tag: str, dl_url: str, log_fn):
    import tkinter.messagebox as _mb
    if _mb.askyesno(
        "Update Available",
        f"v{tag.lstrip('v')} is available  (you have v{APP_VERSION}).\n\n"
        "Download and restart now?",
        parent=root,
    ):
        threading.Thread(target=_do_update, args=(dl_url, log_fn, root),
                         daemon=True).start()


def _do_update(dl_url: str, log_fn, root):
    if not getattr(sys, "frozen", False):
        log_fn("[update] Auto-update only works in the packaged exe.")
        return
    exe = Path(sys.executable)
    tmp = exe.with_name("LOL_Client_Tool_update.exe")
    bat = exe.with_name("lol_update.bat")
    try:
        log_fn("[update] Downloading new version…")
        resp = requests.get(dl_url, stream=True, timeout=120)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        done  = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                if total:
                    log_fn(f"[update] Downloading… {done * 100 // total}%")
        # Guard against a truncated download swapping in a corrupt exe.
        if total and done != total:
            log_fn(f"[update] Download incomplete ({done}/{total} bytes) — aborting.")
            tmp.unlink(missing_ok=True)
            return
        log_fn("[update] Download complete — restarting…")
        pid = os.getpid()
        log = exe.with_name("lol_update_log.txt")
        # The batch waits for THIS process to fully exit (so the exe lock is
        # released), then swaps the file with retries and relaunches.
        # NOTE: use `ping` for delays, not `timeout` — `timeout` needs a console
        # and fails silently in a windowless/detached process, which previously
        # broke the swap. enabledelayedexpansion makes the retry counter work.
        bak = Path(str(exe) + ".bak")
        bat.write_text(
            "@echo off\r\n"
            "setlocal enableextensions enabledelayedexpansion\r\n"
            f'set "EXE={exe}"\r\n'
            f'set "NEW={tmp}"\r\n'
            f'set "BAK={bak}"\r\n'
            f'set "LOG={log}"\r\n'
            'echo === update started === > "%LOG%"\r\n'
            ":waitexit\r\n"
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  ping -n 2 127.0.0.1 >nul\r\n"
            "  goto waitexit\r\n"
            ")\r\n"
            'echo process exited >> "%LOG%"\r\n'
            # Wait longer for Windows to fully release the exe after process exit
            "ping -n 6 127.0.0.1 >nul\r\n"
            "set /a tries=0\r\n"
            # Rename-based swap: rename old→.bak, then rename new→old.
            # Avoids overwriting a locked file; renames within the same dir are atomic.
            ":swap\r\n"
            'del /f "%BAK%" 2>nul\r\n'
            'ren "%EXE%" "LOL_Client_Tool.exe.bak" 2>>"%LOG%"\r\n'
            'if not exist "%EXE%" (\r\n'
            '  ren "%NEW%" "LOL_Client_Tool.exe" 2>>"%LOG%"\r\n'
            '  if exist "%EXE%" (\r\n'
            '    echo swap ok >> "%LOG%"\r\n'
            '    del /f "%BAK%" 2>nul\r\n'
            '    goto launch\r\n'
            '  )\r\n'
            '  ren "%BAK%" "LOL_Client_Tool.exe" 2>nul\r\n'
            ')\r\n'
            "set /a tries+=1\r\n"
            '  echo swap retry !tries! >> "%LOG%"\r\n'
            "if !tries! lss 30 (\r\n"
            "  ping -n 2 127.0.0.1 >nul\r\n"
            "  goto swap\r\n"
            ")\r\n"
            ":launch\r\n"
            "ping -n 2 127.0.0.1 >nul\r\n"
            'echo launching new version >> "%LOG%"\r\n'
            # Relaunch via explorer so the new GUI process gets a clean interactive
            # context (exactly like a double-click). Launching with `start` from a
            # windowless cmd gave the onefile a broken context and failed to load
            # its bundled python DLL.
            'explorer.exe "%EXE%"\r\n'
            'echo done >> "%LOG%"\r\n'
            'del "%~f0"\r\n',
            encoding="ascii",
        )
        import subprocess as _sp
        # CREATE_NO_WINDOW keeps a (hidden) console so console tools behave, with
        # no visible window. Not DETACHED_PROCESS (that removes the console).
        _sp.Popen(
            ["cmd", "/c", str(bat)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        # Give the batch a moment to start, then hard-exit so the running exe is
        # unlocked (root.destroy alone can leave the frozen process alive, which
        # blocks the file swap).
        time.sleep(0.7)
        os._exit(0)
    except Exception as e:
        log_fn(f"[update] Failed: {e}")
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── op.gg build data (champion analysis) ────────────────────────────────────────
# Summoner-spell id → display name (Summoner's Rift relevant set).
SUMMONER_SPELLS = {
    1: "Cleanse", 3: "Exhaust", 4: "Flash", 6: "Ghost", 7: "Heal",
    11: "Smite", 12: "Teleport", 13: "Clarity", 14: "Ignite", 21: "Barrier",
    32: "Snowball", 39: "Snowball",
}

def _spell_name(sid):
    return SUMMONER_SPELLS.get(int(sid), f"Spell {sid}")

# Summoner-spell id → DDragon icon image name (…/img/spell/<name>.png).
SPELL_IMG = {
    1:  "SummonerBoost",    3:  "SummonerExhaust",  4:  "SummonerFlash",
    6:  "SummonerHaste",    7:  "SummonerHeal",     11: "SummonerSmite",
    12: "SummonerTeleport", 13: "SummonerMana",     14: "SummonerDot",
    21: "SummonerBarrier",  32: "SummonerSnowball", 39: "SummonerSnowball",
}

def _parse_opgg_record(text: str):
    """Parse op.gg's MCP 'class-schema + positional record' payload into nested
    dicts. The header lines ('class Name: f1,f2,...') define each record type's
    field order; the trailing expression is a nested Name(arg, arg, ...) tree
    where lists are [..] and scalars are strings/ints/floats. Records map to
    {field: value} using the schema. Returns the top record dict, or None."""
    schema = {}
    for line in text.splitlines():
        m = _re.match(r'class (\w+):\s*(.+)$', line.strip())
        if m:
            schema[m.group(1)] = [f.strip() for f in m.group(2).split(',')]
    expr = None
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s and not s.startswith('class ') and '(' in s:
            expr = s
            break
    if not expr:
        return None
    s, n, i = expr, len(expr), 0

    def ws():
        nonlocal i
        while i < n and s[i] in ' \t':
            i += 1

    def pval():
        nonlocal i
        ws()
        c = s[i]
        if c == '"':
            return pstr()
        if c == '[':
            return plist()
        if c.isalpha() or c == '_':
            j = i
            while i < n and (s[i].isalnum() or s[i] == '_'):
                i += 1
            name = s[j:i]
            ws()
            if i < n and s[i] == '(':
                args = pargs()
                fields = schema.get(name)
                if fields and len(fields) == len(args):
                    return dict(zip(fields, args))
                return {'_class': name, '_args': args}
            return {'null': None, 'true': True, 'false': False}.get(name, name)
        return pnum()

    def pstr():
        nonlocal i
        i += 1
        buf = []
        while i < n and s[i] != '"':
            if s[i] == '\\' and i + 1 < n:
                i += 1
            buf.append(s[i])
            i += 1
        i += 1
        return ''.join(buf)

    def pnum():
        nonlocal i
        j = i
        while i < n and s[i] in '-+.0123456789eE':
            i += 1
        t = s[j:i]
        try:
            return int(t) if ('.' not in t and 'e' not in t.lower()) else float(t)
        except ValueError:
            return t

    def plist():
        nonlocal i
        i += 1
        out = []
        ws()
        while i < n and s[i] != ']':
            out.append(pval())
            ws()
            if i < n and s[i] == ',':
                i += 1
                ws()
        i += 1
        return out

    def pargs():
        nonlocal i
        i += 1
        out = []
        ws()
        while i < n and s[i] != ')':
            out.append(pval())
            ws()
            if i < n and s[i] == ',':
                i += 1
                ws()
        i += 1
        return out

    try:
        return pval()
    except Exception:
        return None


# ── DDragon champion data ─────────────────────────────────────────────────────
class DDragon:
    def __init__(self):
        self._id_to_name: dict = {}
        self._name_to_id: dict = {}   # lowercase name → int id
        self._id_to_key:  dict = {}   # int id → internal key (e.g. "MissFortune")
        self._norm_to_id: dict = {}   # alnum-only name/key → int id (fuzzy match)
        self._version: str = None
        self._icon_dir = CONFIG_DIR / "champ_icons"

    def load(self):
        try:
            ver  = requests.get(f"{DDRAGON_URL}/api/versions.json", timeout=8).json()[0]
            self._version = ver
            data = requests.get(
                f"{DDRAGON_URL}/cdn/{ver}/data/en_US/champion.json", timeout=10
            ).json()["data"]
            for champ in data.values():
                cid  = int(champ["key"])
                name = champ["name"]
                self._id_to_name[cid] = name
                self._name_to_id[name.lower()] = cid
                self._id_to_key[cid] = champ["id"]   # e.g. "MissFortune", "DrMundo"
                # Fuzzy index: strip spaces/punctuation so op.gg's varied name
                # formats ("Miss Fortune", "MissFortune", "Cho'Gath"/"Chogath",
                # "Dr. Mundo") all resolve to the right id.
                self._norm_to_id[self._norm(name)]         = cid
                self._norm_to_id[self._norm(champ["id"])]  = cid
        except Exception as e:
            print(f"[DDragon] {e}")

    @staticmethod
    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]", "", str(s).lower())

    def find_id_fuzzy(self, name: str):
        """Resolve a champion id from a loosely-formatted name (e.g. op.gg's)."""
        cid = self.find_id(name)
        if cid is not None:
            return cid
        return self._norm_to_id.get(self._norm(name))

    def icon_file(self, champ_id: int):
        """Local cached path to a champion's square icon PNG, downloading it
        once if needed. Returns None if unavailable (no data / no network)."""
        key = self._id_to_key.get(int(champ_id))
        if not key or not self._version:
            return None
        try:
            self._icon_dir.mkdir(parents=True, exist_ok=True)
            path = self._icon_dir / f"{champ_id}.png"
            if path.exists() and path.stat().st_size > 0:
                return path
            url = f"{DDRAGON_URL}/cdn/{self._version}/img/champion/{key}.png"
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                path.write_bytes(r.content)
                return path
        except Exception:
            pass
        return None

    def spell_icon_file(self, spell_id: int):
        """Local cached path to a summoner spell's icon PNG, downloading it once
        if needed. Returns None if unavailable (unknown spell / no network)."""
        img = SPELL_IMG.get(int(spell_id))
        if not img or not self._version:
            return None
        try:
            self._icon_dir.mkdir(parents=True, exist_ok=True)
            path = self._icon_dir / f"spell_{int(spell_id)}.png"
            if path.exists() and path.stat().st_size > 0:
                return path
            url = f"{DDRAGON_URL}/cdn/{self._version}/img/spell/{img}.png"
            r = requests.get(url, timeout=8)
            if r.status_code == 200:
                path.write_bytes(r.content)
                return path
        except Exception:
            pass
        return None

    def name(self, champ_id: int) -> str:
        return self._id_to_name.get(int(champ_id), str(champ_id))

    def opgg_name(self, champ_id: int):
        """Champion name in op.gg's UPPER_SNAKE_CASE (e.g. MISS_FORTUNE)."""
        key = self._id_to_key.get(int(champ_id))
        if not key:
            return None
        return _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).upper()

    def find_id(self, name: str):
        return self._name_to_id.get(name.strip().lower())

    def all_display_names(self) -> list:
        return sorted(self._id_to_name.values())

    def all_ids(self) -> set:
        return set(self._id_to_name.keys())


# ── LCU API wrapper ───────────────────────────────────────────────────────────
class LCU:
    """Thin wrapper around the League Client's local HTTPS API."""

    def __init__(self):
        self._sess = None
        self._base = None

    def connect(self) -> bool:
        lf = self._find_lockfile()
        if lf is None:
            return False
        # lockfile format:  name:pid:port:password:protocol
        parts    = lf.read_text().strip().split(":")
        port     = parts[2]
        password = parts[3]
        self._base = f"https://127.0.0.1:{port}"
        s = requests.Session()
        s.verify = False
        s.auth   = ("riot", password)
        self._sess = s
        return True

    def _find_lockfile(self):
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if proc.info["name"] and "LeagueClientUx" in proc.info["name"]:
                    lf = Path(proc.info["exe"]).parent / "lockfile"
                    if lf.exists():
                        return lf
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return None

    def ping(self) -> bool:
        try:
            return self.get("/lol-summoner/v1/current-summoner").status_code == 200
        except Exception:
            return False

    def get(self, ep: str):
        return self._sess.get(f"{self._base}{ep}", timeout=5)

    def post(self, ep: str, body=None):
        return self._sess.post(f"{self._base}{ep}", json=body, timeout=5)

    def patch(self, ep: str, body=None):
        return self._sess.patch(f"{self._base}{ep}", json=body, timeout=5)

    def put(self, ep: str, body=None):
        return self._sess.put(f"{self._base}{ep}", json=body, timeout=5)

    def delete(self, ep: str):
        return self._sess.delete(f"{self._base}{ep}", timeout=5)

    def complete_action(self, action_id: int):
        return self._sess.post(
            f"{self._base}/lol-champ-select/v1/session/actions/{action_id}/complete",
            json={}, timeout=5
        )


# ── TFT "attack click on left click" override ───────────────────────────────
# Verified against a real install: the setting lives at
#   game.cfg:               [General] section, line "EnableLeftMouseButtonAttackMove=0/1"
#   PersistedSettings.json: files[name="Game.cfg"].sections[name="General"]
#                           .settings[name="EnableLeftMouseButtonAttackMove"].value
# Both are patched together so the change survives whichever file the client
# actually reads at game launch.
_TFT_SETTING = "EnableLeftMouseButtonAttackMove"

def _league_config_dir():
    """<League install dir>/Config, located via the running LeagueClientUx.exe."""
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if proc.info["name"] and "LeagueClientUx" in proc.info["name"]:
                cfg_dir = Path(proc.info["exe"]).parent / "Config"
                if cfg_dir.is_dir():
                    return cfg_dir
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None

def _read_game_cfg_setting(cfg_dir: Path):
    text = (cfg_dir / "game.cfg").read_text(encoding="utf-8")
    m = _re.search(rf"^{_TFT_SETTING}=(\S+)$", text, _re.M)
    return m.group(1) if m else None

def _write_game_cfg_setting(cfg_dir: Path, value: str) -> bool:
    path = cfg_dir / "game.cfg"
    text = path.read_text(encoding="utf-8")
    new_text, n = _re.subn(rf"^{_TFT_SETTING}=\S+$",
                           f"{_TFT_SETTING}={value}", text, count=1, flags=_re.M)
    if not n:
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return True

def _write_persisted_setting(cfg_dir: Path, value: str) -> bool:
    path = cfg_dir / "PersistedSettings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = None
    for f in data.get("files", []):
        if f.get("name") == "Game.cfg":
            for sec in f.get("sections", []):
                if sec.get("name") == "General":
                    for s in sec.get("settings", []):
                        if s.get("name") == _TFT_SETTING:
                            entry = s
    if entry is None:
        return False
    entry["value"] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(path)
    return True

def _set_attack_click_on_left_click(cfg_dir: Path, value: str) -> bool:
    """Write `value` ("0" or "1") to both game.cfg and PersistedSettings.json.
    Returns True if at least one file was updated."""
    ok1 = _write_game_cfg_setting(cfg_dir, value)
    ok2 = _write_persisted_setting(cfg_dir, value)
    return ok1 or ok2


# ── Automation engine ─────────────────────────────────────────────────────────
class AutoEngine:
    """Polls the LCU every 2 s and acts based on game flow phase."""

    POLL = 0.5  # seconds

    def __init__(self, lcu: LCU, cfg_fn, log_fn, ddragon=None, on_phase=None):
        self._lcu          = lcu
        self._cfg          = cfg_fn    # callable → dict
        self._log          = log_fn    # callable(str)
        self._dd           = ddragon   # DDragon, for champ-id → op.gg name
        self._on_phase     = on_phase  # callable(phase) — UI reacts to phase
        self._stop         = threading.Event()
        self._last_phase   = ""
        self._done_actions:  set  = set()
        self._action_start:  dict = {}   # aid → monotonic time when isInProgress first seen
        self._prepicked:     dict = {}   # aid → championId the TOOL last hovered
        self._tool_hovers:   dict = {}   # aid → set of every champ the TOOL has hovered
        self._user_pick:     dict = {}   # aid → championId the USER manually hovered
        self._pick_rejected: dict = {}   # aid → champs that became banned/taken
        self._ban_hovered:   dict = {}   # aid → championId hovered for ban (two-phase)
        self._runes_key      = None      # (champ_id, role) of last runes import
        self._items_key      = None      # (champ_id, role) of last item-set import
        self._last_role      = None      # assignedPosition seen last poll (detect swaps)
        self._queue_started     = False  # leader already started queue this lobby
        self._last_ready_status = None   # last "X/Y ready" string logged
        self._last_relay_poll   = 0.0    # throttle relay polling
        self._ready_count       = 0      # exposed to overlay
        self._present_count     = 0      # exposed to overlay
        self._party_size        = 0      # total members in lobby (1 = solo, >1 = in party)
        self._party_size_dec    = 0      # consecutive shrink-polls (hysteresis counter)
        self._champ_select_start = 0.0   # monotonic when champ select began
        self._i_am_ready        = False  # my own ready state (for relay heartbeat)
        self._accept_time       = None   # monotonic when ReadyCheck began
        self._accepted          = False  # already accepted this ready check
        self._accepted_invites: set   = set()   # invite IDs already accepted this session
        self._invite_seen:      dict  = {}      # invitationId → monotonic time first seen
        self._friends_cache:    set   = set()   # cached set of friend summoner IDs
        self._friends_ts:       float = 0.0     # monotonic time of last friends fetch
        self._others_ready_notified: bool = False
        self._sound_cancel: threading.Event = threading.Event()
        self._last_tft_poll:        float = 0.0   # throttle TFT gameMode checks
        self._tft_saved_click_value: str  = None  # pre-TFT value pending restore
        self._tft_detected_logged:  bool  = False # already reported this TFT session

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ── Polling loop ──────────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except requests.exceptions.ConnectionError:
                pass   # League client not running — expected, no need to log
            except Exception as exc:
                self._log(f"[error] {exc}")
            time.sleep(self.POLL)

    def _tick(self):
        cfg = self._cfg()

        # Invites must fire even when idle (no active session → 404 from session endpoint).
        rp = self._lcu.get("/lol-gameflow/v1/gameflow-phase")
        idle_phase = rp.json() if rp.status_code == 200 else ""
        if idle_phase in ("", "None", "Lobby") or rp.status_code != 200:
            self._handle_invites(cfg)

        r   = self._lcu.get("/lol-gameflow/v1/session")
        if r.status_code != 200:
            # No active session — use idle_phase so the overlay updates correctly.
            # Without this, phase stays stale as "Lobby" forever after leaving.
            idle = idle_phase if idle_phase else "None"
            if idle != self._last_phase:
                self._log(f"Phase → {idle}")
                prev_phase       = self._last_phase
                self._last_phase = idle
                self._accept_time = None
                self._accepted    = False
                if self._on_phase:
                    self._on_phase(idle)
                if prev_phase in ("Lobby", "Matchmaking") and idle not in ("Lobby", "Matchmaking", "ReadyCheck"):
                    self._party_size             = 0
                    self._present_count          = 0
                    self._ready_count            = 0
                    self._size_none_streak       = 0
                    self._others_ready_notified  = False
                    self._sound_cancel.set()
                    self._accepted_invites.clear()
                    self._invite_seen.clear()
            return

        phase = r.json().get("phase", "")

        if phase != self._last_phase:
            self._log(f"Phase → {phase}")
            prev_phase       = self._last_phase
            self._last_phase = phase
            # Reset the per-ready-check accept timer on any phase change.
            self._accept_time = None
            self._accepted = False
            if self._on_phase:
                self._on_phase(phase)   # UI: grey the Ready Up button in champ select
            if phase == "ChampSelect":
                self._done_actions.clear()
                self._action_start.clear()
                self._prepicked.clear()
                self._tool_hovers.clear()
                self._user_pick.clear()
                self._pick_rejected.clear()
                self._ban_hovered.clear()
                self._runes_key      = None
                self._items_key      = None
                self._last_role      = None
                self._last_state_key = None   # force [tick] on first poll
                self._champ_select_start = time.monotonic()  # for pre-pick delay
                # A game started — the ready cycle is done; clear my ready state.
                self._i_am_ready = False
                self._log_champ_select_debug()
            if phase == "Lobby":
                self._queue_started     = False
                self._last_ready_status = None
            # Match declined or missed: ReadyCheck → Lobby/Matchmaking means
            # the accept window closed without a game starting. Auto-unready so
            # the user doesn't stay locked in a ready state they can't undo.
            if prev_phase == "ReadyCheck" and phase in ("Lobby", "Matchmaking"):
                self._i_am_ready  = False
                self._ready_count = 0
                self._log("Match declined/missed — ready state cleared.")
            # Leaving lobby/matchmaking: immediately clear stale party_size so
            # the overlay doesn't linger while no lobby is active.
            if prev_phase in ("Lobby", "Matchmaking") and phase not in ("Lobby", "Matchmaking", "ReadyCheck"):
                self._party_size            = 0
                self._size_none_streak      = 0
                self._others_ready_notified = False
                self._sound_cancel.set()
                self._accepted_invites.clear()
                self._invite_seen.clear()

        # Auto accept — after the configured delay (default 0 = immediate)
        if cfg.get("autoAccept") and phase == "ReadyCheck" and not self._accepted:
            if self._accept_time is None:
                self._accept_time = time.monotonic()
            if (time.monotonic() - self._accept_time) * 1000 >= cfg.get("acceptDelay", 0):
                self._lcu.post("/lol-matchmaking/v1/ready-check/accept")
                self._accepted = True
                self._log("Auto-accepted match.")

        if phase == "ChampSelect":
            self._handle_champ_select(cfg)

        # Party ready check — tally ready state and (if leader) start the queue.
        # Runs during Matchmaking too so the leader can cancel if someone unreadies.
        if phase in ("Lobby", "Matchmaking"):
            self._handle_party_ready()
            self._handle_tft_settings(cfg)

        # Invite handling already ran above for idle/lobby phases.

    # ── TFT settings override ────────────────────────────────────────────────
    def _current_game_mode(self):
        """Best-effort current LCU gameMode ('TFT', 'CLASSIC', ...), or None."""
        try:
            r = self._lcu.get("/lol-lobby/v2/lobby")
            if r.status_code == 200:
                gm = r.json().get("gameConfig", {}).get("gameMode")
                if gm:
                    return gm
        except Exception:
            pass
        try:
            r = self._lcu.get("/lol-gameflow/v1/session")
            if r.status_code == 200:
                gm = r.json().get("gameData", {}).get("queue", {}).get("gameMode")
                if gm:
                    return gm
        except Exception:
            pass
        return None

    def _handle_tft_settings(self, cfg):
        """When a TFT lobby/queue is detected, turn off attack-click-on-left-
        click and remember the previous value for a manual restore later.
        Always logs once per TFT session so detection is visible even when
        no file change was needed (e.g. it was already off)."""
        if not cfg.get("tftFixEnabled", True):
            return
        now = time.monotonic()
        if now - self._last_tft_poll < 2.0:
            return
        self._last_tft_poll = now

        if self._current_game_mode() != "TFT":
            self._tft_detected_logged = False   # re-arm for the next TFT lobby
            return
        if self._tft_detected_logged:
            return   # already reported this TFT session

        cfg_dir = _league_config_dir()
        if not cfg_dir:
            self._log("TFT lobby detected, but the League install folder "
                      "couldn't be located.")
            self._tft_detected_logged = True
            return
        try:
            current = _read_game_cfg_setting(cfg_dir)
            if current is None:
                self._log("TFT lobby detected, but the click setting "
                          "couldn't be read from game.cfg.")
            elif current == "0":
                self._log("TFT lobby detected — attack-click on left click "
                          "is already off, nothing to change.")
            elif _set_attack_click_on_left_click(cfg_dir, "0"):
                self._tft_saved_click_value = current
                self._log(
                    f"TFT lobby detected — disabled attack-click on left "
                    f"click (was {current})."
                )
            else:
                self._log("TFT lobby detected, but writing the setting failed.")
        except Exception as exc:
            self._log(f"TFT settings error: {exc}")
        self._tft_detected_logged = True

    def restore_tft_settings(self) -> bool:
        """Manual restore of the pre-TFT value. Returns True if a restore
        actually happened (False if nothing was pending)."""
        if self._tft_saved_click_value is None:
            return False
        cfg_dir = _league_config_dir()
        if not cfg_dir:
            self._log("Restore failed: League client not found.")
            return False
        value = self._tft_saved_click_value
        try:
            if _set_attack_click_on_left_click(cfg_dir, value):
                self._log(f"Restored attack-click on left click to {value}.")
                self._tft_saved_click_value = None
                return True
        except Exception as exc:
            self._log(f"Restore error: {exc}")
        return False

    # ── Diagnostics ───────────────────────────────────────────────────────────
    def _log_champ_select_debug(self):
        try:
            r = self._lcu.get("/lol-champ-select/v1/session")
            if r.status_code != 200:
                self._log(f"[debug] champ-select session HTTP {r.status_code}")
                return
            s = r.json()
            cell = s.get("localPlayerCellId", "?")
            team = s.get("myTeam", [])
            me = next((p for p in team if str(p.get("cellId", "")) == str(cell)), {})
            role = (me.get("assignedPosition") or "none").lower()
            actions_flat = [a for g in s.get("actions", []) for a in g
                            if str(a.get("actorCellId", "")) == str(cell)]
            action_summary = ", ".join(
                f"{a.get('type')}(id={a.get('id')} prog={a.get('isInProgress')})"
                for a in actions_flat
            ) or "none"
            self._log(
                f"[debug] cell={cell} role={role} "
                f"my_actions=[{action_summary}]"
            )
        except Exception as e:
            self._log(f"[debug] error: {e}")

    # ── Champion select ───────────────────────────────────────────────────────
    # Faithful port of Terevenen2/LOL-CLient-TOOL MainWindow.axaml.cs auto pick/ban.
    # Key mechanics taken from the reference:
    #   • A single PATCH carries the FULL action body and "completed": true/false —
    #     true commits a pick/ban, false just hovers (pre-pick). No /complete call.
    #   • Bans avoid champions already banned or in an ally's championPickIntent.
    #   • Picks remove champions already taken by allies/enemies from the playable
    #     pool before choosing.
    # Adapted for Python: the reference's blocking `await Task.Delay` is replaced by
    # non-blocking elapsed-time tracking so the poll thread is never stalled.
    def _handle_champ_select(self, cfg: dict):
        r = self._lcu.get("/lol-champ-select/v1/session")
        if r.status_code != 200:
            return
        session = r.json()

        # ── bans + ally pick intents (reference: bans list + championPickIntent) ──
        bans: set = set()
        for bid in session.get("bans", {}).get("myTeamBans", []):
            if int(bid): bans.add(int(bid))
        for bid in session.get("bans", {}).get("theirTeamBans", []):
            if int(bid): bans.add(int(bid))

        pick_intents: set = set()
        for p in session.get("myTeam", []):
            intent = int(p.get("championPickIntent", 0) or 0)
            if intent:
                pick_intents.add(intent)

        # ── phase timer: how long until the current sub-phase ends ──
        _timer      = session.get("timer", {})
        time_left   = int(_timer.get("adjustedTimeLeftInPhase", 0) or 0)   # ms
        is_infinite = bool(_timer.get("isInfinite", False))

        # ── locate our own cell ──
        local_cell_id = session.get("localPlayerCellId")
        if local_cell_id is None:
            return
        my_cell = str(local_cell_id)

        assigned_role = ""
        for p in session.get("myTeam", []):
            if str(p.get("cellId", "")) == my_cell:
                assigned_role = (p.get("assignedPosition") or "").lower()
                break

        # ── detect a role swap (position trade with a teammate) ──
        # The pre-pick / hover logic re-evaluates automatically when the new role's
        # best champion differs from what's hovered, and runes re-import is handled
        # by the (champ, role) key below — so we only log the change here. (We must
        # NOT clear _prepicked, or the existing hover would look like a user pick.)
        if assigned_role != self._last_role:
            if self._last_role is not None and assigned_role:
                self._log(
                    f"Role changed: {ROLE_LABEL.get(self._last_role, self._last_role)} → "
                    f"{ROLE_LABEL.get(assigned_role, assigned_role)} — re-evaluating."
                )
            self._last_role = assigned_role

        # ── champions already taken (removed from the playable pool, like the ref) ──
        # Duplicate-allowed champions (-3) never count as taken — an ally holding
        # one doesn't stop us from picking our own.
        taken: set = set()
        for p in session.get("myTeam", []):
            if str(p.get("cellId", "")) != my_cell:
                cid = int(p.get("championId", 0) or 0)
                if cid and cid not in DUP_ALLOWED_CHAMPS: taken.add(cid)
        for p in session.get("theirTeam", []):
            cid = int(p.get("championId", 0) or 0)
            if cid and cid not in DUP_ALLOWED_CHAMPS: taken.add(cid)

        # ── resolve role-based priority lists (this tool's own feature) ──
        role_cfg_map = cfg.get("roleChampions", {})
        if assigned_role in ROLES:
            role_key  = assigned_role
            pick_prio = [int(c) for c in role_cfg_map.get(role_key, {}).get("picks", [])]
            ban_prio  = [int(c) for c in role_cfg_map.get(role_key, {}).get("bans",  [])]
        else:
            role_key  = "fill"
            seen: set = set()
            pick_prio, ban_prio = [], []
            for rk in ROLES:
                for c in role_cfg_map.get(rk, {}).get("picks", []):
                    if int(c) not in seen:
                        pick_prio.append(int(c)); seen.add(int(c))
            seen.clear()
            for rk in ROLES:
                for c in role_cfg_map.get(rk, {}).get("bans", []):
                    if int(c) not in seen:
                        ban_prio.append(int(c)); seen.add(int(c))

        # Permabans always come first, regardless of role.
        perma = [int(c) for c in cfg.get("permaBans", [])]
        if perma:
            ban_prio = perma + [c for c in ban_prio if c not in set(perma)]

        # Champions actually selectable this session (Practice Tool → all)
        playable = self._get_pickable_ids()
        # Reference removes already-taken champions from the playable pool.
        # Duplicate-allowed champions (-3) are always playable regardless of the
        # pickable list, bans, or an ally already having one.
        playable_now = (playable - taken - bans) | DUP_ALLOWED_CHAMPS

        # ── diagnostic: log only on state change ──
        _states = [
            f"{a.get('type')}(id={a.get('id')} champ={a.get('championId')} prog={a.get('isInProgress')} done={a.get('completed')})"
            for grp in session.get("actions", [])
            for a in grp
            if str(a.get("actorCellId", "")) == my_cell
        ]
        _key = f"{_states}|bans={sorted(bans)}|intent={sorted(pick_intents)}|t={time_left // 2000}"
        if _key != getattr(self, "_last_state_key", None):
            self._log(f"[tick] {_states}  bans={sorted(bans)}  t={time_left}ms inf={is_infinite}")
            self._last_state_key = _key

        # ── champions an ally is CURRENTLY hovering for a ban ──────────────────
        # If an ally is hovering our intended ban champion at any time (no delay),
        # we move our ban to the next champion on the priority list — no point
        # double-banning, and they may be banning it for us.
        ally_banning: set = set()
        for _grp in session.get("actions", []):
            for _act in _grp:
                if str(_act.get("actorCellId", "")) == my_cell:
                    continue
                if (_act.get("type") == "ban" and _act.get("isInProgress")
                        and not _act.get("completed")):
                    _cid = int(_act.get("championId", 0) or 0)
                    if _cid:
                        ally_banning.add(_cid)

        # ── action loop (mirrors reference foreach actions → foreach team) ──
        for action_group in session.get("actions", []):
            for action in action_group:
                if str(action.get("actorCellId", "")) != my_cell:
                    continue

                aid         = int(action.get("id", -1))
                atype       = action.get("type", "")
                in_progress = bool(action.get("isInProgress", False))
                completed   = bool(action.get("completed", False))

                # ── Detect a manual hover by the user on a pick action ──
                # A championId the TOOL never hovered (and that we haven't already
                # rejected as unavailable) means the user chose it themselves — that
                # champion takes priority and is what we lock in (works before and
                # during the pick phase).
                if atype == "pick" and aid not in self._done_actions:
                    cur      = int(action.get("championId", 0) or 0)
                    rejected = self._pick_rejected.get(aid, set())
                    if (cur
                            and cur not in self._tool_hovers.get(aid, set())
                            and cur not in rejected):
                        if self._user_pick.get(aid) != cur:
                            self._user_pick[aid] = cur
                            name = self._dd.name(cur) if self._dd else f"#{cur}"
                            self._log(f"You hovered {name} — that's what will be locked.")

                    # If the user's hovered champion got banned or picked by someone
                    # else, drop it so picks fall back to the priority list. A
                    # duplicate-allowed champion (-3) is never dropped — lock it in.
                    ov = self._user_pick.get(aid)
                    if ov and ov not in playable_now and ov not in DUP_ALLOWED_CHAMPS:
                        self._pick_rejected.setdefault(aid, set()).add(ov)
                        self._user_pick.pop(aid, None)
                        name = self._dd.name(ov) if self._dd else f"#{ov}"
                        self._log(f"{name} was banned or taken — moving to the next option.")

                # ── PICK ── reference: isInProgress && type==pick && autoPick
                if (cfg.get("autoPick")
                        and atype == "pick"
                        and in_progress
                        and not completed
                        and aid not in self._done_actions):
                    # The user's hovered champion wins (validity already checked above).
                    override = self._user_pick.get(aid)
                    if aid not in self._action_start:
                        self._action_start[aid] = time.monotonic()
                    if (time.monotonic() - self._action_start[aid]) * 1000 < min(cfg.get("pickDelay", 3000), 29000):
                        # Still waiting to lock. Keep the intended champion hovered so a
                        # role swap mid-turn is reflected — but never override a user's hover.
                        if override is None:
                            champ = self._best(pick_prio, set(), playable_now)
                            if champ and self._prepicked.get(aid) != champ:
                                if self._commit_action(action, champ, complete=False):
                                    self._prepicked[aid] = champ
                                    self._tool_hovers.setdefault(aid, set()).add(champ)
                        continue
                    # Lock: the user's hovered champion wins; otherwise the next
                    # available champion on the priority list.
                    champ = override if override else self._best(pick_prio, set(), playable_now)
                    if champ:
                        ok = self._commit_action(action, champ, complete=True)
                        if ok:
                            src = "your pick" if override else f"{ROLE_LABEL.get(role_key, role_key)}"
                            self._log(f"Locked champion #{champ} ({src})")
                            self._done_actions.add(aid)
                    else:
                        self._log(f"No available pick for {ROLE_LABEL.get(role_key, role_key)}. pick_prio={pick_prio}")

                # ── BAN ── two-phase: hover the champion first so the LCU registers
                # it, THEN complete on a later poll. A single championId+completed
                # PATCH is silently ignored for bans (champion was never hovered);
                # this mirrors why picks work — they're pre-hovered before locking.
                elif (cfg.get("autoBan")
                        and atype == "ban"
                        and in_progress
                        and not completed
                        and aid not in self._done_actions):
                    # The ban-turn timer starts once and is NEVER reset on a
                    # target switch, so switching away from an ally's champion can
                    # never postpone the lock (that bug banned nothing).
                    if aid not in self._action_start:
                        self._action_start[aid] = time.monotonic()
                    # Never target a champion a teammate intends to pick
                    # (championPickIntent) or is hovering for a ban: the client
                    # REJECTS banning a teammate's intended pick with HTTP 400, so
                    # even permabans must yield — we fall through to the next
                    # priority instead of retrying a ban that can never lock.
                    unavail = bans | pick_intents | ally_banning

                    # Keep our current hover if it's still valid — don't oscillate
                    # back to a higher priority an ally merely moved off of. Only
                    # retarget when the current pick is taken or an ally is on it.
                    current = self._ban_hovered.get(aid)
                    if current and current not in unavail:
                        champ = current
                    else:
                        champ = self._best(ban_prio, unavail, set(range(1_000_000)))
                        if current and current != champ and current in ally_banning:
                            pname = self._dd.name(current) if self._dd else f"#{current}"
                            cname = (self._dd.name(champ) if self._dd else f"#{champ}") \
                                    if champ else "the next option"
                            self._log(f"Ally hovering {pname} for ban — switching to {cname}.")

                    cur     = int(action.get("championId", 0) or 0)
                    elapsed = (time.monotonic() - self._action_start[aid]) * 1000
                    # Lock once the ban delay has passed, or force it through if
                    # the ban timer is nearly up so we never miss the ban.
                    ready  = elapsed >= min(cfg.get("banDelay", 8000), 8000)
                    ending = (not is_infinite) and 0 < time_left <= 4000
                    # ── Debug dump: exact state driving the ban decision ──
                    _raw_bans = [
                        (str(a.get("actorCellId")), int(a.get("championId", 0) or 0),
                         bool(a.get("isInProgress")), bool(a.get("completed")))
                        for g in session.get("actions", []) for a in g
                        if a.get("type") == "ban"
                    ]
                    _dbg(f"[ban] aid={aid} champ={champ} current={current} "
                         f"cur(session)={cur} hovered={self._ban_hovered.get(aid)} "
                         f"elapsed={int(elapsed)}ms banDelay={min(cfg.get('banDelay', 8000), 8000)} "
                         f"ready={ready} ending={ending} time_left={time_left} inf={is_infinite}")
                    _dbg(f"[ban]   ban_prio={ban_prio} bans={sorted(bans)} "
                         f"pick_intents={sorted(pick_intents)} ally_banning={sorted(ally_banning)} "
                         f"unavail={sorted(unavail)}")
                    _dbg(f"[ban]   raw ban actions (cell,champ,inprog,done)={_raw_bans}")
                    _dbg("[ban]   myTeam (cell,champ,pickIntent,pos)=" + str([
                        (mb.get("cellId"), int(mb.get("championId", 0) or 0),
                         int(mb.get("championPickIntent", 0) or 0),
                         mb.get("assignedPosition", ""))
                        for mb in session.get("myTeam", [])
                    ]))
                    if not champ:
                        _dbg("[ban]   -> NO VALID CHAMP (nothing to ban)")
                        self._log(f"No valid ban for {ROLE_LABEL.get(role_key, role_key)}. Add champions to the ban list!")
                    elif cur != champ or self._ban_hovered.get(aid) != champ:
                        # Phase 1 — (re)hover the target; timer is not reset.
                        _dbg(f"[ban]   -> HOVER {champ} (cur={cur} hovered={self._ban_hovered.get(aid)})")
                        if self._commit_action(action, champ, complete=False):
                            self._ban_hovered[aid] = champ
                            self._log(f"[debug] Ban hover: #{champ}  [{ROLE_LABEL.get(role_key, role_key)}]")
                    elif ready or ending:
                        # Phase 2 — champion is hovered and stuck; lock it in.
                        _dbg(f"[ban]   -> LOCK {champ} (ready={ready} ending={ending})")
                        if self._commit_action(action, champ, complete=True):
                            self._log(f"Banned champion #{champ}")
                            self._done_actions.add(aid)
                    else:
                        _dbg(f"[ban]   -> WAIT (hovered {champ}, {int(elapsed)}/{min(cfg.get('banDelay', 8000), 8000)}ms)")

                # ── PRE-PICK ── hover our intended champion before our turn (after
                # the pre-pick delay), unless the user has already hovered one.
                elif (cfg.get("autoPrePick")
                        and atype == "pick"
                        and not in_progress
                        and not completed
                        and aid not in self._done_actions
                        and aid not in self._user_pick
                        and (time.monotonic() - getattr(self, "_champ_select_start", 0))
                            * 1000 >= cfg.get("prePickDelay", 500)):
                    champ = self._best(pick_prio, set(), playable_now)
                    if champ and self._prepicked.get(aid) != champ:
                        if self._commit_action(action, champ, complete=False):
                            self._prepicked[aid] = champ
                            self._tool_hovers.setdefault(aid, set()).add(champ)
                            self._log(f"Pre-pick hover: #{champ}  [{ROLE_LABEL.get(role_key, role_key)}]")

        # ── Post-lock imports — once our champion is locked in ──
        # Runes, spells and the item set are each toggled independently.
        # Keyed on (champion, role) so a position swap after locking re-imports
        # the correct meta data for the new role.
        locked = 0
        for grp in session.get("actions", []):
            for a in grp:
                if (str(a.get("actorCellId", "")) == my_cell
                        and a.get("type") == "pick"
                        and a.get("completed")):
                    locked = int(a.get("championId", 0) or 0)
        if locked:
            key = (locked, assigned_role)
            do_runes  = bool(cfg.get("autoRunes"))
            do_spells = bool(cfg.get("autoSpells"))
            if (do_runes or do_spells) and key != self._runes_key:
                self._runes_key = key
                threading.Thread(
                    target=self._import_runes_spells,
                    args=(locked, assigned_role, do_runes, do_spells),
                    daemon=True,
                ).start()
            if cfg.get("autoItems") and key != self._items_key:
                self._items_key = key
                threading.Thread(
                    target=self._import_item_set,
                    args=(locked, assigned_role),
                    daemon=True,
                ).start()

    def _import_runes_spells(self, champ_id: int, position: str,
                             do_runes: bool = True, do_spells: bool = True):
        """Import the meta rune page and/or summoner spells for the locked-in
        champion — runes and spells are toggled independently on the dashboard.
        Primary source: op.gg Diamond+ stats for this exact champion and role
        (the most-played page across recent diamond+ games). Falls back to the
        League client's own recommendation if op.gg is unavailable. Worker thread."""
        if not (do_runes or do_spells):
            return
        try:
            champ_name = self._dd.opgg_name(champ_id) if self._dd else None
            opgg_pos   = OPGG_POSITION.get((position or "").lower())
            label      = self._dd.name(champ_id) if self._dd else f"#{champ_id}"

            prim = sub = None
            perks: list = []
            spells: list = []

            # ── Primary: op.gg Diamond+ for this champion + role ──
            if champ_name and opgg_pos:
                data = self._opgg_runes(champ_name, opgg_pos)
                if data:
                    prim, perks, sub, spells = data
                    self._log(f"Runes: using Diamond+ meta for {label} ({opgg_pos}).")

            # ── Fallback: League client's own recommended page ──
            if not (prim and sub and perks):
                page = None
                for _ in range(5):
                    r = self._lcu.get("/lol-perks/v1/recommended-pages")
                    if r.status_code == 200:
                        cand = [p for p in r.json()
                                if int(p.get("championId", 0) or 0) == champ_id]
                        if cand:
                            pos = (position or "").upper()
                            page = next(
                                (p for p in cand
                                 if (p.get("position", "") or "").upper() == pos),
                                cand[0],
                            )
                            break
                    time.sleep(1)
                if page:
                    perks  = page.get("perks") or []
                    prim   = page.get("primaryPerkStyleId")
                    sub    = page.get("secondaryPerkStyleId")
                    if not spells:
                        spells = page.get("summonerSpellIds") or []

            # ── Apply runes ──
            if do_runes:
                if prim and sub and len(perks) >= 6:
                    self._make_rune_room()
                    rp = self._lcu.post("/lol-perks/v1/pages", {
                        "name":            f"Auto: {label}",
                        "primaryStyleId":  int(prim),
                        "subStyleId":      int(sub),
                        "selectedPerkIds": [int(x) for x in perks],
                        "current":         True,
                    })
                    if rp.status_code in (200, 201):
                        self._log(f"Imported meta runes for {label}.")
                    else:
                        self._log(f"[runes] page create HTTP {rp.status_code} {rp.text[:120]}")
                else:
                    self._log(f"Runes: no meta data available for {label}.")

            # ── Apply summoner spells (meta if found, else role default) ──
            if do_spells:
                if len(spells) < 2:
                    spells = list(ROLE_SPELLS.get((position or "").lower(), (4, 14)))
                rs = self._lcu.patch(
                    "/lol-champ-select/v1/session/my-selection",
                    {"spell1Id": int(spells[0]), "spell2Id": int(spells[1])},
                )
                if rs.status_code in (200, 204):
                    self._log("Imported meta summoner spells.")
                else:
                    self._log(f"[runes] spells HTTP {rs.status_code} {rs.text[:120]}")
        except Exception as e:
            self._log(f"[runes] error: {e}")

    def _opgg_runes(self, champ_name: str, opgg_pos: str):
        """Query op.gg for the most-played Diamond+ rune page + spells for this
        champion and role. Returns (primary_style, perk_ids, sub_style, spell_ids)
        or None. perk_ids = keystone+primary minors + secondary minors + shards."""
        try:
            body = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "lol_get_champion_analysis",
                    "arguments": {
                        "champion":  champ_name,
                        "position":  opgg_pos,
                        "tier":      "diamond_plus",
                        "game_mode": "ranked",
                        "desired_output_fields": [
                            "data.runes.primary_page_id",
                            "data.runes.primary_rune_ids",
                            "data.runes.secondary_page_id",
                            "data.runes.secondary_rune_ids",
                            "data.runes.stat_mod_ids",
                            "data.summoner_spells.ids",
                        ],
                    },
                },
            }
            r = requests.post(
                "https://mcp-api.op.gg/mcp", json=body, timeout=15,
                headers={"Accept": "application/json, text/event-stream"},
            )
            r.raise_for_status()
            text = r.json()["result"]["content"][0]["text"]

            m = _re.search(
                r"Runes\((\d+),\[([\d,]+)\],(\d+),\[([\d,]+)\],\[([\d,]+)\]",
                text,
            )
            if not m:
                return None
            prim   = int(m.group(1))
            sub    = int(m.group(3))
            primary_runes   = [int(x) for x in m.group(2).split(",") if x]
            secondary_runes = [int(x) for x in m.group(4).split(",") if x]
            shards          = [int(x) for x in m.group(5).split(",") if x]
            perks  = primary_runes + secondary_runes + shards

            spells: list = []
            sm = _re.search(r"SummonerSpells\(\[([\d,]+)\]", text)
            if sm:
                spells = [int(x) for x in sm.group(1).split(",") if x][:2]

            if prim and sub and len(perks) >= 6:
                return prim, perks, sub, spells
            return None
        except Exception as e:
            self._log(f"[runes] op.gg fetch failed: {e}")
            return None

    def _make_rune_room(self):
        """Delete an editable rune page if we're at the page limit, so a new
        one can be created. Prefers deleting a previous 'Auto:' page."""
        try:
            inv   = self._lcu.get("/lol-perks/v1/inventory").json()
            limit = int(inv.get("ownedPageCount", 2) or 2)
            pages = self._lcu.get("/lol-perks/v1/pages").json()
            editable = [p for p in pages if p.get("isEditable") or p.get("isDeletable")]
            if len(pages) >= limit and editable:
                target = next(
                    (p for p in editable if str(p.get("name", "")).startswith("Auto:")),
                    editable[0],
                )
                self._lcu.delete(f"/lol-perks/v1/pages/{target.get('id')}")
        except Exception:
            pass

    def _opgg_items(self, champ_name: str, opgg_pos: str):
        """op.gg Diamond+ item IDs for a champion + role. Returns a dict with
        {starter, core, boots, situational} id lists, or None."""
        try:
            body = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "lol_get_champion_analysis",
                    "arguments": {
                        "champion": champ_name, "position": opgg_pos,
                        "tier": "diamond_plus", "game_mode": "ranked",
                        "desired_output_fields": [
                            "data.starter_items.ids", "data.core_items.ids",
                            "data.boots.ids", "data.fourth_items[].ids",
                            "data.fifth_items[].ids", "data.sixth_items[].ids",
                        ],
                    },
                },
            }
            r = requests.post("https://mcp-api.op.gg/mcp", json=body, timeout=15,
                              headers={"Accept": "application/json, text/event-stream"})
            r.raise_for_status()
            rec = _parse_opgg_record(r.json()["result"]["content"][0]["text"])
        except Exception as e:
            self._log(f"[items] op.gg fetch failed: {e}")
            return None
        if not rec or "data" not in rec:
            return None
        d = rec["data"]

        def ids(x):
            return [int(i) for i in (x.get("ids") or [])] if isinstance(x, dict) else []

        # Situational: the top option from each of the 4th/5th/6th item slots.
        situational: list = []
        for k in ("fourth_items", "fifth_items", "sixth_items"):
            arr = d.get(k)
            if isinstance(arr, list):
                for opt in arr[:2]:
                    situational += ids(opt)
        # dedupe situational, keep order
        seen, situ = set(), []
        for i in situational:
            if i not in seen:
                seen.add(i); situ.append(i)

        out = {"starter": ids(d.get("starter_items")), "core": ids(d.get("core_items")),
               "boots": ids(d.get("boots")), "situational": situ}
        return out if out["core"] else None

    def _import_item_set(self, champ_id: int, position: str):
        """Build and push an op.gg Diamond+ item set for the locked-in champion
        to the League client. Worker thread. Replaces our own prior set."""
        try:
            champ_name = self._dd.opgg_name(champ_id) if self._dd else None
            opgg_pos   = OPGG_POSITION.get((position or "").lower())
            label      = self._dd.name(champ_id) if self._dd else f"#{champ_id}"
            if not (champ_name and opgg_pos):
                return
            items = self._opgg_items(champ_name, opgg_pos)
            if not items:
                self._log(f"Items: no Diamond+ item data for {label}.")
                return

            sid = self._get_summoner_id()
            if not sid:
                _dbg("[items] no summoner id"); return
            r = self._lcu.get(f"/lol-item-sets/v1/item-sets/{sid}/sets")
            if r.status_code != 200:
                self._log(f"[items] GET sets HTTP {r.status_code}")
                return
            data = r.json()
            # Drop any item set we created before so they don't pile up.
            existing = [s for s in (data.get("itemSets") or [])
                        if not str(s.get("title", "")).startswith("op.gg:")]

            def block(name, id_list):
                order, counts = [], {}
                for i in id_list:
                    i = int(i)
                    if i not in counts:
                        order.append(i); counts[i] = 0
                    counts[i] += 1
                if not order:
                    return None
                return {"type": name,
                        "items": [{"id": str(i), "count": counts[i]} for i in order]}

            blocks = [b for b in (
                block("Starting",    items["starter"]),
                block("Core",        items["core"]),
                block("Boots",       items["boots"]),
                block("Situational", items["situational"]),
            ) if b]
            if not blocks:
                return

            new_set = {
                "title": f"op.gg: {label} ({opgg_pos})",
                "type": "custom", "map": "any", "mode": "any",
                "priority": False, "sortrank": 0, "startedFrom": "blank",
                "associatedChampions": [int(champ_id)],
                "associatedMaps": [11, 12],
                "uid": str(_uuid.uuid4()),
                "blocks": blocks,
            }
            data["itemSets"] = [new_set] + existing
            pr = self._lcu.put(f"/lol-item-sets/v1/item-sets/{sid}/sets", data)
            if pr.status_code in (200, 201, 204):
                self._log(f"Imported item set for {label} ({opgg_pos}).")
            else:
                self._log(f"[items] PUT sets HTTP {pr.status_code} {pr.text[:150]}")
                _dbg(f"[items] PUT failed HTTP {pr.status_code} {pr.text[:300]}")
        except Exception as e:
            self._log(f"[items] error: {e}")
            _dbg(f"[items] exception: {e}")

    # ── Party ready check ─────────────────────────────────────────────────────
    # Separate copies of the tool coordinate through a relay server the user runs
    # (config "relayUrl"). Each member POSTs its own (hashed) ready state; every
    # tool reads the party's ready set and the leader's tool starts the queue when
    # everyone is ready. The relay only ever sees opaque hashes — no names or IDs.
    @staticmethod
    def _h(prefix: str, *parts) -> str:
        raw = prefix + ":" + ":".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _lobby_identity(self):
        """Return (group, my_key, members, is_leader, party, can_start) for the
        current lobby, or None if not in a lobby."""
        lob_r = self._lcu.get("/lol-lobby/v2/lobby")
        if lob_r.status_code != 200:
            return None
        lob = lob_r.json()
        members = lob.get("members", [])
        if not members:
            return None
        local = lob.get("localMember", {})
        my_sid = local.get("summonerId") or self._get_summoner_id()
        # Group key: prefer partyId; fall back to the sorted roster (same for all
        # members) so the relay groups the party consistently.
        party = lob.get("partyId") or "+".join(
            sorted(str(m.get("summonerId")) for m in members))
        group     = self._h("g", party)
        my_key    = self._h("m", party, my_sid)
        is_leader = bool(local.get("isLeader"))
        can_start = bool(lob.get("canStartActivity", True))
        return group, my_key, members, is_leader, party, can_start

    def _post_ready(self, url: str, group: str, my_key: str, ready: bool) -> bool:
        try:
            r = requests.post(
                f"{url}/ready",
                json={"party": group, "member": my_key, "ready": bool(ready)},
                timeout=6,
            )
            return r.status_code == 200
        except Exception as e:
            self._log(f"Ready: can't reach relay at {url} ({e})")
            return False

    def broadcast_party_ready(self, ready: bool, log_all: bool = False) -> bool:
        """POST our ready/unready state to the relay. Called by the UI."""
        url = (self._cfg().get("relayUrl") or "").strip().rstrip("/")
        if not url:
            self._log("Ready: no relay URL set — add it in Settings.")
            return False
        ident = self._lobby_identity()
        if not ident:
            return False
        group, my_key = ident[0], ident[1]
        if self._post_ready(url, group, my_key, ready):
            self._i_am_ready = bool(ready)   # remembered for the heartbeat
            return True
        return False

    def _relay_party_state(self, url: str, group: str):
        """Return (present_set, ready_set) from the relay. present_set is None if
        the relay is an older version that doesn't report presence."""
        try:
            r = requests.get(f"{url}/party", params={"id": group}, timeout=6)
            if r.status_code == 200:
                data = r.json()
                ready   = set(data.get("ready", []))
                present = set(data["present"]) if "present" in data else None
                return present, ready
        except Exception:
            pass
        return None, set()

    def _handle_party_ready(self):
        cfg = self._cfg()
        try:
            now       = time.monotonic()
            size_due  = now - getattr(self, "_last_size_poll",  0) >= 1.0
            relay_due = now - getattr(self, "_last_relay_poll", 0) >= 2.0

            ready_up = cfg.get("readyUpEnabled", True)
            url      = (cfg.get("relayUrl") or "").strip().rstrip("/")
            # Relay poll only makes sense when the feature is on and a URL exists.
            do_relay = relay_due and ready_up and bool(url)

            if not size_due and not do_relay:
                return

            # One _lobby_identity() call shared by both polls.
            ident = self._lobby_identity()

            if size_due:
                self._last_size_poll = now
                if ident:
                    self._party_size       = len(ident[2])
                    self._size_none_streak = 0
                else:
                    self._size_none_streak = getattr(self, "_size_none_streak", 0) + 1
                    if self._size_none_streak >= 3:
                        self._party_size = 0
                _dbg(f"size_poll: party_size={self._party_size} none_streak={getattr(self,'_size_none_streak',0)}" +
                     (f" members={len(ident[2])}" if ident else " (None)"))

            if not do_relay:
                return
            self._last_relay_poll = now

            if not ident:
                self._ready_count   = 0
                self._present_count = 0
                _dbg("relay_poll: no ident → pc=0 rc=0")
                return
            group, my_key, members, is_leader, party, can_start = ident

            # Heartbeat presence every cycle so the relay knows this member has
            # the tool running (independent of ready state). Members without the
            # tool never appear, so they're excluded from the ready check.
            self._post_ready(url, group, my_key, self._i_am_ready)

            present_set, ready_set = self._relay_party_state(url, group)
            member_keys = {self._h("m", party, m.get("summonerId")) for m in members}
            if present_set is None:        # old relay — fall back to whole roster
                present_set = member_keys
            present_count = len(present_set & member_keys)
            ready_count   = len(ready_set & member_keys)
            _dbg(f"relay_poll: members={len(members)} present_set={len(present_set)} "
                 f"member_keys={len(member_keys)} pc={present_count} rc={ready_count}")

            self._ready_count   = ready_count
            self._present_count = present_count

            # Sound: play once when all OTHER tool users in the party are ready.
            others_present   = present_count - 1
            others_ready_cnt = ready_count - (1 if self._i_am_ready else 0)
            all_others_ready = others_present > 0 and others_ready_cnt == others_present
            if all_others_ready and not self._others_ready_notified:
                self._others_ready_notified = True
                if cfg.get("neekoSoundEnabled", True):
                    vol = max(0, min(100, int(cfg.get("neekoSoundVolume", 80)))) / 100.0
                    self._sound_cancel.clear()
                    _delayed_play(READY_SOUND, self._sound_cancel, delay=30.0, volume=vol)
            elif not all_others_ready:
                self._others_ready_notified = False
                self._sound_cancel.set()

            # If the user drops to being the only tool user (or solo), clear
            # their ready state so it doesn't silently persist into the next
            # party (where the button won't be visible to let them undo it).
            if present_count <= 1 and self._i_am_ready:
                self._i_am_ready  = False
                self._ready_count = 0
                ready_count       = 0

            status = f"{ready_count}/{present_count}"
            if status != self._last_ready_status:
                extra = ("" if present_count == len(members)
                         else f"  ({len(members)} in party)")
                self._log(f"Party ready: {ready_count}/{present_count} tool users ready.{extra}")
                self._last_ready_status = status

            if not is_leader:
                return
            # Start once everyone who HAS the tool is ready (members without it
            # are ignored). present_count >= 1 always includes us (the leader).
            all_ready = (present_count >= 1 and ready_count == present_count)
            if all_ready and not self._queue_started and can_start:
                r = self._lcu.post("/lol-lobby/v2/lobby/matchmaking/search")
                if r.status_code in (200, 204):
                    self._queue_started = True
                    self._log("Everyone is ready — starting queue!")
                else:
                    self._log(f"[party] start queue HTTP {r.status_code} {r.text[:120]}")
            elif not all_ready and self._queue_started:
                # Someone switched to unready after the queue started — cancel it.
                r = self._lcu.delete("/lol-lobby/v2/lobby/matchmaking/search")
                if r.status_code in (200, 204):
                    self._queue_started = False
                    self._log("Someone unreadied — queue canceled.")
                else:
                    self._log(f"[party] cancel queue HTTP {r.status_code} {r.text[:120]}")
        except Exception as exc:
            self._log(f"[party] error: {exc}")

    def _commit_action(self, action: dict, champ: int, complete: bool) -> bool:
        """PATCH an action with the full reference body. complete=True locks the
        pick/ban; complete=False only hovers (pre-pick intent)."""
        aid = int(action.get("id", -1))
        body = {
            "actorCellId":  action.get("actorCellId"),
            "championId":   champ,
            "completed":    complete,
            "id":           aid,
            "isAllyAction": True,
            "type":         action.get("type", "string"),
        }
        r = self._lcu.patch(f"/lol-champ-select/v1/session/actions/{aid}", body)
        _dbg(f"[commit] type={action.get('type')} aid={aid} champ={champ} "
             f"complete={complete} -> HTTP {r.status_code}"
             + ("" if r.status_code in (200, 204) else f" body={r.text[:300]}"))
        if r.status_code not in (200, 204):
            self._log(f"[{action.get('type')}] PATCH HTTP {r.status_code} body={r.text[:150]}")
            return False
        return True

    # ── LCU helpers ───────────────────────────────────────────────────────────
    def _get_summoner_id(self) -> int:
        try:
            return int(
                self._lcu.get(
                    "/lol-summoner/v1/current-summoner"
                ).json()["summonerId"]
            )
        except Exception:
            return 0

    def _get_pickable_ids(self) -> set:
        # Use the session-scoped endpoint so Practice Tool (all champs available)
        # and normal games (ownership/free-rotation filtered) both work correctly.
        try:
            r = self._lcu.get("/lol-champ-select/v1/pickable-champion-ids")
            if r.status_code == 200:
                data = r.json()
                if data:
                    return {int(c) for c in data}
        except Exception:
            pass
        # Fallback: assume everything is pickable
        return set(range(1_000_000))

    @staticmethod
    def _best(priority: list, unavailable: set, playable: set):
        """Return highest-priority champion that is playable and not unavailable."""
        for cid in priority:
            if cid not in unavailable and cid in playable:
                return cid
        return None

    def _get_friends(self) -> set:
        """Return the set of friend summoner IDs, cached for 30 seconds."""
        if time.monotonic() - self._friends_ts < 30:
            return self._friends_cache
        try:
            r = self._lcu.get("/lol-chat/v1/friends")
            if r.status_code == 200:
                self._friends_cache = {
                    int(f["summonerId"])
                    for f in r.json()
                    if f.get("summonerId")
                }
                self._friends_ts = time.monotonic()
        except Exception:
            pass
        return self._friends_cache

    def _send_party_chat(self, message: str, delay: float = 2.5):
        """Post a message to the party lobby chat. Waits `delay` seconds first so
        the lobby has time to initialise after an invite accept. Retries up to 4×."""
        def _do():
            time.sleep(delay)
            for attempt in range(5):
                try:
                    r = self._lcu.get("/lol-chat/v1/conversations")
                    _dbg(f"party_chat attempt={attempt} status={r.status_code}")
                    if r.status_code != 200:
                        self._log(f"[chat] conversations {r.status_code} (attempt {attempt})")
                        time.sleep(1.0)
                        continue
                    convs = r.json()
                    types = [c.get("type") for c in convs]
                    _dbg(f"party_chat: {len(convs)} convs types={types}")
                    party_conv = next(
                        (c for c in convs
                         if c.get("type") in ("party", "customGame")), None)
                    if not party_conv:
                        self._log(f"[chat] no party conv yet (types={types}, attempt {attempt})")
                        time.sleep(1.0)
                        continue
                    cid = party_conv.get("id", "")
                    if not cid:
                        self._log("[chat] party conv has no id")
                        return
                    r2 = self._lcu.post(
                        f"/lol-chat/v1/conversations/{cid}/messages",
                        {"body": message, "type": "chat"},
                    )
                    _dbg(f"party_chat: post status={r2.status_code}")
                    if r2.status_code in (200, 204):
                        return   # success
                    self._log(f"[chat] post failed {r2.status_code}")
                    return
                except Exception as exc:
                    _dbg(f"party_chat: exception {exc}")
                    self._log(f"[chat] exception: {exc}")
                    return
        threading.Thread(target=_do, daemon=True).start()

    def _handle_invites(self, cfg: dict):
        """Accept pending lobby invites from friends (optionally whitelist-filtered)."""
        if not cfg.get("autoAcceptInvites"):
            return
        try:
            r = self._lcu.get("/lol-lobby/v2/received-invitations")
            _dbg(f"[invite] received-invitations status={r.status_code}")
            if r.status_code != 200:
                return
            all_invs = r.json()
            _dbg(f"[invite] raw invites: {all_invs}")
            pending = [
                inv for inv in all_invs
                if inv.get("state") == "Pending"
                and inv.get("invitationId") not in self._accepted_invites
            ]
            _dbg(f"[invite] pending count={len(pending)}")
        except Exception as exc:
            _dbg(f"[invite] exception fetching invites: {exc}")
            return
        if not pending:
            return

        friends   = self._get_friends()
        _dbg(f"[invite] friends count={len(friends)} ids={list(friends)[:10]}")
        whitelist = {n.strip().lower()
                     for n in cfg.get("inviteWhitelist", []) if n.strip()}

        for inv in pending:
            inv_id      = inv.get("invitationId", "")
            sender_id   = int(inv.get("fromSummonerId", 0) or 0)
            sender_name = inv.get("fromSummonerName", str(sender_id))
            _dbg(f"[invite] checking inv={inv_id} sender_id={sender_id} sender_name={sender_name!r}")

            if sender_id not in friends:
                _dbg(f"[invite] sender_id={sender_id} not in friends — skipping")
                continue
            if whitelist and sender_name.lower() not in whitelist:
                _dbg(f"[invite] {sender_name!r} not in whitelist {whitelist} — skipping")
                continue

            first_seen = self._invite_seen.get(inv_id)
            if first_seen is None:
                self._invite_seen[inv_id] = time.monotonic()
                _dbg(f"[invite] first_seen — waiting one tick")
                continue
            if time.monotonic() - first_seen < 0.5:
                continue

            r2 = self._lcu.post(
                f"/lol-lobby/v2/received-invitations/{inv_id}/accept")
            _dbg(f"[invite] accept status={r2.status_code} body={r2.text[:200]}")
            if r2.status_code in (200, 204):
                self._log(f"Auto-accepted invite from {sender_name}")
                self._accepted_invites.add(inv_id)
                self._invite_seen.pop(inv_id, None)
                self._send_party_chat("Party invite auto-accepted")
            else:
                self._log(f"[invite] Accept failed for {sender_name}: HTTP {r2.status_code}")


# ── Theme colors  (League of Legends "hextech" palette) ────────────────────────
DARK   = "#0a141f"   # main content background (deep navy)
DARKER = "#050a12"   # sidebar / header background
PANEL  = "#0f1c2b"   # inputs, listboxes, secondary buttons
GOLD   = "#c8aa6e"   # hextech gold accent
GREEN  = "#0fb894"   # on / connected (hextech teal-green)
RED    = "#c0392b"   # off / errors
TEXT   = "#cdd6e0"   # primary text
WHITE  = "#f0e6d2"   # LoL parchment off-white

# Redesign palette
SIDEBAR     = DARKER
HEADER      = DARKER
CARD        = "#0d1a28"   # card / tile surface
CARD_BORDER = "#785a28"   # hextech gold hairline outline
EDGE_GOLD   = "#5c4a24"   # dim gold edge / rules
BRIGHT_GOLD = "#f0d5a0"   # highlight ticks
TRACK_OFF   = "#32414f"   # toggle track when off
TEXT_BRIGHT = "#f0e6d2"   # card titles, stat values (parchment)
MUTED       = "#7a8ba0"   # subtitles (muted blue-grey)
FAINT       = "#586a7c"   # hints / stat labels
NAV_ACTIVE  = "#0f2133"   # highlighted sidebar row
TEAL        = "#0ac8b9"   # hextech teal highlight
FIELD_BG    = "#050a12"   # recessed input fields (clearly visible on cards)
BTN_BG      = "#1e3350"   # secondary buttons (visible on DARK and CARD)
BTN_HOV     = "#2a456a"   # secondary button hover
CHIP_BG     = "#16283f"   # squishy champion tag fill
CHIP_BADGE  = "#0e1c2e"   # rank-number badge fill inside a chip

# Virtual-key codes for F-keys and navigation keys (overlay hotkey system)
_OVERLAY_VK = {
    "F1":  0x70, "F2":  0x71, "F3":  0x72, "F4":  0x73,
    "F5":  0x74, "F6":  0x75, "F7":  0x76, "F8":  0x77,
    "F9":  0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "Home": 0x24, "End": 0x23, "Insert": 0x2D, "Delete": 0x2E,
    "Prior": 0x21, "Next": 0x22,   # PgUp / PgDn (tkinter keysym names)
}
_OVERLAY_MOD_VK      = {"Ctrl": 0x11, "Shift": 0x10, "Alt": 0x12}
_OVERLAY_KEY_DISPLAY = {"Prior": "PgUp", "Next": "PgDn"}
# Tkinter keysyms that are pure modifier keys — skip these during capture.
_OVERLAY_IGNORE_KEYS = {
    "Control_L", "Control_R", "Shift_L", "Shift_R",
    "Alt_L", "Alt_R", "Meta_L", "Meta_R",
    "Super_L", "Super_R", "Caps_Lock", "Num_Lock", "Scroll_Lock",
}

def _overlay_key_vk(key: str) -> int:
    """Resolve a bare key name to a Windows VK code (0 if unknown)."""
    vk = _OVERLAY_VK.get(key)
    if vk:
        return vk
    if len(key) == 1:
        k = key.upper()
        if "A" <= k <= "Z":
            return ord(k)          # 0x41–0x5A
        if "0" <= k <= "9":
            return ord(k)          # 0x30–0x39
    return 0

def _overlay_parse_combo(combo: str):
    """'Ctrl+K' → ([mod_vk, …], key_vk). Returns ([], 0) if unresolvable."""
    parts = combo.strip().split("+")
    key   = parts[-1]
    mods  = [_OVERLAY_MOD_VK[p] for p in parts[:-1] if p in _OVERLAY_MOD_VK]
    return (mods, _overlay_key_vk(key))

def _overlay_combo_label(combo: str) -> str:
    """Human-readable form of a stored combo string, e.g. 'Ctrl+Prior' → 'Ctrl+PgUp'."""
    if not combo:
        return "None"
    parts = combo.split("+")
    parts[-1] = _OVERLAY_KEY_DISPLAY.get(parts[-1], parts[-1])
    return "+".join(parts)

# ── Typography  (one source of truth for every widget) ─────────────────────────
FONT_SECTION = ("Segoe UI", 12, "bold")   # ornamented section headers
FONT_TITLE   = ("Segoe UI", 13)           # card / automation titles
FONT_LABEL   = ("Segoe UI", 10)           # standard labels
FONT_SMALL   = ("Segoe UI", 9)            # secondary text / inputs
FONT_HINT    = ("Segoe UI", 8)            # sub-hints
FONT_BTN     = ("Segoe UI", 9, "bold")    # buttons
FONT_MONO    = ("Consolas", 10)           # log console
FONT_STATUS  = ("Segoe UI", 10)           # header status readouts (uniform)

# Shared surfaces
TIP_BG  = "#12263a"   # tooltip background (dark navy)

BTN_STYLE = dict(relief="flat", cursor="hand2", font=FONT_BTN,
                 activeforeground=WHITE)


def _shade(hex_color: str, factor: float) -> str:
    """Lighten (factor>1) or darken (factor<1) a #rrggbb colour."""
    try:
        r = int(hex_color[1:3], 16); g = int(hex_color[3:5], 16); b = int(hex_color[5:7], 16)
        r = max(0, min(255, int(r * factor)))
        g = max(0, min(255, int(g * factor)))
        b = max(0, min(255, int(b * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


def _blend(c1: str, c2: str, t: float) -> str:
    """Linearly interpolate between two #rrggbb colours (t: 0→c1, 1→c2)."""
    try:
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        r = [int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3)]
        return f"#{r[0]:02x}{r[1]:02x}{r[2]:02x}"
    except Exception:
        return c2 if t >= 0.5 else c1


# Tk's Canvas has no anti-aliasing for arcs / ovals / diagonal lines — hand
# drawn rounded shapes look visibly jagged ("grainy") at widget size. These
# helpers render each shape once via PIL at 4x scale and downsample with
# LANCZOS (true anti-aliasing), caching by (size, colours) so repeated draws
# are free. The module-level caches also keep every PhotoImage referenced,
# which Tk requires — an un-referenced PhotoImage is silently garbage
# collected and vanishes from the canvas.
_SHAPE_CACHE = {}
_SS = 4   # supersampling factor

def _render_pill_image(w: int, h: int, fill: str, border: str, border_width=1.6):
    w = max(h, int(round(w)))
    key = ("pill", w, h, fill, border, round(border_width * 10))
    if key in _SHAPE_CACHE:
        return _SHAPE_CACHE[key]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageTk
        W, H = w * _SS, h * _SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d  = ImageDraw.Draw(im)
        r  = H // 2
        d.rounded_rectangle([0, 0, W - 1, H - 1], radius=r, fill=fill)
        bw = max(1, int(round(border_width * _SS)))
        if border != fill:
            d.rounded_rectangle([bw / 2, bw / 2, W - 1 - bw / 2, H - 1 - bw / 2],
                                radius=max(1, r - bw // 2), outline=border, width=bw)
        photo = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
    except Exception:
        photo = None
    _SHAPE_CACHE[key] = photo
    return photo

def _render_circle_image(diameter: int, fill=None, outline=None, outline_width=1.6):
    """Anti-aliased circle: optional filled interior and/or outline ring
    (transparent elsewhere). Returns a PhotoImage, or None if PIL missing."""
    key = ("circle", diameter, fill, outline, round(outline_width * 10))
    if key in _SHAPE_CACHE:
        return _SHAPE_CACHE[key]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageTk
        D = diameter * _SS
        im = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d  = ImageDraw.Draw(im)
        inset = _SS  # keep the stroke off the very edge so it isn't clipped
        if fill is not None:
            d.ellipse([inset, inset, D - 1 - inset, D - 1 - inset], fill=fill)
        if outline is not None:
            bw = max(1, int(round(outline_width * _SS)))
            d.ellipse([inset, inset, D - 1 - inset, D - 1 - inset],
                      outline=outline, width=bw)
        photo = ImageTk.PhotoImage(im.resize((diameter, diameter), Image.LANCZOS))
    except Exception:
        photo = None
    _SHAPE_CACHE[key] = photo
    return photo

def _render_diamond_image(size: int, fill: str, outline: str, outline_width=1.4):
    """Anti-aliased diamond (rotated square) for slider handles / accents."""
    key = ("diamond", size, fill, outline, round(outline_width * 10))
    if key in _SHAPE_CACHE:
        return _SHAPE_CACHE[key]
    photo = None
    try:
        from PIL import Image, ImageDraw, ImageTk
        S = size * _SS
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d  = ImageDraw.Draw(im)
        m  = _SS
        pts = [(S / 2, m), (S - m, S / 2), (S / 2, S - m), (m, S / 2)]
        d.polygon(pts, fill=fill, outline=outline,
                  width=max(1, int(round(outline_width * _SS))))
        photo = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
    except Exception:
        photo = None
    _SHAPE_CACHE[key] = photo
    return photo


class HexCard(tk.Canvas):
    """A League-style angular panel: chamfered corners, a gold hairline border,
    and bright gold ticks on two opposite corners. Content goes in `.body`.

    autofit=True makes the card grow to fit its content's height (for panels
    with variable content, e.g. settings sections)."""

    def __init__(self, parent, fill=CARD, border=CARD_BORDER, chamfer=13,
                 width=300, height=96, bg=DARK, autofit=False):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self._fill    = fill
        self._border  = border
        self._ch      = chamfer
        self._autofit = autofit
        self.body     = tk.Frame(self, bg=fill)
        self._win     = self.create_window(3, 3, window=self.body, anchor="nw")
        self.bind("<Configure>", self._redraw)
        if autofit:
            self.body.bind("<Configure>", self._on_body)

    def _on_body(self, _e):
        need = self.body.winfo_reqheight() + 6
        if need != int(self["height"]):
            self.config(height=need)

    def _redraw(self, e):
        self.delete("shape")
        w, h, c = e.width, e.height, self._ch
        pts = [c, 0, w - c, 0, w - 1, c, w - 1, h - c,
               w - c, h - 1, c, h - 1, 0, h - c, 0, c]
        self.create_polygon(pts, fill=self._fill, outline=self._border,
                            width=1, tags="shape")
        # Bright hextech ticks on the top-left and bottom-right chamfers.
        self.create_line(0, c, c, 0, fill=GOLD, width=2, tags="shape")
        self.create_line(w - c, h - 1, w - 1, h - c, fill=GOLD, width=2,
                         tags="shape")
        self.tag_lower("shape")
        # Keep the content frame inset just inside the border.
        self.coords(self._win, 3, 3)
        if self._autofit:
            self.itemconfig(self._win, width=w - 6)          # height = content
        else:
            self.itemconfig(self._win, width=w - 6, height=h - 6)


class HexButton(tk.Canvas):
    """A League-style angular action button with chamfered corners, a gold
    border and corner ticks. Reconfigure its look with set_look()."""

    def __init__(self, parent, command, width=230, height=46, chamfer=9, bg=DARK):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self._command = command
        self._ch      = chamfer
        self._text    = "START"
        self._fill    = DARK
        self._border  = GOLD
        self._fg      = GOLD
        self._enabled = True
        self._hover   = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", lambda _e: self._draw())
        self.config(cursor="hand2")

    def set_look(self, text, fill, border, fg, enabled):
        self._text, self._fill, self._border = text, fill, border
        self._fg, self._enabled = fg, enabled
        self.config(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _click(self, _e):
        if self._enabled and self._command:
            self._command()

    def _enter(self, _e):
        self._hover = True;  self._draw()

    def _leave(self, _e):
        self._hover = False; self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1:
            w = int(self["width"])
        if h <= 1:
            h = int(self["height"])
        c = self._ch
        fill = self._fill
        if self._enabled and self._hover:
            fill = _shade(fill, 1.18)
        pts = [c, 0, w - c, 0, w - 1, c, w - 1, h - c,
               w - c, h - 1, c, h - 1, 0, h - c, 0, c]
        self.create_polygon(pts, fill=fill, outline=self._border, width=1)
        tick = DARK if fill.lower() == GOLD.lower() else GOLD
        self.create_line(0, c, c, 0, fill=tick, width=2)
        self.create_line(w - c, h - 1, w - 1, h - c, fill=tick, width=2)
        self.create_text(w / 2, h / 2, text=self._text, fill=self._fg,
                         font=("Segoe UI", 12, "bold"))


class HexSlider(tk.Canvas):
    """A horizontal slider with an always-visible gold diamond handle and a
    gold-filled track. Click or drag to set a 0–100 value; calls command(value)."""

    def __init__(self, parent, value=80, command=None, width=170, height=24,
                 bg=CARD):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._val     = max(0, min(100, int(value)))
        self._command = command
        self._pad     = 10
        self.bind("<Button-1>", self._set_from_x)
        self.bind("<B1-Motion>", self._set_from_x)
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def _pxw(self):
        w = self.winfo_width()
        return w if w > 1 else int(self["width"])

    def _draw(self):
        self.delete("all")
        w = self._pxw()
        h = int(self["height"])
        pad, cy = self._pad, h // 2
        x0, x1 = pad, w - pad
        hx = x0 + (x1 - x0) * self._val / 100.0
        # groove, then gold fill up to the handle
        self.create_line(x0, cy, x1, cy, fill=EDGE_GOLD, width=3,
                         capstyle="round")
        self.create_line(x0, cy, hx, cy, fill=GOLD, width=3, capstyle="round")
        # always-visible gold diamond handle (anti-aliased)
        hs  = 16
        him = _render_diamond_image(hs, GOLD, BRIGHT_GOLD, outline_width=1.4)
        if him is not None:
            self._handle_img = him
            self.create_image(hx, cy, image=him)
        else:
            r = 7
            self.create_polygon(hx, cy - r, hx + r, cy, hx, cy + r, hx - r, cy,
                                fill=GOLD, outline=BRIGHT_GOLD)

    def _set_from_x(self, e):
        w = self._pxw()
        pad = self._pad
        frac = (e.x - pad) / max(1, (w - 2 * pad))
        val = int(round(max(0.0, min(1.0, frac)) * 100))
        self._val = val
        self._draw()
        if self._command:
            self._command(val)

    def set(self, val):
        self._val = max(0, min(100, int(val)))
        self._draw()

    def get(self):
        return self._val


class ToggleSwitch(tk.Canvas):
    """iOS-style pill toggle. Green track when on, grey when off.

    Clicking flips the state, redraws, and calls command(new_state).
    Use .set(value) to change state programmatically (silent by default)."""

    W, H = 48, 26

    def __init__(self, parent, initial=False, command=None, bg=CARD):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._on      = bool(initial)
        self._pos     = 1.0 if self._on else 0.0   # animation progress 0→1
        self._anim    = None
        self._command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        p = self._pos
        # Track colour fades grey→green as the knob slides across.
        track = _blend(TRACK_OFF, GREEN, p)
        timg  = _render_pill_image(self.W - 4, self.H - 4, track, track)
        if timg is not None:
            self._track_img = timg
            self.create_image(2, 2, anchor="nw", image=timg)
        else:
            r = (self.H - 4) / 2
            self.create_oval(2, 2, 2 + 2 * r, self.H - 2, fill=track, outline=track)
            self.create_oval(self.W - 2 - 2 * r, 2, self.W - 2, self.H - 2,
                             fill=track, outline=track)
            self.create_rectangle(2 + r, 2, self.W - 2 - r, self.H - 2,
                                  fill=track, outline=track)
        d, pad = self.H - 8, 4                  # knob diameter, edge padding
        off_x  = pad
        on_x   = self.W - pad - d
        kx     = off_x + (on_x - off_x) * p     # interpolated knob position
        knob   = _blend("#c9cdd2", WHITE, p)
        kimg   = _render_circle_image(d, fill=knob)
        if kimg is not None:
            self._knob_img = kimg
            self.create_image(kx + d / 2, self.H / 2, image=kimg)
        else:
            self.create_oval(kx, pad, kx + d, pad + d, fill=knob, outline=knob)

    def _animate_to(self, target):
        """Step the knob toward target (0 or 1) a few frames for a slide."""
        if self._anim is not None:
            self.after_cancel(self._anim)
            self._anim = None

        def _step():
            diff = target - self._pos
            if abs(diff) <= 0.16:
                self._pos = target
                self._draw()
                self._anim = None
                return
            self._pos += 0.22 if diff > 0 else -0.22
            self._draw()
            self._anim = self.after(12, _step)

        _step()

    def _click(self, _evt):
        self._on = not self._on
        self._animate_to(1.0 if self._on else 0.0)
        if self._command:
            self._command(self._on)

    def set(self, value, silent=True):
        value = bool(value)
        if value == self._on:
            return
        self._on = value
        self._animate_to(1.0 if value else 0.0)
        if not silent and self._command:
            self._command(self._on)

    def get(self):
        return self._on


class ChampionChip(tk.Canvas):
    """A single squishy, pill-shaped champion tag: a circular portrait
    avatar (with a small rank badge tucked in its corner) + name + a small
    "×" remove hotspot. Purely a rendering widget — drag/remove interaction
    is owned and driven entirely by the parent ChampionList."""

    HEIGHT = 34
    _REMOVE_W = 26   # width of the clickable "×" hit zone, from the right edge

    def __init__(self, parent, key, label, accent, bg=CARD, icon=None, stats=None):
        super().__init__(parent, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0, cursor="fleur")
        self.key           = key
        self._label        = label
        self._accent       = accent
        self._icon         = icon   # ImageTk.PhotoImage (circular) or None
        self._stats        = stats  # (games, wins) | "nodata" | None (not fetched)
        self._rank         = 0
        self._dragging     = False
        self._hover_remove = False
        self._ghost        = False
        self._cur_y        = 0.0    # animated Y position (owned by ChampionList)
        self._target_y     = 0.0
        self.bind("<Configure>", lambda _e: self._draw())
        self._draw()

    def set_rank(self, n: int):
        self._rank = n
        self._draw()

    def set_icon(self, icon):
        self._icon = icon
        self._draw()

    def set_dragging(self, on: bool):
        if on == self._dragging:
            return
        self._dragging = on
        self._draw()

    def set_hover_remove(self, on: bool):
        if on == self._hover_remove:
            return
        self._hover_remove = on
        self._draw()

    def set_ghost(self, on: bool):
        self._ghost = on
        self._draw()

    def hit_remove(self, x: int) -> bool:
        w = self.winfo_width() or 260
        return x >= w - self._REMOVE_W

    def _pxw(self):
        w = self.winfo_width()
        return w if w > 1 else 260

    def _pill(self, fill, border, width=1):
        """Draw the pill background+border. Prefers a PIL-supersampled,
        anti-aliased render (Tk's native arcs/lines have no anti-aliasing
        and look jagged/"grainy" at this size); falls back to hand-drawn
        vector shapes only if PIL is unavailable."""
        w_px, h = self._pxw(), self.HEIGHT
        img = _render_pill_image(w_px, h, fill, border, border_width=width)
        if img is not None:
            self._pill_img = img   # keep a reference — Tk GCs unreferenced PhotoImages
            self.create_image(0, 0, anchor="nw", image=img)
        else:
            self._pill_vector(fill, border, width=max(1, int(round(width))))

    def _pill_vector(self, fill, border, width=1):
        w, h = self._pxw(), self.HEIGHT
        r = h / 2
        self.create_arc(0, 0, h, h, start=90, extent=180, fill=fill, outline=fill)
        self.create_arc(w - h, 0, w, h, start=-90, extent=180, fill=fill, outline=fill)
        self.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill)
        self.create_arc(1, 1, h - 1, h - 1, start=90, extent=180,
                        style="arc", outline=border, width=width)
        self.create_arc(w - h + 1, 1, w - 1, h - 1, start=-90, extent=180,
                        style="arc", outline=border, width=width)
        self.create_line(r, 1, w - r, 1, fill=border, width=width)
        self.create_line(r, h - 1, w - r, h - 1, fill=border, width=width)

    def _draw(self):
        self.delete("all")
        w, h = self._pxw(), self.HEIGHT

        if self._ghost:
            # Faint dashed "drop here" placeholder — no content, just a hint
            # that a chip will land in this slot.
            fill = _blend(CARD, CHIP_BG, 0.4)
            self._pill(fill, self._accent, width=1)
            self.create_text(w / 2, h / 2, text="⌄ drop here ⌄", fill=FAINT,
                             font=("Segoe UI", 8))
            return

        fill   = _shade(CHIP_BG, 1.35) if self._dragging else CHIP_BG
        border = BRIGHT_GOLD if self._dragging else self._accent
        self._pill(fill, border)

        # Circular portrait — full chip diameter, forming the pill's left cap.
        d  = self.HEIGHT
        cx = cy = d / 2
        if self._icon is not None:
            self.create_image(cx, cy, image=self._icon)
        else:
            av = _render_circle_image(d, fill=CHIP_BADGE)
            if av is not None:
                self._avatar_img = av
                self.create_image(cx, cy, image=av)
        ring = _render_circle_image(d, outline=border, outline_width=2)
        if ring is not None:
            self._ring_img = ring
            self.create_image(cx, cy, image=ring)
        elif self._icon is None:
            self.create_oval(1, 1, d - 1, d - 1, fill=CHIP_BADGE, outline=border)

        # Rank badge tucked in the portrait's bottom-right corner
        if self._rank:
            bd = 16
            bx, by = d - 7, h - 7
            badge = _render_circle_image(bd, fill=DARKER, outline=border,
                                         outline_width=1.4)
            if badge is not None:
                self._badge_img = badge
                self.create_image(bx, by, image=badge)
            else:
                self.create_oval(bx - bd / 2, by - bd / 2, bx + bd / 2,
                                 by + bd / 2, fill=DARKER, outline=border)
            self.create_text(bx, by, text=str(self._rank), fill=TEXT_BRIGHT,
                             font=("Segoe UI", 8, "bold"))

        # ── op.gg history for this champion, in the chip's right third ────────
        # (games played + win-rate over the past 6 months, all queues, for the
        #  summoner signed into the League client). Only drawn when the champion
        #  actually has recorded games — no games means a blank section, no text.
        if isinstance(self._stats, tuple):
            games, wins = self._stats
            sx = w - self._REMOVE_W - 6           # right edge of the stats block
            stats_left = w * 0.66                 # right third boundary
            # faint divider between name and stats
            self.create_line(stats_left, 7, stats_left, h - 7,
                             fill=_blend(CARD, border, 0.5))
            wr = round(100 * wins / games) if games else 0
            wr_c = GREEN if wr >= 50 else RED
            self.create_text(sx, h / 2 - 6, text=f"{wr}% WR", fill=wr_c,
                             font=("Segoe UI", 9, "bold"), anchor="e")
            self.create_text(sx, h / 2 + 7,
                             text=f"{games}g · {wins}w", fill=MUTED,
                             font=("Segoe UI", 8), anchor="e")

        # Champion name
        self.create_text(d + 8, h / 2, text=self._label, fill=TEXT_BRIGHT,
                         font=FONT_LABEL, anchor="w")

        # Remove "×"
        rx = w - self._REMOVE_W / 2 - 2
        self.create_text(rx, h / 2, text="✕",
                         fill=RED if self._hover_remove else FAINT,
                         font=("Segoe UI", 10, "bold"))


class ChampionList(tk.Frame):
    """A vertical stack of squishy ChampionChip tags with physically
    floating click-and-drag reordering: the dragged chip follows the cursor
    directly while its siblings smoothly slide out of the way to show where
    it will land. Supports dragging a chip out to a linked sibling list
    (see bind_cross) for cross-list moves, e.g. Pick Priority → Ban Priority.

    set_items() takes [(key, label), ...] in priority order (top = highest).
    on_reorder(new_key_order) fires once, on drop, only if the order changed.
    on_remove(key) fires when a chip's "×" is clicked."""

    GAP    = 6
    _TWEEN = 0.35   # fraction of remaining distance closed per animation tick

    def __init__(self, parent, accent, bg=CARD, get_icon=None, get_stats=None,
                 on_reorder=None, on_remove=None, autosize=False):
        super().__init__(parent, bg=bg)
        # Children use place(), so they never drive our own size. By default the
        # caller packs us fill="both", expand=True and we lay chips out within
        # that. With autosize=True we instead set our OWN height to fit the
        # chips (for use inside an autofit card, e.g. the permaban panel).
        self._accent     = accent
        self._bg         = bg
        self._get_icon   = get_icon
        self._get_stats  = get_stats
        self._on_reorder = on_reorder
        self._on_remove  = on_remove
        self._autosize   = autosize
        self._on_cross_move    = None
        self._on_cross_release = None
        self._chips: list[ChampionChip] = []
        self._drag          = None   # {"chip","moved","external","grab_dy","order"}
        self._press_remove  = None   # chip pending a remove-click
        self._ghost          = None
        self._ghost_index    = None
        self._ticker          = None

    # ── Public API used by RolePanel ────────────────────────────────────────
    def bind_cross(self, on_move, on_release):
        """on_move(key, label, x_root, y_root) — fires continuously while a
        chip is being dragged outside this list's own bounds.
        on_release(key, label, x_root, y_root) — fires once on drop."""
        self._on_cross_move    = on_move
        self._on_cross_release = on_release

    def contains_point(self, x_root, y_root) -> bool:
        x0, y0 = self.winfo_rootx(), self.winfo_rooty()
        return (x0 <= x_root <= x0 + self.winfo_width() and
                y0 <= y_root <= y0 + self.winfo_height())

    def local_index_for_y(self, y_root) -> int:
        local_y = y_root - self.winfo_rooty()
        for i in range(len(self._chips)):
            if local_y < i * (ChampionChip.HEIGHT + self.GAP) + ChampionChip.HEIGHT / 2:
                return i
        return len(self._chips)

    def show_incoming_ghost(self, y_root):
        idx = self.local_index_for_y(y_root)
        if self._ghost is not None and self._ghost_index == idx:
            return
        self._ghost_index = idx
        if self._ghost is None:
            self._ghost = ChampionChip(self, "__ghost__", "", self._accent, bg=self._bg)
            self._ghost.set_ghost(True)
        self._relayout()

    def hide_incoming_ghost(self):
        if self._ghost is not None:
            g = self._ghost
            self._ghost = None
            self._ghost_index = None
            g.destroy()
            self._relayout()

    # ── Populate ─────────────────────────────────────────────────────────────
    def set_items(self, items):
        self._stop_ticker()
        self._drag = None
        if self._ghost is not None:
            self._ghost.destroy()
            self._ghost = None
        for c in self._chips:
            c.destroy()
        self._chips = []
        for key, label in items:
            icon  = self._get_icon(key) if self._get_icon else None
            stats = self._get_stats(key) if self._get_stats else None
            chip = ChampionChip(self, key, label, self._accent, bg=self._bg,
                                icon=icon, stats=stats)
            chip.bind("<ButtonPress-1>",   lambda e, c=chip: self._on_press(c, e))
            chip.bind("<B1-Motion>",       lambda e, c=chip: self._on_drag(c, e))
            chip.bind("<ButtonRelease-1>", lambda e, c=chip: self._on_release(c, e))
            chip.bind("<Motion>",          lambda e, c=chip: self._on_hover(c, e))
            self._chips.append(chip)
        self._update_ranks()
        self._relayout(animate=False)
        self.update_idletasks()

    def _update_ranks(self):
        for i, chip in enumerate(self._chips):
            chip.set_rank(i + 1)

    # ── Layout / animation ──────────────────────────────────────────────────
    def _total_height(self, n=None):
        n = len(self._chips) if n is None else n
        return max(ChampionChip.HEIGHT,
                   n * (ChampionChip.HEIGHT + self.GAP) - self.GAP) if n else ChampionChip.HEIGHT

    def _relayout(self, animate=True, skip=None):
        seq = list(self._chips)
        if self._ghost is not None:
            seq.insert(min(self._ghost_index, len(seq)), self._ghost)
        for i, c in enumerate(seq):
            y = i * (ChampionChip.HEIGHT + self.GAP)
            c._target_y = y
            if c is skip:
                continue
            if self._drag and self._drag["chip"] is c:
                continue   # currently floating under direct cursor control
            if c is self._ghost or not animate:
                c._cur_y = y
                c.place(x=0, y=y, relwidth=1.0, height=ChampionChip.HEIGHT)
        if self._autosize:
            # Drive our own height so an autofit parent card sizes to the chips.
            n = len(seq)
            self.config(height=(n * (ChampionChip.HEIGHT + self.GAP)
                                if n else 0))
        if animate:
            self._start_ticker()

    def _start_ticker(self):
        if self._ticker is None:
            self._tick()

    def _stop_ticker(self):
        if self._ticker is not None:
            self.after_cancel(self._ticker)
            self._ticker = None

    def _tick(self):
        moving = False
        for c in self._chips:
            if self._drag and self._drag["chip"] is c:
                continue
            if abs(c._target_y - c._cur_y) < 0.5:
                if c._cur_y != c._target_y:
                    c._cur_y = c._target_y
                    c.place(x=0, y=int(c._cur_y), relwidth=1.0, height=ChampionChip.HEIGHT)
                continue
            moving = True
            c._cur_y += (c._target_y - c._cur_y) * self._TWEEN
            c.place(x=0, y=int(c._cur_y), relwidth=1.0, height=ChampionChip.HEIGHT)
        self._ticker = self.after(16, self._tick) if (moving or self._drag) else None

    def _reorder_from_float(self, chip, local_y):
        """While `chip` floats freely at `local_y` (its top-left), recompute
        self._chips order so the other chips animate to make room around it.

        Deliberately rank-based rather than compared against siblings'
        current _target_y: those can be transiently stale (e.g. still
        reflecting last frame's gap-inclusive layout) on the very first
        motion event of a drag, which produced off-by-one boundary glitches
        on large/fast jumps. Assuming `others` re-pack back-to-back from
        slot 0 and asking "which of those slots does the float's top align
        closest to" is self-consistent regardless of prior layout state."""
        others = [c for c in self._chips if c is not chip]
        slot = ChampionChip.HEIGHT + self.GAP
        insert_at = max(0, min(len(others), round(local_y / slot)))
        new_order = others[:insert_at] + [chip] + others[insert_at:]
        if new_order != self._chips:
            self._chips = new_order
            self._update_ranks()
            self._relayout(skip=chip)

    # ── Interaction ──────────────────────────────────────────────────────────
    def _on_hover(self, chip, event):
        if self._drag is None:
            chip.set_hover_remove(chip.hit_remove(event.x))

    def _on_press(self, chip, event):
        if chip.hit_remove(event.x):
            self._press_remove = chip
            return
        self._drag = {"chip": chip, "moved": False, "external": False,
                      "grab_dy": event.y, "order": [c.key for c in self._chips]}
        chip.set_dragging(True)
        # Canvas shadows BOTH .lift() and .tkraise() with the canvas-item
        # tag_raise() — call Misc's version directly to actually raise the
        # widget itself in its parent's stacking order.
        tk.Misc.tkraise(chip)
        self._start_ticker()

    def _on_drag(self, chip, event):
        d = self._drag
        if not d or d["chip"] is not chip:
            return
        d["moved"] = True
        chip.set_hover_remove(False)
        xr, yr = event.x_root, event.y_root

        if self.contains_point(xr, yr):
            if d["external"]:
                d["external"] = False
                if self._on_cross_move:
                    self._on_cross_move(chip.key, chip._label, xr, yr)  # tells RolePanel to hide flying/ghost
            local_y = yr - self.winfo_rooty() - d["grab_dy"]
            local_y = max(0, min(local_y, self._total_height() - ChampionChip.HEIGHT))
            chip._cur_y = chip._target_y = local_y
            chip.place(x=0, y=int(local_y), relwidth=1.0, height=ChampionChip.HEIGHT)
            self._reorder_from_float(chip, local_y)
        else:
            if not d["external"]:
                d["external"] = True
                chip.place_forget()
                self._relayout(skip=chip)
            if self._on_cross_move:
                self._on_cross_move(chip.key, chip._label, xr, yr)

    def _on_release(self, chip, event):
        if self._press_remove is chip:
            if chip.hit_remove(event.x) and self._on_remove:
                self._on_remove(chip.key)
            self._press_remove = None
            return

        d = self._drag
        if not d or d["chip"] is not chip:
            return
        was_external = d["external"]
        xr, yr = event.x_root, event.y_root
        self._drag = None
        self._stop_ticker()

        if was_external:
            if self._on_cross_release:
                self._on_cross_release(chip.key, chip._label, xr, yr)
            # A successful cross-list drop rebuilds this list via App's
            # refresh_list (the chip no longer exists) — a cancelled one
            # leaves our model untouched, so just restore this chip's view.
            try:
                if self.winfo_exists():
                    self._relayout()
            except tk.TclError:
                pass
            return

        chip.set_dragging(False)
        new_order = [c.key for c in self._chips]
        self._relayout()
        if new_order != d["order"] and self._on_reorder:
            self._on_reorder(new_order)
        chip.set_hover_remove(chip.hit_remove(event.x))


# ── GUI ───────────────────────────────────────────────────────────────────────
class RolePanel:
    """The pick + ban list editor for a single role."""

    def __init__(self, parent: tk.Widget, role: str, app: "App"):
        self._role = role
        self._app  = app
        self._widgets: dict = {}
        self._flying = None   # shared "chip crossing between lists" overlay

        self._wrap = wrap = tk.Frame(parent, bg=DARK)
        wrap.pack(fill="both", expand=True, padx=24, pady=(6, 16))
        wrap.columnconfigure(0, weight=1, uniform="rp")
        wrap.columnconfigure(1, weight=1, uniform="rp")
        wrap.rowconfigure(0, weight=1)

        picks = self._build_side(wrap, 0, "picks", "Pick Priority", TEAL)
        bans  = self._build_side(wrap, 1, "bans",  "Ban Priority",  RED)
        self._link_cross(picks, bans, "picks", "bans", RED)
        self._link_cross(bans, picks, "bans", "picks", TEAL)

    # ── Cross-list drag (Pick Priority ↔ Ban Priority) ─────────────────────────
    def _link_cross(self, src: "ChampionList", dst: "ChampionList",
                    src_key: str, dst_key: str, dst_accent: str):
        def on_move(cid, label, xr, yr):
            if dst.contains_point(xr, yr):
                self._show_flying(label, dst_accent, xr, yr)
                dst.show_incoming_ghost(yr)
            else:
                self._hide_flying()
                dst.hide_incoming_ghost()

        def on_release(cid, label, xr, yr):
            self._hide_flying()
            dst.hide_incoming_ghost()
            if dst.contains_point(xr, yr):
                idx = dst.local_index_for_y(yr)
                self._app.move_between_lists(self._role, src_key, dst_key, cid, idx)

        src.bind_cross(on_move, on_release)

    def _show_flying(self, label, accent, x_root, y_root):
        if self._flying is None:
            self._flying = ChampionChip(self._wrap, "__flying__", label, accent, bg=DARK)
            self._flying.set_dragging(True)
        else:
            self._flying._label  = label
            self._flying._accent = accent
        lx = x_root - self._wrap.winfo_rootx() - 100
        ly = y_root - self._wrap.winfo_rooty() - ChampionChip.HEIGHT // 2
        self._flying.place(x=lx, y=ly, width=220, height=ChampionChip.HEIGHT)
        self._flying._draw()
        tk.Misc.tkraise(self._flying)   # Canvas shadows tkraise() with tag_raise

    def _hide_flying(self):
        if self._flying is not None:
            self._flying.place_forget()

    def _build_side(self, parent, col: int, list_key: str,
                    title: str, accent: str):
        role  = self._role
        app   = self._app

        card = HexCard(parent, fill=CARD, border=CARD_BORDER)
        card.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0
                  else (6, 0))
        container = card.body

        hdr = tk.Frame(container, bg=CARD)
        hdr.pack(fill="x", padx=14, pady=(12, 0))
        dia = tk.Canvas(hdr, width=11, height=11, bg=CARD,
                        highlightthickness=0, bd=0)
        dia.create_polygon(5, 0, 11, 5, 5, 11, 0, 5, fill=accent, outline=accent)
        dia.pack(side="left", padx=(0, 8), pady=(3, 0))
        tk.Label(hdr, text=title.upper(), bg=CARD, fg=accent,
                 font=FONT_SECTION).pack(side="left")
        tk.Label(container, text="Top = highest priority",
                 bg=CARD, fg=FAINT, font=FONT_HINT).pack(anchor="w", padx=14)

        champ_list = ChampionList(
            container, accent, bg=CARD, get_icon=app.get_champ_icon,
            # op.gg history is shown on picks only — not bans.
            get_stats=(app.get_champ_stats if list_key == "picks" else None),
            on_reorder=lambda order, r=role, k=list_key: app.reorder_items(r, k, order),
            on_remove=lambda cid, r=role, k=list_key: app.remove_item(r, k, cid),
        )
        champ_list.pack(fill="both", expand=True, padx=14, pady=(6, 2))
        self._widgets[f"{list_key}_lb"] = champ_list

        # Champion search / add
        add_row = tk.Frame(container, bg=CARD)
        add_row.pack(fill="x", padx=14, pady=(10, 12))

        tk.Label(add_row, text="Add:", bg=CARD, fg=TEXT,
                 font=FONT_SMALL).pack(side="left")

        entry_var = tk.StringVar()
        self._widgets[f"{list_key}_ev"] = entry_var

        entry = tk.Entry(add_row, textvariable=entry_var,
                         bg=FIELD_BG, fg=WHITE, insertbackground=WHITE,
                         relief="flat", width=16, font=FONT_SMALL,
                         highlightthickness=1, highlightbackground=EDGE_GOLD,
                         highlightcolor=GOLD)
        entry.pack(side="left", padx=6, ipady=3)

        # Autocomplete listbox (shown below entry, hidden when empty)
        ac_lb = tk.Listbox(container, bg=FIELD_BG, fg=WHITE,
                           selectbackground=accent, selectforeground=WHITE,
                           relief="flat", height=5, font=FONT_SMALL,
                           highlightthickness=1, highlightbackground=EDGE_GOLD)
        self._widgets[f"{list_key}_ac"] = ac_lb

        # Capture for closures
        _ev       = entry_var
        _ac       = ac_lb
        _role     = role
        _list_key = list_key

        def _do_add(name: str = ""):
            n   = (name or _ev.get()).strip()
            cid = app.ddragon.find_id(n)
            if cid is None:
                app.log(f"Unknown champion: {n!r}")
                return
            lst = app.cfg["roleChampions"][_role][_list_key]
            if cid not in lst:
                lst.insert(0, cid)                       # newest goes to the top
                bumped = None
                if len(lst) > MAX_PRIORITY_ITEMS:        # keep only the top 5
                    bumped = lst.pop(MAX_PRIORITY_ITEMS)
                save_config(app.cfg)       # persist immediately
                app.refresh_list(_role, _list_key)
                app.request_icon_prefetch()
                app.log(
                    f"Added {app.ddragon.name(cid)} to the top of "
                    f"{ROLE_LABEL[_role]} {_list_key[:-1]} list"
                )
                if bumped is not None:
                    app.log(f"{ROLE_LABEL[_role]} {_list_key[:-1]} list capped at "
                            f"{MAX_PRIORITY_ITEMS} — removed {app.ddragon.name(bumped)}.")
            _ev.set("")
            _ac.pack_forget()

        def _ac_update(*_):
            q = _ev.get().lower()
            _ac.delete(0, "end")
            if not q:
                _ac.pack_forget()
                return
            hits = [n for n in app.ddragon.all_display_names()
                    if q in n.lower()][:6]
            if not hits:
                _ac.pack_forget()
                return
            for h in hits:
                _ac.insert("end", "  " + h)
            _ac.pack(fill="x", padx=14, pady=(0, 8))

        def _ac_select(_evt):
            sel = _ac.curselection()
            if sel:
                _do_add(name=_ac.get(sel[0]).strip())

        entry_var.trace_add("write", _ac_update)
        ac_lb.bind("<<ListboxSelect>>", _ac_select)
        entry.bind("<Return>", lambda _: _do_add())

        tk.Button(add_row, text="＋", bg=accent, fg=WHITE, width=3,
                  activebackground=_shade(accent, 1.2), command=_do_add,
                  **BTN_STYLE).pack(side="left")

        return champ_list

    def get_champ_list(self, list_key: str) -> ChampionList:
        return self._widgets[f"{list_key}_lb"]


class OpGGDialog(tk.Toplevel):
    """Dialog that fetches op.gg champion stats and auto-fills pick/ban lists."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self._app      = app
        self._summoner = None   # (game_name, tag_line, region) once detected
        self.title("op.gg Auto-fill")
        self.configure(bg=DARK)
        self.resizable(False, False)
        self.grab_set()

        tk.Label(self, text="Summoner (from League Client):", bg=DARK, fg=TEXT,
                 font=("Segoe UI", 10)).pack(padx=20, pady=(20, 4), anchor="w")

        self._lbl_summoner = tk.Label(self, text="Detecting…", bg=DARK, fg=TEXT,
                                      font=("Segoe UI", 10, "bold"), wraplength=420)
        self._lbl_summoner.pack(padx=20, pady=(0, 10), anchor="w")

        opts = tk.Frame(self, bg=DARK)
        opts.pack(padx=20, pady=(0, 10), anchor="w")
        self._do_picks = tk.BooleanVar(value=True)
        self._do_bans  = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Fill picks  (from your stats)",
                       variable=self._do_picks, bg=DARK, fg=TEXT,
                       activebackground=DARK, selectcolor=PANEL).pack(
            side="left", padx=(0, 12))
        tk.Checkbutton(opts, text="Fill bans  (meta suggestions)",
                       variable=self._do_bans, bg=DARK, fg=TEXT,
                       activebackground=DARK, selectcolor=PANEL).pack(side="left")

        btn_row = tk.Frame(self, bg=DARK)
        btn_row.pack(padx=20, pady=(0, 10))
        self._btn_fetch = tk.Button(btn_row, text="Fetch & Fill", bg=GOLD, fg="#000",
                                    activebackground=GOLD, command=self._go,
                                    state="disabled", **BTN_STYLE)
        self._btn_fetch.pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="Cancel", bg=PANEL, fg=TEXT,
                  activebackground=PANEL, command=self.destroy,
                  **BTN_STYLE).pack(side="left")

        self._lbl_status = tk.Label(self, text="", bg=DARK, fg=TEXT,
                                    font=("Segoe UI", 9), wraplength=420)
        self._lbl_status.pack(padx=20, pady=(0, 16))

        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self):
        lcu = self._app._lcu
        try:
            if not lcu._sess:
                raise RuntimeError("League Client not running")
            r = lcu.get("/lol-summoner/v1/current-summoner")
            if r.status_code != 200:
                raise RuntimeError("Could not read summoner from client")
            d = r.json()
            game_name = d.get("gameName") or d.get("displayName", "")
            tag_line  = d.get("tagLine", "")
            region    = "na"
            try:
                rr = lcu.get("/riotclient/region-locale")
                if rr.status_code == 200:
                    region = rr.json().get("webRegion", "NA").lower()
            except Exception:
                pass
            self._summoner = (game_name, tag_line, region)
            label = f"{game_name}#{tag_line}  ({region.upper()})"
            self.after(0, lambda: (
                self._lbl_summoner.config(text=label, fg=GOLD),
                self._btn_fetch.config(state="normal"),
            ))
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self._lbl_summoner.config(text=msg, fg=RED))

    def _set_status(self, msg: str, color: str = TEXT):
        self.after(0, lambda: self._lbl_status.config(text=msg, fg=color))

    def _go(self):
        if not self._summoner:
            return
        self._lbl_status.config(text="Fetching data…", fg=TEXT)
        self._btn_fetch.config(state="disabled")
        threading.Thread(
            target=self._run,
            args=(*self._summoner, self._do_picks.get(), self._do_bans.get()),
            daemon=True,
        ).start()

    def _run(self, game_name: str, tag_line: str, region: str,
             do_picks: bool, do_bans: bool):
        result = self._app._opgg_fetch(game_name, tag_line, region, self._set_status)
        if result is not None:
            rows, champ_role, mastery_by_id = result
            self.after(0, lambda: self._app._opgg_apply(
                rows, champ_role, mastery_by_id, do_picks, do_bans, self))
        else:
            self.after(0, lambda: self._btn_fetch.config(state="normal"))


# ── Local REST API (Stream Deck, home-automation, etc.) ──────────────────────
def _make_api_handler(app):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  self._handle()
        def do_POST(self): self._handle()

        def _handle(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/ready-up":
                app.after(0, app._toggle_party_ready)
                self._respond({"ok": True, "action": "ready-up"})
            elif path == "/accept":
                def _do():
                    try: app._lcu.post("/lol-matchmaking/v1/ready-check/accept")
                    except Exception: pass
                threading.Thread(target=_do, daemon=True).start()
                self._respond({"ok": True, "action": "accept"})
            elif path == "/status":
                eng = app._engine
                self._respond({
                    "phase":         getattr(eng, "_last_phase",    ""),
                    "ready":         getattr(eng, "_i_am_ready",    False),
                    "ready_count":   getattr(eng, "_ready_count",   0),
                    "present_count": getattr(eng, "_present_count", 0),
                })
            else:
                self.send_response(404)
                self.end_headers()

        def _respond(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_): pass

    return _Handler


def _start_local_api(app, port: int):
    try:
        srv = HTTPServer(("127.0.0.1", port), _make_api_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        app.log(
            f"Stream Deck API → http://127.0.0.1:{port}"
            f"  (GET /ready-up  /accept  /status)"
        )
    except OSError as e:
        app.log(f"[api] Could not bind port {port}: {e}")


# ── Always-on-top overlay ─────────────────────────────────────────────────────
class LCUOverlay:
    """Frameless always-on-top button that floats over the League client window.
    Shows FIND MATCH in lobby and ACCEPT during a ready check.
    Drag to reposition."""

    _LCU_CLASS = "RCLIENT"   # LeagueClientUx window class

    def __init__(self, app: "App"):
        self._app         = app
        self._phase       = ""
        self._drag_x      = self._drag_y = 0
        self._dragged     = False
        self._rel_x       = None   # overlay offset from LCU top-left (logical px)
        self._rel_y       = None   # None = not yet placed
        self._hwnd_cache  = None   # cached LCU hwnd for use during drag

        win = tk.Toplevel(app)
        win.withdraw()
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-transparentcolor", "#010A13")
        win.configure(bg="#010A13")

        btn = tk.Canvas(win, bg="#010A13", highlightthickness=0,
                        cursor="hand2", width=220, height=53)
        btn.pack(fill="both", expand=True, padx=1, pady=1)

        for w in (win, btn):
            w.bind("<ButtonPress-1>",  self._drag_start)
            w.bind("<B1-Motion>",      self._drag_move)
        btn.bind("<ButtonRelease-1>", lambda e: (not self._dragged) and self._click())
        btn.bind("<Enter>", lambda e: self._btn_set_hover(True))
        btn.bind("<Leave>", lambda e: self._btn_set_hover(False))

        self._win           = win
        self._btn           = btn
        self._btn_label     = ""
        self._btn_style     = "find"
        self._btn_prev_style = "find"
        self._btn_hover     = False
        self._btn_small     = False
        self._btn_shown     = None   # 4-tuple of colours currently rendered
        self._btn_anim      = None   # pending after() id for the colour morph
        self._ct_state      = None   # "clickable" | "passthrough" | "hidden"
        self._ct_lmb_was    = False
        self._ct_hide_until = 0.0
        self._click_grace   = 0.0    # suppress self-foreground hide right after a click
        self._tick()
        self._ct_poll()

    # ── League client window ──────────────────────────────────────────────────
    @staticmethod
    def _find_lcu():
        # Try known class name first
        hwnd = ctypes.windll.user32.FindWindowW(LCUOverlay._LCU_CLASS, None)
        if hwnd:
            return hwnd
        # Fall back: find the largest visible window owned by LeagueClientUx.exe
        result: list = []
        _Proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _cb(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if "LeagueClientUx" in psutil.Process(pid.value).name():
                    r = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                    result.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            except Exception:
                pass
            return True
        ctypes.windll.user32.EnumWindows(_Proc(_cb), 0)
        return max(result, default=(0, None))[1]

    @staticmethod
    def _lcu_rect(hwnd):
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r

    @staticmethod
    def _dpi_scale():
        try:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
        except Exception:
            return 1.0

    # ── phase + visibility ────────────────────────────────────────────────────
    def set_phase(self, phase: str):
        self._phase = phase
        self._refresh()

    # ── Click-through / auto-hide management ─────────────────────────────────
    _GWL_EXSTYLE       = -20
    _WS_EX_TRANSPARENT = 0x00000020
    # WS_EX_NOACTIVATE: the overlay receives mouse clicks but never becomes the
    # foreground window, so clicking Ready Up can't steal focus from League and
    # spuriously trip the "our own window is in front → hide" rule in _refresh.
    _WS_EX_NOACTIVATE  = 0x08000000

    def _ct_update(self, state: str):
        """Apply one of three states to the overlay window:
        'clickable'   — fully visible, receives mouse events
        'passthrough' — fully visible, clicks fall through to League client
        'hidden'      — invisible and click-through (role selection in progress)
        """
        if state == self._ct_state:
            return
        self._ct_state = state
        user32 = ctypes.windll.user32
        hwnd   = self._win.winfo_id()
        base   = user32.GetWindowLongW(hwnd, self._GWL_EXSTYLE) | self._WS_EX_NOACTIVATE
        if state == "clickable":
            new_style, alpha = base & ~self._WS_EX_TRANSPARENT, 1.0
        elif state == "passthrough":
            new_style, alpha = base | self._WS_EX_TRANSPARENT, 1.0
        else:  # hidden
            new_style, alpha = base | self._WS_EX_TRANSPARENT, 0.0
        user32.SetWindowLongW(hwnd, self._GWL_EXSTYLE, new_style)
        # Commit the ex-style change. WS_EX_NOACTIVATE is cached until the frame
        # is recalculated, so without SWP_FRAMECHANGED the overlay would still
        # steal foreground on the first click. The window is frameless, so this
        # has no visible cost.
        _SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP)
        self._win.wm_attributes("-alpha", alpha)
        if state != "clickable":
            self._btn_set_hover(False)

    # Proportional region of the LCU window that contains the role-select button
    # (the asterisk * at the bottom of the lobby, right of Find Match).
    # Values are (x0, y0, x1, y1) as fractions of the LCU window size.
    # Derived from screenshot: button sits at ~54 % across, ~95 % down.
    _ROLE_ZONE = (0.46, 0.88, 0.62, 1.00)

    def _ct_poll(self):
        """Poll at 80ms:
        - Hide when user clicks the role-select button in the LCU window.
        - Pass mouse events through to LCU when cursor is not over the overlay.
        - Become fully interactive when cursor is over the overlay.
        """
        if self._win.state() != "withdrawn":
            try:
                user32 = ctypes.windll.user32

                # Cursor position — physical screen pixels
                pt = ctypes.wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                cx, cy = pt.x, pt.y

                # Overlay bounds — physical screen pixels via GetWindowRect
                ov_rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(self._win.winfo_id(), ctypes.byref(ov_rect))
                over = (ov_rect.left <= cx <= ov_rect.right and
                        ov_rect.top  <= cy <= ov_rect.bottom)

                # Role-select zone — proportional to LCU window
                in_role_zone = False
                lcu_hwnd = self._find_lcu()
                if lcu_hwnd:
                    lcu_rect = ctypes.wintypes.RECT()
                    user32.GetWindowRect(lcu_hwnd, ctypes.byref(lcu_rect))
                    lx, ly = lcu_rect.left, lcu_rect.top
                    lw = lcu_rect.right  - lcu_rect.left
                    lh = lcu_rect.bottom - lcu_rect.top
                    x0, y0, x1, y1 = self._ROLE_ZONE
                    in_role_zone = (lx + int(lw * x0) <= cx <= lx + int(lw * x1) and
                                    ly + int(lh * y0) <= cy <= ly + int(lh * y1))

                lmb     = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
                was_lmb = self._ct_lmb_was
                self._ct_lmb_was = lmb

                # Click in role zone (but not on the overlay itself) → hide for 3 s
                if lmb and not was_lmb and in_role_zone and not over:
                    self._ct_hide_until = time.monotonic() + 3.0

                if time.monotonic() < self._ct_hide_until:
                    self._ct_update("hidden")
                elif over:
                    self._ct_update("clickable")
                else:
                    self._ct_update("passthrough")
            except Exception:
                pass
        self._app.after(80, self._ct_poll)

    # ── Refresh loop ──────────────────────────────────────────────────────────
    def _tick(self):
        self._refresh()
        # Faster tick in graph mode for smooth scrolling; slower otherwise
        delay = 600
        self._app.after(delay, self._tick)

    def _refresh(self):
        phase = self._phase
        _dbg(f"refresh: phase={phase!r}")
        if not self._app.cfg.get("overlayEnabled", True):
            self._win.withdraw()
            _dbg("refresh: withdraw (overlay disabled)")
            return
        if phase not in ("Lobby", "Matchmaking", "ReadyCheck"):
            self._win.withdraw()
            _dbg("refresh: withdraw (bad phase)")
            return
        hwnd = self._find_lcu()
        if not hwnd or ctypes.windll.user32.IsIconic(hwnd):
            self._win.withdraw()
            _dbg("refresh: withdraw (no hwnd or iconic)")
            return

        # Hide if another app's window is covering the centre of the League client.
        # This lets the overlay stay visible when League is on a second monitor or
        # beside a smaller window, while hiding it when League is behind a full-screen
        # or large foreground window.
        user32       = ctypes.windll.user32
        fg           = user32.GetForegroundWindow()
        overlay_hwnd = self._win.winfo_id()

        # Tk wraps the overlay Toplevel, so GetForegroundWindow() returns the
        # wrapper HWND while winfo_id() returns the inner child — they differ.
        # Compare top-level roots so the overlay's OWN window (which becomes
        # foreground when you click Ready Up) is never mistaken for the main
        # tool window and never triggers a hide.
        _GA_ROOT = 2
        user32.GetAncestor.restype  = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        overlay_root = user32.GetAncestor(overlay_hwnd, _GA_ROOT)
        fg_root      = user32.GetAncestor(fg, _GA_ROOT) if fg else None
        is_overlay   = (overlay_root is not None and fg_root == overlay_root)

        if (fg and fg != hwnd and not is_overlay
                and time.monotonic() >= self._click_grace):
            # A window that isn't League and isn't our overlay is in front.
            # The LOL Client Tool's own UI is treated like any other window: it
            # only hides the overlay if it actually covers League's centre (the
            # check below), not merely by being focused.
            fg_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))

            # If the foreground window belongs to the League client's OWN
            # process, the user is looking at League — even if it's a second
            # League window that isn't the exact handle _find_lcu() returned.
            # Never treat that as a covering app (otherwise clicking League can
            # blink the overlay out for a couple of refresh cycles).
            league_pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(league_pid))
            if fg_pid.value != league_pid.value:
                # Otherwise hide only when another app covers League's centre.
                lc_rect = ctypes.wintypes.RECT()
                fg_rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(lc_rect))
                user32.GetWindowRect(fg,   ctypes.byref(fg_rect))
                lc_cx = (lc_rect.left + lc_rect.right)  // 2
                lc_cy = (lc_rect.top  + lc_rect.bottom) // 2
                if (fg_rect.left <= lc_cx <= fg_rect.right and
                        fg_rect.top <= lc_cy <= fg_rect.bottom):
                    self._win.withdraw()
                    _dbg("refresh: withdraw (fg window covering league centre)")
                    return

        relay_on   = bool(getattr(self._app, "_relay_connected", False))
        ready_up   = bool(self._app.cfg.get("readyUpEnabled", True))
        ping       = getattr(self._app, "_ping_val", None)
        ping_s     = f"  {ping}ms" if ping is not None else "  ---ms"
        eng        = self._app._engine
        party_size = getattr(eng, "_party_size", 0)
        _dbg(f"refresh: relay_on={relay_on} ready_up={ready_up} ping={ping} party_size={party_size}")

        if phase == "ReadyCheck":
            self._win.withdraw()
            _dbg("refresh: withdraw (ReadyCheck)")
            return

        # Hide when not in a lobby — applies regardless of relay status.
        if party_size == 0:
            self._win.withdraw()
            _dbg("refresh: withdraw (party_size=0, not in lobby)")
            return

        if not relay_on or not ready_up:
            # In a lobby but relay off or feature disabled — small ping only.
            self._set_btn(f"{ping}ms" if ping is not None else "---ms",
                          "find", small=True)
            _dbg("refresh: show small ping (relay off or ready_up off)")
        else:
            rc    = getattr(eng, "_ready_count",  0)
            pc    = getattr(eng, "_present_count", 0)
            i_rdy = getattr(eng, "_i_am_ready",   False)
            _dbg(f"refresh: party_size={party_size} pc={pc} rc={rc}")
            if party_size >= 2 and pc > 1:
                # Multi-person party with at least one other tool user.
                self._set_btn(f"READY UP  [{rc}/{pc}]{ping_s}",
                              "ready" if i_rdy else "notready")
                _dbg(f"refresh: show READY UP [{rc}/{pc}]")
            else:
                # Solo in lobby OR in party but no other tool users.
                self._set_btn(f"{ping}ms" if ping is not None else "---ms",
                              "find", small=True)
                _dbg(f"refresh: show small ping (party_size={party_size} pc={pc})")

        rect  = self._lcu_rect(hwnd)
        scale = self._dpi_scale()
        lx = int(rect.left  / scale)
        ly = int(rect.top   / scale)
        lw = int((rect.right  - rect.left) / scale)
        lh = int((rect.bottom - rect.top)  / scale)

        self._hwnd_cache = hwnd

        # Always size from the button — canvas overlays it via place so they
        # share the exact same pixel footprint.
        self._win.update_idletasks()
        if self._btn_small:
            ow = max(self._win.winfo_reqwidth(),  68)
            oh = max(self._win.winfo_reqheight(),  32)
        else:
            ow = max(self._win.winfo_reqwidth(),  192)
            oh = max(self._win.winfo_reqheight(),  53)

        if self._btn_small:
            # Small ping button: always formula-placed, not saved.
            # Centre X matches the big button; Y sits just above "Autofill Protected".
            sx = int(lw * 0.829) // 2 - ow // 2
            sy = int(lh * 0.865) - oh - 4
            new_geom = f"{ow}x{oh}+{lx + sx}+{ly + sy}"
        else:
            if self._rel_x is None:
                saved_x = self._app.cfg.get("overlayRelX")
                saved_y = self._app.cfg.get("overlayRelY")
                if saved_x is not None and saved_y is not None:
                    self._rel_x = int(saved_x)
                    self._rel_y = int(saved_y)
                else:
                    self._rel_x = int(lw * 0.829 - ow) // 2
                    self._rel_y = int(lh * 0.873) - oh // 2
            new_geom = f"{ow}x{oh}+{lx + self._rel_x}+{ly + self._rel_y}"

        if new_geom != getattr(self, "_last_geom", None):
            self._last_geom = new_geom
            self._win.geometry(new_geom)
        if self._win.state() == "withdrawn":
            self._ct_state = None   # force style re-apply after hiding
            self._win.deiconify()

    # ── LCU actions ───────────────────────────────────────────────────────────
    def _click(self):
        if self._dragged:
            self._dragged = False
            return
        if self._btn_small:
            return
        # Clicking briefly disturbs focus/z-order; give it time to settle back
        # on League before the refresh loop is allowed to hide the overlay again.
        self._click_grace = time.monotonic() + 2.0
        lcu:  "LCU" = self._app._lcu
        phase = self._phase
        hwnd  = self._find_lcu()
        def _do():
            try:
                if phase == "ReadyCheck":
                    lcu.post("/lol-matchmaking/v1/ready-check/accept")
                elif phase in ("Lobby", "Matchmaking"):
                    self._app._toggle_party_ready()
            except Exception:
                pass
            # Return focus to the League client so the overlay stays visible
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        threading.Thread(target=_do, daemon=True).start()

    # ── drag to reposition ────────────────────────────────────────────────────
    def _drag_start(self, event):
        self._drag_x  = event.x_root - self._win.winfo_x()
        self._drag_y  = event.y_root - self._win.winfo_y()
        self._dragged = False

    def _drag_move(self, event):
        self._dragged = True
        nx = event.x_root - self._drag_x
        ny = event.y_root - self._drag_y
        self._win.geometry(f"+{nx}+{ny}")
        hwnd = self._hwnd_cache
        if hwnd:
            rect  = self._lcu_rect(hwnd)
            scale = self._dpi_scale()
            self._rel_x = nx - int(rect.left / scale)
            self._rel_y = ny - int(rect.top  / scale)
            # Persist so position survives restarts.
            self._app.cfg["overlayRelX"] = self._rel_x
            self._app.cfg["overlayRelY"] = self._rel_y
            save_config(self._app.cfg)

    # ── LoL-style canvas button ───────────────────────────────────────────────

    _LOL_FONT = "Palatino Linotype"   # closest serif to Beaufort on Windows

    # Button themes: (outer_dark, inner_highlight, fill, text)
    _BTN_THEMES = {
        "find":     ("#463714", "#C8AA6E", "#091428", "#C8AA6E"),
        "ready":    ("#145A28", "#1EBF5A", "#091A10", "#FFFFFF"),
        "notready": ("#5A1A10", "#C83232", "#1A0808", "#E88080"),
    }
    _BTN_HOVER = {
        "find":     ("#5A4A1A", "#F0E6D3", "#091428", "#F0E6D3"),
        "ready":    ("#1A7A34", "#4AE87A", "#091A10", "#FFFFFF"),
        "notready": ("#7A2010", "#E85050", "#1A0808", "#FFFFFF"),
    }

    @staticmethod
    def _lol_pts(x0, y0, x1, y1, cut):
        return [x0+cut,y0, x1-cut,y0, x1,y0+cut, x1,y1-cut,
                x1-cut,y1, x0+cut,y1, x0,y1-cut, x0,y0+cut]

    def _set_btn(self, text: str, style: str, small: bool = False):
        # Normalise ping digits → "000ms" so width stays stable as ping fluctuates.
        stable = _re.sub(r'(\d+|---)\s*ms', '000ms', text)
        if small:
            fnt   = tkfont.Font(family=self._LOL_FONT, size=10, weight="bold")
            new_w = max(fnt.measure(stable) + 28, 68)
            new_h = 32
        else:
            fnt   = tkfont.Font(family=self._LOL_FONT, size=12, weight="bold")
            if 'ms' not in stable:
                stable += '  000ms'
            new_w = max(fnt.measure(stable) + 56, 192)
            new_h = 53

        size_changed = (int(self._btn["width"]) != new_w or
                        int(self._btn["height"]) != new_h)
        if size_changed:
            self._btn.config(width=new_w, height=new_h)

        content_changed = (text != self._btn_label or
                           style != self._btn_style or
                           small != self._btn_small)
        self._btn_label = text
        self._btn_style = style
        self._btn_small = small

        if content_changed or size_changed:
            self._draw_lol_btn()

    def _btn_set_hover(self, val: bool):
        if val == self._btn_hover:
            return
        self._btn_hover = val
        self._draw_lol_btn()

    def _draw_lol_btn(self):
        target = (self._BTN_HOVER if self._btn_hover else self._BTN_THEMES).get(
            self._btn_style, self._BTN_THEMES["find"])

        # Animate the colour sweep when toggling between the red "not ready"
        # and green "ready" states (the text/numbers update immediately).
        prev = self._btn_prev_style
        self._btn_prev_style = self._btn_style
        toggling = ({prev, self._btn_style} == {"ready", "notready"})

        if toggling and not self._btn_small and self._btn_shown is not None:
            self._animate_btn(self._btn_shown, target)
        else:
            if self._btn_anim is not None:
                self._app.after_cancel(self._btn_anim)
                self._btn_anim = None
            self._render_btn(target)

    def _animate_btn(self, c_from, c_to, steps=6):
        if self._btn_anim is not None:
            self._app.after_cancel(self._btn_anim)
            self._btn_anim = None

        def _step(i):
            t   = i / steps
            cur = tuple(_blend(c_from[k], c_to[k], t) for k in range(4))
            self._render_btn(cur)
            if i < steps:
                self._btn_anim = self._app.after(26, lambda: _step(i + 1))
            else:
                self._btn_anim = None
                self._render_btn(c_to)

        _step(1)

    def _render_btn(self, colors):
        self._btn_shown = colors
        c = self._btn
        w = int(c["width"])
        h = int(c["height"])
        b_dark, b_hi, fill, fg = colors
        cut     = 5 if self._btn_small else 9
        font_sz = 10 if self._btn_small else 12

        pts_outer  = self._lol_pts(0, 0, w, h, cut)
        pts_border = self._lol_pts(1, 1, w-1, h-1, cut-1)
        pts_inner  = self._lol_pts(2, 2, w-2, h-2, cut-2)
        cx, cy     = w // 2, h // 2 + 1

        if c.find_withtag("bg"):
            # Update existing items in-place — no blank frame between delete and redraw.
            c.coords("bg",     pts_outer)
            c.itemconfig("bg", fill=b_dark)
            c.coords("border",     pts_border)
            c.itemconfig("border", fill=b_hi)
            c.coords("fill_",     pts_inner)
            c.itemconfig("fill_", fill=fill)
            c.coords("label", cx, cy)
            c.itemconfig("label", text=self._btn_label, fill=fg,
                         font=(self._LOL_FONT, font_sz, "bold"))
        else:
            c.create_polygon(pts_outer,  fill=b_dark, outline="", tags="bg")
            c.create_polygon(pts_border, fill=b_hi,   outline="", tags="border")
            c.create_polygon(pts_inner,  fill=fill,   outline="", tags="fill_")
            c.create_text(cx, cy, text=self._btn_label, fill=fg,
                          font=(self._LOL_FONT, font_sz, "bold"),
                          anchor="center", tags="label")



class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}   v{APP_VERSION}")
        self.configure(bg=DARK)
        self.geometry("940x690")
        self.minsize(900, 630)
        self.resizable(True, True)

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("", encoding="utf-8")  # clear on startup
        try:                                        # clear the debug log too, so
            _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)  # it doesn't grow
            _DEBUG_LOG.write_text("", encoding="utf-8")           # across sessions
        except Exception:
            pass

        self.cfg     = load_config()
        self.ddragon = DDragon()
        self._lcu    = LCU()
        self._engine = AutoEngine(self._lcu, lambda: self.cfg, self.log,
                                  self.ddragon, self._on_phase_change)

        self._role_panels: dict = {}   # role → RolePanel
        self._delay_vars:  dict = {}
        self._connected    = False
        self._icon_images:   dict = {}   # champ id → ImageTk.PhotoImage (circular avatar)
        self._icon_pil_cache: dict = {}  # champ id → PIL.Image, awaiting main-thread materialize
        self._spell_icons:   dict = {}   # (spell id, size) → ImageTk.PhotoImage
        self._opgg_stats:    dict = {}   # champ id → (season games, wins) from op.gg
        self._opgg_fetched   = False     # True once a stats fetch has succeeded
        self._opgg_fetching  = False     # a fetch worker is currently running

        # Dashboard state
        self._auto_switches: dict = {}   # key → ToggleSwitch
        self._nav_rows:      dict = {}   # page-name → (row, accent, label)
        self._pages:         dict = {}   # page-name → content frame

        self._build_ui()

        # System tray + start-minimised when auto-launched at Windows startup
        self._setup_tray()
        if "--startup" in sys.argv and getattr(self, "_tray", None):
            self.withdraw()   # start hidden in the tray

        # Always-on-top overlay over the League client
        self._overlay = LCUOverlay(self)

        # Global hotkey for overlay enable/disable toggle
        threading.Thread(target=self._watch_overlay_hotkey, daemon=True).start()

        # Local REST API for Stream Deck / external triggers
        _api_port = self.cfg.get("localApiPort", 8778)
        if _api_port:
            self.after(500, lambda: _start_local_api(self, _api_port))

        # Load champion data in background
        threading.Thread(target=self._load_champs, daemon=True).start()

        # Watch for the League client to open (auto-connect on launch)
        threading.Thread(target=self._watch_for_client, daemon=True).start()

        # Watch the ready-up relay's reachability for the header status bubble
        threading.Thread(target=self._watch_relay, daemon=True).start()

        # Live ping to the regional Riot servers, shown under the Ready Up button
        threading.Thread(target=self._watch_ping, daemon=True).start()

        # Auto-load the Builds tab for whatever champion is hovered in champ select
        threading.Thread(target=self._watch_build_hover, daemon=True).start()

        # Check for updates 3 s after startup so the UI is fully loaded first
        self.after(3000, lambda: threading.Thread(
            target=_update_check, args=(self, self.log), daemon=True,
        ).start())

    # ── Champion data ─────────────────────────────────────────────────────────
    def _load_champs(self):
        self.log("Loading champion data...")
        self.ddragon.load()
        self.after(0, self._refresh_all)
        self.log(f"Loaded {len(self.ddragon.all_display_names())} champions.")
        self._prefetch_icons()   # network I/O — safe here, already off the UI thread
        self.refresh_opgg_stats()   # per-chip op.gg history (needs the client connected)

    def get_champ_stats(self, cid):
        """op.gg season history for a champion, for the chip's right third.
        Returns (games, wins) if the summoner has played it, "nodata" if a
        fetch succeeded but this champ isn't in it, or None if not yet fetched."""
        if not self._opgg_fetched:
            return None
        return self._opgg_stats.get(int(cid), "nodata")

    def refresh_opgg_stats(self, force=False):
        """Kick off a background fetch of the signed-in summoner's per-champion
        season history from op.gg. No-op if already loaded (unless force) or a
        fetch is in flight."""
        if self._opgg_fetching:
            return
        if self._opgg_fetched and not force:
            return
        self._opgg_fetching = True
        threading.Thread(target=self._opgg_stats_worker, daemon=True).start()

    def _opgg_stats_worker(self):
        try:
            if not self.ddragon.all_ids():
                return   # champ data not loaded yet — _load_champs will retry
            summ = self._detect_summoner()
            if not summ:
                return   # client not connected yet — _on_connected will retry
            game_name, tag_line, region = summ
            stats = self._opgg_champ_stats(game_name, tag_line, region)
            if stats is None:
                return
            id_map = {}
            for name, gw in stats.items():
                cid = self.ddragon.find_id_fuzzy(name)
                if cid is not None:
                    id_map[cid] = gw
            self._opgg_stats  = id_map
            self._opgg_fetched = True
            self.after(0, self._refresh_all)
            self.log(f"op.gg history loaded for {game_name}#{tag_line} — "
                     f"{len(id_map)} champions (all queues, past 6 months).")
        except Exception as e:
            _dbg(f"[opgg] stats worker error: {e}")
        finally:
            self._opgg_fetching = False

    # ── Champion build recommendations (op.gg, diamond+) ───────────────────────
    @staticmethod
    def _dedupe(seq, cap=None):
        out = []
        for x in seq:
            if x and x not in out:
                out.append(x)
                if cap and len(out) >= cap:
                    break
        return out

    def _region_upper(self):
        try:
            r = self._lcu.get("/riotclient/region-locale")
            if r.status_code == 200:
                return str(r.json().get("webRegion", "NA")).upper()
        except Exception:
            pass
        return "NA"

    def _champ_select_pick(self):
        """(champion display name, op.gg position) for the local player's
        current champ-select pick/intent, or None if not in champ select."""
        try:
            r = self._lcu.get("/lol-champ-select/v1/session")
            if r.status_code != 200:
                return None
            s = r.json()
            cell = s.get("localPlayerCellId")
            cid, assigned = 0, ""
            for mbr in s.get("myTeam", []):
                if mbr.get("cellId") == cell:
                    cid = int(mbr.get("championId") or 0) or \
                          int(mbr.get("championPickIntent") or 0)
                    assigned = (mbr.get("assignedPosition") or "").lower()
                    break
            if not cid:
                return None
            pos = OPGG_POSITION.get(assigned, "")
            return (self.ddragon.name(cid), pos)
        except Exception:
            return None

    def _watch_build_hover(self):
        """Poll champ select and mirror the locally-hovered champion into the
        Builds tab — auto-pick pre-hover or a manual hover both count, replacing
        whatever was searched. Runs on its own daemon thread."""
        last = None
        while True:
            try:
                pick = self._champ_select_pick()   # (name, pos) or None
            except Exception:
                pick = None
            if pick and pick != last:
                last = pick
                self.after(0, lambda p=pick: self._auto_load_build(*p))
            elif pick is None and last is not None:
                # Left champ select — let the next hover re-trigger a load.
                last = None
            time.sleep(1.0)

    def _auto_load_build(self, name, pos):
        """Push a champ-select hover into the Builds tab (UI thread)."""
        if not hasattr(self, "_build_champ_var"):
            return   # Builds page hasn't been built yet
        self._build_champ_var.set(name)
        if hasattr(self, "_build_ac"):
            self._build_ac.place_forget()
        if pos:
            self._set_build_pos(pos)
        self._load_build(name, pos or self._build_pos_var.get())

    def _fetch_build(self, champ_name, position, tier="diamond_plus"):
        """Fetch a champion's diamond+ build/skill recommendations from op.gg.
        Returns a structured dict (see _render_build) or an {'error': msg}."""
        cid = self.ddragon.find_id_fuzzy(champ_name)
        if cid is None:
            return {"error": f"Unknown champion: {champ_name!r}"}
        og_name = self.ddragon.opgg_name(cid)          # UPPER_SNAKE_CASE
        pos = (position or "").lower() or "mid"
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "lol_get_champion_analysis",
                "arguments": {
                    "champion": og_name, "position": pos,
                    "game_mode": "ranked", "tier": tier,
                    "desired_output_fields": [
                        "data.summary.average_stats.win_rate",
                        "data.summary.average_stats.pick_rate",
                        "data.summary.average_stats.ban_rate",
                        "data.starter_items.ids_names", "data.core_items.ids_names",
                        "data.boots.ids_names",
                        "data.fourth_items[].ids_names",
                        "data.fifth_items[].ids_names",
                        "data.sixth_items[].ids_names",
                        "data.summoner_spells.ids",
                        "data.skills.order", "data.skill_masteries.ids",
                        "data.runes.primary_page_name", "data.runes.primary_rune_names",
                        "data.runes.secondary_page_name", "data.runes.secondary_rune_names",
                        "data.damage_type",
                    ],
                },
            },
        }
        try:
            r = requests.post("https://mcp-api.op.gg/mcp", json=body, timeout=20,
                              headers={"Accept": "application/json, text/event-stream"})
            r.raise_for_status()
            text = r.json()["result"]["content"][0]["text"]
        except Exception as e:
            _dbg(f"[build] fetch failed: {e}")
            return {"error": "Could not reach op.gg. Check your connection."}

        rec = _parse_opgg_record(text)
        if not rec or "data" not in rec:
            return {"error": "op.gg returned no build data for this pick."}
        d = rec["data"]

        def names(x):
            return x.get("ids_names", []) if isinstance(x, dict) else (x or [])

        def top_options(arr, k=3):
            out = []
            if isinstance(arr, list):
                for opt in arr:
                    nm = (opt.get("ids_names") or []) if isinstance(opt, dict) else []
                    if nm:
                        out.append(nm[0])
                    if len(out) >= k:
                        break
            return out

        st = (d.get("summary") or {}).get("average_stats") or {}
        spells = d.get("summoner_spells") or {}
        spell_ids = spells.get("ids") if isinstance(spells, dict) else []
        runes = d.get("runes") or {}
        return {
            "champion":   self.ddragon.name(cid),
            "position":   pos,
            "win_rate":   st.get("win_rate"),
            "pick_rate":  st.get("pick_rate"),
            "ban_rate":   st.get("ban_rate"),
            "damage":     d.get("damage_type"),
            "spells":     [_spell_name(s) for s in (spell_ids or [])],
            "starter":    names(d.get("starter_items")),
            "core":       names(d.get("core_items")),
            "boots":      names(d.get("boots")),
            "situational": self._dedupe(
                top_options(d.get("fourth_items"), 2)
                + top_options(d.get("fifth_items"), 2)
                + top_options(d.get("sixth_items"), 2), cap=4),
            "skill_order": (d.get("skills") or {}).get("order", []),
            "max_order":   (d.get("skill_masteries") or {}).get("ids", []),
            "rune_primary_page":   runes.get("primary_page_name", ""),
            "rune_primary":        runes.get("primary_rune_names", []),
            "rune_secondary_page": runes.get("secondary_page_name", ""),
            "rune_secondary":      runes.get("secondary_rune_names", []),
            # Up to 3 distinct spell + rune-page combos (most popular first),
            # each pushable to the client. op.gg exposes one per tier, so the
            # variety comes from sampling several rank tiers.
            "combos": self._fetch_combos(og_name, pos),
        }

    # Rank tiers sampled for combo variety, broadest ("all") first so the most
    # popular combo is combo #1 and higher-elo variants follow.
    _COMBO_TIERS = [("all", "All ranks"), ("diamond_plus", "Diamond+"),
                    ("challenger", "Challenger")]

    def _fetch_one_combo(self, og_name, position, tier):
        """One (spells + rune page) combo for a champion/role at a rank tier,
        with the IDs needed to push it to the client, or None."""
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "lol_get_champion_analysis",
                "arguments": {
                    "champion": og_name, "position": position,
                    "game_mode": "ranked", "tier": tier,
                    "desired_output_fields": [
                        "data.runes.primary_page_id", "data.runes.primary_page_name",
                        "data.runes.primary_rune_ids", "data.runes.primary_rune_names",
                        "data.runes.secondary_page_id", "data.runes.secondary_page_name",
                        "data.runes.secondary_rune_ids", "data.runes.secondary_rune_names",
                        "data.runes.stat_mod_ids", "data.runes.pick_rate",
                        "data.summoner_spells.ids", "data.summoner_spells.ids_names",
                    ],
                },
            },
        }
        try:
            r = requests.post("https://mcp-api.op.gg/mcp", json=body, timeout=15,
                              headers={"Accept": "application/json, text/event-stream"})
            r.raise_for_status()
            rec = _parse_opgg_record(r.json()["result"]["content"][0]["text"])
        except Exception as e:
            _dbg(f"[combo] {tier} fetch failed: {e}")
            return None
        if not rec or "data" not in rec:
            return None
        rn = (rec["data"] or {}).get("runes") or {}
        sp = (rec["data"] or {}).get("summoner_spells") or {}
        prim = [int(x) for x in (rn.get("primary_rune_ids") or [])]
        sec  = [int(x) for x in (rn.get("secondary_rune_ids") or [])]
        shard = [int(x) for x in (rn.get("stat_mod_ids") or [])]
        sids = [int(x) for x in (sp.get("ids") or [])][:2]
        if not (prim and sec and len(sids) == 2):
            return None
        return {
            "primary_page_id":     int(rn.get("primary_page_id") or 0),
            "primary_page_name":   rn.get("primary_page_name", ""),
            "primary_rune_ids":    prim,
            "primary_rune_names":  rn.get("primary_rune_names", []),
            "secondary_page_id":   int(rn.get("secondary_page_id") or 0),
            "secondary_page_name": rn.get("secondary_page_name", ""),
            "secondary_rune_ids":  sec,
            "secondary_rune_names": rn.get("secondary_rune_names", []),
            "stat_mod_ids":        shard,
            "spell_ids":           sids,
            "spell_names":         [_spell_name(s) for s in sids],
            "pick_rate":           rn.get("pick_rate"),
        }

    def _fetch_combos(self, og_name, position):
        """Up to 3 DISTINCT (spells + rune page) combos, most popular first.
        Samples several rank tiers in parallel, then dedupes."""
        results = {}
        def work(tier, label):
            c = self._fetch_one_combo(og_name, position, tier)
            if c:
                c["label"] = label
                results[tier] = c
        threads = [threading.Thread(target=work, args=(t, lbl), daemon=True)
                   for t, lbl in self._COMBO_TIERS]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=18)
        seen, combos = set(), []
        for tier, _label in self._COMBO_TIERS:
            c = results.get(tier)
            if not c:
                continue
            key = (c["primary_page_id"], tuple(c["primary_rune_ids"]),
                   c["secondary_page_id"], tuple(c["secondary_rune_ids"]),
                   tuple(c["stat_mod_ids"]), tuple(c["spell_ids"]))
            if key in seen:
                continue
            seen.add(key)
            combos.append(c)
            if len(combos) >= 3:
                break
        return combos

    def _detect_summoner(self):
        """(game_name, tag_line, region) for the signed-in summoner, or None."""
        lcu = self._lcu
        try:
            if not lcu._sess:
                return None
            r = lcu.get("/lol-summoner/v1/current-summoner")
            if r.status_code != 200:
                return None
            d = r.json()
            game_name = d.get("gameName") or d.get("displayName", "")
            tag_line  = d.get("tagLine", "")
            if not game_name:
                return None
            region = "na"
            try:
                rr = lcu.get("/riotclient/region-locale")
                if rr.status_code == 200:
                    region = rr.json().get("webRegion", "NA").lower()
            except Exception:
                pass
            return (game_name, tag_line, region)
        except Exception:
            return None

    def _opgg_champ_stats(self, game_name, tag_line, region):
        """All-queue (ranked AND unranked) per-champion history for the user
        over roughly the past 6 months, aggregated from op.gg match history.
        Returns {op.gg champion name: (games, wins)} or None on failure.

        NOTE: op.gg's MCP caps match history at 20 games with no pagination
        (verified — cursor/page args are ignored), so this covers up to the
        20 most recent games that fall inside the 6-month window."""
        from datetime import datetime, timezone, timedelta
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "lol_list_summoner_matches",
                "arguments": {
                    "game_name": game_name, "tag_line": tag_line,
                    "region": region.upper(), "limit": 20,
                    "desired_output_fields": [
                        "data.game_history[].created_at",
                        "data.game_history[].game_type",
                        "data.game_history[].participants[].champion_name",
                        "data.game_history[].participants[].stats.result",
                    ],
                },
            },
        }
        try:
            r = requests.post(
                "https://mcp-api.op.gg/mcp", json=body, timeout=20,
                headers={"Accept": "application/json, text/event-stream"},
            )
            r.raise_for_status()
            text = r.json()["result"]["content"][0]["text"]
        except Exception as e:
            _dbg(f"[opgg] match history fetch failed: {e}")
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(days=182)
        agg: dict = {}   # champ name -> [games, wins]
        # Each game: GameHistory("<created_at>","<game_type>",[Participant("<champ>",Stats("<result>"),...)],"id")
        pat = _re.compile(
            r'GameHistory\("([^"]+)","([^"]*)",\[Participant\("([^"]+)",'
            r'Stats\("([^"]*)"\)'
        )
        for mo in pat.finditer(text):
            created, _gtype, champ, result = mo.groups()
            try:
                when = datetime.fromisoformat(created)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < cutoff:
                    continue   # older than 6 months
            except Exception:
                pass   # unparseable date — count it rather than drop it
            res = result.upper()
            if res == "UNKNOWN":
                continue   # remake / no result — not a real game
            gw = agg.setdefault(champ, [0, 0])
            gw[0] += 1
            if res == "WIN":
                gw[1] += 1
        return {name: (g, w) for name, (g, w) in agg.items() if g > 0}

    # ── Champion portrait icons (for the squishy pick/ban chips) ───────────────
    def request_icon_prefetch(self):
        """Fetch+cache icons for any champion currently on a list that isn't
        cached yet. Safe to call often — it's a cheap no-op once everything
        referenced is already cached."""
        threading.Thread(target=self._prefetch_icons, daemon=True).start()

    def _prefetch_icons(self):
        ids = set()
        for role in ROLES:
            for key in ("picks", "bans"):
                ids.update(int(c) for c in self.cfg["roleChampions"][role][key])
        ids.update(int(c) for c in self.cfg.get("permaBans", []))
        got_new = False
        for cid in ids:
            if cid in self._icon_images or cid in self._icon_pil_cache:
                continue
            pil_img = self._load_circular_icon(cid)
            if pil_img is not None:
                self._icon_pil_cache[cid] = pil_img
                got_new = True
        if got_new:
            self.after(0, self._materialize_icons)

    def _load_circular_icon(self, cid, size=None):
        """Off-main-thread: download/cache the raw icon then mask it into a
        circular RGBA PIL image sized to the chip's diameter, with an
        anti-aliased (supersampled) circular edge. None on failure."""
        try:
            from PIL import Image, ImageDraw
            if size is None:
                size = ChampionChip.HEIGHT
            path = self.ddragon.icon_file(cid)
            if not path:
                return None
            im = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
            ss = 4
            mask = Image.new("L", (size * ss, size * ss), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size * ss - 1, size * ss - 1), fill=255)
            mask = mask.resize((size, size), Image.LANCZOS)   # smooth circle edge
            im.putalpha(mask)
            return im
        except Exception:
            return None

    def _materialize_icons(self):
        """Main thread only: PhotoImage objects must be created on the Tk
        thread. Turns any pending PIL images into real PhotoImages, then
        refreshes the champion lists so the new icons actually show up."""
        try:
            from PIL import ImageTk
        except Exception:
            self._icon_pil_cache.clear()
            return
        for cid, pil_img in list(self._icon_pil_cache.items()):
            if cid not in self._icon_images:
                try:
                    self._icon_images[cid] = ImageTk.PhotoImage(pil_img)
                except Exception:
                    pass
        self._icon_pil_cache.clear()
        self._refresh_all()

    def get_champ_icon(self, cid):
        return self._icon_images.get(int(cid))

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=DARK)

        # Top header bar (logo + connection status)
        self._build_header()

        # Body: left sidebar navigation + stacked content pages
        body = tk.Frame(self, bg=DARK)
        body.pack(fill="both", expand=True)

        sidebar = tk.Frame(body, bg=SIDEBAR, width=196)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Thin gold rule separating sidebar from content
        tk.Frame(body, bg=EDGE_GOLD, width=1).pack(side="left", fill="y")

        content = tk.Frame(body, bg=DARK)
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        # Build each page into its own frame, all stacked in cell (0,0).
        pages = [
            ("Dashboard", self._build_dashboard),
            ("Champions", self._build_champions),
            ("Builds",    self._build_builds),
            ("Logs",      self._build_log),
            ("Settings",  self._build_settings),
        ]
        for name, builder in pages:
            frame = tk.Frame(content, bg=DARK)
            frame.grid(row=0, column=0, sticky="nsew")
            self._pages[name] = frame
            builder(frame)

        # Sidebar nav — Settings pinned to the bottom, like the mockup.
        self._nav_button(sidebar, "Dashboard")
        self._nav_button(sidebar, "Champions")
        self._nav_button(sidebar, "Builds")
        self._nav_button(sidebar, "Logs")
        tk.Frame(sidebar, bg=EDGE_GOLD, height=1).pack(
            fill="x", padx=18, pady=(14, 14))
        self._nav_button(sidebar, "Settings")

        self._show_page("Dashboard")

    def _build_header(self):
        hdr = tk.Frame(self, bg=HEADER, height=68)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Hextech emblem: nested diamonds with a teal gem core + corner ticks.
        logo = tk.Canvas(hdr, width=56, height=56, bg=HEADER,
                         highlightthickness=0, bd=0)
        logo.pack(side="left", padx=(22, 12), pady=6)
        cx, cy = 28, 28
        logo.create_polygon(cx, 4, 52, cy, cx, 52, 4, cy,
                            outline=GOLD, fill="", width=2)
        logo.create_polygon(cx, 12, 44, cy, cx, 44, 12, cy,
                            outline=EDGE_GOLD, fill="", width=1)
        logo.create_polygon(cx, 20, 36, cy, cx, 36, 20, cy,
                            outline=GOLD, fill=TEAL)
        # tiny gold ticks at the four outer points
        for (x, y) in ((cx, 4), (52, cy), (cx, 52), (4, cy)):
            logo.create_oval(x - 1, y - 1, x + 1, y + 1, fill=BRIGHT_GOLD,
                             outline=BRIGHT_GOLD)

        tk.Label(hdr, text="LOL CLIENT TOOL", bg=HEADER, fg=TEXT_BRIGHT,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(hdr, text=f"v{APP_VERSION}", bg=HEADER, fg=GOLD,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 0), pady=(6, 0))

        # Right side: three status readouts, all the same size & style.
        # Order (right→left): Client · Relay · Ping.
        self._lbl_conn = tk.Label(hdr, text="● Client: waiting",
                                  bg=HEADER, fg=MUTED, font=FONT_STATUS)
        self._lbl_conn.pack(side="right", padx=(0, 24))
        self._lbl_relay = tk.Label(hdr, text="● Relay: not set",
                                   bg=HEADER, fg=MUTED, font=FONT_STATUS)
        self._lbl_relay.pack(side="right", padx=(0, 18))
        self._lbl_ping = tk.Label(hdr, text="● Ping: —", bg=HEADER, fg=MUTED,
                                  font=FONT_STATUS)
        self._lbl_ping.pack(side="right", padx=(0, 18))

        # Gold double-rule beneath the header (bright over dim).
        tk.Frame(self, bg=GOLD,      height=1).pack(fill="x")
        tk.Frame(self, bg=EDGE_GOLD, height=1).pack(fill="x")

    def _nav_button(self, sidebar, name):
        row = tk.Frame(sidebar, bg=SIDEBAR, height=52)
        row.pack(fill="x")
        row.pack_propagate(False)
        accent = tk.Frame(row, bg=SIDEBAR, width=3)
        accent.pack(side="left", fill="y")
        lbl = tk.Label(row, text=name, bg=SIDEBAR, fg=MUTED,
                       font=("Segoe UI", 12), anchor="w")
        lbl.pack(side="left", fill="both", expand=True, padx=(21, 0))
        self._nav_rows[name] = (row, accent, lbl)

        def _enter(_e):
            if self._active_page != name:
                row.config(bg=NAV_ACTIVE); lbl.config(bg=NAV_ACTIVE, fg=TEXT)
        def _leave(_e):
            if self._active_page != name:
                row.config(bg=SIDEBAR); lbl.config(bg=SIDEBAR, fg=MUTED)
        for w in (row, lbl, accent):
            w.bind("<Button-1>", lambda _e, n=name: self._show_page(n))
            w.bind("<Enter>", _enter)
            w.bind("<Leave>", _leave)

    _active_page = None

    def _show_page(self, name):
        self._active_page = name
        for n, (row, accent, lbl) in self._nav_rows.items():
            on = (n == name)
            row.config(bg=NAV_ACTIVE if on else SIDEBAR)
            accent.config(bg=GOLD if on else SIDEBAR)
            lbl.config(bg=NAV_ACTIVE if on else SIDEBAR,
                       fg=GOLD if on else MUTED,
                       font=("Segoe UI", 12, "bold") if on
                            else ("Segoe UI", 12))
        self._pages[name].tkraise()

    def _build_champions(self, parent):
        # ── Inline ornamented header: diamond + CHAMPIONS + rule + op.gg ───────
        head = tk.Frame(parent, bg=DARK)
        head.pack(fill="x", padx=30, pady=(20, 14))
        tk.Button(head, text="⭳  op.gg Auto-fill", bg=BTN_BG, fg=GOLD,
                  activebackground=BTN_HOV, activeforeground=GOLD, relief="flat",
                  cursor="hand2", padx=14, pady=5, font=FONT_BTN,
                  command=self._open_opgg_dialog).pack(side="right", padx=(14, 0))
        dia = tk.Canvas(head, width=12, height=12, bg=DARK,
                        highlightthickness=0, bd=0)
        dia.create_polygon(6, 0, 12, 6, 6, 12, 0, 6, fill=GOLD, outline=GOLD)
        dia.pack(side="left", padx=(0, 9), pady=(3, 0))
        tk.Label(head, text="CHAMPIONS", bg=DARK, fg=GOLD,
                 font=FONT_SECTION).pack(side="left")
        tk.Frame(head, bg=EDGE_GOLD, height=1).pack(
            side="left", fill="x", expand=True, padx=(14, 14))

        # ── Role tabs — the highlighted tab marks the active role. ─────────────
        sel = tk.Frame(parent, bg=DARK)
        sel.pack(fill="x", padx=30, pady=(0, 14))
        tk.Label(sel, text="EDITING ROLE", bg=DARK, fg=FAINT,
                 font=FONT_HINT).pack(side="left", padx=(0, 12), pady=(4, 0))

        # ── Permaban panel — same card language as the pick/ban lists. ─────────
        pb_card = HexCard(parent, fill=CARD, border=CARD_BORDER, autofit=True)
        pb_card.pack(fill="x", padx=30, pady=(0, 4))
        pbb = pb_card.body

        pb_hdr = tk.Frame(pbb, bg=CARD)
        pb_hdr.pack(fill="x", padx=16, pady=(12, 0))
        pdia = tk.Canvas(pb_hdr, width=11, height=11, bg=CARD,
                         highlightthickness=0, bd=0)
        pdia.create_polygon(5, 0, 11, 5, 5, 11, 0, 5, fill=RED, outline=RED)
        pdia.pack(side="left", padx=(0, 8), pady=(3, 0))
        tk.Label(pb_hdr, text="PERMABAN", bg=CARD, fg=RED,
                 font=FONT_SECTION).pack(side="left")
        tk.Label(pb_hdr, text="banned first every game · any role", bg=CARD,
                 fg=FAINT, font=FONT_HINT).pack(side="left", padx=(10, 0),
                                                pady=(4, 0))

        # Add row: entry + button, with the autocomplete dropdown below it.
        pg = tk.Frame(pbb, bg=CARD)
        pg.pack(fill="x", padx=16, pady=(10, 0))
        pg.columnconfigure(1, weight=1)

        self._perma_var = tk.StringVar()
        tk.Label(pg, text="Add:", bg=CARD, fg=TEXT,
                 font=FONT_SMALL).grid(row=0, column=0, sticky="w", pady=(2, 0))
        pe = tk.Entry(pg, textvariable=self._perma_var, bg=FIELD_BG, fg=WHITE,
                      relief="flat", insertbackground=WHITE, font=FONT_SMALL,
                      highlightthickness=1, highlightbackground=EDGE_GOLD,
                      highlightcolor=GOLD)
        pe.grid(row=0, column=1, sticky="we", padx=8, ipady=4)
        pe.bind("<Return>", lambda *_: self._perma_enter())
        tk.Button(pg, text="＋ Add", bg=BTN_BG, fg=GOLD, width=8,
                  activebackground=BTN_HOV, activeforeground=GOLD,
                  relief="flat", cursor="hand2", font=FONT_BTN,
                  command=self._add_permaban).grid(row=0, column=2, sticky="we")

        # Live autocomplete dropdown — grid row 1, shown only while typing.
        self._perma_ac = tk.Listbox(pg, bg=FIELD_BG, fg=WHITE,
                                    selectbackground=RED, selectforeground=WHITE,
                                    relief="flat", height=5, font=FONT_SMALL,
                                    highlightthickness=1, highlightbackground=EDGE_GOLD)
        self._perma_var.trace_add("write", self._perma_ac_update)
        self._perma_ac.bind("<<ListboxSelect>>", self._perma_ac_select)

        # Current permabans as squishy champion chips (portraits + drag-reorder
        # + per-chip × remove) — same interface as the pick/ban lists, no stats.
        self._perma_list = ChampionList(
            pbb, RED, bg=CARD, get_icon=self.get_champ_icon, autosize=True,
            on_reorder=self._reorder_permabans, on_remove=self._remove_permaban)
        self._perma_list.pack(fill="x", padx=16, pady=(8, 12))

        # ── Per-role pick / ban lists ──────────────────────────────────────────
        holder = tk.Frame(parent, bg=DARK)
        holder.pack(fill="both", expand=True)

        self._role_frames: dict = {}
        self._role_btns:   dict = {}
        for role in ROLES:
            rf = tk.Frame(holder, bg=DARK)
            self._role_frames[role] = rf
            self._role_panels[role] = RolePanel(rf, role, self)

        def show_role(role):
            for rf in self._role_frames.values():
                rf.pack_forget()
            self._role_frames[role].pack(fill="both", expand=True)
            for r, b in self._role_btns.items():
                active = (r == role)
                b.config(bg=(GOLD if active else BTN_BG),
                         fg=(DARK if active else TEXT))

        for role in ROLES:
            b = tk.Button(sel, text=ROLE_LABEL[role], bg=BTN_BG, fg=TEXT,
                          activebackground=_shade(GOLD, 1.05), relief="flat",
                          cursor="hand2", padx=18, pady=6, font=FONT_BTN,
                          command=lambda r=role: show_role(r))
            b.pack(side="left", padx=(0, 6))
            self._role_btns[role] = b

        show_role(ROLES[0])   # default to Top

    # ── Dashboard helpers ──────────────────────────────────────────────────────
    def _section_header(self, parent, text, padx=0, pady=(0, 12)):
        """LoL-style header: gold diamond + gold uppercase label + gold rule."""
        row = tk.Frame(parent, bg=DARK)
        row.pack(fill="x", padx=padx, pady=pady)
        dia = tk.Canvas(row, width=12, height=12, bg=DARK,
                        highlightthickness=0, bd=0)
        dia.create_polygon(6, 0, 12, 6, 6, 12, 0, 6, fill=GOLD, outline=GOLD)
        dia.pack(side="left", padx=(0, 9), pady=(3, 0))
        tk.Label(row, text=text.upper(), bg=DARK, fg=GOLD,
                 font=FONT_SECTION).pack(side="left")
        tk.Frame(row, bg=EDGE_GOLD, height=1).pack(
            side="left", fill="x", expand=True, padx=(14, 0))
        return row

    def _build_dashboard(self, parent):
        self._party_ready = False
        self._run_state   = "disabled"
        pad = tk.Frame(parent, bg=DARK)
        pad.pack(fill="both", expand=True, padx=30, pady=(20, 16))

        # ── Control bar: single angular hextech Start/Stop button ──────────────
        ctrl = tk.Frame(pad, bg=DARK)
        ctrl.pack(fill="x", pady=(0, 18))
        self._run_btn = HexButton(ctrl, command=self._on_run_click,
                                  width=220, height=46, bg=DARK)
        self._run_btn.pack(side="left")

        # ── ACTIVE AUTOMATIONS — angular cards with pill toggles ───────────────
        self._section_header(pad, "Active Automations")
        grid = tk.Frame(pad, bg=DARK)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1, uniform="cards")
        grid.columnconfigure(1, weight=1, uniform="cards")
        # Each card is (title, description, toggles). `toggles` is a list of
        # (sub-label, config key): one entry = a plain card (title + toggle +
        # description); two entries = a split card with a labelled toggle per row.
        _auto_items = [
            ("Auto Accept",        "Accept found matches",
             [("", "autoAccept")]),
            ("Auto Pick & Ban",    None,
             [("Pick", "autoPick"), ("Ban", "autoBan")]),
            ("Auto Pre-Pick",      "Hover your pre-pick",
             [("", "autoPrePick")]),
            ("Auto Runes & Spells", None,
             [("Runes", "autoRunes"), ("Spells", "autoSpells")]),
            ("Auto Item Set",      "Import op.gg item set",
             [("", "autoItems")]),
            ("Auto Invites",       "Accept friend invites",
             [("", "autoAcceptInvites")]),
        ]
        for r in range((len(_auto_items) + 1) // 2):
            grid.rowconfigure(r, weight=1, uniform="rows")

        def _toggle(parent, subkey):
            on = bool(self.cfg.get(subkey, DEFAULT_CONFIG.get(subkey, True)))
            sw = ToggleSwitch(parent, initial=on, bg=CARD,
                              command=lambda v, k=subkey: self._on_auto_switch(k, v))
            self._auto_switches[subkey] = sw
            return sw

        for i, (title, desc, toggles) in enumerate(_auto_items):
            row, col = divmod(i, 2)
            card = HexCard(grid, fill=CARD, border=CARD_BORDER, height=104)
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
            # Vertically-centred content so cards stay balanced as they grow.
            inner = tk.Frame(card.body, bg=CARD)
            inner.pack(expand=True, fill="x", padx=18)

            if len(toggles) == 1:
                # Plain card: title (left) + toggle (right edge), description below.
                top = tk.Frame(inner, bg=CARD)
                top.pack(fill="x")
                tk.Label(top, text=title, bg=CARD, fg=TEXT_BRIGHT,
                         font=FONT_TITLE).pack(side="left")
                _toggle(top, toggles[0][1]).pack(side="right")
                if desc:
                    tk.Label(inner, text=desc, bg=CARD, fg=MUTED,
                             font=FONT_LABEL).pack(anchor="w", pady=(5, 0))
            else:
                # Split card: two vertically-stacked toggles, each toggle at the
                # right edge (aligned with the other cards) with its label right
                # beside it.
                tk.Label(inner, text=title, bg=CARD, fg=TEXT_BRIGHT,
                         font=FONT_TITLE).pack(anchor="w")
                for sublabel, subkey in toggles:
                    r_ = tk.Frame(inner, bg=CARD)
                    r_.pack(fill="x", pady=(6, 0))
                    _toggle(r_, subkey).pack(side="right")
                    tk.Label(r_, text=sublabel, bg=CARD, fg=MUTED,
                             font=FONT_LABEL).pack(side="right", padx=(0, 8))

        self._set_run_state("disabled")

    # ── Builds tab (op.gg champion analysis, diamond+) ──────────────────────────
    _ABILITY_COLORS = {"Q": TEAL, "W": GREEN, "E": GOLD, "R": RED}

    def _build_builds(self, parent):
        # Inline ornamented header + "diamond+" note on the right.
        head = tk.Frame(parent, bg=DARK)
        head.pack(fill="x", padx=30, pady=(14, 8))
        tk.Label(head, text="◆  DIAMOND+ · RANKED", bg=DARK, fg=FAINT,
                 font=FONT_HINT).pack(side="right", padx=(12, 0), pady=(3, 0))
        dia = tk.Canvas(head, width=12, height=12, bg=DARK,
                        highlightthickness=0, bd=0)
        dia.create_polygon(6, 0, 12, 6, 6, 12, 0, 6, fill=GOLD, outline=GOLD)
        dia.pack(side="left", padx=(0, 9), pady=(3, 0))
        tk.Label(head, text="BUILDS", bg=DARK, fg=GOLD,
                 font=FONT_SECTION).pack(side="left")
        tk.Frame(head, bg=EDGE_GOLD, height=1).pack(
            side="left", fill="x", expand=True, padx=(14, 14))

        # ── Controls: champion search + position tabs + actions ────────────────
        ctl = tk.Frame(parent, bg=DARK)
        ctl.pack(fill="x", padx=30, pady=(0, 8))
        tk.Label(ctl, text="Champion:", bg=DARK, fg=TEXT,
                 font=FONT_SMALL).pack(side="left", padx=(0, 6))
        self._build_champ_var = tk.StringVar()
        bentry = tk.Entry(ctl, textvariable=self._build_champ_var, width=18,
                          bg=FIELD_BG, fg=WHITE, relief="flat", insertbackground=WHITE,
                          font=FONT_SMALL, highlightthickness=1,
                          highlightbackground=EDGE_GOLD, highlightcolor=GOLD)
        bentry.pack(side="left", ipady=3)
        bentry.bind("<Return>", lambda *_: self._build_entry_return())
        tk.Button(ctl, text="Search", bg=BTN_BG, fg=GOLD, activebackground=BTN_HOV,
                  activeforeground=GOLD, relief="flat", cursor="hand2", font=FONT_BTN,
                  padx=12, pady=3, command=self._load_build).pack(side="left", padx=(8, 0))
        tk.Button(ctl, text="⤓ Current pick", bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_HOV, activeforeground=WHITE, relief="flat",
                  cursor="hand2", font=FONT_BTN, padx=12, pady=3,
                  command=self._load_current_pick).pack(side="left", padx=(8, 0))

        # Position tabs
        posrow = tk.Frame(parent, bg=DARK)
        posrow.pack(fill="x", padx=30, pady=(0, 10))
        tk.Label(posrow, text="POSITION", bg=DARK, fg=FAINT,
                 font=FONT_HINT).pack(side="left", padx=(0, 12), pady=(4, 0))
        self._build_pos_var  = tk.StringVar(value="mid")
        self._build_pos_btns = {}
        for label, pos in [("Top", "top"), ("Jungle", "jungle"), ("Mid", "mid"),
                           ("ADC", "adc"), ("Support", "support")]:
            b = tk.Button(posrow, text=label, bg=BTN_BG, fg=TEXT,
                          activebackground=_shade(GOLD, 1.05), relief="flat",
                          cursor="hand2", padx=14, pady=5, font=FONT_BTN,
                          command=lambda p=pos: self._set_build_pos(p))
            b.pack(side="left", padx=(0, 6))
            self._build_pos_btns[pos] = b
        self._set_build_pos("mid")

        # Autocomplete dropdown — floated over the result area with place() (not
        # packed) so showing/hiding it never shoves the build cards up or down.
        self._build_entry = bentry
        self._build_ac = tk.Listbox(parent, bg=PANEL, fg=WHITE, width=22,
                                    selectbackground=GOLD, selectforeground=DARK,
                                    relief="flat", height=6, font=FONT_SMALL,
                                    highlightthickness=1, highlightbackground=EDGE_GOLD)
        self._build_champ_var.trace_add("write", self._build_ac_update)
        self._build_ac.bind("<<ListboxSelect>>", self._build_ac_select)

        # ── Result area (scrollable) ───────────────────────────────────────────
        canvas = tk.Canvas(parent, bg=DARK, highlightthickness=0)
        vsb = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._build_result = tk.Frame(canvas, bg=DARK)
        win = canvas.create_window((0, 0), window=self._build_result, anchor="nw")
        self._build_result.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self._build_placeholder("Search a champion above, or load your current "
                                "champ-select pick.")

    def _set_build_pos(self, pos):
        self._build_pos_var.set(pos)
        for p, b in self._build_pos_btns.items():
            on = (p == pos)
            b.config(bg=(GOLD if on else BTN_BG), fg=(DARK if on else TEXT))

    def _build_ac_update(self, *_):
        if not hasattr(self, "_build_ac"):
            return
        q = self._build_champ_var.get().lower().strip()
        self._build_ac.delete(0, "end")
        if not q:
            self._build_ac.place_forget()
            return
        hits = [n for n in self.ddragon.all_display_names() if q in n.lower()][:6]
        if not hits:
            self._build_ac.place_forget()
            return
        for h in hits:
            self._build_ac.insert("end", "  " + h)
        self._build_ac.configure(height=len(hits))
        # Float just below the entry, overlaying the results.
        self._build_ac.place(in_=self._build_entry, x=0, y=2, rely=1.0)
        tk.Misc.tkraise(self._build_ac)

    def _build_ac_select(self, _evt):
        sel = self._build_ac.curselection()
        if sel:
            self._build_champ_var.set(self._build_ac.get(sel[0]).strip())
            self._build_ac.place_forget()
            self._load_build()

    def _build_entry_return(self):
        """Enter in the champion box: if the suggestion list is showing, load the
        top suggestion; otherwise load whatever was typed."""
        ac = getattr(self, "_build_ac", None)
        if ac is not None and ac.winfo_ismapped() and ac.size() > 0:
            top = ac.get(0).strip()
            self._build_champ_var.set(top)
            ac.place_forget()
            self._load_build(top)
        else:
            self._load_build()

    def _assigned_role_pos(self):
        """The op.gg position for the local player's assigned role in champ
        select, pulled live from the client, or None if not applicable."""
        try:
            if not getattr(self._lcu, "_sess", None):
                return None
            r = self._lcu.get("/lol-champ-select/v1/session")
            if r.status_code != 200:
                return None
            s = r.json()
            cell = s.get("localPlayerCellId")
            for mbr in s.get("myTeam", []):
                if mbr.get("cellId") == cell:
                    assigned = (mbr.get("assignedPosition") or "").lower()
                    return OPGG_POSITION.get(assigned) or None
        except Exception:
            return None
        return None

    def _build_placeholder(self, msg):
        for w in self._build_result.winfo_children():
            w.destroy()
        tk.Label(self._build_result, text=msg, bg=DARK, fg=FAINT,
                 font=FONT_LABEL, wraplength=560, justify="left").pack(
            anchor="w", padx=30, pady=20)

    def _load_current_pick(self):
        pick = self._champ_select_pick()
        if not pick:
            self._build_placeholder("Not in champion select — pick or hover a "
                                    "champion, or search one above.")
            return
        name, pos = pick
        self._build_champ_var.set(name)
        if hasattr(self, "_build_ac"):
            self._build_ac.place_forget()
        if pos:
            self._set_build_pos(pos)
        self._load_build(name, pos or self._build_pos_var.get())

    def _load_build(self, champ=None, position=None):
        champ = (champ or self._build_champ_var.get()).strip()
        if not champ:
            return
        if hasattr(self, "_build_ac"):
            self._build_ac.place_forget()
        # When importing/loading a build during champ select, reflect the role
        # the client assigned us on the Position indicator (unless the caller
        # already supplied a position, e.g. current pick / hover).
        if position is None:
            position = self._assigned_role_pos()
        position = position or self._build_pos_var.get()
        self._set_build_pos(position)   # keep the indicator in sync with the load
        # Sequence token: if a newer request starts before this fetch returns
        # (e.g. the hovered champion changed), the stale result is discarded.
        self._build_req = getattr(self, "_build_req", 0) + 1
        seq = self._build_req
        self._build_placeholder(f"Loading {champ} ({position}) build from op.gg…")
        threading.Thread(target=self._load_build_worker,
                         args=(champ, position, seq), daemon=True).start()

    def _load_build_worker(self, champ, position, seq=0):
        data = self._fetch_build(champ, position)
        # Warm the summoner-spell icon disk cache off the main thread so the
        # render can build PhotoImages from local files without any network.
        for combo in (data or {}).get("combos", []):
            for sid in combo.get("spell_ids", []):
                self.ddragon.spell_icon_file(sid)
        def _apply():
            if seq == getattr(self, "_build_req", seq):
                self._render_build(data)
        self.after(0, _apply)

    def _spell_photo(self, sid, size=22):
        """A cached Tk PhotoImage for a summoner spell icon, or None. Reads the
        already-cached PNG (warmed by _load_build_worker); must run on the main
        thread because PhotoImage creation is Tk-thread-only."""
        key = (int(sid), size)
        if key in self._spell_icons:
            return self._spell_icons[key]
        try:
            from PIL import Image, ImageTk
            path = self.ddragon.spell_icon_file(sid)
            if not path:
                return None
            im = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(im)
            self._spell_icons[key] = photo
            return photo
        except Exception:
            return None

    def _ability_badges(self, parent, letters, sep):
        row = tk.Frame(parent, bg=CARD)
        for i, a in enumerate(letters):
            if i:
                tk.Label(row, text=sep, bg=CARD, fg=FAINT,
                         font=FONT_SMALL).pack(side="left", padx=2)
            tk.Label(row, text=a, bg=CARD,
                     fg=self._ABILITY_COLORS.get(a, TEXT),
                     font=("Segoe UI", 11, "bold")).pack(side="left")
        return row

    def _render_build(self, data):
        for w in self._build_result.winfo_children():
            w.destroy()
        if not data or data.get("error"):
            tk.Label(self._build_result,
                     text=(data or {}).get("error", "No build data."),
                     bg=DARK, fg=RED, font=FONT_LABEL, wraplength=560,
                     justify="left").pack(anchor="w", padx=30, pady=20)
            return

        pad = tk.Frame(self._build_result, bg=DARK)
        pad.pack(fill="x", padx=30, pady=(4, 12))

        def card(host, title):
            self._section_header(host, title, pady=(10, 6))
            c = HexCard(host, fill=CARD, border=CARD_BORDER, autofit=True)
            c.pack(fill="x")
            inner = tk.Frame(c.body, bg=CARD)
            inner.pack(fill="both", expand=True, padx=14, pady=9)
            return inner

        def kv(parent, label, value, value_fg=TEXT_BRIGHT, wrap=460):
            r = tk.Frame(parent, bg=CARD)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=CARD, fg=MUTED, font=FONT_SMALL,
                     width=11, anchor="w").pack(side="left")
            tk.Label(r, text=value, bg=CARD, fg=value_fg, font=FONT_LABEL,
                     anchor="w", justify="left", wraplength=wrap).pack(
                side="left", fill="x", expand=True)
            return r

        # ── Champion summary (full-width stat strip) ───────────────────────────
        wr = data.get("win_rate")
        pr = data.get("pick_rate")
        br = data.get("ban_rate")
        summ = card(pad, f"{data['champion']} · {data['position'].upper()}")
        line = tk.Frame(summ, bg=CARD)
        line.pack(fill="x")
        def stat(lbl, val, fg=TEXT_BRIGHT):
            box = tk.Frame(line, bg=CARD)
            box.pack(side="left", padx=(0, 24))
            tk.Label(box, text=lbl, bg=CARD, fg=FAINT, font=FONT_HINT).pack(anchor="w")
            tk.Label(box, text=val, bg=CARD, fg=fg,
                     font=("Segoe UI", 13, "bold")).pack(anchor="w")
        if wr is not None:
            stat("WIN RATE", f"{round(wr*100)}%", GREEN if wr >= 0.5 else RED)
        if pr is not None:
            stat("PICK RATE", f"{round(pr*100, 1)}%")
        if br is not None:
            stat("BAN RATE", f"{round(br*100, 1)}%")
        if data.get("damage"):
            stat("DAMAGE", data["damage"])

        # ── Runes + spells: up to 3 popular combos, each applies to the client ─
        rn = card(pad, "Runes & Spells")
        combos = data.get("combos") or []
        if combos:
            tk.Label(rn, text="Choose a combo to push its runes & summoner "
                     "spells to your client:", bg=CARD, fg=MUTED,
                     font=FONT_SMALL).pack(anchor="w", pady=(0, 2))
            for combo in combos:
                self._render_combo_row(rn, combo)
            self._build_combo_status = tk.Label(
                rn, text="", bg=CARD, fg=MUTED, font=FONT_SMALL,
                anchor="w", justify="left", wraplength=560)
            self._build_combo_status.pack(anchor="w", pady=(4, 0))
        else:
            # Fallback: op.gg gave a single set (no combo IDs) — show it read-only.
            self._build_combo_status = None
            if data.get("rune_primary"):
                kv(rn, data.get("rune_primary_page", "Primary"),
                   " · ".join(data["rune_primary"]), TEAL)
            if data.get("rune_secondary"):
                kv(rn, data.get("rune_secondary_page", "Secondary"),
                   " · ".join(data["rune_secondary"]), MUTED)
            if data.get("spells"):
                kv(rn, "Spells", "  +  ".join(data["spells"]), GOLD)

        # ── Build + Ability order side by side (use the horizontal space) ──────
        cols = tk.Frame(pad, bg=DARK)
        cols.pack(fill="x")
        colA = tk.Frame(cols, bg=DARK)
        colA.pack(side="left", fill="both", expand=True, padx=(0, 10))
        colB = tk.Frame(cols, bg=DARK)
        colB.pack(side="left", fill="both", expand=True)

        it = card(colA, "Build")
        if data.get("starter"):
            kv(it, "Starting", ", ".join(data["starter"]), wrap=210)
        if data.get("core"):
            kv(it, "Core", "  →  ".join(data["core"]), GOLD, wrap=210)
        if data.get("boots"):
            kv(it, "Boots", ", ".join(data["boots"]), wrap=210)
        if data.get("situational"):
            kv(it, "Situational", ", ".join(data["situational"]), MUTED, wrap=210)

        sk = card(colB, "Ability Order")
        order = data.get("skill_order") or []
        maxo  = data.get("max_order") or []
        if order:
            r = tk.Frame(sk, bg=CARD); r.pack(fill="x", pady=1)
            tk.Label(r, text="Levels 1-3", bg=CARD, fg=MUTED, font=FONT_SMALL,
                     width=11, anchor="w").pack(side="left")
            self._ability_badges(r, order[:3], "→").pack(side="left")
        if maxo:
            r = tk.Frame(sk, bg=CARD); r.pack(fill="x", pady=1)
            tk.Label(r, text="Level 4+ max", bg=CARD, fg=MUTED, font=FONT_SMALL,
                     width=11, anchor="w").pack(side="left")
            self._ability_badges(r, maxo, ">").pack(side="left")
        if order:
            tk.Label(sk, text="Full order", bg=CARD, fg=MUTED, font=FONT_SMALL,
                     anchor="w").pack(anchor="w", pady=(6, 0))
            grid = tk.Frame(sk, bg=CARD); grid.pack(anchor="w", pady=(2, 0))
            for i, a in enumerate(order):
                col = tk.Frame(grid, bg=CARD)
                col.grid(row=0, column=i, padx=1)
                tk.Label(col, text=a, bg=CARD,
                         fg=self._ABILITY_COLORS.get(a, TEXT),
                         font=("Segoe UI", 10, "bold")).pack()
                tk.Label(col, text=str(i + 1), bg=CARD, fg=FAINT,
                         font=("Segoe UI", 7)).pack()

    def _render_combo_row(self, parent, combo):
        """One selectable spell + rune-page combo with an Apply button."""
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(2, 2))

        tk.Button(row, text="Apply", bg=BTN_BG, fg=GOLD, activebackground=BTN_HOV,
                  activeforeground=GOLD, relief="flat", cursor="hand2", font=FONT_BTN,
                  padx=12, pady=4,
                  command=lambda c=combo: self._apply_combo(c)).pack(
            side="left", padx=(0, 10))

        # Summoner-spell icons to the right of the build (fall back to names).
        spells = tk.Frame(row, bg=CARD)
        spells.pack(side="right", padx=(8, 2))
        for sid in combo.get("spell_ids", []):
            photo = self._spell_photo(sid)
            if photo is not None:
                sl = tk.Label(spells, image=photo, bg=CARD, bd=0)
                sl.image = photo   # keep a reference so Tk won't GC it
                sl.pack(side="left", padx=1)
            else:
                tk.Label(spells, text=_spell_name(sid), bg=CARD, fg=TEXT_BRIGHT,
                         font=FONT_SMALL).pack(side="left", padx=2)

        info = tk.Frame(row, bg=CARD)
        info.pack(side="left", fill="x", expand=True)

        # Header line: tier label · pick-rate (spells shown as icons at right)
        head = tk.Frame(info, bg=CARD)
        head.pack(fill="x", anchor="w")
        tk.Label(head, text=combo.get("label", ""), bg=CARD, fg=GOLD,
                 font=FONT_BTN).pack(side="left")
        pr = combo.get("pick_rate")
        if isinstance(pr, (int, float)):
            tk.Label(head, text=f"· {round(pr*100)}%", bg=CARD, fg=FAINT,
                     font=FONT_HINT).pack(side="left", padx=(6, 0))

        # Detail line: keystone + the two rune pages (compact, one line — the
        # spells in the header plus the keystone are what distinguish combos).
        prim = (combo.get("primary_rune_names") or [])
        keystone = prim[0] if prim else ""
        pages = "  +  ".join(p for p in (combo.get("primary_page_name", ""),
                                         combo.get("secondary_page_name", "")) if p)
        detail = tk.Frame(info, bg=CARD)
        detail.pack(fill="x", anchor="w")
        if keystone:
            tk.Label(detail, text=keystone, bg=CARD, fg=TEAL,
                     font=FONT_SMALL).pack(side="left")
        if pages:
            tk.Label(detail, text=("· " if keystone else "") + pages, bg=CARD,
                     fg=MUTED, font=FONT_SMALL).pack(side="left", padx=(6, 0))

    def _set_combo_status(self, text, color):
        lbl = getattr(self, "_build_combo_status", None)
        if lbl is not None:
            try:
                if lbl.winfo_exists():
                    lbl.config(text=text, fg=color)
            except tk.TclError:
                pass

    def _apply_combo(self, combo):
        """Push the chosen combo's runes + summoner spells to the League client."""
        if not getattr(self._lcu, "_sess", None):
            self._set_combo_status("Open the League client first to apply this.", RED)
            return
        self._set_combo_status("Applying runes & summoner spells…", MUTED)
        threading.Thread(target=self._apply_combo_worker,
                         args=(combo,), daemon=True).start()

    def _apply_rune_page(self, name, prim, sub, perks):
        """Create a rune page and make it current, replacing any prior page we
        created and freeing a slot if the client is at its page limit."""
        try:
            pages = self._lcu.get("/lol-perks/v1/pages").json()
        except Exception:
            pages = []
        for p in pages if isinstance(pages, list) else []:
            if (str(p.get("name", "")).startswith("op.gg:")
                    and (p.get("isDeletable") or p.get("isEditable"))):
                self._lcu.delete(f"/lol-perks/v1/pages/{p.get('id')}")
        self._engine._make_rune_room()   # frees a slot if still at the limit
        return self._lcu.post("/lol-perks/v1/pages", {
            "name":            name,
            "primaryStyleId":  int(prim),
            "subStyleId":      int(sub),
            "selectedPerkIds": [int(x) for x in perks],
            "current":         True,
        })

    def _apply_combo_worker(self, combo):
        champ = (self._build_champ_var.get() or "Build").strip()
        runes_ok = spells_in_champ_select = False
        try:
            perks = (combo.get("primary_rune_ids", [])
                     + combo.get("secondary_rune_ids", [])
                     + combo.get("stat_mod_ids", []))
            if combo.get("primary_page_id") and combo.get("secondary_page_id") \
                    and len(perks) >= 6:
                rp = self._apply_rune_page(f"op.gg: {champ}",
                                           combo["primary_page_id"],
                                           combo["secondary_page_id"], perks)
                runes_ok = getattr(rp, "status_code", 0) in (200, 201)

            sids = combo.get("spell_ids", [])
            spells_status = None
            if len(sids) == 2:
                rs = self._lcu.patch("/lol-champ-select/v1/session/my-selection",
                                     {"spell1Id": int(sids[0]), "spell2Id": int(sids[1])})
                spells_status = getattr(rs, "status_code", 0)
                spells_in_champ_select = spells_status in (200, 204)
        except Exception as e:
            self.after(0, lambda: self._set_combo_status(f"Apply failed: {e}", RED))
            return

        if runes_ok and spells_in_champ_select:
            msg, col = "Applied runes + summoner spells ✓", GREEN
        elif runes_ok:
            # Spells only stick during champ select.
            msg = ("Runes applied ✓  —  summoner spells will apply when you're in "
                   "champ select.")
            col = GOLD
        elif spells_in_champ_select:
            msg, col = "Summoner spells applied ✓ (runes page couldn't be created).", GOLD
        else:
            msg, col = "Couldn't apply — is the League client connected?", RED
        self.after(0, lambda: self._set_combo_status(msg, col))

    def _build_log(self, parent):
        wrap = tk.Frame(parent, bg=DARK)
        wrap.pack(fill="both", expand=True, padx=30, pady=(20, 16))
        self._section_header(wrap, "Logs")
        # Row with a shortcut to the detailed debug log file on disk.
        tools = tk.Frame(wrap, bg=DARK)
        tools.pack(fill="x", pady=(0, 6))
        tk.Label(tools, text="Detailed diagnostics are written to debug.log.",
                 bg=DARK, fg=MUTED, font=FONT_SMALL).pack(side="left")
        tk.Button(tools, text="Open Debug Log", bg=BTN_BG, fg=GOLD,
                  activebackground=BTN_HOV, activeforeground=GOLD, relief="flat",
                  cursor="hand2", font=FONT_BTN, padx=12, pady=3,
                  command=self._open_debug_log).pack(side="right")
        # Gold-outlined console panel for consistency with the cards.
        border = tk.Frame(wrap, bg=CARD_BORDER)
        border.pack(fill="both", expand=True)
        self._log_box = scrolledtext.ScrolledText(
            border, height=10, width=72,
            bg=CARD, fg="#aab0b8",
            font=FONT_MONO, relief="flat", bd=0,
            state="disabled", wrap="word",
            insertbackground=WHITE, padx=10, pady=8)
        self._log_box.pack(fill="both", expand=True, padx=1, pady=1)

    def _open_debug_log(self):
        """Open the on-disk debug.log in the default text viewer."""
        try:
            _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            if not _DEBUG_LOG.exists():
                _DEBUG_LOG.write_text("", encoding="utf-8")
            os.startfile(str(_DEBUG_LOG))   # noqa: Windows-only, matches this app
            self.log(f"Opened debug log: {_DEBUG_LOG}")
        except Exception as e:
            self.log(f"Couldn't open debug log ({_DEBUG_LOG}): {e}")

    def _build_settings(self, parent):
        # Save bar pinned to the bottom, above a gold rule.
        savebar = tk.Frame(parent, bg=DARK)
        savebar.pack(side="bottom", fill="x", padx=24, pady=(8, 12))
        tk.Button(savebar, text="Save Config",
                  bg=GOLD, fg=DARK, activebackground=_shade(GOLD, 1.1),
                  relief="flat", cursor="hand2", padx=20, pady=6,
                  font=FONT_BTN, command=self._save).pack(side="right")
        tk.Frame(parent, bg=EDGE_GOLD, height=1).pack(side="bottom", fill="x")

        # Scrollable body so settings stay reachable in a small window.
        canvas = tk.Canvas(parent, bg=DARK, highlightthickness=0)
        vsb = tk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=DARK)
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        def _wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Helper — ornamented header + chamfered card; returns the CARD-bg body.
        def _section(title, first=False):
            self._section_header(body, title, padx=24,
                                 pady=((16 if first else 18), 8))
            card = HexCard(body, fill=CARD, border=CARD_BORDER, autofit=True)
            card.pack(fill="x", padx=24)
            inner = tk.Frame(card.body, bg=CARD)
            inner.pack(fill="both", expand=True, padx=16, pady=14)
            return inner

        # Helper — checkbox on a card surface.
        def _check(parent, text, var, cmd):
            return tk.Checkbutton(
                parent, text=text, variable=var, bg=CARD, fg=TEXT,
                activebackground=CARD, activeforeground=TEXT, selectcolor=PANEL,
                font=FONT_SMALL, anchor="w", command=cmd)

        # Helper — labelled row (label left, widget right).
        def _row(parent, label_text):
            r = tk.Frame(parent, bg=CARD)
            r.pack(anchor="w", pady=(8, 0))
            tk.Label(r, text=label_text, bg=CARD, fg=TEXT,
                     font=FONT_SMALL).pack(side="left")
            return r

        # Helper — hint text below a control.
        def _hint(parent, text):
            tk.Label(parent, text=text, bg=CARD, fg=FAINT,
                     font=FONT_HINT).pack(anchor="w", pady=(5, 0))

        # Helper — recessed entry field (clearly visible on a card).
        def _entry(parent, var, width):
            return tk.Entry(parent, textvariable=var, width=width, bg=FIELD_BG,
                            fg=WHITE, relief="flat", insertbackground=WHITE,
                            font=FONT_SMALL, highlightthickness=1,
                            highlightbackground=EDGE_GOLD, highlightcolor=GOLD)

        # Helper — secondary button (visible on the card surface).
        def _sbtn(parent, text, cmd):
            return tk.Button(parent, text=text, bg=BTN_BG, fg=TEXT,
                             activebackground=BTN_HOV, relief="flat",
                             cursor="hand2", padx=12, pady=4, font=FONT_BTN,
                             command=cmd)

        # ── Overlay ───────────────────────────────────────────────────────────
        s = _section("Overlay", first=True)

        self._overlay_enabled_var = tk.BooleanVar(
            value=bool(self.cfg.get("overlayEnabled", True)))
        _check(s, "Show overlay on League client", self._overlay_enabled_var,
               lambda: (
                   self.cfg.__setitem__(
                       "overlayEnabled", self._overlay_enabled_var.get()),
                   save_config(self.cfg),
               )).pack(anchor="w")

        # Keybind capture widget.
        kb_row = _row(s, "Toggle keybind:")

        raw_key = str(self.cfg.get("overlayToggleKey") or "")
        self._kb_btn = tk.Button(
            kb_row,
            text=f"  {_overlay_combo_label(raw_key)}  ",
            bg=BTN_BG, fg=WHITE, activebackground=BTN_HOV, relief="flat",
            font=FONT_BTN, padx=8, pady=2, cursor="hand2")
        self._kb_btn.pack(side="left", padx=(8, 0))

        def _start_kb_capture():
            self.focus_set()   # pull focus away from any text entry
            self._kb_btn.config(text="  Press a key…  ", fg=MUTED, state="disabled")
            _tid = [None]

            def _commit(combo):
                if _tid[0]:
                    self.after_cancel(_tid[0])
                try:
                    self.unbind("<KeyPress>")
                except Exception:
                    pass
                self.cfg["overlayToggleKey"] = combo
                save_config(self.cfg)
                lbl = _overlay_combo_label(combo) if combo else "None"
                self._kb_btn.config(text=f"  {lbl}  ", fg=WHITE, state="normal")

            def _on_key(ev):
                key = ev.keysym
                if key in _OVERLAY_IGNORE_KEYS:
                    return   # modifier-only press — keep waiting
                # Plain Escape = clear the keybind
                if key == "Escape" and not (ev.state & 0x4) and not (ev.state & 0x1):
                    _commit("")
                    return
                # Build modifier prefix
                mods = []
                if ev.state & 0x4:      mods.append("Ctrl")
                if ev.state & 0x1:      mods.append("Shift")
                if ev.state & 0x20000:  mods.append("Alt")
                # Normalise single-letter keysym to uppercase
                if len(key) == 1:
                    key2 = key.upper()
                else:
                    key2 = key
                combo = "+".join(mods + [key2])
                # Accept only if the key part resolves to a VK code
                if _overlay_key_vk(key2):
                    _commit(combo)
                else:
                    _commit(self.cfg.get("overlayToggleKey") or "")  # restore

            self.bind("<KeyPress>", _on_key)
            _tid[0] = self.after(5000, lambda: _commit(
                self.cfg.get("overlayToggleKey") or ""))   # 5 s timeout

        self._kb_btn.config(command=_start_kb_capture)

        # Hover tooltip on the keybind button.
        _kbt = [None]
        def _kbt_show(e):
            x = self._kb_btn.winfo_rootx()
            y = self._kb_btn.winfo_rooty() + self._kb_btn.winfo_height() + 4
            w = tk.Toplevel(self._kb_btn)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{x}+{y}")
            tk.Label(w,
                     text="Click, then press any key:\n"
                          "  A–Z  ·  0–9  ·  F1–F12\n"
                          "  Home  ·  End  ·  Insert  ·  Delete  ·  PgUp  ·  PgDn\n\n"
                          "Hold Ctrl, Alt, or Shift for combos (e.g. Ctrl+K)\n"
                          "Press Esc to clear the keybind",
                     bg=TIP_BG, fg=TEXT, font=FONT_SMALL,
                     padx=10, pady=8, justify="left",
                     relief="solid", borderwidth=1).pack()
            _kbt[0] = w
        def _kbt_hide(e):
            if _kbt[0]:
                _kbt[0].destroy()
                _kbt[0] = None
        self._kb_btn.bind("<Enter>", _kbt_show)
        self._kb_btn.bind("<Leave>", _kbt_hide)

        _sbtn(s, "Reset overlay position", self._reset_overlay_pos).pack(
            anchor="w", pady=(10, 0))

        # ── Party Ready-Up ────────────────────────────────────────────────────
        s = _section("Party Ready-Up")

        self._ready_up_var = tk.BooleanVar(
            value=bool(self.cfg.get("readyUpEnabled", True)))
        _check(s, "Enabled", self._ready_up_var,
               self._on_ready_up_toggle).pack(anchor="w")

        relay_row = _row(s, "Relay URL:")
        self._relay_var = tk.StringVar(value=str(self.cfg.get("relayUrl", "") or ""))
        self._relay_var.trace_add(
            "write",
            lambda *a: self.cfg.__setitem__("relayUrl", self._relay_var.get().strip()))
        relay_entry = _entry(relay_row, self._relay_var, 36)
        relay_entry.pack(side="left", padx=(8, 0))

        def _persist_relay(*_):
            self.cfg["relayUrl"] = self._relay_var.get().strip()
            save_config(self.cfg)
            self.log("Relay URL saved.")
        relay_entry.bind("<FocusOut>", _persist_relay)
        relay_entry.bind("<Return>",  _persist_relay)
        _hint(s, "e.g. http://your-server-ip:8777  (same for everyone in the party)")

        # ── Auto-Accept Invites ───────────────────────────────────────────────
        s = _section("Auto-Accept Invites")

        self._invite_var = tk.BooleanVar(
            value=bool(self.cfg.get("autoAcceptInvites", False)))
        _check(s, "Accept lobby invites from friends", self._invite_var,
               lambda: self.cfg.__setitem__(
                   "autoAcceptInvites", self._invite_var.get())).pack(anchor="w")

        wl_row = _row(s, "Friends only:")
        whitelist_str = ", ".join(self.cfg.get("inviteWhitelist", []))
        self._invite_whitelist_var = tk.StringVar(value=whitelist_str)
        wl_entry = _entry(wl_row, self._invite_whitelist_var, 36)
        wl_entry.pack(side="left", padx=(8, 0))

        def _persist_whitelist(*_):
            raw = self._invite_whitelist_var.get()
            self.cfg["inviteWhitelist"] = [n.strip() for n in raw.split(",")
                                           if n.strip()]
            save_config(self.cfg)
        wl_entry.bind("<FocusOut>", _persist_whitelist)
        wl_entry.bind("<Return>",   _persist_whitelist)
        _hint(s, "Comma-separated  ·  leave blank to accept from any friend")

        # Tooltip on the whitelist entry showing the name#tag format.
        _tip = [None]
        def _tip_show(e):
            x = wl_entry.winfo_rootx()
            y = wl_entry.winfo_rooty() + wl_entry.winfo_height() + 4
            w = tk.Toplevel(wl_entry)
            w.wm_overrideredirect(True)
            w.wm_geometry(f"+{x}+{y}")
            tk.Label(w,
                     text="Format: SummonerName#TagLine\n"
                          "e.g.  CoolPlayer#NA1, FriendName#EUW",
                     bg=TIP_BG, fg=TEXT, font=FONT_HINT,
                     padx=8, pady=5, justify="left",
                     relief="solid", borderwidth=1).pack()
            _tip[0] = w
        def _tip_hide(e):
            if _tip[0]:
                _tip[0].destroy()
                _tip[0] = None
        wl_entry.bind("<Enter>", _tip_show)
        wl_entry.bind("<Leave>", _tip_hide)

        # ── TFT Left-Click Fix ───────────────────────────────────────────────
        s = _section("TFT Left-Click Fix")

        self._tft_fix_var = tk.BooleanVar(
            value=bool(self.cfg.get("tftFixEnabled", True)))
        _check(s, 'Disable "attack click on left click" in TFT lobbies',
               self._tft_fix_var,
               lambda: (
                   self.cfg.__setitem__("tftFixEnabled", self._tft_fix_var.get()),
                   save_config(self.cfg),
               )).pack(anchor="w")
        _hint(s, "Applied automatically once a Teamfight Tactics lobby or "
                 "queue is detected")

        tft_row = tk.Frame(s, bg=CARD)
        tft_row.pack(anchor="w", pady=(10, 0))
        self._tft_status_var = tk.StringVar()
        tk.Label(tft_row, textvariable=self._tft_status_var, bg=CARD,
                 fg=MUTED, font=FONT_SMALL).pack(side="left", padx=(0, 10))
        _sbtn(tft_row, "Restore normal settings",
              self._on_restore_tft).pack(side="left")
        self._update_tft_status()

        # ── Timings ───────────────────────────────────────────────────────────
        s = _section("Timings  (seconds)")

        g = tk.Frame(s, bg=CARD)
        g.pack(anchor="nw")
        for i, (label, key, note, max_val) in enumerate([
            ("Accept delay:",   "acceptDelay",
             "Wait before auto-accepting a found match",               60),
            ("Pre-pick delay:", "prePickDelay",
             "Wait into champ select before hovering your pre-pick",   60),
            ("Pick delay:",     "pickDelay",
             "Wait after your pick turn starts before locking in",     29),
            ("Ban delay:",      "banDelay",
             "Wait after your ban turn starts before banning",          8),
        ]):
            tk.Label(g, text=label, bg=CARD, fg=TEXT,
                     font=FONT_SMALL).grid(row=i, column=0, sticky="w", pady=4)
            var = tk.DoubleVar(value=round(int(self.cfg.get(key, 1000)) / 1000, 1))
            self._delay_vars[key] = var

            def _on_change(k=key, v=var, mx=max_val):
                self.cfg[k] = int(round(min(v.get(), mx) * 1000))

            tk.Spinbox(g, from_=0, to=max_val, increment=0.5, textvariable=var,
                       width=8, bg=FIELD_BG, fg=WHITE, relief="flat", format="%.1f",
                       buttonbackground=BTN_BG, insertbackground=WHITE,
                       highlightthickness=1, highlightbackground=EDGE_GOLD,
                       command=_on_change).grid(row=i, column=1, padx=(12, 16),
                                                sticky="w")
            tk.Label(g, text=note, bg=CARD, fg=FAINT,
                     font=FONT_HINT).grid(row=i, column=2, sticky="w")

        # ── Stream Deck API ───────────────────────────────────────────────────
        s = _section("Stream Deck API")

        api_row = _row(s, "Port:")
        self._api_port_var = tk.StringVar(
            value=str(self.cfg.get("localApiPort", 8778)))
        api_port_entry = _entry(api_row, self._api_port_var, 8)
        api_port_entry.pack(side="left", padx=(8, 0))

        def _persist_api_port(*_):
            try:
                p = int(self._api_port_var.get())
            except ValueError:
                return
            self.cfg["localApiPort"] = p
            save_config(self.cfg)
            self.log(f"Stream Deck API port saved to {p} — restart to apply.")
        api_port_entry.bind("<FocusOut>", _persist_api_port)
        api_port_entry.bind("<Return>",   _persist_api_port)
        _api_port_now = self.cfg.get("localApiPort", 8778)
        _hint(s, (f"GET http://127.0.0.1:{_api_port_now}/ready-up"
                  f"  ·  /accept  ·  /status  ·  0 = disabled"))

        # ── Startup ───────────────────────────────────────────────────────────
        s = _section("Startup")

        self._startup_var = tk.BooleanVar(value=self._startup_enabled())
        _check(s, "Launch at Windows startup (minimised to tray)",
               self._startup_var,
               lambda: self._set_startup(self._startup_var.get())).pack(anchor="w")
        _hint(s, "Runs quietly in the tray and auto-connects when League opens")

        # ── Updates ───────────────────────────────────────────────────────────
        s = _section("Updates")

        upd_row = tk.Frame(s, bg=CARD)
        upd_row.pack(anchor="w")
        _sbtn(upd_row, "Check for Updates",
              self._manual_update_check).pack(side="left")
        tk.Label(upd_row, text=f"Current version:  v{APP_VERSION}",
                 bg=CARD, fg=FAINT,
                 font=FONT_HINT).pack(side="left", padx=(12, 0))

        # ── Audio ─────────────────────────────────────────────────────────────
        s = _section("Audio")

        self._neeko_sound_var = tk.BooleanVar(
            value=bool(self.cfg.get("neekoSoundEnabled", True)))
        _check(s, "Neeko's friendly ready up reminder", self._neeko_sound_var,
               lambda: (
                   self.cfg.__setitem__("neekoSoundEnabled",
                                        self._neeko_sound_var.get()),
                   save_config(self.cfg),
               )).pack(anchor="w")
        _hint(s, "Plays after 30 s when all other tool users in your party are ready")

        vol_row = _row(s, "Volume:")
        self._neeko_vol_var = tk.IntVar(
            value=int(self.cfg.get("neekoSoundVolume", 80)))
        pct_str = tk.StringVar(value=f"{self._neeko_vol_var.get()}%")

        def _on_vol_change(val):
            self._neeko_vol_var.set(val)
            pct_str.set(f"{val}%")
            self.cfg["neekoSoundVolume"] = val
            save_config(self.cfg)

        HexSlider(vol_row, value=self._neeko_vol_var.get(),
                  command=_on_vol_change, bg=CARD).pack(side="left", padx=(8, 0))
        tk.Label(vol_row, textvariable=pct_str, bg=CARD, fg=TEXT,
                 font=FONT_SMALL, width=4, anchor="w").pack(
            side="left", padx=(6, 0))
        tk.Button(vol_row, text="🔊", bg=BTN_BG, fg=TEXT,
                  relief="flat", cursor="hand2", padx=6, pady=1,
                  activebackground=BTN_HOV, font=FONT_LABEL,
                  command=lambda: _play_sound(
                      READY_SOUND,
                      max(0, min(100, self._neeko_vol_var.get())) / 100.0,
                  )).pack(side="left", padx=(8, 0))

        # Bottom breathing room so the last card isn't flush with the save bar.
        tk.Frame(body, bg=DARK, height=18).pack(fill="x")

    # ── Auto-connect watcher ──────────────────────────────────────────────────
    def _ping_host(self):
        """Resolve the regional Riot host from the client (cached)."""
        host = getattr(self, "_cached_ping_host", None)
        if host:
            return host
        host = DEFAULT_PING_HOST
        try:
            if self._connected:
                r = self._lcu.get("/riotclient/region-locale")
                if r.status_code == 200:
                    wr = str(r.json().get("webRegion", "")).lower()
                    host = REGION_HOST.get(wr, DEFAULT_PING_HOST)
                    self._cached_ping_host = host   # cache only once we know region
        except Exception:
            pass
        return host

    def _set_ping(self, text, fg):
        lbl = getattr(self, "_lbl_ping", None)
        if lbl:
            lbl.config(text=text, fg=fg)

    def _watch_ping(self):
        """Background thread: real ICMP ping to the regional Riot host, averaged
        over a few samples and smoothed, shown colour-coded in the header."""
        ema = None  # exponential moving average for a steady readout
        self._ping_val  = None
        self._ping_hist = []   # [(monotonic, ms_or_None)]  rolling 60 s
        while True:
            host = self._ping_host()
            ms = self._icmp_ms(host)
            now = time.monotonic()
            if ms is None:
                ema = None
                self._ping_val = None
                self._ping_hist.append((now, None))
                self.after(0, lambda: self._set_ping("● Ping: —", MUTED))
            else:
                ema = ms if ema is None else (0.8 * ema + 0.2 * ms)
                val = max(1, int(round(ema)))   # never show the impossible 0
                self._ping_val = val
                raw = max(1, int(round(ms)))    # raw single-probe for graph
                self._ping_hist.append((now, raw))
                fg  = GREEN if val < 60 else (GOLD if val < 120 else RED)
                self.after(0, lambda v=val, c=fg: self._set_ping(f"● Ping: {v} ms", c))
            # Prune to last 60 s
            cutoff = now - 60
            self._ping_hist = [(t, v) for t, v in self._ping_hist if t >= cutoff]
            time.sleep(3)

    @staticmethod
    def _icmp_ms(host):
        """Average ICMP round-trip (ms) over 4 pings, or None if unreachable."""
        try:
            out = subprocess.run(
                ["ping", "-n", "1", "-w", "900", host],
                capture_output=True, text=True, timeout=3,
                creationflags=0x08000000,   # CREATE_NO_WINDOW
            ).stdout
            times = [int(t) for t in _re.findall(r"time[=<](\d+)\s*ms", out)]
            times = [t for t in times if t >= 1]   # drop sub-ms (local) artifacts
            if times:
                return sum(times) / len(times)
        except Exception:
            pass
        return None

    def _watch_relay(self):
        """Background thread: ping the ready-up relay and reflect its status in
        the header bubble (not set / connected / offline)."""
        self._relay_connected = False
        while True:
            if not self.cfg.get("readyUpEnabled", True):
                self._relay_connected = False
                self.after(0, lambda: self._lbl_relay.config(
                    text="● Relay: disabled", fg=MUTED))
                time.sleep(5)
                continue
            url = (self.cfg.get("relayUrl") or "").strip().rstrip("/")
            if not url:
                self._relay_connected = False
                self.after(0, lambda: self._lbl_relay.config(
                    text="● Relay: not set", fg=MUTED))
            else:
                ok = False
                try:
                    ok = requests.get(f"{url}/ping", timeout=4).status_code == 200
                except Exception:
                    ok = False
                self._relay_connected = ok
                if ok:
                    self.after(0, lambda: self._lbl_relay.config(
                        text="● Relay: connected", fg=GREEN))
                else:
                    self.after(0, lambda: self._lbl_relay.config(
                        text="● Relay: offline", fg=RED))
            time.sleep(5)

    def _watch_for_client(self):
        """Background thread: polls every 3 s for LeagueClientUx and
        connects automatically when it appears, then detects when it closes."""
        POLL = 3  # seconds between checks
        while True:
            try:
                if not self._connected:
                    # Try to connect each poll until we succeed
                    if self._lcu.connect() and self._lcu.ping():
                        self._connected = True
                        self.after(0, self._on_connected)
                else:
                    # Already connected — check client is still alive
                    if not self._lcu.ping():
                        self._connected = False
                        self.after(0, self._on_disconnected)
            except Exception:
                if self._connected:
                    self._connected = False
                    self.after(0, self._on_disconnected)
            time.sleep(POLL)

    # ── Controls ──────────────────────────────────────────────────────────────
    def _set_run_state(self, state):
        """Drive the single hextech Start/Stop button.
        states: 'disabled' (no client) · 'idle' (connected, stopped) · 'running'."""
        self._run_state = state
        if not getattr(self, "_run_btn", None):
            return
        if state == "disabled":
            self._run_btn.set_look("▶  START", DARK, EDGE_GOLD, FAINT, False)
        elif state == "idle":
            self._run_btn.set_look("▶  START", GOLD, GOLD, DARK, True)
        else:  # running
            self._run_btn.set_look("■  STOP", DARK, GOLD, GOLD, True)

    def _on_run_click(self):
        if self._run_state == "running":
            self._stop()
        elif self._run_state == "idle":
            self._start()

    def _on_connected(self):
        self._lbl_conn.config(text="● Client: connected", fg=GREEN)
        self.log("League client detected — connected automatically.")
        # Auto-start automation so it's already running by the time champ
        # select begins — no need to click Start manually.
        self._start()
        self.refresh_opgg_stats()   # now that a summoner is available

    def _on_disconnected(self):
        self._lbl_conn.config(text="● Client: waiting", fg=MUTED)
        self._set_run_state("disabled")
        self._engine.stop()
        self.log("League client closed. Waiting for it to reopen…")

    def _start(self):
        self._engine.start()
        self._set_run_state("running")
        self.log("Automation started.")

    def _stop(self):
        self._engine.stop()
        self._set_run_state("idle")
        self.log("Automation stopped.")

    # ── Launch at Windows startup (per-user, no admin) ─────────────────────────
    _RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_NAME = "LOL Client Tool"

    def _startup_enabled(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY) as k:
                winreg.QueryValueEx(k, self._RUN_NAME)
            return True
        except Exception:
            return False

    def _set_startup(self, enable: bool):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enable:
                    if not getattr(sys, "frozen", False):
                        self.log("Launch at startup only works in the packaged exe.")
                        return
                    # "--startup" makes it start hidden in the tray.
                    cmd = f'"{sys.executable}" --startup'
                    winreg.SetValueEx(k, self._RUN_NAME, 0, winreg.REG_SZ, cmd)
                    self.log("Will launch at Windows startup (minimised to tray).")
                else:
                    try:
                        winreg.DeleteValue(k, self._RUN_NAME)
                    except FileNotFoundError:
                        pass
                    self.log("Removed from Windows startup.")
        except Exception as e:
            self.log(f"Startup setting failed: {e}")

    # ── System tray ────────────────────────────────────────────────────────────
    def _setup_tray(self):
        self._tray = None
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            # No tray support — closing the window just exits (default behaviour).
            return
        img = Image.new("RGBA", (64, 64), (24, 24, 24, 255))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(200, 170, 60, 255))
        d.text((26, 22), "L", fill=(20, 20, 20, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda *_: self.after(0, self._show_window),
                             default=True),
            pystray.MenuItem("Quit", lambda *_: self.after(0, self._quit_app)),
        )
        self._tray = pystray.Icon("loltool", img, "LOL Client Tool", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()
        # Closing the window hides to tray instead of quitting (keeps automation
        # running in the background).
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

    def _hide_to_tray(self):
        self.withdraw()

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit_app(self):
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        self.destroy()
        os._exit(0)

    def _on_phase_change(self, phase):
        """Engine callback: reset ready state on champ select, update overlay."""
        def _do():
            prev = getattr(self, "_prev_phase", "")
            self._prev_phase = phase
            if phase == "ChampSelect":
                self._party_ready = False
            if prev == "ReadyCheck" and phase in ("Lobby", "Matchmaking"):
                self._party_ready = False
            if hasattr(self, "_overlay"):
                self._overlay.set_phase(phase)
        self.after(0, _do)

    def _on_auto_switch(self, key: str, value: bool):
        """Called when a dashboard ToggleSwitch is clicked."""
        self.cfg[key] = bool(value)
        save_config(self.cfg)
        # Keep Settings panel checkbox in sync if it exists.
        if key == "autoAcceptInvites" and hasattr(self, "_invite_var"):
            self._invite_var.set(bool(value))

    def _toggle_auto(self, key: str):
        """Flip an automation programmatically (external trigger / hotkey)."""
        default = DEFAULT_CONFIG.get(key, True)
        active  = not bool(self.cfg.get(key, default))
        self.cfg[key] = active
        save_config(self.cfg)
        sw = self._auto_switches.get(key)
        if sw:
            sw.set(active)   # reflect on the dashboard toggle (silent)
        if key == "autoAcceptInvites" and hasattr(self, "_invite_var"):
            self._invite_var.set(active)

    def _toggle_party_ready(self):
        """Toggle your ready state and broadcast it to the relay so every party
        member's tool can tally it. The leader's tool starts queue once everyone
        is ready."""
        if not self._connected:
            self.log("Ready Up: connect to the League client first.")
            return

        eng  = self._engine
        # Use the engine's state as truth — it may have auto-reset _i_am_ready
        # (e.g. when dropping to solo) without the App's _party_ready following.
        want = not eng._i_am_ready
        # Optimistic update — reflect the new state immediately in the overlay.
        pc  = eng._present_count
        self._party_ready        = want
        eng._i_am_ready          = want
        eng._ready_count         = max(0, min(pc, eng._ready_count + (1 if want else -1)))

        def _do():
            ok = eng.broadcast_party_ready(want, log_all=want)
            if ok:
                self.log(f"You are {'READY' if want else 'not ready'}.")
            else:
                # Server rejected — revert all three optimistic fields.
                self._party_ready = not want
                eng._i_am_ready   = not want
                eng._ready_count  = max(0, min(eng._present_count,
                                               eng._ready_count - (1 if want else -1)))
                self.log("Ready Up: couldn't register — set a Relay URL in Settings "
                         "and make sure you're in a party lobby.")

        threading.Thread(target=_do, daemon=True).start()

    def _reset_overlay_pos(self):
        for k in ("overlayRelX", "overlayRelY", "overlayPosVersion"):
            self.cfg.pop(k, None)
        save_config(self.cfg)
        if self._overlay:
            self._overlay._rel_x = None
            self._overlay._rel_y = None
        self.log("Overlay position reset — it will re-centre next time the client is detected.")

    def _toggle_overlay_enabled(self):
        new_val = not self.cfg.get("overlayEnabled", True)
        self.cfg["overlayEnabled"] = new_val
        save_config(self.cfg)
        if hasattr(self, "_overlay_enabled_var"):
            self._overlay_enabled_var.set(new_val)
        self.log(f"Overlay {'enabled' if new_val else 'disabled'} (hotkey).")

    def _watch_overlay_hotkey(self):
        """Background thread: polls GetAsyncKeyState for the overlay toggle keybind."""
        was_down = False
        while True:
            combo = (self.cfg.get("overlayToggleKey") or "").strip()
            mod_vks, key_vk = _overlay_parse_combo(combo) if combo else ([], 0)
            if key_vk:
                gaks = ctypes.windll.user32.GetAsyncKeyState
                key_down  = bool(gaks(key_vk) & 0x8000)
                mods_down = all(bool(gaks(m) & 0x8000) for m in mod_vks)
                is_down   = key_down and mods_down
                if is_down and not was_down:
                    self.after(0, self._toggle_overlay_enabled)
                was_down = is_down
            else:
                was_down = False
            time.sleep(0.05)

    def _on_ready_up_toggle(self):
        enabled = self._ready_up_var.get()
        self.cfg["readyUpEnabled"] = enabled
        save_config(self.cfg)
        if not enabled:
            def _disconnect():
                try:
                    self._engine.broadcast_party_ready(False)
                except Exception:
                    pass
                self._engine._i_am_ready    = False
                self._engine._ready_count   = 0
                self._engine._present_count = 0
                self._party_ready = False
            threading.Thread(target=_disconnect, daemon=True).start()
        self.log(f"Ready Up {'enabled' if enabled else 'disabled'}.")

    def _save(self):
        for k, v in self._delay_vars.items():
            self.cfg[k] = int(round(v.get() * 1000))   # seconds (UI) → ms (config)
        if hasattr(self, "_overlay_enabled_var"):
            self.cfg["overlayEnabled"] = self._overlay_enabled_var.get()
        if hasattr(self, "_ready_up_var"):
            self.cfg["readyUpEnabled"] = self._ready_up_var.get()
        if hasattr(self, "_relay_var"):
            self.cfg["relayUrl"] = self._relay_var.get().strip()
        if hasattr(self, "_invite_var"):
            self.cfg["autoAcceptInvites"] = self._invite_var.get()
        if hasattr(self, "_invite_whitelist_var"):
            raw = self._invite_whitelist_var.get()
            self.cfg["inviteWhitelist"] = [n.strip() for n in raw.split(",")
                                           if n.strip()]
        if hasattr(self, "_neeko_sound_var"):
            self.cfg["neekoSoundEnabled"] = self._neeko_sound_var.get()
        if hasattr(self, "_neeko_vol_var"):
            self.cfg["neekoSoundVolume"] = self._neeko_vol_var.get()
        if hasattr(self, "_tft_fix_var"):
            self.cfg["tftFixEnabled"] = self._tft_fix_var.get()
        save_config(self.cfg)
        self.log("Config saved.")

    def _on_restore_tft(self):
        if self._engine.restore_tft_settings():
            self.log("TFT click setting restored to normal.")
        else:
            self.log("Nothing to restore — no TFT override is active.")
        self._update_tft_status(reschedule=False)

    def _update_tft_status(self, reschedule=True):
        if hasattr(self, "_tft_status_var"):
            pending = self._engine._tft_saved_click_value
            self._tft_status_var.set(
                f"Override active (will restore to {pending})"
                if pending is not None else "No override pending")
        if reschedule:
            self.after(1000, self._update_tft_status)

    def _manual_update_check(self):
        """Settings button: check GitHub for a newer release and, if found,
        offer to download + install it. Reports the result either way."""
        import tkinter.messagebox as _mb
        self.log("Checking for updates…")

        def _do():
            try:
                r = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    timeout=10, headers={"Accept": "application/vnd.github+json"},
                )
                if r.status_code != 200:
                    self.log(f"Update check failed: HTTP {r.status_code}")
                    self.after(0, lambda: _mb.showwarning(
                        "Update check failed",
                        f"Could not reach GitHub (HTTP {r.status_code}).",
                        parent=self))
                    return
                data = r.json()
                tag  = data.get("tag_name", "")
                if _ver(tag) <= _ver(APP_VERSION):
                    self.log(f"You're on the latest version (v{APP_VERSION}).")
                    self.after(0, lambda: _mb.showinfo(
                        "Up to date",
                        f"You're running the latest version (v{APP_VERSION}).",
                        parent=self))
                    return
                dl_url = _client_asset_url(data.get("assets", []))
                if not dl_url:
                    self.log("A newer release exists but has no .exe asset.")
                    return
                self.after(0, lambda: _update_prompt(self, tag, dl_url, self.log))
            except Exception as e:
                self.log(f"Update check error: {e}")
                self.after(0, lambda: _mb.showwarning(
                    "Update check failed", str(e), parent=self))

        threading.Thread(target=_do, daemon=True).start()

    # ── List management (called by RolePanel) ─────────────────────────────────
    def refresh_list(self, role: str, key: str):
        panel = self._role_panels.get(role)
        if panel is None:
            return
        champ_list = panel.get_champ_list(key)
        items = [(int(cid), self.ddragon.name(int(cid)))
                 for cid in self.cfg["roleChampions"][role][key]]
        champ_list.set_items(items)

    def _refresh_all(self):
        for role in ROLES:
            for k in ("picks", "bans"):
                    self.refresh_list(role, k)
        self._refresh_permabans()

    # ── Permaban (global, always banned first) ─────────────────────────────────
    def _refresh_permabans(self):
        if not hasattr(self, "_perma_list"):
            return
        items = [(int(cid), self.ddragon.name(int(cid)))
                 for cid in self.cfg.get("permaBans", [])]
        self._perma_list.set_items(items)

    def _perma_ac_update(self, *_):
        if not hasattr(self, "_perma_ac"):
            return
        q = self._perma_var.get().lower().strip()
        self._perma_ac.delete(0, "end")
        if not q:
            self._perma_ac.grid_remove()
            return
        hits = [n for n in self.ddragon.all_display_names() if q in n.lower()][:6]
        if not hits:
            self._perma_ac.grid_remove()
            return
        for h in hits:
            self._perma_ac.insert("end", "  " + h)
        self._perma_ac.grid(row=1, column=1, sticky="we", padx=8, pady=(4, 0))

    def _perma_ac_select(self, _evt):
        sel = self._perma_ac.curselection()
        if sel:
            self._add_permaban(self._perma_ac.get(sel[0]).strip())

    def _perma_enter(self):
        # Enter adds the top dropdown match; falls back to the typed text.
        if hasattr(self, "_perma_ac") and self._perma_ac.size() > 0:
            self._add_permaban(self._perma_ac.get(0).strip())
        else:
            self._add_permaban()

    def _add_permaban(self, name: str = ""):
        name = (name or self._perma_var.get()).strip()
        if not name:
            return
        cid = self.ddragon.find_id(name)
        if cid is None:
            self.log(f"Unknown champion: {name!r}")
            return
        lst = self.cfg.setdefault("permaBans", [])
        if cid not in lst:
            lst.append(cid)
            save_config(self.cfg)
            self._refresh_permabans()
            self.request_icon_prefetch()      # load the new chip's portrait
            self.log(f"Permaban added: {self.ddragon.name(cid)}")
        self._perma_var.set("")
        if hasattr(self, "_perma_ac"):
            self._perma_ac.grid_remove()

    def _remove_permaban(self, cid):
        """Called by a permaban chip's × remove hotspot."""
        cid = int(cid)
        lst = self.cfg.get("permaBans", [])
        if cid in lst:
            removed = self.ddragon.name(cid)
            lst.remove(cid)
            save_config(self.cfg)
            self._refresh_permabans()
            self.log(f"Permaban removed: {removed}")

    def _reorder_permabans(self, order):
        """Called when permaban chips are drag-reordered."""
        self.cfg["permaBans"] = [int(c) for c in order]
        save_config(self.cfg)
        self.log("Reordered permaban priority.")

    def reorder_items(self, role: str, key: str, new_order: list):
        """Called by ChampionList once a drag-and-drop reorder is dropped."""
        self.cfg["roleChampions"][role][key] = [int(cid) for cid in new_order]
        save_config(self.cfg)              # persist the new order immediately
        self.log(f"Reordered {ROLE_LABEL[role]} {key[:-1]} priority.")

    def remove_item(self, role: str, key: str, cid: int):
        lst = self.cfg["roleChampions"][role][key]
        if cid in lst:
            name = self.ddragon.name(cid)
            lst.remove(cid)
            save_config(self.cfg)          # persist the removal immediately
            self.refresh_list(role, key)
            self.log(f"Removed {name} from {ROLE_LABEL[role]} {key[:-1]} list")

    def move_between_lists(self, role: str, from_key: str, to_key: str,
                           cid: int, insert_index: int):
        """Called by RolePanel when a chip is dragged from one priority list
        (picks/bans) to the other within the same role. Caps the destination
        at MAX_PRIORITY_ITEMS — if the drop pushes it past that, the 6th
        champion (whoever now sits at that position) is bumped off."""
        cid = int(cid)
        src = self.cfg["roleChampions"][role][from_key]
        dst = self.cfg["roleChampions"][role][to_key]
        if cid in src:
            src.remove(cid)
        if cid in dst:
            dst.remove(cid)   # shouldn't happen, but avoid a duplicate
        dst.insert(max(0, min(insert_index, len(dst))), cid)
        bumped = None
        if len(dst) > MAX_PRIORITY_ITEMS:
            bumped = dst.pop(MAX_PRIORITY_ITEMS)
        save_config(self.cfg)
        self.refresh_list(role, from_key)
        self.refresh_list(role, to_key)
        self.log(f"Moved {self.ddragon.name(cid)} from {ROLE_LABEL[role]} "
                 f"{from_key[:-1]} to {to_key[:-1]} list")
        if bumped is not None:
            self.log(f"{ROLE_LABEL[role]} {to_key[:-1]} list capped at "
                     f"{MAX_PRIORITY_ITEMS} — removed {self.ddragon.name(bumped)}.")

    # ── op.gg Auto-fill ───────────────────────────────────────────────────────
    def _open_opgg_dialog(self):
        if not self.ddragon.all_display_names():
            self.log("Champion data not loaded yet — please wait a moment.")
            return
        OpGGDialog(self)

    def _opgg_fetch(self, game_name: str, tag_line: str, region: str,
                    status_fn) -> list | None:
        """Fetch op.gg season champion stats via MCP API. Returns [(role, name, games, wins), ...]."""
        status_fn(f"Fetching {game_name}#{tag_line} season stats…")

        def _post(body):
            return requests.post(
                "https://mcp-api.op.gg/mcp", json=body, timeout=15,
                headers={"Accept": "application/json, text/event-stream"},
            )

        # ── Request 1: season champion stats ─────────────────────────────────
        profile_body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "lol_get_summoner_profile",
                "arguments": {
                    "game_name": game_name, "tag_line": tag_line,
                    "region": region.upper(),
                    "desired_output_fields": ["data.summoner.most_champions"],
                },
            },
        }
        # ── Request 2: meta champion→primary-role map ─────────────────────────
        meta_body = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "lol_list_lane_meta_champions",
                "arguments": {
                    "region": region.upper(),
                    "desired_output_fields": [
                        "data.positions.top[].champion",    "data.positions.top[].role_rate",
                        "data.positions.mid[].champion",    "data.positions.mid[].role_rate",
                        "data.positions.jungle[].champion", "data.positions.jungle[].role_rate",
                        "data.positions.adc[].champion",    "data.positions.adc[].role_rate",
                        "data.positions.support[].champion","data.positions.support[].role_rate",
                    ],
                },
            },
        }
        try:
            r1 = _post(profile_body)
            r2 = _post(meta_body)
            r1.raise_for_status(); r2.raise_for_status()
        except Exception as e:
            status_fn(f"Network error: {e}", RED)
            return None

        def _text(resp):
            try:
                return resp.json()["result"]["content"][0]["text"]
            except Exception:
                return ""

        profile_text = _text(r1)
        meta_text    = _text(r2)
        if not profile_text or not meta_text:
            status_fn("No data returned from op.gg.", RED)
            return None

        status_fn("Parsing season stats…")

        # ── Parse season champion stats ───────────────────────────────────────
        # Format: ChampionStat(id, play, win, lose, ...numbers/nulls..., "champion_name")
        champ_stats: dict = {}
        for mo in _re.finditer(
            r'ChampionStat\(\d+,(\d+),(\d+),[^"]*"([^"]+)"\)', profile_text
        ):
            games, wins, name = int(mo.group(1)), int(mo.group(2)), mo.group(3)
            if games > 0:
                champ_stats[name] = (games, wins)

        if not champ_stats:
            status_fn("No season champion stats found — play more ranked games.", RED)
            return None

        # ── Parse meta champion→role map (role_rate = fraction of games in that lane) ──
        # Response: Positions([...top...],[...mid...],[...jungle...],[...adc...],[...support...]))
        pos_m = _re.search(r'Positions\(\[(.*)\]\)\)', meta_text, _re.DOTALL)
        if not pos_m:
            status_fn("Could not parse meta champion roles.", RED)
            return None

        role_order = ["top", "middle", "jungle", "bottom", "utility"]
        champ_role: dict = {}  # name -> (role, rate)
        for role, segment in zip(role_order, pos_m.group(1).split("],[")):
            for mo in _re.finditer(r'Top\("([^"]+)",([\d.]+)\)', segment):
                name, rate = mo.group(1), float(mo.group(2))
                if name not in champ_role or rate > champ_role[name][1]:
                    champ_role[name] = (role, rate)

        # ── Fetch recent match history for exact role overrides ───────────────
        status_fn("Cross-referencing recent games for role accuracy…")
        hist_body = {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "lol_list_summoner_matches",
                "arguments": {
                    "game_name": game_name, "tag_line": tag_line,
                    "region": region.upper(), "limit": 20,
                    "desired_output_fields": [
                        "data.game_history[].participants[].champion_name",
                        "data.game_history[].participants[].position",
                        "data.game_history[].participants[].stats.result",
                    ],
                },
            },
        }
        try:
            r3 = _post(hist_body)
            hist_text = _text(r3)
        except Exception:
            hist_text = ""

        # Build champion→role and recent win/loss from match history
        # Recent games are double-counted in the final stats so recently-active
        # champions score higher through the sqrt(games) term in Bayesian scoring.
        hist_role:  dict = {}  # champ -> role (exact, overrides meta)
        hist_extra: dict = defaultdict(lambda: [0, 0])  # champ -> [extra_games, extra_wins]
        if hist_text:
            hist_pat = _re.compile(
                r'Participant\("([^"]+)",(null|"([^"]+)"),Stats\("([^"]+)"\)\)'
            )
            for mo in hist_pat.finditer(hist_text):
                champ  = mo.group(1)
                pos    = mo.group(3)
                result = mo.group(4)
                if pos:
                    hist_role.setdefault(champ, self._opgg_role(pos))
                    hist_extra[champ][0] += 1
                    if result == "WIN":
                        hist_extra[champ][1] += 1

        # ── Combine ───────────────────────────────────────────────────────────
        rows = []
        for name, (season_g, season_w) in champ_stats.items():
            role = hist_role.get(name) or (champ_role.get(name, (None,))[0])
            if not role:
                continue
            extra_g, extra_w = hist_extra.get(name, [0, 0])
            rows.append((role, name, season_g + extra_g, season_w + extra_w))

        if not rows:
            status_fn("No champion-role matches found.", RED)
            return None

        # Fetch mastery 4+ champions for the role-fill fallback
        mastery_by_id: dict = {}   # champ_id -> champion_points
        try:
            mr = self._lcu.get(
                "/lol-champion-mastery/v1/local-player/champion-mastery"
            )
            if mr.status_code == 200:
                for m in mr.json():
                    if m.get("championLevel", 0) >= 4:
                        mastery_by_id[int(m["championId"])] = int(
                            m.get("championPoints", 0)
                        )
        except Exception:
            pass

        total = sum(g for _, _, g, _ in rows)
        status_fn(f"Found {len(rows)} champions from {total} season games (recent games weighted 2×).")
        return rows, champ_role, mastery_by_id

    @staticmethod
    def _opgg_role(pos: str) -> str:
        p = pos.lower().strip()
        if p in ("top", "toplane"):                            return "top"
        if p in ("jungle", "jng", "jung", "jungler"):         return "jungle"
        if p in ("mid", "middle", "midlane"):                  return "middle"
        if p in ("bot", "bottom", "adc", "carry", "botlane"): return "bottom"
        if p in ("sup", "supp", "support", "utility"):        return "utility"
        return ""

    def _opgg_apply(self, rows: list, champ_role: dict, mastery_by_id: dict,
                    do_picks: bool, do_bans: bool, dialog: "OpGGDialog"):
        by_role: dict = defaultdict(list)
        for role, name, games, wins in rows:
            bwr   = (wins + 1) / (games + 2)
            score = bwr * _math.sqrt(games)
            by_role[role].append((score, name))
        for role in by_role:
            by_role[role].sort(reverse=True)

        # Build champ_id → primary role for champions with ≥30 % role rate.
        # Used to decide whether a mastery champion "belongs" in a given role.
        common_role_by_id: dict = {}
        for opgg_name, (meta_role, rate) in champ_role.items():
            if rate >= 0.3:
                cid = self.ddragon.find_id(opgg_name)
                if cid is not None:
                    common_role_by_id[cid] = meta_role

        changed: list = []
        for role in ROLES:
            rc = self.cfg["roleChampions"].setdefault(
                role, {"picks": [], "bans": []}
            )
            if do_picks:
                ids = []
                for _, name in by_role.get(role, [])[:5]:
                    cid = self.ddragon.find_id(name)
                    if cid is not None:
                        ids.append(cid)

                # Supplement with mastery 4+ champs commonly played in this role
                if len(ids) < 3 and mastery_by_id:
                    added_names: list = []
                    for cid in sorted(mastery_by_id, key=mastery_by_id.__getitem__,
                                      reverse=True):
                        if len(ids) >= 5:
                            break
                        if cid in ids or common_role_by_id.get(cid) != role:
                            continue
                        ids.append(cid)
                        added_names.append(self.ddragon.name(cid) or str(cid))
                    if added_names:
                        self.log(
                            f"op.gg auto-fill: added mastery picks for "
                            f"{ROLE_LABEL[role]} — {', '.join(added_names)}."
                        )

                if ids:
                    rc["picks"] = ids
                    changed.append(role)
            if do_bans:
                ban_ids = []
                for name in META_BANS.get(role, []):
                    cid = self.ddragon.find_id(name)
                    if cid is not None:
                        ban_ids.append(cid)
                if ban_ids:
                    rc["bans"] = ban_ids

        self._refresh_all()
        save_config(self.cfg)
        self.request_icon_prefetch()
        roles_str = ", ".join(ROLE_LABEL[r] for r in changed) or "none"
        self.log(f"op.gg auto-fill complete. Picks updated: {roles_str}.")
        if do_bans:
            self.log("Ban lists filled with meta suggestions.")
        dialog.destroy()

    # ── Log ───────────────────────────────────────────────────────────────────
    # Messages whose prefix marks them as diagnostics are written to the log file
    # only — they never reach the on-screen log box, keeping the UI clean.
    _DEBUG_PREFIXES = ("[tick]", "[debug]", "[trace]", "[ban]", "[pick]")

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}\n"
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
        if msg.startswith(self._DEBUG_PREFIXES):
            return   # diagnostic — file only, not shown to the end user
        def _do():
            self._log_box.config(state="normal")
            self._log_box.insert("end", line)
            self._log_box.see("end")
            if int(self._log_box.index("end-1c").split(".")[0]) > 500:
                self._log_box.delete("1.0", "2.0")
            self._log_box.config(state="disabled")
        self.after(0, _do)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Prevent multiple instances (including different versions) from running
    # simultaneously. CreateMutexW returns ERROR_ALREADY_EXISTS (183) when
    # another instance already holds the mutex.
    import ctypes as _ct
    _mutex = _ct.windll.kernel32.CreateMutexW(None, False, "Global\\LOL_Client_Tool")
    if _ct.windll.kernel32.GetLastError() == 183:
        _r = tk.Tk(); _r.withdraw()
        tk.messagebox.showerror(
            "Already Running",
            "LOL Client Tool is already running.\n\nClose the existing instance first.",
            parent=_r,
        )
        _r.destroy()
        sys.exit(0)

    if getattr(sys, "frozen", False):
        for _f in (
            Path(sys.executable).with_name("LOL_Client_Tool_update.exe"),
            Path(str(sys.executable) + ".bak"),
        ):
            try: _f.unlink()
            except Exception: pass

    app = App()
    app.mainloop()