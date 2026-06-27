"""Build LOL_Relay.exe (the ready-up relay server) and place it on the Desktop."""
import subprocess, sys, os, shutil

here    = os.path.dirname(os.path.abspath(__file__))
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
dest    = os.path.join(desktop, "LOL_Relay.exe")
tmp     = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "lol_relay_pyi")

print("Building LOL_Relay.exe (console app, no dependencies)...")
subprocess.check_call([
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--console",
    "--name", "LOL_Relay",
    "--distpath", tmp,
    "--workpath", os.path.join(tmp, "build"),
    "--specpath", tmp,
    os.path.join(here, "lol_relay.py"),
])

built = os.path.join(tmp, "LOL_Relay.exe")
shutil.copy2(built, dest)
print(f"Copied to Desktop: {dest}")
print("=== Relay Build Complete ===")
