"""Build LOL_Client_Tool.exe and place it on the Desktop."""
import subprocess, sys, os, shutil, traceback

here = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(here, "build_log.txt")

def log(msg):
    print(msg)
    with open(log_path, "a") as f:
        f.write(msg + "\n")

try:
    log("=== LOL Tool Build Started ===")
    log(f"Python: {sys.executable}")
    log(f"Working dir: {here}")

    log("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "pyinstaller", "requests", "psutil", "urllib3", "-q"])
    log("Dependencies installed.")

    # Use %TEMP% to avoid Windows MAX_PATH (260 char) limit from deep outputs path
    tmp = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "lol_pyi")
    os.makedirs(tmp, exist_ok=True)
    log(f"Temp build dir: {tmp}")

    log("Building exe (this takes ~1-2 minutes)...")