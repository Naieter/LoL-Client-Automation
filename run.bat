@echo off
echo LOL Client Tool - Role-Based Champion Selection
echo ------------------------------------------------
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://python.org
    pause
    exit /b
)

:: Install dependencies (safe to run multiple times)
pip install -q requests psutil urllib3

:: Launch the tool
python lol_tool.py

pause
