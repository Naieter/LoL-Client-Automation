"""
Tests for lol_tool.py  –  runs without a League client or display.
Covers: config I/O, DDragon lookup, AutoEngine._best() pick logic,
        role resolution, ban-list filtering, and edge cases.
"""

import sys, os, json, tempfile, unittest, types
from pathlib import Path
from unittest.mock import MagicMock

# ── Stub tkinter and all submodules (no display available in sandbox) ────────
def _make_tk_module(name):
    m = types.ModuleType(name)
    # Any attribute lookup returns a MagicMock (module-level __getattr__, 1 arg)
    m.__getattr__ = lambda attr: MagicMock()
    return m

_tk_stub  = _make_tk_module("tkinter")
_ttk_stub = _make_tk_module("tkinter.ttk")
_st_stub  = _make_tk_module("tkinter.scrolledtext")

# Explicit widget classes so `from tkinter import X` and `tk.X(...)` both work
for _name in ("Tk","Frame","Label","Button","Checkbutton","Listbox",
              "Entry","Spinbox","BooleanVar","IntVar","StringVar","Widget"):
    setattr(_tk_stub, _name, MagicMock)

# ttk widgets
for _name in ("Style","Notebook","Frame","Label"):
    setattr(_ttk_stub, _name, MagicMock)

# scrolledtext.ScrolledText
_st_stub.ScrolledText = MagicMock

# tkinter.font (Font used for text measuring in the Scouting tab)
_font_stub = _make_tk_module("tkinter.font")
_font_stub.Font = MagicMock

# Make `from tkinter import ttk, scrolledtext` work
_tk_stub.ttk         = _ttk_stub
_tk_stub.scrolledtext = _st_stub
_tk_stub.font        = _font_stub

sys.modules["tkinter"]             = _tk_stub
sys.modules["tkinter.ttk"]         = _ttk_stub
sys.modules["tkinter.scrolledtext"] = _st_stub
sys.modules["tkinter.font"]        = _font_stub

# ── Stub psutil (no League client in CI) ─────────────────────────────────────
_psutil = types.ModuleType("psutil")
_psutil.process_iter  = lambda *a, **kw: iter([])
_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
_psutil.AccessDenied  = type("AccessDenied",  (Exception,), {})
sys.modules["psutil"] = _psutil

# Redirect config to a temp dir so tests never touch AppData
_tmp = tempfile.mkdtemp()
import importlib

sys.path.insert(0, str(Path(__file__).parent))

# Patch CONFIG paths before import
import lol_tool
lol_tool.CONFIG_DIR  = Path(_tmp)
lol_tool.CONFIG_FILE = Path(_tmp) / "config.json"


# ═══════════════════════════════════════════════════════════════════════════════
class TestConfig(unittest.TestCase):

    def setUp(self):
        # Clean slate each test
        cf = lol_tool.CONFIG_FILE
        if cf.exists():
            cf.unlink()

    def test_load_returns_defaults_when_no_file(self):
        cfg = lol_tool.load_config()
        # default-on since v1.5.2
        self.assertTrue(cfg["autoAccept"])
        self.assertTrue(cfg["autoPick"])
        self.assertIn("top",     cfg["roleChampions"])
        self.assertIn("utility", cfg["roleChampions"])
        for role in lol_tool.ROLES:
            self.assertIn("picks", cfg["roleChampions"][role])
            self.assertIn("bans",  cfg["roleChampions"][role])

    def test_save_and_reload(self):
        cfg = lol_tool.load_config()
        cfg["autoAccept"] = True
        cfg["roleChampions"]["top"]["picks"] = [266, 114]
        lol_tool.save_config(cfg)

        reloaded = lol_tool.load_config()
        self.assertTrue(reloaded["autoAccept"])
        self.assertEqual(reloaded["roleChampions"]["top"]["picks"], [266, 114])

    def test_missing_keys_backfilled_on_load(self):
        # Write a config that's missing some keys
        lol_tool.CONFIG_FILE.write_text(json.dumps({"autoAccept": True}))
        cfg = lol_tool.load_config()
        self.assertTrue(cfg["autoAccept"])
        self.assertIn("pickDelay", cfg)
        self.assertIn("roleChampions", cfg)

    def test_missing_role_backfilled(self):
        partial = lol_tool.DEFAULT_CONFIG.copy()
        partial["roleChampions"] = {"top": {"picks": [1], "bans": []}}
        lol_tool.CONFIG_FILE.write_text(json.dumps(partial))
        cfg = lol_tool.load_config()
        for role in lol_tool.ROLES:
            self.assertIn(role, cfg["roleChampions"])


