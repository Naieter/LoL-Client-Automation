"""
Tests for lol_tool.py  –  runs without a League client or display.
Covers: config I/O, DDragon lookup, AutoEngine._best() pick logic,
        role resolution, ban-list filtering, ARAM bench-swap/reroll decisions,
        op.gg game modes + ARAM payload parsing, build-overlay helpers.
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
class TestAram(unittest.TestCase):
    """ARAM bench-swap / reroll decisions and config — pure, no client."""

    swap   = staticmethod(lol_tool.AutoEngine._aram_swap_target)
    reroll = staticmethod(lol_tool.AutoEngine._aram_should_reroll)
    bench  = staticmethod(lol_tool.AutoEngine._aram_bench_ids)

    def setUp(self):
        cf = lol_tool.CONFIG_FILE
        if cf.exists():
            cf.unlink()

    # ── config ──
    def test_defaults_have_aram_list_and_toggles(self):
        cfg = lol_tool.load_config()
        self.assertIn(lol_tool.ARAM_ROLE, cfg["roleChampions"])
        self.assertEqual(cfg["roleChampions"][lol_tool.ARAM_ROLE]["picks"], [])
        self.assertTrue(cfg["autoAramSwap"])
        self.assertTrue(cfg["autoAramReroll"])

    def test_old_config_gets_aram_list_backfilled(self):
        old = {"autoAccept": True,
               "roleChampions": {r: {"picks": [], "bans": []} for r in lol_tool.ROLES}}
        lol_tool.CONFIG_FILE.write_text(json.dumps(old))
        cfg = lol_tool.load_config()
        self.assertEqual(cfg["roleChampions"][lol_tool.ARAM_ROLE], {"picks": [], "bans": []})
        self.assertTrue(cfg["autoAramSwap"])          # new toggles default on

    def test_aram_is_not_a_draft_role(self):
        # The draft pick/ban code iterates ROLES; ARAM must stay out of it.
        self.assertNotIn(lol_tool.ARAM_ROLE, lol_tool.ROLES)
        self.assertEqual(lol_tool.ROLE_LABEL[lol_tool.ARAM_ROLE], "ARAM")
        self.assertEqual(lol_tool.ROLE_SPELLS[lol_tool.ARAM_ROLE][0], 4)   # Flash

    # ── bench swap ──
    def test_swaps_to_higher_priority_bench_champion(self):
        self.assertEqual(self.swap([1, 2, 3], current=3, bench=[9, 2]), 2)

    def test_no_swap_when_current_outranks_bench(self):
        self.assertIsNone(self.swap([1, 2, 3], current=1, bench=[2, 3]))

    def test_no_swap_when_bench_has_nothing_listed(self):
        self.assertIsNone(self.swap([1, 2], current=5, bench=[7, 8]))

    def test_unlisted_current_swaps_to_any_listed_bench_champion(self):
        self.assertEqual(self.swap([4, 5], current=99, bench=[5]), 5)

    def test_picks_the_best_of_several_bench_options(self):
        self.assertEqual(self.swap([1, 2, 3, 4], current=4, bench=[3, 2]), 2)

    def test_empty_priority_never_swaps(self):
        self.assertIsNone(self.swap([], current=1, bench=[2, 3]))

    def test_swap_tolerates_string_ids(self):
        self.assertEqual(self.swap(["10", "20"], current="20", bench=["10"]), 10)

    # ── reroll ──
    def test_reroll_only_when_nothing_listed_in_hand_or_on_bench(self):
        self.assertTrue(self.reroll([1, 2], current=5, bench=[7], rerolls_remaining=1))
        self.assertFalse(self.reroll([1, 2], current=1, bench=[7], rerolls_remaining=1))
        self.assertFalse(self.reroll([1, 2], current=5, bench=[2], rerolls_remaining=1))

    def test_no_reroll_without_rerolls_list_or_permission(self):
        self.assertFalse(self.reroll([1], current=5, bench=[], rerolls_remaining=0))
        self.assertFalse(self.reroll([],  current=5, bench=[], rerolls_remaining=2))
        self.assertFalse(self.reroll([1], current=5, bench=[], rerolls_remaining=2,
                                     allow=False))
        self.assertFalse(self.reroll([1], current=5, bench=[], rerolls_remaining=None))

    # ── bench parsing ──
    def test_bench_ids_from_both_lcu_shapes(self):
        self.assertEqual(self.bench({"benchChampions": [{"championId": 12},
                                                        {"championId": 0}]}), [12])
        self.assertEqual(self.bench({"benchChampionIds": [3, "4", None]}), [3, 4])
        self.assertEqual(self.bench({"benchEnabled": True}), [])
        self.assertEqual(self.bench({}), [])


# ═══════════════════════════════════════════════════════════════════════════════
# A real op.gg MCP ARAM payload (Jinx, captured 2026-10-18) — the parser must
# pull item ids, names, spells and skills out of it unchanged.
_OPGG_ARAM_SAMPLE = """class LolGetChampionAnalysis: data
class Data: summary,core_items,boots,starter_items,fourth_items,fifth_items,summoner_spells,runes,skills,skill_masteries,counters_meta
class Summary: average_stats
class AverageStats: win_rate,pick_rate
class CoreItems: ids,ids_names
class FifthItem: ids_names
class SummonerSpells: ids
class Runes: primary_page_name,primary_rune_names,secondary_page_name,secondary_rune_names
class Skills: order
class CountersMeta: message

