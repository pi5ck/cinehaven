"""Start CineHaven for real, check it comes up, then quit. Used by CI on every platform.

Exit code 0 = the main window opened with all its tabs and nothing raised an error.
"""
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# inside a Windows bundle: python, GTK, mpv and ffmpeg live in ../../bin
_bundle_bin = os.path.join(os.path.dirname(os.path.dirname(HERE)), "bin")
if os.path.isdir(_bundle_bin):
    os.environ["PATH"] = _bundle_bin + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_bundle_bin)
os.environ["CINEHAVEN_APP_ID"] = "io.github.cinehaven.SmokeTest"
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="cinehaven-smoke-")

errors = []
sys.excepthook = lambda t, v, tb: errors.append("".join(traceback.format_exception(t, v, tb)))

print("platform:", sys.platform, sys.version.split()[0], "·", sys.executable)
for mod in ("gi", "libtorrent", "mpv"):
    try:
        m = __import__(mod)
        print(f"import {mod}: ok", getattr(m, "__version__", ""))
    except Exception as e:
        print(f"import {mod}: FAILED – {e}")
        errors.append(f"{mod}: {e}")
if errors:
    sys.exit(1)

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402
print("GTK", Gtk.get_major_version(), Gtk.get_minor_version(), "· libadwaita", Adw.MAJOR_VERSION, Adw.MINOR_VERSION)

from cinehaven import DATA_DIR, DOWNLOAD_DIR, app, localmedia  # noqa: E402
print("data folder:", DATA_DIR)
print("downloads:", DOWNLOAD_DIR)
print("drives:", localmedia.roots())

result = {}


def check(application):
    win = application.get_active_window()
    result["window"] = win is not None
    if win:
        pages = []
        child = win.views.get_first_child() if hasattr(win.views, "get_first_child") else None
        for name in ("discover", "videos", "live", "cameras", "youtube", "mylist", "library"):
            pages.append(name if win.views.get_child_by_name(name) else f"MISSING:{name}")
        result["tabs"] = pages
        result["player"] = type(win.video).__name__
    application.quit()
    return False


a = app.App()
a.connect("activate", lambda ap: GLib.timeout_add_seconds(10, check, ap))
GLib.timeout_add_seconds(90, lambda: (errors.append("timed out"), a.quit(), False)[2])
try:
    a.run([])
except Exception:
    errors.append(traceback.format_exc())

print("result:", result)
for e in errors:
    print("ERROR:", e)
ok = result.get("window") and not any(t.startswith("MISSING") for t in result.get("tabs", ["MISSING"])) and not errors
print("SMOKE TEST", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
