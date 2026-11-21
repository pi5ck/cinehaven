import os
import sys

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"

APP_NAME = "CineHaven"
APP_ID = os.environ.get("CINEHAVEN_APP_ID", "io.github.cinehaven.CineHaven")
APP_VERSION = "1.0.16"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+{sys.platform}; streaming torrent player)"


def _data_home():
    """Where per-user app data lives on this platform."""
    if os.environ.get("XDG_DATA_HOME"):
        return os.environ["XDG_DATA_HOME"]
    if WINDOWS:
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    if MACOS:
        return os.path.expanduser("~/Library/Application Support")
    return os.path.expanduser("~/.local/share")


_xdg_data = _data_home()
DATA_DIR = os.path.join(_xdg_data, "cinehaven")


def _videos_dir():
    if MACOS:
        return os.path.expanduser("~/Movies")
    if WINDOWS:
        return os.path.join(os.path.expanduser("~"), "Videos")
    try:
        import subprocess
        d = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=2).stdout.strip()
        if d and d != os.path.expanduser("~"):
            return d
    except Exception:
        pass
    return os.path.expanduser("~/Videos")


def _download_dir():
    """The folder or drive the user chose for films (Menu → Where films are kept), else ~/Videos/CineHaven."""
    from .storage import chosen_dir
    return chosen_dir() or os.path.join(_videos_dir(), "CineHaven")


DOWNLOAD_DIR = _download_dir()
OLD_DATA_DIR = os.path.join(_xdg_data, "openreel")          # earlier name of the app
OLD_DOWNLOAD_DIR = os.path.join(_videos_dir(), "OpenReel")
UPDATE_REPO = "pi5ck/cinehaven"   # GitHub repository the self-updater follows
