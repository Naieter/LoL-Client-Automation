# LOL Client Tool

A Windows desktop tool that connects to the League of Legends client and automates champion select, ready checks, and party coordination.

> **Note:** Third-party tools that automate client actions may violate Riot Games' Terms of Service. Use at your own risk.

---

## Download

Grab the latest release from the [Releases page](https://github.com/Naieter/LoL-Client-Automation/releases/latest):

- **`LOL_Client_Tool.exe`** — the main tool (run on every party member's PC)
- **`LOL_Relay.exe`** — the ready-up relay server (run on one PC or server, reachable by everyone in the party)

---

## Features

### Champion Select Automation
- **Auto Accept** — accepts ready checks automatically (configurable delay)
- **Auto Pick** — locks your champion from a per-role priority list (configurable delay)
- **Auto Pre-Pick** — hovers your champion during the planning phase
- **Auto Ban** — bans from your per-role ban list (configurable delay); perma-ban list always goes first regardless of role
- **Auto Runes** — imports meta rune pages and summoner spells from op.gg for your locked champion

### Party Ready-Up
Coordinate queue with your party — everyone marks themselves ready in the tool and the leader's client starts the queue automatically when all tool users are ready.

- Shows a **floating overlay button** over the League client (draggable, always-on-top)
  - **Red** = not ready, **Green** = ready — click to toggle
  - Displays `READY UP [X/Y]` (tool users ready / tool users present)
  - Shows a small **ping button** when solo in a lobby or when no other party members have the tool
  - Hides automatically during a ready check, when not in a lobby, or when you switch to another app
- Ready state **auto-clears** when a match is declined or missed, or when all other tool users leave
- The overlay can be **toggled on/off** from Settings, or via a configurable keybind (any key, with optional Ctrl/Alt/Shift modifier)

### Auto-Accept Invites
Automatically accepts lobby invites from friends.

- Optionally restrict to a whitelist of specific summoners (`SummonerName#TagLine` format, comma-separated)
- Sends `"Invite auto-accepted"` in party chat after joining
- Leave the whitelist blank to accept from any friend

### Stream Deck Integration
A local REST API lets a Stream Deck (or any HTTP client) trigger actions:

| Endpoint | Action |
|---|---|
| `GET /ready-up` | Toggle your ready state |
| `GET /accept` | Accept a ready check |
| `GET /status` | JSON: current phase, ready state, party counts |

Enable it in **Settings → Stream Deck API** by entering a port (e.g. `8778`). Set to `0` to disable. Requires restart after changing the port.

**Stream Deck setup:** add a "Website" button pointed at `http://127.0.0.1:8778/ready-up`.

### Other
- **Ping display** — live latency to Riot's regional servers, shown under the overlay button
- **Auto-update** — checks GitHub for new releases on launch and prompts to update
- **System tray** — minimizes to tray; can launch hidden at Windows startup
- **Ultimate Bravery** — rolls a random available champion during champion select

---

## Client Tool Setup

1. Download **`LOL_Client_Tool.exe`** from [Releases](https://github.com/Naieter/LoL-Client-Automation/releases/latest)
2. Run it — it connects to the League client automatically when League opens
3. Go to each role tab (Top, Jungle, Mid, ADC, Support) and add your champion priorities
4. Automations are on by default — toggle them in **Settings**

### Per-Role Champion Priorities

Each role has a **Picks** list and a **Bans** list. Champions are locked/banned in order — the first available champion in your list that isn't already picked or banned wins.

### Perma-Bans

Champions in the perma-ban list are always banned first, before role-specific bans, regardless of your assigned position.

### Configuring Delays

All delays are in **Settings → Timings**. Setting a delay of `0` acts immediately. The default is 3 seconds before lock/ban.

---

## Party Ready-Up Setup

The ready-up feature requires a **relay server** running on a PC or server reachable by everyone in the party. One person in the group hosts it — this can be any PC, including one of the players'.

### Step 1 — Host: Run the Relay

1. Download **`LOL_Relay.exe`** from [Releases](https://github.com/Naieter/LoL-Client-Automation/releases/latest)
2. Run it — it listens on port `8777` by default
3. The console shows your **LAN IP** and **public IP** on startup:
   ```
   LAN:    http://192.168.1.50:8777
   Public: http://203.0.113.42:8777
   ```
4. Share the appropriate URL with your party:
   - **Same network (LAN):** use the LAN IP
   - **Different networks:** use the public IP (requires port forwarding `8777` on your router)

To use a different port: `LOL_Relay.exe 9000`

### Step 2 — Everyone: Enter the Relay URL

In the client tool: **Settings → Party Ready-Up → Relay URL**, enter the URL the host gave you (e.g. `http://192.168.1.50:8777`). Make sure **Enabled** is checked.

### Step 3 — Using Ready Up

1. Get into a lobby together
2. Each player clicks **READY UP** in the tool when they want to queue
3. Once all tool users are ready, the **party leader's** client starts the queue automatically
4. If someone unreadies, the queue is canceled automatically

The overlay button over the League client shows the current state and lets you toggle ready without switching windows.

---

## Settings Reference

### Overlay
| Setting | Default | Description |
|---|---|---|
| Show overlay | On | Show the floating overlay button over the League client |
| Toggle keybind | None | Global key (or combo) to toggle the overlay on/off |
| Reset overlay position | — | Move the overlay back to its default position |

### Party Ready-Up
| Setting | Default | Description |
|---|---|---|
| Enabled | On | Enable relay-based party coordination |
| Relay URL | — | URL of the relay server (e.g. `http://192.168.1.50:8777`) |

### Auto-Accept Invites
| Setting | Default | Description |
|---|---|---|
| Accept lobby invites | Off | Auto-accept invites from friends |
| Friends only | — | Comma-separated `Name#Tag` list; blank = any friend |

### Timings
| Setting | Default | Description |
|---|---|---|
| Accept delay | 0s | Wait before accepting a ready check |
| Pre-pick delay | 0.5s | Wait into champ select before hovering pre-pick |
| Pick delay | 3s | Wait after your pick turn starts before locking in |
| Ban delay | 3s | Wait after your ban turn starts before banning |

### Stream Deck API
| Setting | Default | Description |
|---|---|---|
| Port | 0 (off) | Local REST API port; `0` = disabled |

### Startup
| Setting | Default | Description |
|---|---|---|
| Launch at Windows startup | Off | Start hidden in the system tray when Windows starts |

---

## Config & Log Locations

| File | Path |
|---|---|
| Config | `%LOCALAPPDATA%\LOL_Client_TOOL\config.json` |
| Log | `%LOCALAPPDATA%\LOL_Client_TOOL\lol_tool.log` |

---

## Building From Source

### Requirements
- Windows 10/11
- Python 3.8+

### Install dependencies
```
pip install -r requirements.txt
```

### Run from source
```
python lol_tool.py
```
Or use `run.bat` / `run.ps1` / `launch.vbs` (installs deps automatically).

### Build the exe
```
python build_exe.py
```
Outputs `LOL_Client_Tool.exe` to your Desktop.

### Build the relay exe
```
python build_relay.py
```

### Run tests
```
python -m pytest test_lol_tool.py -v
```

---

## How It Works

The tool polls for the League client process using `psutil`, reads the LCU port and auth token from `LeagueClientUx.exe`'s command-line arguments, and connects to the local HTTPS API at `127.0.0.1:{port}`. Champion data is fetched from Riot's DDragon CDN on first launch and cached locally.

The ready-up relay is a small in-memory HTTP server. Clients post opaque hashes of summoner IDs — the relay never sees names or IDs. Presence entries expire after 20 seconds if a client stops heartbeating (e.g. tool closed).

All LCU communication is local. Relay traffic stays within your party's network.