LolGetChampionAnalysis(Data(Summary(AverageStats(0.53,0.13)),CoreItems([3032,3085,3031],["Yun Tal Wildarrows","Runaan's Hurricane","Infinity Edge"]),CoreItems([3006],["Berserker's Greaves"]),CoreItems([1038,2003,2003],["B. F. Sword","Health Potion","Health Potion"]),[CoreItems([3036],["Lord Dominik's Regards"]),CoreItems([3031],["Infinity Edge"]),CoreItems([3072],["Bloodthirster"])],[FifthItem(["Bloodthirster"]),FifthItem(["Blade of The Ruined King"]),FifthItem(["Infinity Edge"])],SummonerSpells([4,6]),Runes("Precision",["Lethal Tempo","Presence of Mind","Legend: Bloodline","Coup de Grace"],"Domination",["Treasure Hunter","Taste of Blood"]),Skills(["Q","W","E","Q","Q","R","Q","W","Q","W","R","W","W","E","E"]),SummonerSpells(["Q","W","E"]),CountersMeta("Insufficient matchup sample. See data.summary.positions[].counters[] for raw matchup data (smaller sample).")))
"""


class TestOpggModes(unittest.TestCase):

    def test_lcu_game_mode_maps_to_opgg_mode(self):
        self.assertEqual(lol_tool._opgg_mode("ARAM"), "aram")
        self.assertEqual(lol_tool._opgg_mode("aram"), "aram")
        self.assertEqual(lol_tool._opgg_mode("CLASSIC"), "ranked")
        self.assertEqual(lol_tool._opgg_mode("PRACTICETOOL"), "ranked")
        self.assertEqual(lol_tool._opgg_mode("URF"), "urf")
        self.assertEqual(lol_tool._opgg_mode("NEXUSBLITZ"), "nexus_blitz")
        self.assertEqual(lol_tool._opgg_mode(""), "ranked")
        self.assertEqual(lol_tool._opgg_mode(None), "ranked")

    def test_aram_payload_parses_items_spells_and_skills(self):
        rec = lol_tool._parse_opgg_record(_OPGG_ARAM_SAMPLE)
        self.assertIsNotNone(rec)
        d = rec["data"]
        self.assertEqual(d["core_items"]["ids"], [3032, 3085, 3031])
        self.assertEqual(d["core_items"]["ids_names"][0], "Yun Tal Wildarrows")
        self.assertEqual(d["boots"]["ids_names"], ["Berserker's Greaves"])
        self.assertEqual(d["starter_items"]["ids"], [1038, 2003, 2003])
        self.assertEqual(d["summoner_spells"]["ids"], [4, 6])          # Flash + Ghost
        self.assertEqual(d["fourth_items"][0]["ids"], [3036])
        self.assertEqual(d["skills"]["order"][:3], ["Q", "W", "E"])
        self.assertEqual(d["runes"]["primary_rune_names"][0], "Lethal Tempo")
        self.assertAlmostEqual(d["summary"]["average_stats"]["win_rate"], 0.53)


# ═══════════════════════════════════════════════════════════════════════════════
class TestBuildOverlayHelpers(unittest.TestCase):

    def setUp(self):
        cf = lol_tool.CONFIG_FILE
        if cf.exists():
            cf.unlink()

    def test_default_build_overlay_hotkey_is_ctrl_z(self):
        cfg = lol_tool.load_config()
        self.assertEqual(cfg["buildOverlayKey"], "Ctrl+Z")
        self.assertEqual(lol_tool._overlay_parse_combo("Ctrl+Z"), ([0x11], 0x5A))
        self.assertEqual(lol_tool._overlay_combo_label("Ctrl+Z"), "Ctrl+Z")
        self.assertIsNone(cfg["buildOverlayX"])
        self.assertFalse(cfg["buildOverlayAutoShow"])

    def test_old_config_gets_ctrl_z_default(self):
        lol_tool.CONFIG_FILE.write_text(json.dumps({"autoAccept": True}))
        cfg = lol_tool.load_config()
        self.assertEqual(cfg["buildOverlayKey"], "Ctrl+Z")

    def test_summary_lines_cover_every_section_in_order(self):
        data = {
            "champion": "Jinx", "position": "aram",
            "starter": ["B. F. Sword", "Health Potion"],
            "core": ["Yun Tal Wildarrows", "Runaan's Hurricane"],
            "boots": ["Berserker's Greaves"],
            "situational": ["Lord Dominik's Regards"],
            "skill_order": ["Q", "W", "E", "Q"], "max_order": ["Q", "W", "E"],
            "spells": ["Flash", "Ghost"],
            "rune_primary_page": "Precision", "rune_primary": ["Lethal Tempo", "Presence of Mind"],
            "rune_secondary_page": "Domination", "rune_secondary": ["Treasure Hunter"],
        }
        rows = lol_tool._build_summary_lines(data)
        self.assertEqual([r[0] for r in rows],
                         ["Start", "Core", "Boots", "Later", "Skills", "Spells", "Runes"])
        text = dict(rows)
        self.assertEqual(text["Core"], "Yun Tal Wildarrows › Runaan's Hurricane")
        self.assertIn("Q › W › E", text["Skills"])
        self.assertIn("start Q W E", text["Skills"])
        self.assertEqual(text["Spells"], "Flash + Ghost")
        self.assertEqual(text["Runes"], "Lethal Tempo  ·  Precision + Domination")

    def test_summary_lines_empty_for_missing_or_error(self):
        self.assertEqual(lol_tool._build_summary_lines(None), [])
        self.assertEqual(lol_tool._build_summary_lines({"error": "nope"}), [])
        self.assertEqual(lol_tool._build_summary_lines({"champion": "Lux"}), [])

    def test_overlay_pairs_items_with_ids(self):
        data = {"core": ["A", "B", "C"], "core_ids": [1, 0]}
        self.assertEqual(lol_tool.BuildOverlay._pairs(data, "core"),
                         [(1, "A"), (0, "B"), (0, "C")])
        self.assertEqual(lol_tool.BuildOverlay._pairs({}, "boots"), [])



# ═══════════════════════════════════════════════════════════════════════════════
class _FakeResp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body
    def json(self):
        return self._body


class _ScoutHost:
    """Just enough of App for the roster builders: an LCU stub keyed by path,
    the real resolver methods, and a champion namer."""
    _scout_member_identity    = staticmethod(lol_tool.App._scout_member_identity)
    _scout_resolve_summoner   = lol_tool.App._scout_resolve_summoner
    _scout_resolve_puuid      = lol_tool.App._scout_resolve_puuid
    _scout_my_summoner_id     = lol_tool.App._scout_my_summoner_id
    _scout_lobby_roster       = lol_tool.App._scout_lobby_roster
    _scout_champselect_roster = lol_tool.App._scout_champselect_roster

    def __init__(self, routes):
        self._routes = routes
        self._lcu = MagicMock()
        self._lcu.get.side_effect = lambda ep: _FakeResp(
            200, routes[ep]) if ep in routes else _FakeResp(404)

    def _scout_champ_name(self, cid):
        return f"Champ{cid}" if cid else None


class TestScoutingBothTeams(unittest.TestCase):

    def test_champ_select_scouts_their_team_when_names_are_exposed(self):
        host = _ScoutHost({
            "/lol-champ-select/v1/session": {
                "myTeam": [{"summonerId": 1, "championId": 22}],
                "theirTeam": [{"summonerId": 2, "championId": 51},
                              {"summonerId": 0, "puuid": "abc-123", "championId": 0},
                              {"summonerId": 0, "puuid": "", "championId": 0}],
            },
            "/lol-summoner/v1/summoners/1": {"gameName": "Me", "tagLine": "NA1"},
            "/lol-summoner/v1/summoners/2": {"gameName": "Foe", "tagLine": "EUW"},
            "/lol-summoner/v2/summoners/puuid/abc-123": {"gameName": "Foe2", "tagLine": "NA1"},
        })
        roster = host._scout_champselect_roster()
        self.assertEqual([(p["name"], p["ally"], p.get("champ")) for p in roster],
                         [("Me", True, "Champ22"), ("Foe", False, "Champ51"),
                          ("Foe2", False, None)])

    def test_champ_select_hidden_enemies_yield_allies_only(self):
        host = _ScoutHost({
            "/lol-champ-select/v1/session": {
                "myTeam": [{"summonerId": 1, "championPickIntent": 7}],
                "theirTeam": [{"summonerId": 0, "puuid": "", "championId": 0}] * 5,
            },
            "/lol-summoner/v1/summoners/1": {"gameName": "Me", "tagLine": "NA1"},
        })
        roster = host._scout_champselect_roster()
        self.assertEqual([(p["name"], p["ally"]) for p in roster], [("Me", True)])

    def test_custom_lobby_scouts_both_sides_relative_to_me(self):
        host = _ScoutHost({
            "/lol-lobby/v2/lobby": {
                "members": [{"summonerId": 1, "gameName": "Me", "tagLine": "NA1"}],
                "gameConfig": {
                    "customTeam100": [{"summonerId": 9, "gameName": "Foe", "tagLine": "NA1"}],
                    "customTeam200": [{"summonerId": 1, "gameName": "Me", "tagLine": "NA1"},
                                      {"summonerId": 3, "gameName": "Pal", "tagLine": "NA1"}],
                },
            },
            "/lol-summoner/v1/current-summoner": {"summonerId": 1},
        })
        roster = host._scout_lobby_roster()
        self.assertEqual(sorted((p["name"], p["ally"]) for p in roster),
                         [("Foe", False), ("Me", True), ("Pal", True)])

    def test_normal_lobby_is_allies_only_without_duplicates(self):
        host = _ScoutHost({
            "/lol-lobby/v2/lobby": {
                "members": [{"summonerId": 1, "gameName": "Me", "tagLine": "NA1"},
                            {"summonerId": 2, "gameName": "Pal", "tagLine": "NA1"}],
                "gameConfig": {"customTeam100": [], "customTeam200": []},
            },
        })
        roster = host._scout_lobby_roster()
        self.assertEqual([(p["name"], p["ally"]) for p in roster],
                         [("Me", True), ("Pal", True)])


# ═══════════════════════════════════════════════════════════════════════════════
class _HttpResp:
    def __init__(self, body=None, status=200):
        self._body, self.status = body, status
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")
    def json(self):
        return self._body


def _ok(text="payload"):
    return _HttpResp({"result": {"content": [{"text": text}]}})


class TestOpggRetry(unittest.TestCase):
    """Every op.gg lookup retries once, 15 s after a failure."""

    def _run(self, replies, **kw):
        """Run _opgg_post against a scripted list of replies/exceptions.
        Returns (result_or_exception, post_calls, sleeps)."""
        calls, sleeps = [], []
        it = iter(replies)

        def fake_post(url, **k):
            calls.append(url)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r

        real_post, real_sleep = lol_tool.requests.post, lol_tool.time.sleep
        lol_tool.requests.post = fake_post
        lol_tool.time.sleep = sleeps.append
        try:
            try:
                out = lol_tool._opgg_post({"x": 1}, 5, **kw)
            except Exception as e:
                out = e
        finally:
            lol_tool.requests.post, lol_tool.time.sleep = real_post, real_sleep
        return out, calls, sleeps

    def test_delay_is_fifteen_seconds(self):
        self.assertEqual(lol_tool.OPGG_RETRY_DELAY, 15)

    def test_success_first_try_does_not_wait_or_retry(self):
        out, calls, sleeps = self._run([_ok("hi")])
        self.assertEqual((out, len(calls), sleeps), ("hi", 1, []))

    def test_connection_error_retries_once_after_15s(self):
        out, calls, sleeps = self._run([ConnectionError("down"), _ok("later")])
        self.assertEqual(out, "later")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [15])

    def test_http_error_then_success(self):
        out, calls, sleeps = self._run([_HttpResp(status=503), _ok("fine")])
        self.assertEqual((out, len(calls), sleeps), ("fine", 2, [15]))

    def test_malformed_reply_is_retried(self):
        out, calls, sleeps = self._run([_HttpResp({"result": {}}), _ok("fine")])
        self.assertEqual((out, len(calls), sleeps), ("fine", 2, [15]))

    def test_second_failure_propagates_after_exactly_one_retry(self):
        out, calls, sleeps = self._run([TimeoutError("a"), TimeoutError("b"), _ok()])
        self.assertIsInstance(out, TimeoutError)
        self.assertEqual(str(out), "b")
        self.assertEqual((len(calls), sleeps), (2, [15]))     # never a third try

    def test_validation_error_is_not_retried(self):
        bad = _HttpResp({"error": {"code": -32600, "message": "position required"}})
        out, calls, sleeps = self._run([bad, _ok()])
        self.assertIsInstance(out, lol_tool._OpggError)
        self.assertEqual((len(calls), sleeps), (1, []))

    def test_other_rpc_error_is_retried(self):
        flaky = _HttpResp({"error": {"code": -32000, "message": "busy"}})
        out, calls, sleeps = self._run([flaky, _ok("ok")])
        self.assertEqual((out, len(calls), sleeps), ("ok", 2, [15]))

    def test_on_retry_callback_runs_before_the_wait(self):
        seen = []
        out, calls, sleeps = self._run([OSError("net"), _ok("ok")],
                                       on_retry=lambda e: seen.append(str(e)))
        self.assertEqual((out, seen, sleeps), ("ok", ["net"], [15]))

    def test_broken_callback_does_not_break_the_retry(self):
        def boom(_e):
            raise ValueError("ui gone")
        out, _calls, sleeps = self._run([OSError("net"), _ok("ok")], on_retry=boom)
        self.assertEqual((out, sleeps), ("ok", [15]))

    def test_no_op_gg_call_bypasses_the_helper(self):
        import re
        src = Path(lol_tool.__file__).read_text(encoding="utf-8")
        direct = [m.start() for m in re.finditer(r"requests\.post\(", src)]
        # the only op.gg POST left is the one inside _opgg_post (the other is
        # the ready-up relay)
        lines = [src[:i].count("\n") + 1 for i in direct]
        urls = [src.splitlines()[ln - 1] + src.splitlines()[ln] for ln in lines]
        self.assertEqual(sum("op.gg" in u or "OPGG_URL" in u for u in urls), 1, urls)

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
        TestAram,
        TestOpggModes,
        TestBuildOverlayHelpers,
        TestScoutingBothTeams,
        TestOpggRetry,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
