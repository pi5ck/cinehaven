"""Torrent engine + local streaming server.

libtorrent does the downloading (sequential, with piece deadlines around the
current play position), and a tiny HTTP server on 127.0.0.1 serves the file to
the player.  Reads block until the needed pieces have arrived *and passed their
SHA-1 hash check*, so the player never sees unverified or missing data, and
seeking anywhere simply re-targets the download.
"""
import json
import mimetypes
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import libtorrent as lt

from . import APP_NAME, APP_VERSION, DATA_DIR, DOWNLOAD_DIR, USER_AGENT

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".ogv", ".ogg",
             ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv", ".flv", ".3gp", ".divx"}
TORRENT_DIR = os.path.join(DATA_DIR, "torrents")
STATE_FILE = os.path.join(DATA_DIR, "library.json")
READAHEAD_BYTES = 48 * 1024 * 1024      # keep ~48 MB ahead of the playhead urgent
PREBUFFER_BYTES = 3 * 1024 * 1024       # needed before playback auto-starts
CHUNK = 256 * 1024


def normalize_magnet(text):
    """Turn whatever was pasted / passed by the desktop into a clean magnet URI.

    Handles: GNOME's 'magnet:///?xt=' rewrite, 'magnet://?', capitals, surrounding
    spaces/quotes/<>, URL-encoded links, a magnet buried in other text, and a bare
    40-hex / 32-base32 info hash. Returns None if there is no magnet in it.
    """
    import re
    import urllib.parse
    t = (text or "").strip().strip("\"'<>")
    m = re.search(r"magnet:(?:%3F|[/?])\S*", t, re.I)
    if m:
        t = m.group(0)
        if re.match(r"magnet:%3F", t, re.I) or "%3D" in t[:40].upper():
            t = urllib.parse.unquote(t)
        t = re.sub(r"^magnet:/*\??", "magnet:?", t, flags=re.I)
        t = t.rstrip(").,;\"'>")
        return t if re.search(r"xt=urn:bt(ih|mh):", t, re.I) else None
    if re.fullmatch(r"[0-9a-fA-F]{40}|[A-Za-z2-7]{32}", t):
        return "magnet:?xt=urn:btih:" + t
    return None


