"""Build LOL_Client_Tool.exe and place it on the Desktop."""
import subprocess, sys, os, shutil

here    = os.path.dirname(os.path.abspath(__file__))
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
dest    = os.path.join(desktop, "LOL_Client_Tool.exe")
tmp     = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "lol_pyi")
log_path = os.path.join(here, "build_log.txt")

def log(msg):
    print(msg)
    with open(log_path, "a") as f:
        f.write(msg + "\n")

log("=== LOL Tool Build Started ===")
log(f"Python: {sys.executable}")
log(f"Working dir: {here}")

log("Installing dependencies...")
subprocess.check_call([sys.executable, "-m", "pip", "install",
                       "pyinstaller", "requests", "psutil", "urllib3",
                       "pystray", "Pillow", "-q"])
log("Dependencies installed.")

os.makedirs(tmp, exist_ok=True)
log(f"Temp build dir: {tmp}")

log("Building exe (this takes ~1-2 minutes)...")
subprocess.check_call([
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "LOL_Client_Tool",
    "--distpath", tmp,
    "--workpath", os.path.join(tmp, "build"),
    "--specpath", tmp,
    "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL",
    os.path.join(here, "lol_tool.py"),
])

built = os.path.join(tmp, "LOL_Client_Tool.exe")
shutil.copy2(built, dest)
log(f"Copied to Desktop: {dest}")
log("=== Build Complete ===")
