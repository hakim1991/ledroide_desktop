import os
import shutil
from pathlib import Path

BACKEND_DIR   = Path(__file__).resolve().parent.parent
ROOT_DIR      = BACKEND_DIR.parent
MODULES_DIR   = ROOT_DIR / "modules"
AVAILABLE_DIR = ROOT_DIR / "modules-available"

PORT  = int(os.environ.get("LEDROIDE_BACKEND_PORT", 8765))
TOKEN = os.environ.get("LEDROIDE_TOKEN", "")

# User data lives outside the install dir so app updates never overwrite it.
DATA_DIR = Path(os.environ.get("LEDROIDE_DATA_DIR") or BACKEND_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["LEDROIDE_DATA_DIR"] = str(DATA_DIR)

# Modules installed by the user (catalog, archive): the install dir is wiped on
# every app update, so only the modules shipped with the app live in MODULES_DIR.
USER_MODULES_DIR = DATA_DIR / "installed-modules"

CONFIGS_FILE = DATA_DIR / "configs.json"
STATE_FILE   = DATA_DIR / "state.json"

SELF_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
RENDERER_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "null", "file://"]


def migrate_legacy_files():
    if DATA_DIR == BACKEND_DIR:
        return
    for name in ("configs.json", "state.json"):
        legacy, target = BACKEND_DIR / name, DATA_DIR / name
        if legacy.exists() and not target.exists():
            shutil.copy2(legacy, target)
