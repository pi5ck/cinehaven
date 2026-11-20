import os

APP_NAME = "CineHaven"
APP_ID = os.environ.get("CINEHAVEN_APP_ID", "io.github.cinehaven.CineHaven")
APP_VERSION = "1.0.1"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+Linux; streaming torrent player)"

_xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
DATA_DIR = os.path.join(_xdg_data, "cinehaven")


def _videos_dir():
    try:
        import subprocess
        d = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, timeout=2).stdout.strip()
        if d and d != os.path.expanduser("~"):
            return d
    except Exception:
        pass
    return os.path.expanduser("~/Videos")


DOWNLOAD_DIR = os.path.join(_videos_dir(), "CineHaven")
OLD_DATA_DIR = os.path.join(_xdg_data, "openreel")          # earlier name of the app
OLD_DOWNLOAD_DIR = os.path.join(_videos_dir(), "OpenReel")
UPDATE_REPO = "pi5ck/cinehaven"   # GitHub repository the self-updater follows