def is_video(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Item:
    """One torrent in the library, streaming exactly one chosen file."""

    def __init__(self, engine, handle, meta):
        self.engine = engine
        self.handle = handle
        self.meta = meta            # title, source, ia_id, file_name, sha1, trust...
        self.file_index = meta.get("file_index")
        self.lock = threading.Lock()
        self.play_piece = 0         # where the reader currently is
        self.verified = meta.get("verified")  # None / True / False (checksum)
        self.key = str(handle.info_hash())      # cached: handle dies on removal

    # -- identity ---------------------------------------------------------
    @property
    def title(self):
        return self.meta.get("title") or self.handle.status().name or "Loading…"

    def has_metadata(self):
        return self.handle.status().has_metadata

    # -- file selection ---------------------------------------------------
    def choose_file(self, name=None):
        ti = self.handle.torrent_file()
        fs = ti.files()
        idx = None
        if name:
            for i in range(fs.num_files()):
                p = fs.file_path(i).replace("\\", "/")
                if p == name or p.endswith("/" + name):
                    idx = i
                    break
        if idx is None:  # largest video file
            vids = [i for i in range(fs.num_files()) if is_video(fs.file_path(i))]
            if vids:
                idx = max(vids, key=fs.file_size)
        if idx is None:
            return None
        prios = [0] * fs.num_files()
        prios[idx] = 4
        # grab small subtitle files from the same torrent too
        for i in range(fs.num_files()):
            if fs.file_path(i).lower().endswith((".srt", ".vtt", ".ass")) and fs.file_size(i) < 2_000_000:
                prios[i] = 4
        self.handle.prioritize_files(prios)
        self.file_index = idx
        self.meta["file_index"] = idx
        self.meta["file_name"] = fs.file_path(idx)
        if not self.meta.get("title"):
            self.meta["title"] = self.handle.status().name
        self.engine.save_torrent_file(self)
        self.engine.save_state()
        return idx

    def file_info(self):
        ti = self.handle.torrent_file()
        fs = ti.files()
        i = self.file_index
        return (os.path.join(self.handle.status().save_path, fs.file_path(i)),
                fs.file_offset(i), fs.file_size(i), ti.piece_length(), ti.num_pieces())

    def all_files(self):
        ti = self.handle.torrent_file()
        if ti is None:
            return []
        fs = ti.files()
        return [(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())]

    def subtitle_paths(self):
        out = []
        if not self.has_metadata():
            return out
        ti = self.handle.torrent_file()
        fs = ti.files()
        prog = self.handle.file_progress()
        for i in range(fs.num_files()):
            p = fs.file_path(i)
            if p.lower().endswith((".srt", ".vtt", ".ass")) and prog[i] == fs.file_size(i):
                out.append(os.path.join(self.handle.status().save_path, p))
        return out

    # -- direct-from-archive fallback ---------------------------------------
    @property
    def http_url(self):
        """Direct download URL of the same file (Internet Archive items only)."""
        if not self.meta.get("ia_id") or self.file_index is None or not self.has_metadata():
            return None
        import urllib.parse
        ti = self.handle.torrent_file()
        path = ti.files().file_path(self.file_index).replace("\\", "/")
        root = ti.name() + "/"
        if path.startswith(root):
            path = path[len(root):]
        return "https://archive.org/download/%s/%s" % (urllib.parse.quote(self.meta["ia_id"]),
                                                       urllib.parse.quote(path))

    def boundary_pieces(self):
        """Pieces shared with neighbouring files (which the Archive may have changed since)."""
        _, off, size, plen, n = self.file_info()
        first, last = off // plen, (off + size - 1) // plen
        out = set()
        if off % plen:
            out.add(first)
        if (off + size) % plen and last < n - 1:
            out.add(last)
        return out

    def http_range(self, start, end):
        req = urllib.request.Request(self.http_url, headers={"User-Agent": USER_AGENT,
                                                             "Range": f"bytes={start}-{end}"})
        last = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if r.status == 206 and len(data) == end - start + 1:
                    return data
            except Exception as e:
                last = e
            time.sleep(1)
        raise IOError(f"direct download failed: {last}")

    def read_range(self, pos, n, alive):
        """Bytes [pos, pos+n) of the file: from verified torrent pieces, or – for pieces the
        swarm can't supply – straight from the Archive."""
        a, b = self.pieces_for(pos, pos + n - 1)
        h = self.handle
        url = self.http_url
        edge = self.boundary_pieces() if url else set()
        fallback = False
        for p in range(a, b + 1):
            if h.have_piece(p):
                continue
            if p in edge:
                fallback = True
                break
            self.focus(p)
            t0 = time.time()
            while not h.have_piece(p):
                if not alive():
                    return None
                if url and time.time() - t0 > 20:      # swarm stalled – use the Archive directly
                    fallback = True
                    break
                time.sleep(0.05)
            if fallback:
                break
        if fallback:
            return self.http_range(pos, pos + n - 1)
        path = self.file_info()[0]
        with open(path, "rb") as f:
            f.seek(pos)
            return f.read(n)

    def needs_patch(self):
        """True when everything except unobtainable boundary pieces has downloaded."""
        if self.meta.get("patched") or not self.http_url:
            return False
        a, b = self.pieces_for(0, self.file_info()[2] - 1)
        edge = self.boundary_pieces()
        h = self.handle
        missing = [p for p in range(a, b + 1) if not h.have_piece(p)]
        return bool(missing) and all(p in edge for p in missing)

    def patch_from_archive(self, sha1_check, all_missing=False):
        """Fill missing pieces from archive.org (boundary pieces only, or everything still missing
        when the swarm has stalled), then prove the file with the published SHA-1.
        Returns True (verified), False (mismatch) or None (couldn't)."""
        path, off, size, plen, _ = self.file_info()
        h = self.handle
        a, b = self.pieces_for(0, size - 1)
        todo = [p for p in (range(a, b + 1) if all_missing else sorted(self.boundary_pieces()))
                if not h.have_piece(p)]
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()
        for p in todo:
            h.piece_priority(p, 0)               # stop asking peers for it
        # merge into contiguous byte ranges, fetch in 4 MB requests
        ranges = []
        for p in todo:
            start, end = max(0, p * plen - off), min(size - 1, (p + 1) * plen - off - 1)
            if ranges and ranges[-1][1] + 1 >= start:
                ranges[-1][1] = end
            else:
                ranges.append([start, end])
        for start, end in ranges:
            pos = start
            while pos <= end:
                stop = min(end, pos + 4 * 1024 * 1024 - 1)
                data = self.http_range(pos, stop)
                with open(path, "r+b") as f:
                    f.seek(pos)
                    f.write(data)
                pos = stop + 1
        expected = self.meta.get("sha1")
        if not expected:
            return None
        ok = sha1_check(path, expected)
        if ok:
            self.meta["patched"] = True
            self.meta["verified"] = True
            self.verified = True
            if all_missing:
                h.force_recheck()                 # let libtorrent see (and share) the data
        self.engine.save_state()
        return ok

    # -- piece maths ------------------------------------------------------
    def pieces_for(self, start, end):
        _, off, size, plen, _ = self.file_info()
        a = (off + start) // plen
        b = (off + min(end, size - 1)) // plen
        return a, b

    def focus(self, piece):
        """Make pieces from `piece` onward urgent (called as the player reads)."""
        _, off, size, plen, npieces = self.file_info()
        last = (off + size - 1) // plen
        window = max(4, READAHEAD_BYTES // plen)
        with self.lock:
            jumped = abs(piece - self.play_piece) > window
            self.play_piece = piece
        if jumped:
            self.handle.clear_piece_deadlines()
        h = self.handle
        for n, p in enumerate(range(piece, min(piece + window, last + 1))):
            if not h.have_piece(p):
                h.set_piece_deadline(p, 200 + n * 120)

    def start_streaming(self):
        """Prioritise the head and tail of the file (container headers/index)."""
        h = self.handle
        h.set_flags(lt.torrent_flags.sequential_download)
        _, off, size, plen, _ = self.file_info()
        a, b = self.pieces_for(0, PREBUFFER_BYTES)
        for n, p in enumerate(range(a, b + 1)):
            h.set_piece_deadline(p, 50 + n * 40)
        ta, tb = self.pieces_for(max(0, size - 1024 * 1024), size - 1)
        for p in range(ta, tb + 1):
            h.set_piece_deadline(p, 300)
        if h.status().paused or (h.flags() & lt.torrent_flags.paused):
            self.resume()

    def prebuffer_progress(self):
        """0..1 – how much of what we need before auto-play has arrived."""
        if not self.has_metadata() or self.file_index is None:
            return 0.0
        _, _, size, _, _ = self.file_info()
        need = list(range(*self._incl(self.pieces_for(0, PREBUFFER_BYTES))))
        need += list(range(*self._incl(self.pieces_for(max(0, size - 1024 * 1024), size - 1))))
        need = set(need)
        if self.http_url:
            need -= self.boundary_pieces()          # served directly from the Archive
        have = sum(1 for p in need if self.handle.have_piece(p))
        return have / len(need) if need else 1.0

    @staticmethod
    def _incl(ab):
        return ab[0], ab[1] + 1

    def wait_bytes(self, start, length, alive):
        """Block until [start, start+length) is downloaded + verified."""
        a, b = self.pieces_for(start, start + length - 1)
        h = self.handle
        for p in range(a, b + 1):
            if not h.have_piece(p):
                self.focus(p)
                while not h.have_piece(p):
                    if not alive():
                        return False
                    time.sleep(0.05)
        return True

    # -- status -----------------------------------------------------------
    def file_progress(self):
        if self.meta.get("patched"):
            return 1.0
        if not self.has_metadata() or self.file_index is None:
            return 0.0
        size = self.handle.torrent_file().files().file_size(self.file_index)
        done = self.handle.file_progress()[self.file_index]
        return done / size if size else 0.0

    def is_complete(self):
        return self.file_progress() >= 1.0

    def pause(self):
        self.handle.unset_flags(lt.torrent_flags.auto_managed)
        self.handle.pause()
        self.meta["paused"] = True
        self.engine.save_state()

    def resume(self):
        self.handle.set_flags(lt.torrent_flags.auto_managed)
        self.handle.resume()
        self.meta["paused"] = False
        self.engine.save_state()

    def is_paused(self):
        return bool(self.handle.flags() & lt.torrent_flags.paused)

    def stream_url(self):
        return f"http://127.0.0.1:{self.engine.port}/{self.key}/{self.file_index}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_NAME

    def log_message(self, *a):
        pass

    def _item(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 2:
            return None
        it = self.server.engine.items.get(parts[0])
        if it and it.file_index is not None and str(it.file_index) == parts[1]:
            return it
        return None

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        it = self._item()
        if not it:
            self.send_error(404)
            return
        path, _, size, _, _ = it.file_info()
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = 200
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].partition("-")
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            else:  # suffix range
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        alive = lambda: not self.server.engine.closing
        pos = start
        try:
            while pos <= end:
                n = min(CHUNK, end - pos + 1)
                data = it.read_range(pos, n, alive)
                if data is None:
                    return
                if not data:
                    time.sleep(0.05)
                    continue
                self.wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Engine:
    def __init__(self):
        os.makedirs(TORRENT_DIR, exist_ok=True)
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        except OSError:
            pass                                  # its drive isn't connected – handled per film
        self.closing = False
        self.items = {}
        s = lt.default_settings()
        s.update({
            "user_agent": f"{APP_NAME}/{APP_VERSION} libtorrent/{lt.__version__}",
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True, "enable_lsd": True,
            "enable_upnp": True, "enable_natpmp": True,
            "alert_mask": lt.alert.category_t.status_notification
                          | lt.alert.category_t.error_notification,
            "connections_limit": 400,
            "active_downloads": 6, "active_seeds": 10,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                   "router.utorrent.com:6881,dht.transmissionbt.com:6881",
        })
        self.session = lt.session(s)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.engine = self
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self._load_state()

    # -- adding -----------------------------------------------------------
    def _add(self, atp, meta):
        import cinehaven
        offline = False
        if meta.get("save_path"):
            offline = not os.path.isdir(meta["save_path"])  # its drive is unplugged: wait, don't re-download
        else:
            try:
                os.makedirs(cinehaven.DOWNLOAD_DIR, exist_ok=True)
            except OSError:
                raise RuntimeError("The drive where CineHaven keeps your films isn't connected. Plug it in, "
                                   "or choose another place in Menu → Where films are kept, and try again.")
        atp.save_path = meta.get("save_path") or cinehaven.DOWNLOAD_DIR
        meta["save_path"] = atp.save_path
        if meta.get("paused") or offline:
            atp.flags |= lt.torrent_flags.paused
            atp.flags &= ~lt.torrent_flags.auto_managed
        pre = str(atp.ti.info_hash()) if atp.ti is not None else str(atp.info_hashes.v1)
        if pre in self.items:
            return self.items[pre]
        h = self.session.add_torrent(atp)
        key = str(h.info_hash())
        it = Item(self, h, meta)
        it.offline = offline
        self.items[key] = it
        return it

    def add_torrent_bytes(self, data, meta):
        ti = lt.torrent_info(lt.bdecode(data))
        atp = lt.add_torrent_params()
        atp.ti = ti
        key = str(ti.info_hash())
        if key in self.items:
            return self.items[key]
        # Pre-set file priorities so nothing else in the torrent downloads
        it = self._add(atp, meta)
        with open(os.path.join(TORRENT_DIR, key + ".torrent"), "wb") as f:
            f.write(data)
        it.choose_file(meta.get("file_name"))
        self.save_state()
        return it

    def add_magnet(self, uri, meta):
        clean = normalize_magnet(uri)
        if not clean:
            raise ValueError("no magnet link found in that text")
        uri = clean
        atp = lt.parse_magnet_uri(uri)
        meta["magnet"] = uri
        it = self._add(atp, meta)
        self.save_state()
        return it

    def add_torrent_url(self, url, meta):
        return self.add_torrent_bytes(fetch(url), meta)

    def save_torrent_file(self, it):
        path = os.path.join(TORRENT_DIR, it.key + ".torrent")
        if os.path.exists(path) or not it.has_metadata():
            return
        ct = lt.create_torrent(it.handle.torrent_file())
        with open(path, "wb") as f:
            f.write(lt.bencode(ct.generate()))

    def remove(self, it, delete_files=False):
        self.items.pop(it.key, None)
        opts = lt.session.delete_files if delete_files else 0
        self.session.remove_torrent(it.handle, opts)
        try:
            os.remove(os.path.join(TORRENT_DIR, it.key + ".torrent"))
        except FileNotFoundError:
            pass
        self.save_state()

    # -- persistence ------------------------------------------------------
    def save_state(self):
        from .health import save_json
        save_json(STATE_FILE, {k: it.meta for k, it in self.items.items()})

    def _load_state(self):
        from .health import load_json
        data = load_json(STATE_FILE, {})
        for key, meta in data.items():
            tpath = os.path.join(TORRENT_DIR, key + ".torrent")
            try:
                if os.path.exists(tpath):
                    atp = lt.add_torrent_params()
                    with open(tpath, "rb") as f:
                        atp.ti = lt.torrent_info(lt.bdecode(f.read()))
                    it = self._add(atp, meta)
                    if meta.get("file_index") is not None:
                        it.choose_file(meta.get("file_name"))
                elif meta.get("magnet"):
                    self._add(lt.parse_magnet_uri(meta["magnet"]), meta)
            except Exception as e:  # corrupt entry – skip it, keep the rest
                print("could not restore", key, e)

    def poll_alerts(self):
        """Returns a list of (item, event) for the UI."""
        events = []
        for a in self.session.pop_alerts():
            h = getattr(a, "handle", None)
            it = self.items.get(str(h.info_hash())) if h is not None and h.is_valid() else None
            if it is None:
                continue
            if isinstance(a, lt.metadata_received_alert):
                events.append((it, "metadata"))
            elif isinstance(a, lt.torrent_finished_alert):
                events.append((it, "finished"))
            elif isinstance(a, lt.torrent_error_alert):
                events.append((it, "error:" + a.message()))
        return events

    def shutdown(self):
        self.closing = True
        try:
            self.save_state()
        except Exception:
            pass
        self.server.shutdown()
        self.session.pause()
