# LOL Client Tool

A Python desktop tool that connects to the League of Legends client (LCU API) and helps you set role-based champion preferences for champion select.

## Features

- Auto-connects when the League client launches
- Role tabs: Top, Jungle, Mid, ADC, Support
- Set preferred and ban picks per role
- Champion data pulled from Riot's DDragon API
- Settings persist between sessions

## Requirements

- Windows 10/11
- Python 3.8+ ([python.org](https://www.python.org/downloads/))
- League of Legends installed

## Quick Start

**Option 1 — Double-click to run:**
```
run.bat
```
This installs dependencies automatically and launches the tool.

**Option 2 — PowerShell:**
```powershell
.\run.ps1
```

**Option 3 — Silent (no console window):**
Double-click `launch.vbs`

## Installing Dependencies Manually

```
pip install -r requirements.txt
```

Then run:
```
python lol_tool.py
```

## Build a Standalone .exe

To produce `LOL_Client_Tool.exe` on your Desktop:
```
python build_exe.py
```
Requires PyInstaller (installed automatically by the script).

## Running Tests

```
python -m pytest test_lol_tool.py -v
```
Or:
```
python test_lol_tool.py
```

## Project Structure

```
lol_tool.py        — Main application (GUI + LCU API + DDragon)
build_exe.py       — Builds standalone .exe via PyInstaller
run.bat            — Windows launcher (installs deps, runs app)
run.ps1            — PowerShell launcher
launch.vbs         — Silent launcher (no console window)
requirements.txt   — Python dependencies
test_lol_tool.py   — Unit tests (29 tests, no League client needed)
```

## How It Works

The tool polls for the League client process using `psutil`, discovers the LCU port and auth token from the `LeagueClientUx.exe` command-line args, and connects to the local HTTPS API on `127.0.0.1:{port}`. Champion data is fetched from Riot's DDragon CDN on first launch and cached locally.

## Config Storage

Settings are saved to `%LOCALAPPDATA%\LOL_Client_TOOL\config.json`.

## Notes

- The LCU API requires the League client to be running
- The tool reconnects automatically if the client restarts
- All LCU communication is local (no data leaves your machine)