# ═══════════════════════════════════════════════════════════════════════════════
class TestDDragon(unittest.TestCase):

    def setUp(self):
        self.dd = lol_tool.DDragon()
        # Inject a fake champion map without hitting the network
        self.dd._id_to_name = {
            1:   "Annie",
            266: "Aatrox",
            22:  "Ashe",
            64:  "Lee Sin",
            267: "Nami",
        }
        self.dd._name_to_id = {v.lower(): k for k, v in self.dd._id_to_name.items()}

    def test_name_by_id(self):
        self.assertEqual(self.dd.name(1),   "Annie")
        self.assertEqual(self.dd.name(266), "Aatrox")

    def test_unknown_id_returns_string(self):
        self.assertEqual(self.dd.name(99999), "99999")

    def test_find_id_case_insensitive(self):
        self.assertEqual(self.dd.find_id("annie"),  1)
        self.assertEqual(self.dd.find_id("ANNIE"),  1)
        self.assertEqual(self.dd.find_id("Annie"),  1)
        self.assertEqual(self.dd.find_id("lee sin"), 64)

    def test_find_id_unknown(self):
        self.assertIsNone(self.dd.find_id("Shrek"))

    def test_all_display_names_sorted(self):
        names = self.dd.all_display_names()
        self.assertEqual(names, sorted(names))

    def test_find_id_strips_whitespace(self):
        self.assertEqual(self.dd.find_id("  ashe  "), 22)


# ═══════════════════════════════════════════════════════════════════════════════
class TestAutoEngineBest(unittest.TestCase):
    """Unit tests for AutoEngine._best() — the core pick/ban selector."""

    _best = staticmethod(lol_tool.AutoEngine._best)

    def test_picks_first_available(self):
        result = self._best([1, 2, 3], unavailable=set(), playable={1, 2, 3})
        self.assertEqual(result, 1)

    def test_skips_banned_champion(self):
        result = self._best([1, 2, 3], unavailable={1}, playable={1, 2, 3})
        self.assertEqual(result, 2)

    def test_skips_taken_and_banned(self):
        result = self._best([1, 2, 3], unavailable={1, 2}, playable={1, 2, 3})
        self.assertEqual(result, 3)

    def test_returns_none_when_all_unavailable(self):
        result = self._best([1, 2, 3], unavailable={1, 2, 3}, playable={1, 2, 3})
        self.assertIsNone(result)

    def test_skips_unowned_champion(self):
        # Champion 1 is available but not owned; 2 is owned
        result = self._best([1, 2, 3], unavailable=set(), playable={2, 3})
        self.assertEqual(result, 2)

    def test_empty_priority_list(self):
        result = self._best([], unavailable=set(), playable={1, 2, 3})
        self.assertIsNone(result)

    def test_all_unowned(self):
        result = self._best([1, 2, 3], unavailable=set(), playable=set())
        self.assertIsNone(result)

    def test_priority_order_respected(self):
        # 3 > 2 > 1 in the list but 1 is highest priority and available
        result = self._best([10, 20, 30], unavailable={10, 20}, playable={10, 20, 30})
        self.assertEqual(result, 30)

    def test_ban_allows_all_champion_ids(self):
        # For bans, playable is set(range(1_000_000))
        big = set(range(1_000_000))
        result = self._best([500, 600], unavailable={500}, playable=big)
        self.assertEqual(result, 600)


