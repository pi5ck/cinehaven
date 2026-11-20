"""Self-update from GitHub Releases.

* Per-user installs (~/.local/share/cinehaven-app): the new version is downloaded, its
  SHA-256 checked against the release's SHA256SUMS, test-started, then swapped in. The
  previous version is kept; the crash guard rolls back to it if the new one crashes on start.
* System (.deb) installs can't replace themselves without a password, so the user gets a
  link to the new installer instead.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from . import APP_VERSION, DATA_DIR, UPDATE_REPO, USER_AGENT
from .health import load_json, log, save_json

APP_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
STATE = os.path.join(DATA_DIR, "update.json")
CHECK_EVERY = 24 * 3600


def _vt(v):
    return tuple(int(x) for x in v.lstrip("v").split(".")[:3] if x.isdigit())


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def user_install():
    """True if this copy lives in the user's home and can replace itself (Linux/BSD).

    Windows and macOS copies come from an installer, so they get a link to the new one."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    home = os.path.expanduser("~")
    return APP_DIR.startswith(home + "/") and os.access(os.path.dirname(APP_DIR), os.W_OK)


def check(force=False):
    """Returns dict(version, url, notes, asset, sums) if a newer release exists, else None."""
    if not UPDATE_REPO:
        return None
    st = load_json(STATE, {})
    if not force and time.time() - st.get("checked", 0) < CHECK_EVERY:
        return st.get("available") if st.get("available") and \
            _vt(st["available"]["version"]) > _vt(APP_VERSION) else None
    rel = json.loads(_get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"))
    ver = rel.get("tag_name", "").lstrip("v")
    avail = None
    if ver and _vt(ver) > _vt(APP_VERSION):
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        avail = {"version": ver, "url": rel.get("html_url"), "notes": (rel.get("body") or "")[:600],
                 "asset": assets.get(f"cinehaven-{ver}.tar.gz"), "sums": assets.get("SHA256SUMS")}
    save_json(STATE, {"checked": time.time(), "available": avail})
    return avail


def install(avail, progress=None):
    """Download, verify, test and swap in a new version (user installs only). Returns True on success."""
    if not user_install() or not avail.get("asset") or not avail.get("sums"):
        return False
    name = f"cinehaven-{avail['version']}.tar.gz"
    sums = _get(avail["sums"]).decode()
    expected = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(name)), None)
    if not expected:
        raise RuntimeError("release has no checksum for the download")
    data = _get(avail["asset"], timeout=120)
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("download failed its checksum – not installed")
    parent = os.path.dirname(APP_DIR)
    stage = tempfile.mkdtemp(prefix="cinehaven-update-", dir=parent)
    try:
        tar_path = os.path.join(stage, name)
        with open(tar_path, "wb") as f:
            f.write(data)
        new_dir = os.path.join(stage, "app")
        os.makedirs(new_dir)
        with tarfile.open(tar_path) as t:
            t.extractall(new_dir, filter="data")      # refuses absolute paths / links outside
        top = [d for d in os.listdir(new_dir)]
        if len(top) == 1 and os.path.isdir(os.path.join(new_dir, top[0], "cinehaven")):
            new_dir = os.path.join(new_dir, top[0])
        # keep the bundled libraries of the current install
        if os.path.isdir(os.path.join(APP_DIR, "vendor")) and not os.path.exists(os.path.join(new_dir, "vendor")):
            shutil.copytree(os.path.join(APP_DIR, "vendor"), os.path.join(new_dir, "vendor"))
        launcher = os.path.join(new_dir, "cinehaven-launcher")
        os.chmod(launcher, 0o755)
        test = subprocess.run([sys.executable, launcher, "--selftest"], capture_output=True, timeout=60)
        if test.returncode != 0:
            raise RuntimeError("new version failed its self-test: " + test.stderr.decode()[-300:])
        prev = APP_DIR + ".prev"
        shutil.rmtree(prev, ignore_errors=True)
        os.rename(APP_DIR, prev)
        os.rename(new_dir, APP_DIR)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "just-updated"), "w") as f:
            f.write(f"{APP_VERSION} -> {avail['version']}")
        log.info("Updated %s -> %s", APP_VERSION, avail["version"])
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)
