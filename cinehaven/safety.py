"""Fake / malicious torrent detection and file checksum verification."""
import hashlib
import os
import re

DANGEROUS_EXT = (".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".lnk",
                 ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".jar", ".ps1",
                 ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".run", ".appimage",
                 ".sh", ".desktop", ".dll", ".cpl", ".reg", ".iso")
ARCHIVE_EXT = (".zip", ".rar", ".7z", ".tar", ".gz", ".001", ".r00")
BAIT_WORDS = re.compile(r"(codec|password|passwd|keygen|crack|activat|install|"
                        r"setup|player\s*required|click\s*here|download\s*now)", re.I)


def check_files(files):
    """files: list of (path, size).  Returns (ok, problems[], warnings[])."""
    from .engine import is_video
    problems, warnings = [], []
    vids = [(p, s) for p, s in files if is_video(p)]
    for p, s in files:
        low = p.lower()
        base = os.path.basename(low)
        if low.endswith(DANGEROUS_EXT):
            problems.append(f"Contains a program, not a movie: {os.path.basename(p)}")
        if re.search(r"\.(mp4|mkv|avi|mov|wmv)\.[a-z0-9]{2,4}$", base) and not is_video(base):
            problems.append(f"Disguised file name: {os.path.basename(p)}")
        if low.endswith(ARCHIVE_EXT) and s > 50_000_000:
            problems.append("Movie is locked inside an archive (classic password scam)")
        if BAIT_WORDS.search(base) and not is_video(base):
            warnings.append(f"Suspicious extra file: {os.path.basename(p)}")
    if not vids:
        problems.append("No video file inside")
    else:
        main = max(vids, key=lambda v: v[1])
        if main[1] < 40_000_000:
            warnings.append("Main video is very small – may be a sample or fake")
        if main[0].lower().endswith((".wmv", ".asf")) and main[1] < 400_000_000:
            warnings.append("Small WMV files are often DRM 'codec required' scams")
    return (not problems), list(dict.fromkeys(problems)), list(dict.fromkeys(warnings))


def sha1_file(path, expected, progress=None):
    """True if the file's SHA-1 matches the published checksum."""
    h = hashlib.sha1()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / total)
    return h.hexdigest().lower() == (expected or "").lower()
