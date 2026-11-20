"""Self-repair: logging, crash capture, startup self-check, safe JSON storage."""
import json
import logging
import logging.handlers
import os
import shutil
import sys
import threading
import time

from . import APP_VERSION, DATA_DIR, DOWNLOAD_DIR, OLD_DATA_DIR, OLD_DOWNLOAD_DIR

LOG_FILE = os.path.join(DATA_DIR, "cinehaven.log")
SAFE_MODE = os.environ.get("CINEHAVEN_SAFE_MODE") == "1"
log = logging.getLogger("cinehaven")


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    if log.handlers:
        return
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

    def hook(exc_type, exc, tb):
        log.error("Unhandled error", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = hook
    threading.excepthook = lambda a: log.error("Unhandled error in thread %s", a.thread and a.thread.name,
                                               exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    log.info("CineHaven %s starting%s", APP_VERSION, " (safe mode)" if SAFE_MODE else "")


# ── safe JSON storage: atomic writes + a .bak copy to fall back on ────────
def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)


def load_json(path, default):
    """Load JSON; if the file is corrupt, set it aside and use the backup (or the default)."""
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate) as f:
                data = json.load(f)
            if candidate != path:
                log.warning("Restored %s from backup", os.path.basename(path))
                REPAIRS.append(f"Restored {os.path.basename(path)} from its backup")
                save_json(path, data)
            return data
        except FileNotFoundError:
            continue
        except (ValueError, OSError) as e:
            log.warning("Corrupt %s: %s", candidate, e)
            try:
                os.replace(candidate, f"{candidate}.corrupt-{int(time.time())}")
            except OSError:
                pass
    return default


REPAIRS = []      # human-readable list of what the self-check fixed this run
WARNINGS = []


def migrate():
    """Carry over library, settings and downloads from the app's earlier name (OpenReel)."""
    moved = False
    if os.path.isdir(OLD_DATA_DIR) and not os.path.exists(DATA_DIR):
        os.rename(OLD_DATA_DIR, DATA_DIR)
        moved = True
    if os.path.isdir(OLD_DOWNLOAD_DIR) and not os.path.exists(DOWNLOAD_DIR):
        os.rename(OLD_DOWNLOAD_DIR, DOWNLOAD_DIR)
        moved = True
    if moved:
        for fn in ("library.json", "settings.json", "local.json"):
            p = os.path.join(DATA_DIR, fn)
            try:
                with open(p) as f:
                    text = f.read()
                text = text.replace(OLD_DOWNLOAD_DIR, DOWNLOAD_DIR).replace(OLD_DATA_DIR, DATA_DIR)
                with open(p, "w") as f:
                    f.write(text)
            except OSError:
                pass
        REPAIRS.append("Moved your OpenReel library and downloads into CineHaven")
    return moved


def selfcheck():
    """Run at startup. Fixes what it can; returns (repairs, warnings)."""
    for d in (DATA_DIR, DOWNLOAD_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".cinehaven-write-test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            WARNINGS.append(f"Can't write to {d}: {e.strerror}")
            log.error("Not writable: %s (%s)", d, e)
    # leftover temp files from an interrupted save
    for root, _dirs, files in os.walk(DATA_DIR):
        for fn in files:
            if fn.endswith(".tmp"):
                try:
                    os.remove(os.path.join(root, fn))
                    REPAIRS.append("Cleaned up an interrupted save")
                except OSError:
                    pass
    # free space
    try:
        free = shutil.disk_usage(DOWNLOAD_DIR).free
        if free < 2 * 1024**3:
            WARNINGS.append(f"Only {free / 1024**3:.1f} GB free – downloads may fail")
    except OSError:
        pass
    # player library present?
    try:
        import mpv  # noqa: F401
    except Exception as e:
        WARNINGS.append(f"Video player library missing ({e}) – reinstall CineHaven")
    log.info("Self-check: %d repairs, %d warnings", len(REPAIRS), len(WARNINGS))
    return REPAIRS, WARNINGS


def selftest():
    """Used by the updater/crash guard: can this copy of CineHaven start?"""
    import importlib
    for mod in ("engine", "catalog", "safety", "watch", "translate", "livetv", "localmedia", "doctor",
                "update", "player", "app"):
        importlib.import_module("cinehaven." + mod)
    return 0
