#!/usr/bin/env python3
"""
LOL Client Tool  –  Role-Based Champion Selection  (Python rebuild)

Uses the LCU (League Client Update) local API to automate champion select.
Detects your assigned role each game and picks from your per-role priority list.

WARNING: Third-party automation tools may violate Riot Games' Terms of Service
and could result in account penalties. Use at your own risk.
"""

import sys, os, json, threading, time
from pathlib import Path

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
CONFIG_DIR  = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LOL_Client_TOOL"
CONFIG_FILE = CONFIG_DIR / "config.json"
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

DEFAULT_CONFIG = {
    "autoAccept":   False,
    "autoPick":     False,
    "autoPrePick":  False,
    "autoBan":      False,
    "autoReplay":   False,
    "pickDelay":    1500,
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


# ── DDragon champion data ─────────────────────────────────────────────────────
class DDragon:
    def __init__(self):
        self._id_to_name: dict = {}
        self._name_to_id: dict = {}   # lowercase name → int id

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
        except Exception as e:
            print(f"[DDragon] {e}")

    def name(self, champ_id: int) -> str:
        return self._id_to_name.get(int(champ_id), str(champ_id))

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


# ── Automation engine ─────────────────────────────────────────────────────────
class AutoEngine:
    """Polls the LCU every 2 s and acts based on game flow phase."""

    POLL = 2  # seconds

    def __init__(self, lcu: LCU, cfg_fn, log_fn):
        self._lcu          = lcu
        self._cfg          = cfg_fn    # callable → dict
        self._log          = log_fn    # callable(str)
        self._stop         = threading.Event()
        self._last_phase   = ""
        self._done_actions: set = set()   # action IDs already processed
        self._replay_sent  = False        # play-again already requested this game

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
            if phase != "EndOfGame":
                self._replay_sent = False

        # Auto accept
        if cfg.get("autoAccept") and phase == "ReadyCheck":
            self._lcu.post("/lol-matchmaking/v1/ready-check/accept")
            self._log("Auto-accepted match.")

        if phase == "ChampSelect":
            self._handle_champ_select(cfg)

        # Auto replay — click "Play Again" to requeue once the post-game
        # lobby is up, instead of having to return to the client manually.
        if cfg.get("autoReplay") and phase == "EndOfGame" and not self._replay_sent:
            r = self._lcu.post("/lol-lobby/v2/play-again")
            if r.status_code in (200, 204):
                self._log("Auto-replay: requeued for another game.")
                self._replay_sent = True

    # ── Champion select ───────────────────────────────────────────────────────
    def _handle_champ_select(self, cfg: dict):
        r = self._lcu.get("/lol-champ-select/v1/session")
        if r.status_code != 200:
            return
        session = r.json()

        # Collect banned champion IDs
        bans: set = set()
        for bid in session.get("bans", {}).get("myTeamBans", []):
            bans.add(int(bid))
        for bid in session.get("bans", {}).get("theirTeamBans", []):
            bans.add(int(bid))

        # Find my summoner's cell, role, and what allies have picked/intend
        my_sid         = self._get_summoner_id()
        my_cell        = None
        assigned_role  = ""
        my_champ_id    = 0
        ally_picked:   set = set()
        ally_intents:  set = set()
        enemy_picked:  set = set()

        for p in session.get("myTeam", []):
            sid    = str(p.get("summonerId", ""))
            cid    = int(p.get("championId", 0) or 0)
            intent = int(p.get("championPickIntent", 0) or 0)
            if sid == str(my_sid):
                my_cell       = str(p.get("cellId", ""))
                assigned_role = (p.get("assignedPosition") or "").lower()
                my_champ_id   = cid
            else:
                if cid:    ally_picked.add(cid)
                if intent: ally_intents.add(intent)

        for p in session.get("theirTeam", []):
            cid = int(p.get("championId", 0) or 0)
            if cid: enemy_picked.add(cid)

        if my_cell is None:
            return  # summoner not found in session yet

        # Build availability sets
        unavailable_for_pick = bans | ally_picked | enemy_picked
        unavailable_for_ban  = bans | ally_intents    # don't ban what allies want

        # Resolve per-role champion lists
        role_key = assigned_role if assigned_role in ROLES else "top"
        role_cfg = cfg.get("roleChampions", {}).get(role_key, {})
        pick_prio = [int(c) for c in role_cfg.get("picks", [])]
        ban_prio  = [int(c) for c in role_cfg.get("bans",  [])]

        # Champions we actually own / can play
        owned = self._get_owned_ids()

        # Process each action in the action matrix
        for action_group in session.get("actions", []):
            for action in action_group:
                if str(action.get("actorCellId", "")) != my_cell:
                    continue

                aid         = int(action.get("id", -1))
                atype       = action.get("type", "")
                in_progress = bool(action.get("isInProgress", False))
                completed   = bool(action.get("completed", False))

                # Pre-pick (hover intent before your turn)
                if (cfg.get("autoPrePick")
                        and atype == "pick"
                        and not in_progress
                        and not completed
                        and aid not in self._done_actions):
                    champ = self._best(pick_prio, unavailable_for_pick, owned)
                    if champ and champ != my_champ_id:
                        time.sleep(cfg.get("prePickDelay", 500) / 1000)
                        self._lcu.patch(
                            f"/lol-champ-select/v1/session/actions/{aid}",
                            {"championId": champ, "completed": False},
                        )
                        self._log(
                            f"Pre-pick hover: #{champ}  "
                            f"[{ROLE_LABEL.get(role_key, role_key)}]"
                        )

                # Lock pick
                if (cfg.get("autoPick")
                        and atype == "pick"
                        and in_progress
                        and not completed
                        and aid not in self._done_actions):
                    champ = self._best(pick_prio, unavailable_for_pick, owned)
                    if champ:
                        time.sleep(cfg.get("pickDelay", 1500) / 1000)
                        self._lcu.patch(
                            f"/lol-champ-select/v1/session/actions/{aid}",
                            {"championId": champ, "completed": True},
                        )
                        self._log(
                            f"Locked champion #{champ} as "
                            f"{ROLE_LABEL.get(role_key, role_key)}"
                        )
                        self._done_actions.add(aid)
                    else:
                        self._log(
                            f"No available pick for {ROLE_LABEL.get(role_key, role_key)}. "
                            f"Add more champions to that role's list!"
                        )

                # Ban
                if (cfg.get("autoBan")
                        and atype == "ban"
                        and in_progress
                        and not completed
                        and aid not in self._done_actions):
                    # any champion can be banned (no ownership check)
                    champ = self._best(ban_prio, unavailable_for_ban,
                                       set(range(1_000_000)))
                    if champ:
                        time.sleep(cfg.get("banDelay", 2000) / 1000)
                        self._lcu.patch(
                            f"/lol-champ-select/v1/session/actions/{aid}",
                            {"championId": champ, "completed": True},
                        )
                        self._log(f"Banned champion #{champ}")
                        self._done_actions.add(aid)
                    else:
                        self._log(
                            f"No valid ban for {ROLE_LABEL.get(role_key, role_key)}. "
                            f"Add champions to that role's ban list!"
                        )

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

    def _get_owned_ids(self) -> set:
        try:
            r = self._lcu.get("/lol-champions/v1/owned-champions-minimal")
            if r.status_code != 200:
                return set(range(1_000_000))   # fallback: assume all playable
            owned = set()
            for c in r.json():
                if c.get("active") and (
                    c.get("ownership", {}).get("owned") or c.get("freeToPlay")
                ):
                    owned.add(int(c["id"]))
            return owned
        except Exception:
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.configure(bg=DARK)
        self.resizable(False, False)

        self.cfg     = load_config()
        self.ddragon = DDragon()
        self._lcu    = LCU()
        self._engine = AutoEngine(self._lcu, lambda: self.cfg, self.log)

        self._role_panels: dict = {}   # role → RolePanel
        self._delay_vars:  dict = {}
        self._bool_vars:   dict = {}
        self._connected    = False

        self._build_ui()

        # Load champion data in background
        threading.Thread(target=self._load_champs, daemon=True).start()

        # Watch for the League client to open (auto-connect on launch)
        threading.Thread(target=self._watch_for_client, daemon=True).start()

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
        self._lbl_conn = tk.Label(hdr, text="● Waiting for client…",
                                  bg=DARKER, fg="#888888", font=("Segoe UI", 9))
        self._lbl_conn.pack(side="right", padx=12)

        # Toggles
        tgl = tk.Frame(parent, bg=DARK)
        tgl.pack(fill="x", padx=12, pady=(8, 4))

        for label, key in [
            ("Auto Accept",   "autoAccept"),
            ("Auto Pre-Pick", "autoPrePick"),
            ("Auto Pick",     "autoPick"),
            ("Auto Ban",      "autoBan"),
            ("Auto Replay",   "autoReplay"),
        ]:
            var = tk.BooleanVar(value=bool(self.cfg.get(key, False)))
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

        tk.Label(f, text="Timing  (milliseconds)", bg=DARK, fg=GOLD,
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

            var = tk.IntVar(value=int(self.cfg.get(key, 1000)))
            self._delay_vars[key] = var

            def _on_change(k=key, v=var):
                self.cfg[k] = v.get()

            tk.Spinbox(f, from_=0, to=10000, increment=250, textvariable=var,
                       width=8, bg=PANEL, fg=WHITE, relief="flat",
                       command=_on_change).grid(row=i+1, column=1, padx=10, sticky="w")

            tk.Label(f, text=note, bg=DARK, fg="#555",
                     font=("Segoe UI", 8)).grid(row=i+1, column=2, sticky="w")

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
        self._btn_start.config(state="normal")
        self.log("League client detected — connected automatically.")

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
            self.cfg[k] = v.get()
        for k, v in self._bool_vars.items():
            self.cfg[k] = v.get()
        save_config(self.cfg)
        self.log("Config saved.")

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

    # ── Log ───────────────────────────────────────────────────────────────────
    def log(self, msg: str):
        def _do():
            ts = time.strftime("%H:%M:%S")
            self._log_box.config(state="normal")
            self._log_box.insert("end", f"[{ts}]  {msg}\n")
            self._log_box.see("end")
            if int(self._log_box.index("end-1c").split(".")[0]) > 500:
                self._log_box.delete("1.0", "2.0")
            self._log_box.config(state="disabled")
        self.after(0, _do)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()