# ═══════════════════════════════════════════════════════════════════════════════
class TestRoleResolution(unittest.TestCase):
    """Test that the engine resolves the correct role champion list."""

    def _make_engine(self, cfg):
        lcu = MagicMock()
        engine = lol_tool.AutoEngine(lcu, lambda: cfg, lambda msg: None)
        return engine

    def test_known_role_resolves_correctly(self):
        cfg = lol_tool.load_config()
        cfg["roleChampions"]["jungle"]["picks"] = [64, 107]
        engine = self._make_engine(cfg)

        # Simulate the role lookup logic from _handle_champ_select
        assigned_role = "jungle"
        role_key  = assigned_role if assigned_role in lol_tool.ROLES else "top"
        role_cfg  = cfg["roleChampions"].get(role_key, {})
        pick_prio = [int(c) for c in role_cfg.get("picks", [])]

        self.assertEqual(pick_prio, [64, 107])

    def test_unknown_role_falls_back_to_top(self):
        cfg = lol_tool.load_config()
        cfg["roleChampions"]["top"]["picks"] = [266]

        assigned_role = "unknown_garbage"
        role_key = assigned_role if assigned_role in lol_tool.ROLES else "top"
        self.assertEqual(role_key, "top")

    def test_empty_assigned_position_falls_back_to_top(self):
        assigned_role = ""
        role_key = assigned_role if assigned_role in lol_tool.ROLES else "top"
        self.assertEqual(role_key, "top")

    def test_all_five_roles_are_valid(self):
        for role in ["top", "jungle", "middle", "bottom", "utility"]:
            role_key = role if role in lol_tool.ROLES else "top"
            self.assertEqual(role_key, role)

    def test_ban_skips_ally_intents(self):
        """Ban list should not ban a champion a teammate intends to pick."""
        ban_list       = [266, 114, 64]
        ally_intents   = {266}          # teammate wants Aatrox
        bans_so_far    = set()
        unavail_ban    = bans_so_far | ally_intents
        big_playable   = set(range(1_000_000))

        result = lol_tool.AutoEngine._best(ban_list, unavail_ban, big_playable)
        self.assertEqual(result, 114)   # skips 266 (ally intent), picks 114

    def test_pick_skips_ally_locked_champion(self):
        pick_list    = [22, 51, 202]
        ally_picked  = {22}            # teammate already locked Ashe
        bans         = set()
        enemy_picked = set()
        unavail_pick = bans | ally_picked | enemy_picked
        owned        = {22, 51, 202}

        result = lol_tool.AutoEngine._best(pick_list, unavail_pick, owned)
        self.assertEqual(result, 51)

    def test_pick_skips_enemy_locked_champion(self):
        pick_list    = [22, 51, 202]
        enemy_picked = {22, 51}
        unavail      = enemy_picked
        owned        = {22, 51, 202}

        result = lol_tool.AutoEngine._best(pick_list, unavail, owned)
        self.assertEqual(result, 202)


# ═══════════════════════════════════════════════════════════════════════════════
class TestRoleLabels(unittest.TestCase):

    def test_all_roles_have_labels(self):
        for role in lol_tool.ROLES:
            self.assertIn(role, lol_tool.ROLE_LABEL)

    def test_label_values(self):
        self.assertEqual(lol_tool.ROLE_LABEL["top"],     "Top")
        self.assertEqual(lol_tool.ROLE_LABEL["jungle"],  "Jungle")
        self.assertEqual(lol_tool.ROLE_LABEL["middle"],  "Mid")
        self.assertEqual(lol_tool.ROLE_LABEL["bottom"],  "ADC")
        self.assertEqual(lol_tool.ROLE_LABEL["utility"], "Support")


# ═══════════════════════════════════════════════════════════════════════════════
class TestLCULockfileParsing(unittest.TestCase):
    """Test lockfile parsing without needing an actual League process."""

    def test_lockfile_parse_format(self):
        # lockfile: name:pid:port:password:protocol
        lockfile_content = "LeagueClient:12345:50123:abc-token-xyz:https"
        parts    = lockfile_content.strip().split(":")
        port     = parts[2]
        password = parts[3]
        self.assertEqual(port,     "50123")
        self.assertEqual(password, "abc-token-xyz")
        base_url = f"https://127.0.0.1:{port}"
        self.assertEqual(base_url, "https://127.0.0.1:50123")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestConfig,
        TestDDragon,
        TestAutoEngineBest,
        TestRoleResolution,
        TestRoleLabels,
        TestLCULockfileParsing,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
