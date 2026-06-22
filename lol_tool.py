#!/usr/bin/env python3
"""
LOL Client Tool  –  Role-Based Champion Selection  (Python rebuild)

Uses the LCU (League Client Update) local API to automate champion select.
Detects your assigned role each game and picks from your per-role priority list.

WARNING: Third-party automation tools may violate Riot Games' Terms of Service
and could result in account penalties. Use at your own risk.
"""

import sys, os, json, threading, time, re as _re, math as _math
from pathlib import Path
from collections import defaultdict

import tkinter as tk
from tkinter import ttk, scrolledtext


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


# ── Constants ─────────────────────────────────────────────────────────────────
APP_NAME    = "LOL Client Tool  –  Role-Based Pick"
APP_VERSION = "1.5.9"
GITHUB_REPO = "Naieter/LoL-Client-Automation"
CONFIG_DIR  = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LOL_Client_TOOL"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE    = CONFIG_DIR / "lol_tool.log"
DDRAGON_URL = "https://ddragon.leagueoflegends.com"

# LCU assignedPosition values
ROLES = ["top", "jungle", "middle", "bottom", "utility"]
ROLE_LABEL = {
    "top":     "Top",
    "jungle":  "Jungle",
    "middle":  "Mid",
    "bottom":  "ADC",
    "utility": "Support",
}

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
    "autoRunes":    True,
    "pickDelay":    27000,
    "banDelay":     2000,
    "prePickDelay": 500,
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
        dl_url = next(
            (a["browser_download_url"] for a in data.get("assets", [])
             if a["name"].lower().endswith(".exe")),
            None,
        )
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
        log_fn("[update] Download complete — restarting…")
        pid = os.getpid()
        log = exe.with_name("lol_update_log.txt")
        # The batch waits for THIS process to fully exit (so the exe lock is
        # released), then swaps the file with retries and relaunches.
        # NOTE: use `ping` for delays, not `timeout` — `timeout` needs a console
        # and fails silently in a windowless/detached process, which previously
        # broke the swap. enabledelayedexpansion makes the retry counter work.
        bat.write_text(
            "@echo off\r\n"
            "setlocal enableextensions enabledelayedexpansion\r\n"
            f'set "EXE={exe}"\r\n'
            f'set "NEW={tmp}"\r\n'
            f'set "LOG={log}"\r\n'
            'echo === update started === > "%LOG%"\r\n'
            ":waitexit\r\n"
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  ping -n 2 127.0.0.1 >nul\r\n"
            "  goto waitexit\r\n"
            ")\r\n"
            'echo process exited; waiting for lock release >> "%LOG%"\r\n'
            "ping -n 3 127.0.0.1 >nul\r\n"
            "set /a tries=0\r\n"
            ":swap\r\n"
            'move /y "%NEW%" "%EXE%" >>"%LOG%" 2>&1\r\n'
            'if exist "%NEW%" (\r\n'
            "  set /a tries+=1\r\n"
            '  echo move retry !tries! >> "%LOG%"\r\n'
            "  if !tries! lss 20 (\r\n"
            "    ping -n 2 127.0.0.1 >nul\r\n"
            "    goto swap\r\n"
            "  )\r\n"
            ")\r\n"
            'echo launching new version >> "%LOG%"\r\n'
            'start "" "%EXE%"\r\n'
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


# ── DDragon champion data ─────────────────────────────────────────────────────
class DDragon:
    def __init__(self):
        self._id_to_name: dict = {}
        self._name_to_id: dict = {}   # lowercase name → int id
        self._id_to_key:  dict = {}   # int id → internal key (e.g. "MissFortune")

    def load(self):
        try:
            ver  = requests.get(f"{DDRAGON_URL}/api/versions.json", timeout=8).json()[0]
            data = requests.get(
                f"{DDRAGON_URL}/cdn/{ver}/data/en_US/champion.json", timeout=10
            ).json()["data"]
            for champ in data.values():
                cid  = int(champ["key"])
                name = champ["name"]
                self._id_to_name[cid] = name
                self._name_to_id[name.lower()] = cid
                self._id_to_key[cid] = champ["id"]   # e.g. "MissFortune", "DrMundo"
        except Exception as e:
            print(f"[DDragon] {e}")

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


# ── Automation engine ─────────────────────────────────────────────────────────
class AutoEngine:
    """Polls the LCU every 2 s and acts based on game flow phase."""

    POLL = 0.5  # seconds

    def __init__(self, lcu: LCU, cfg_fn, log_fn, ddragon=None):
        self._lcu          = lcu
        self._cfg          = cfg_fn    # callable → dict
        self._log          = log_fn    # callable(str)
        self._dd           = ddragon   # DDragon, for champ-id → op.gg name
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
        self._last_role      = None      # assignedPosition seen last poll (detect swaps)

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
        r   = self._lcu.get("/lol-gameflow/v1/session")
        if r.status_code != 200:
            return

        phase = r.json().get("phase", "")

        if phase != self._last_phase:
            self._log(f"Phase → {phase}")
            self._last_phase = phase
            if phase == "ChampSelect":
                self._done_actions.clear()
                self._action_start.clear()
                self._prepicked.clear()
                self._tool_hovers.clear()
                self._user_pick.clear()
                self._pick_rejected.clear()
                self._ban_hovered.clear()
                self._runes_key = None
                self._last_role = None
                self._log_champ_select_debug()

        # Auto accept
        if cfg.get("autoAccept") and phase == "ReadyCheck":
            self._lcu.post("/lol-matchmaking/v1/ready-check/accept")
            self._log("Auto-accepted match.")

        if phase == "ChampSelect":
            self._handle_champ_select(cfg)

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
        taken: set = set()
        for p in session.get("myTeam", []):
            if str(p.get("cellId", "")) != my_cell:
                cid = int(p.get("championId", 0) or 0)
                if cid: taken.add(cid)
        for p in session.get("theirTeam", []):
            cid = int(p.get("championId", 0) or 0)
            if cid: taken.add(cid)

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

        # Champions actually selectable this session (Practice Tool → all)
        playable = self._get_pickable_ids()
        # Reference removes already-taken champions from the playable pool
        playable_now = playable - taken - bans

        # ── diagnostic: log only on state change ──
        _states = [
            f"{a.get('type')}(id={a.get('id')} champ={a.get('championId')} prog={a.get('isInProgress')} done={a.get('completed')})"
            for grp in session.get("actions", [])
            for a in grp
            if str(a.get("actorCellId", "")) == my_cell
        ]
        _key = f"{_states}|bans={sorted(bans)}|intent={sorted(pick_intents)}"
        if _key != getattr(self, "_last_state_key", None):
            self._log(f"[tick] {_states}  bans={sorted(bans)}")
            self._last_state_key = _key

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
                    # else, drop it so picks fall back to the priority list.
                    ov = self._user_pick.get(aid)
                    if ov and ov not in playable_now:
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
                    if (time.monotonic() - self._action_start[aid]) * 1000 < cfg.get("pickDelay", 27000):
                        # Still waiting to lock. Keep the intended champion hovered so a
                        # role swap mid-turn is reflected — but never override a champion
                        # the user hovered themselves.
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
                    if aid not in self._action_start:
                        self._action_start[aid] = time.monotonic()
                    champ = self._best(ban_prio, bans | pick_intents, set(range(1_000_000)))
                    if not champ:
                        self._log(f"No valid ban for {ROLE_LABEL.get(role_key, role_key)}. Add champions to the ban list!")
                    elif self._ban_hovered.get(aid) != champ:
                        # Phase 1 — hover the ban target (no completion yet)
                        if self._commit_action(action, champ, complete=False):
                            self._ban_hovered[aid] = champ
                            self._action_start[aid] = time.monotonic()  # start delay after hover
                            self._log(f"[debug] Ban hover: #{champ}  [{ROLE_LABEL.get(role_key, role_key)}]")
                    elif (time.monotonic() - self._action_start[aid]) * 1000 >= cfg.get("banDelay", 2000):
                        # Phase 2 — champion is hovered, lock it the SAME way the pick
                        # locks: atomic PATCH completed:true (the pick proves this works
                        # once the champion is already hovered).
                        cur = int(action.get("championId", 0) or 0)
                        if cur != champ:
                            # hover didn't stick yet — re-hover and wait another cycle
                            self._ban_hovered[aid] = None
                            continue
                        if self._commit_action(action, champ, complete=True):
                            self._log(f"Banned champion #{champ}")
                            self._done_actions.add(aid)

                # ── PRE-PICK ── hover our intended champion before our turn, unless
                # the user has already hovered one themselves (then leave it alone).
                elif (cfg.get("autoPrePick")
                        and atype == "pick"
                        and not in_progress
                        and not completed
                        and aid not in self._done_actions
                        and aid not in self._user_pick):
                    champ = self._best(pick_prio, set(), playable_now)
                    if champ and self._prepicked.get(aid) != champ:
                        if self._commit_action(action, champ, complete=False):
                            self._prepicked[aid] = champ
                            self._tool_hovers.setdefault(aid, set()).add(champ)
                            self._log(f"Pre-pick hover: #{champ}  [{ROLE_LABEL.get(role_key, role_key)}]")

        # ── Auto runes + summoner spells — once our champion is locked in ──
        # Keyed on (champion, role) so a position swap after locking re-imports
        # the correct meta page for the new role.
        if cfg.get("autoRunes"):
            locked = 0
            for grp in session.get("actions", []):
                for a in grp:
                    if (str(a.get("actorCellId", "")) == my_cell
                            and a.get("type") == "pick"
                            and a.get("completed")):
                        locked = int(a.get("championId", 0) or 0)
            if locked:
                key = (locked, assigned_role)
                if key != self._runes_key:
                    self._runes_key = key
                    threading.Thread(
                        target=self._import_runes_spells,
                        args=(locked, assigned_role),
                        daemon=True,
                    ).start()

    def _import_runes_spells(self, champ_id: int, position: str):
        """Import the meta rune page + summoner spells for the locked-in champion.
        Primary source: op.gg Diamond+ stats for this exact champion and role
        (the most-played page across recent diamond+ games). Falls back to the
        League client's own recommendation if op.gg is unavailable. Worker thread."""
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


# ── Theme colors ──────────────────────────────────────────────────────────────
DARK   = "#1e2327"
DARKER = "#14181b"
PANEL  = "#2a2f35"
GOLD   = "#c89b3c"
GREEN  = "#27ae60"
RED    = "#c0392b"
TEXT   = "#cccccc"
WHITE  = "#ffffff"

BTN_STYLE = dict(relief="flat", cursor="hand2", font=("Segoe UI", 9),
                 activeforeground=WHITE)


# ── GUI ───────────────────────────────────────────────────────────────────────
class RolePanel:
    """The pick + ban list editor for a single role."""

    def __init__(self, parent: tk.Widget, role: str, app: "App"):
        self._role = role
        self._app  = app
        self._widgets: dict = {}

        left  = tk.Frame(parent, bg=DARK)
        right = tk.Frame(parent, bg=DARK)
        left.pack(side="left",  fill="both", expand=True, padx=10, pady=10)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        self._build_side(left,  "picks", "Pick Priority",  GREEN)
        self._build_side(right, "bans",  "Ban Priority",   RED)

    def _build_side(self, container: tk.Frame, list_key: str,
                    title: str, accent: str):
        role  = self._role
        app   = self._app

        tk.Label(container, text=title, bg=DARK, fg=accent,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(container, text="Top = highest priority",
                 bg=DARK, fg="#555", font=("Segoe UI", 8)).pack(anchor="w")

        lb = tk.Listbox(container, bg=DARKER, fg=WHITE,
                        selectbackground=GOLD, selectforeground="#000",
                        relief="flat", height=11, font=("Segoe UI", 10),
                        activestyle="none")
        lb.pack(fill="both", expand=True, pady=(4, 2))
        self._widgets[f"{list_key}_lb"] = lb

        # Reorder / remove
        btn_row = tk.Frame(container, bg=DARK)
        btn_row.pack(fill="x")

        def move(delta, r=role, k=list_key):
            app.move_item(r, k, delta)

        def remove(r=role, k=list_key):
            app.remove_item(r, k)

        tk.Button(btn_row, text="▲", bg=PANEL, fg=TEXT,
                  activebackground=PANEL, command=lambda: move(-1),
                  **BTN_STYLE).pack(side="left", padx=2, pady=2)
        tk.Button(btn_row, text="▼", bg=PANEL, fg=TEXT,
                  activebackground=PANEL, command=lambda: move(1),
                  **BTN_STYLE).pack(side="left", padx=2, pady=2)
        tk.Button(btn_row, text="Remove", bg=PANEL, fg=TEXT,
                  activebackground=PANEL, command=remove,
                  **BTN_STYLE).pack(side="left", padx=2, pady=2)

        # Champion search / add
        add_row = tk.Frame(container, bg=DARK)
        add_row.pack(fill="x", pady=(10, 0))

        tk.Label(add_row, text="Add:", bg=DARK, fg=TEXT,
                 font=("Segoe UI", 9)).pack(side="left")

        entry_var = tk.StringVar()
        self._widgets[f"{list_key}_ev"] = entry_var

        entry = tk.Entry(add_row, textvariable=entry_var,
                         bg=PANEL, fg=WHITE, insertbackground=WHITE,
                         relief="flat", width=18, font=("Segoe UI", 9))
        entry.pack(side="left", padx=4)

        # Autocomplete listbox (shown below entry, hidden when empty)
        ac_lb = tk.Listbox(container, bg=PANEL, fg=WHITE,
                           selectbackground=GOLD, selectforeground="#000",
                           relief="flat", height=5, font=("Segoe UI", 9))
        self._widgets[f"{list_key}_ac"] = ac_lb

        # Capture for closures
        _lb       = lb
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
                lst.append(cid)
                app.refresh_list(_role, _list_key)
                app.log(
                    f"Added {app.ddragon.name(cid)} to "
                    f"{ROLE_LABEL[_role]} {_list_key[:-1]} list"
                )
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
            _ac.pack(fill="x")

        def _ac_select(_evt):
            sel = _ac.curselection()
            if sel:
                _do_add(name=_ac.get(sel[0]).strip())

        entry_var.trace_add("write", _ac_update)
        ac_lb.bind("<<ListboxSelect>>", _ac_select)
        entry.bind("<Return>", lambda _: _do_add())

        tk.Button(add_row, text="+", bg=accent, fg=WHITE,
                  activebackground=accent, command=_do_add,
                  **BTN_STYLE).pack(side="left")

    def get_listbox(self, list_key: str) -> tk.Listbox:
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
        rows = self._app._opgg_fetch(game_name, tag_line, region, self._set_status)
        if rows is not None:
            self.after(0, lambda: self._app._opgg_apply(rows, do_picks, do_bans, self))
        else:
            self.after(0, lambda: self._btn_fetch.config(state="normal"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}   v{APP_VERSION}")
        self.configure(bg=DARK)
        self.resizable(False, False)

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("", encoding="utf-8")  # clear on startup

        self.cfg     = load_config()
        self.ddragon = DDragon()
        self._lcu    = LCU()
        self._engine = AutoEngine(self._lcu, lambda: self.cfg, self.log, self.ddragon)

        self._role_panels: dict = {}   # role → RolePanel
        self._delay_vars:  dict = {}
        self._bool_vars:   dict = {}
        self._connected    = False

        self._build_ui()

        # Load champion data in background
        threading.Thread(target=self._load_champs, daemon=True).start()

        # Watch for the League client to open (auto-connect on launch)
        threading.Thread(target=self._watch_for_client, daemon=True).start()

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

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",     background=DARK,  borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=TEXT,
                        padding=[12, 5])
        style.map("TNotebook.Tab",
                  background=[("selected", GOLD)],
                  foreground=[("selected", "#000")])
        style.configure("TFrame", background=DARK)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        # Main tab
        main_frame = ttk.Frame(nb)
        nb.add(main_frame, text="  Main  ")
        self._build_main(main_frame)

        # Per-role tabs
        for role in ROLES:
            frame = ttk.Frame(nb)
            nb.add(frame, text=f"  {ROLE_LABEL[role]}  ")
            panel = RolePanel(frame, role, self)
            self._role_panels[role] = panel

        # Settings tab
        sett_frame = ttk.Frame(nb)
        nb.add(sett_frame, text="  Settings  ")
        self._build_settings(sett_frame)

    def _build_main(self, parent):
        # Header
        hdr = tk.Frame(parent, bg=DARKER)
        hdr.pack(fill="x", padx=8, pady=(8, 0))
        tk.Label(hdr, text="⚔  LOL Client Tool", bg=DARKER, fg=GOLD,
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=10, pady=8)
        tk.Label(hdr, text=f"v{APP_VERSION}", bg=DARKER, fg="#888888",
                 font=("Segoe UI", 9)).pack(side="left", pady=8)
        self._lbl_conn = tk.Label(hdr, text="● Waiting for client…",
                                  bg=DARKER, fg="#888888", font=("Segoe UI", 9))
        self._lbl_conn.pack(side="right", padx=12)

        # Toggles
        tgl = tk.Frame(parent, bg=DARK)
        tgl.pack(fill="x", padx=12, pady=(8, 4))

        for label, key in [
            ("Auto Accept",       "autoAccept"),
            ("Auto Pre-Pick",     "autoPrePick"),
            ("Auto Pick",         "autoPick"),
            ("Auto Ban",          "autoBan"),
            ("Auto Runes/Spells", "autoRunes"),
        ]:
            var = tk.BooleanVar(value=bool(self.cfg.get(key, True)))
            self._bool_vars[key] = var

            def _on_toggle(k=key, v=var):
                self.cfg[k] = v.get()

            tk.Checkbutton(tgl, text=label, variable=var,
                           bg=DARK, fg=TEXT,
                           activebackground=DARK, selectcolor=PANEL,
                           command=_on_toggle).pack(side="left", padx=8)

        # Action buttons
        btns = tk.Frame(parent, bg=DARK)
        btns.pack(fill="x", padx=12, pady=4)

        tk.Button(btns, text="Connect to Client",
                  bg=GOLD, fg="#000", activebackground=GOLD,
                  padx=10, command=self._connect,
                  **BTN_STYLE).pack(side="left", padx=(0, 6))

        self._btn_start = tk.Button(btns, text="▶  Start",
                                    bg=GREEN, fg=WHITE, activebackground=GREEN,
                                    padx=10, state="disabled",
                                    command=self._start, **BTN_STYLE)
        self._btn_start.pack(side="left", padx=(0, 6))

        self._btn_stop = tk.Button(btns, text="■  Stop",
                                   bg=RED, fg=WHITE, activebackground=RED,
                                   padx=10, state="disabled",
                                   command=self._stop, **BTN_STYLE)
        self._btn_stop.pack(side="left", padx=(0, 6))

        tk.Button(btns, text="Save Config",
                  bg=PANEL, fg=TEXT, activebackground=PANEL,
                  padx=10, command=self._save,
                  **BTN_STYLE).pack(side="right")

        tk.Button(btns, text="op.gg Auto-fill",
                  bg=PANEL, fg=GOLD, activebackground=PANEL,
                  padx=10, command=self._open_opgg_dialog,
                  **BTN_STYLE).pack(side="right", padx=(0, 6))

        # Assigned role indicator (updated while automation runs)
        self._lbl_role = tk.Label(parent, text="Assigned role: —",
                                  bg=DARK, fg=GOLD,
                                  font=("Segoe UI", 10, "bold"))
        self._lbl_role.pack(anchor="w", padx=14, pady=(4, 0))

        # Log
        self._log_box = scrolledtext.ScrolledText(
            parent, height=18, width=78,
            bg=DARKER, fg="#aaaaaa",
            font=("Consolas", 9), relief="flat",
            state="disabled", wrap="word")
        self._log_box.pack(padx=8, pady=8)

    def _build_settings(self, parent):
        f = tk.Frame(parent, bg=DARK)
        f.pack(padx=20, pady=20, anchor="nw")

        tk.Label(f, text="Timing  (seconds)", bg=DARK, fg=GOLD,
                 font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(0, 10), sticky="w")

        for i, (label, key, note) in enumerate([
            ("Pre-pick delay:",  "prePickDelay",
             "Hover champion before your turn"),
            ("Pick delay:",      "pickDelay",
             "Wait before locking champion"),
            ("Ban delay:",       "banDelay",
             "Wait before confirming ban"),
        ]):
            tk.Label(f, text=label, bg=DARK, fg=TEXT,
                     font=("Segoe UI", 9)).grid(
                row=i+1, column=0, sticky="w", pady=4)

            # Config is stored in milliseconds; the UI works in seconds.
            var = tk.DoubleVar(value=round(int(self.cfg.get(key, 1000)) / 1000, 1))
            self._delay_vars[key] = var

            def _on_change(k=key, v=var):
                self.cfg[k] = int(round(v.get() * 1000))

            tk.Spinbox(f, from_=0, to=60, increment=0.5, textvariable=var,
                       width=8, bg=PANEL, fg=WHITE, relief="flat", format="%.1f",
                       command=_on_change).grid(row=i+1, column=1, padx=10, sticky="w")

            tk.Label(f, text=note, bg=DARK, fg="#555",
                     font=("Segoe UI", 8)).grid(row=i+1, column=2, sticky="w")

        # Updates
        tk.Label(f, text="Updates", bg=DARK, fg=GOLD,
                 font=("Segoe UI", 11, "bold")).grid(
            row=5, column=0, columnspan=3, pady=(24, 6), sticky="w")
        tk.Button(f, text="Check for Updates", bg=PANEL, fg=WHITE, relief="flat",
                  font=("Segoe UI", 9), padx=12, pady=4,
                  command=self._manual_update_check).grid(
            row=6, column=0, sticky="w", pady=2)
        tk.Label(f, text=f"Current version: v{APP_VERSION}", bg=DARK, fg="#555",
                 font=("Segoe UI", 8)).grid(row=6, column=1, columnspan=2,
                                            padx=10, sticky="w")

        # Warning box
        warn = tk.Frame(f, bg="#2a1a1a", padx=12, pady=10)
        warn.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        tk.Label(warn, text="⚠  Terms of Service Warning", bg="#2a1a1a",
                 fg=RED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(warn,
                 text=(
                     "Third-party tools that automate client actions may violate\n"
                     "Riot Games' Terms of Service.  Account restrictions or bans\n"
                     "are possible.  Use at your own risk."
                 ),
                 bg="#2a1a1a", fg="#cc6666",
                 font=("Segoe UI", 9), justify="left").pack(anchor="w", pady=(4, 0))

    # ── Auto-connect watcher ──────────────────────────────────────────────────
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
    def _connect(self):
        """Manual connect button — useful if auto-connect hasn't fired yet."""
        def _do():
            self.log("Connecting to League client...")
            try:
                if self._lcu.connect() and self._lcu.ping():
                    self._connected = True
                    self.after(0, self._on_connected)
                else:
                    self.log("Could not connect — is the League client running?")
            except Exception as e:
                self.log(f"Connection error: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _on_connected(self):
        self._lbl_conn.config(text="● Connected", fg=GREEN)
        self.log("League client detected — connected automatically.")
        # Auto-start automation so it's already running by the time champ
        # select begins — no need to click Start manually.
        self._start()

    def _on_disconnected(self):
        self._lbl_conn.config(text="● Waiting for client…", fg="#888888")
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="disabled")
        self._engine.stop()
        self.log("League client closed. Waiting for it to reopen…")

    def _start(self):
        self._engine.start()
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self.log("Automation started.")

    def _stop(self):
        self._engine.stop()
        self._btn_stop.config(state="disabled")
        self._btn_start.config(state="normal")
        self.log("Automation stopped.")

    def _save(self):
        for k, v in self._delay_vars.items():
            self.cfg[k] = int(round(v.get() * 1000))   # seconds (UI) → ms (config)
        for k, v in self._bool_vars.items():
            self.cfg[k] = v.get()
        save_config(self.cfg)
        self.log("Config saved.")

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
                dl_url = next(
                    (a["browser_download_url"] for a in data.get("assets", [])
                     if a["name"].lower().endswith(".exe")),
                    None,
                )
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
        lb = panel.get_listbox(key)
        lb.delete(0, "end")
        for cid in self.cfg["roleChampions"][role][key]:
            lb.insert("end", f"  {self.ddragon.name(int(cid))}")

    def _refresh_all(self):
        for role in ROLES:
            for k in ("picks", "bans"):
                    self.refresh_list(role, k)

    def move_item(self, role: str, key: str, delta: int):
        panel = self._role_panels.get(role)
        if panel is None:
            return
        lb  = panel.get_listbox(key)
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        lst = self.cfg["roleChampions"][role][key]
        new = idx + delta
        if 0 <= new < len(lst):
            lst[idx], lst[new] = lst[new], lst[idx]
            self.refresh_list(role, key)
            lb.selection_set(new)

    def remove_item(self, role: str, key: str):
        panel = self._role_panels.get(role)
        if panel is None:
            return
        lb  = panel.get_listbox(key)
        sel = lb.curselection()
        if not sel:
            return
        self.cfg["roleChampions"][role][key].pop(sel[0])
        self.refresh_list(role, key)

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

        total = sum(g for _, _, g, _ in rows)
        status_fn(f"Found {len(rows)} champions from {total} season games (recent games weighted 2×).")
        return rows

    @staticmethod
    def _opgg_role(pos: str) -> str:
        p = pos.lower().strip()
        if p in ("top", "toplane"):                            return "top"
        if p in ("jungle", "jng", "jung", "jungler"):         return "jungle"
        if p in ("mid", "middle", "midlane"):                  return "middle"
        if p in ("bot", "bottom", "adc", "carry", "botlane"): return "bottom"
        if p in ("sup", "supp", "support", "utility"):        return "utility"
        return ""

    def _opgg_apply(self, rows: list, do_picks: bool, do_bans: bool,
                    dialog: "OpGGDialog"):
        by_role: dict = defaultdict(list)
        for role, name, games, wins in rows:
            bwr   = (wins + 1) / (games + 2)
            score = bwr * _math.sqrt(games)
            by_role[role].append((score, name))
        for role in by_role:
            by_role[role].sort(reverse=True)

        changed: list = []
        for role in ROLES:
            rc = self.cfg["roleChampions"].setdefault(
                role, {"picks": [], "bans": []}
            )
            if do_picks and role in by_role:
                ids = []
                for _, name in by_role[role][:5]:
                    cid = self.ddragon.find_id(name)
                    if cid is not None:
                        ids.append(cid)
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
    app = App()
    app.mainloop()