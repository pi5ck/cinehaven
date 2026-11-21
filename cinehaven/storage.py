"""Where films are kept: choosing a folder or drive, moving the library there safely,
and opening that drive by itself when CineHaven starts (Linux, without any system change)."""
import json
import os
import shutil
import subprocess
import sys
import time

from . import DATA_DIR

LINUX = sys.platform.startswith("linux")
FOLDER = "CineHaven"
FILESYSTEMS = {"ntfs", "ntfs3", "exfat", "vfat", "ext4", "ext3", "btrfs", "xfs", "f2fs", "hfsplus", "apfs"}
SKIP_PARTS = {"EFI System", "Microsoft reserved", "Windows recovery environment", "BIOS boot", "Linux swap"}


def default_dir():
    from . import _videos_dir
    return os.path.join(_videos_dir(), FOLDER)


def chosen_dir():
    """The folder the user picked, read straight from settings (used at start-up)."""
    try:
        with open(os.path.join(DATA_DIR, "settings.json")) as f:
            d = json.load(f).get("download_dir")
        return d or None
    except (OSError, ValueError):
        return None


def free_space(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    try:
        u = shutil.disk_usage(p or "/")
        return u.free, u.total
    except OSError:
        return None, None


def gb(n):
    return "?" if n is None else (f"{n / 1024**4:.1f} TB" if n >= 1024**4 else f"{n / 1024**3:.0f} GB")


def folder_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


# ── drives (Linux) ─────────────────────────────────────────────────────────
def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def where(path):
    """{"uuid", "mount", "rel"} for a folder on a separate drive, else None."""
    if not LINUX:
        return None
    rc, out, _ = _run(["findmnt", "-n", "-o", "SOURCE,TARGET", "-T", path], 5)
    if rc or not out:
        return None
    src, target = out.split(None, 1)
    if not src.startswith("/dev/") or target == "/" or not target.startswith(("/run/media/", "/media/", "/mnt/")):
        return None
    rc, uuid, _ = _run(["lsblk", "-no", "UUID", src], 5)
    if rc or not uuid:
        return None
    return {"uuid": uuid.splitlines()[0].strip(), "mount": target, "rel": os.path.relpath(path, target)}


def mount_uuid(uuid):
    """Open (mount) a drive like the Files app does – no password for the user's own disks."""
    dev = f"/dev/disk/by-uuid/{uuid}"
    rc, out, err = _run(["findmnt", "-n", "-o", "TARGET", "-S", f"UUID={uuid}"], 5)
    if rc == 0 and out:
        return out.splitlines()[0]
    if not shutil.which("udisksctl") or not os.path.exists(dev):
        return None
    rc, out, err = _run(["udisksctl", "mount", "--no-user-interaction", "-b", dev], 30)
    if rc == 0 and " at " in out:
        return out.rsplit(" at ", 1)[1].strip().rstrip(".")
    rc, out, _ = _run(["findmnt", "-n", "-o", "TARGET", "-S", f"UUID={uuid}"], 5)
    return out.splitlines()[0] if rc == 0 and out else None


def mount_device(dev):
    rc, out, err = _run(["udisksctl", "mount", "--no-user-interaction", "-b", dev], 30)
    if rc == 0 and " at " in out:
        return out.rsplit(" at ", 1)[1].strip().rstrip(".")
    if "AlreadyMounted" in err or "already mounted" in err:
        rc, out, _ = _run(["findmnt", "-n", "-o", "TARGET", "-S", dev], 5)
        return out.splitlines()[0] if out else None
    raise RuntimeError(err or "The drive couldn't be opened")


def ensure_available(settings):
    """At start-up: make sure the chosen folder is reachable, opening its drive if needed.
    Returns the folder, or None if its drive isn't connected."""
    d = settings.get("download_dir")
    if not d:
        return default_dir()
    if os.path.isdir(d):
        return d
    uuid = settings.get("download_uuid")
    if uuid:
        mnt = mount_uuid(uuid)
        if mnt:
            path = os.path.join(mnt, settings.get("download_rel") or FOLDER)
            if os.path.isdir(path):
                if path != d:                     # the drive opened somewhere new
                    settings["download_dir"] = path
                return path
    return None


def places():
    """Drives worth offering: [{label, path (or None), dev, free, total, mounted}]."""
    out = []
    home = os.path.expanduser("~")
    f, t = free_space(home)
    out.append({"label": "This computer (your home folder)", "path": home, "dev": None, "free": f, "total": t,
                "mounted": True})
    if not LINUX:
        return out
    rc, js, _ = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,MOUNTPOINTS,LABEL,UUID,PARTTYPENAME,TYPE,RM,TRAN"], 5)
    try:
        devs = json.loads(js)["blockdevices"]
    except (ValueError, KeyError):
        return out

    def walk(nodes, disk=None):
        for n in nodes:
            yield n, disk or n
            yield from walk(n.get("children") or [], disk or n)
    for n, disk in walk(devs):
        if n.get("type") not in ("part", "disk", "crypt") or (n.get("fstype") or "") not in FILESYSTEMS:
            continue
        if n.get("parttypename") in SKIP_PARTS or (n.get("size") or 0) < 16 * 1024**3:
            continue
        mps = [m for m in (n.get("mountpoints") or []) if m]
        if any(m in ("/", "/boot", "/boot/efi", "/home") or m.startswith(("/snap", "/var", "/run/timeshift"))
               for m in mps):
            continue
        size = n.get("size") or 0
        usb = disk.get("tran") == "usb" or disk.get("rm")
        name = n.get("label") or f"{gb(size)} {'USB drive' if usb else 'drive'}"
        mp = mps[0] if mps else None
        f, t = free_space(mp) if mp else (None, size)
        out.append({"label": name, "path": mp, "dev": n["path"], "free": f, "total": t, "mounted": bool(mp),
                    "fstype": n.get("fstype"), "usb": bool(usb)})
    return out


def looks_like_windows(path):
    return os.path.isdir(os.path.join(path, "Windows", "System32"))


# ── switching folders inside the running app ──────────────────────────────
def set_download_dir(path):
    """Point every part of CineHaven at the new folder."""
    for name, mod in list(sys.modules.items()):
        if (name == "cinehaven" or name.startswith("cinehaven.")) and hasattr(mod, "DOWNLOAD_DIR"):
            mod.DOWNLOAD_DIR = path


def _rewrite(obj, old, new):
    pre = old.rstrip(os.sep) + os.sep

    def fix(s):
        return new.rstrip(os.sep) + os.sep + s[len(pre):] if isinstance(s, str) and s.startswith(pre) else s
    if isinstance(obj, dict):
        return {fix(k): _rewrite(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v, old, new) for v in obj]
    return fix(obj)


def move_library(engine, settings, target, progress=lambda done, total, text: None):
    """Move every film CineHaven keeps into target. Runs in a worker thread.
    Returns {"moved": n, "skipped": [...], "bytes": n}."""
    import cinehaven
    from . import localmedia
    old = cinehaven.DOWNLOAD_DIR
    os.makedirs(target, exist_ok=True)
    probe = os.path.join(target, ".cinehaven-write-test")
    with open(probe, "w") as fh:
        fh.write("ok")
    os.remove(probe)
    total = folder_size(old) if os.path.isdir(old) else 0
    need = total - folder_size(target)
    free, _t = free_space(target)
    if free is not None and need > free - 1024**3:
        raise RuntimeError(f"Not enough room there: the films need {gb(need)} and only {gb(free)} is free.")
    progress(0, total, "Moving films…")
    moving = []
    real_old = os.path.realpath(old)
    for it in list(engine.items.values()):
        try:
            sp = it.handle.status().save_path
        except Exception:
            continue
        if os.path.realpath(sp) == real_old or sp == old:
            it.handle.move_storage(target)
            moving.append(it)
    deadline = time.time() + 6 * 3600
    while moving and time.time() < deadline:
        left = []
        for it in moving:
            st = it.handle.status()
            if st.moving_storage or os.path.realpath(st.save_path) != os.path.realpath(target):
                left.append(it)
        moving = left
        done = folder_size(target)
        progress(min(done, total), total, f"Moving films… {gb(done)} of {gb(total)}")
        if moving:
            time.sleep(1)
    if moving:
        raise RuntimeError("Moving took too long – some films are still in the old folder.")
    for it in engine.items.values():
        if os.path.realpath(it.meta.get("save_path") or old) == real_old:
            it.meta["save_path"] = target
    engine.save_state()
    # anything else in the old folder (finished files from earlier versions, fixed copies, …)
    skipped = []
    if os.path.isdir(old):
        for name in os.listdir(old):
            src, dst = os.path.join(old, name), os.path.join(target, name)
            if os.path.exists(dst):
                if os.path.isdir(src) and not os.listdir(src):
                    os.rmdir(src)
                else:
                    skipped.append(name)
                continue
            progress(folder_size(target), total, f"Moving {name}…")
            shutil.move(src, dst)
        try:
            os.rmdir(old)
        except OSError:
            pass
    # everything that remembers a path: recently played, resume points, My Videos
    fixed = _rewrite(settings, old, target)
    settings.clear()
    settings.update(fixed)
    try:
        localmedia.save(_rewrite(localmedia.load(), old, target))
    except Exception:
        pass
    remember(settings, target)
    set_download_dir(target)
    progress(total, total, "Done")
    return {"moved": len(engine.items), "skipped": skipped, "bytes": total}


def remember(settings, target):
    settings["download_dir"] = None if os.path.realpath(target) == os.path.realpath(default_dir()) else target
    w = where(target)
    settings["download_uuid"] = w["uuid"] if w else None
    settings["download_rel"] = w["rel"] if w else None
