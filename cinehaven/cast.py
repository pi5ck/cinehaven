"""Cast to a TV: Chromecast / Google TV, and DLNA / UPnP smart TVs (Samsung, LG, Sony, Philips…).

* Videos on this computer are offered to the TV by a small web server that only shares the one
  video being cast, under a random address, to devices on the home network.
* Chromecast uses pychromecast when it's installed. DLNA needs nothing extra: discovery (SSDP)
  and control (SOAP) are plain HTTP.
"""
import html
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import DATA_DIR
from .health import log

SSDP = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".ts": "video/mp2t", ".mpg": "video/mpeg",
        ".mpeg": "video/mpeg", ".wmv": "video/x-ms-wmv", ".ogv": "video/ogg", ".flv": "video/x-flv",
        ".srt": "application/x-subrip", ".vtt": "text/vtt", ".m3u8": "application/vnd.apple.mpegurl"}


def lan_ip():
    """This computer's address on the home network (the one the default route uses)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))     # no packet is sent; it just picks the LAN interface
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    if ip.startswith("127.") or ip.startswith("10.6."):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            pass
    return ip


def mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(path)[0] or "video/mp4"


# ── file server ──────────────────────────────────────────────────────────
class _Files(BaseHTTPRequestHandler):
    server_version = "CineHaven"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head):
        url_path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        path = self.server.shared.get(url_path)
        if path is None:                          # a file inside a shared folder (HLS segments)
            for prefix, folder in self.server.folders.items():
                if url_path.startswith(prefix):
                    name = url_path[len(prefix):]
                    if name and "/" not in name and ".." not in name:
                        path = os.path.join(folder, name)
                    break
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mime_for(path))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.end_headers()
        if head:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FileServer:
    """Shares chosen files on the LAN under unguessable addresses."""

    def __init__(self):
        self.httpd = None

    def _start(self):
        if self.httpd:
            return
        self.httpd = ThreadingHTTPServer(("0.0.0.0", 0), _Files)
        self.httpd.daemon_threads = True
        self.httpd.shared = {}
        self.httpd.folders = {}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def share(self, path):
        """-> URL a TV can fetch the file from."""
        self._start()
        self.httpd.shared.clear()                 # only the video being cast is shared
        self.httpd.folders.clear()
        name = urllib.parse.quote(os.path.basename(path))
        key = f"/{secrets.token_urlsafe(12)}/{name}"
        self.httpd.shared[key] = path
        return f"http://{lan_ip()}:{self.httpd.server_address[1]}{key}"

    def share_folder(self, folder, entry):
        """-> URL of `entry` inside `folder` (its neighbours are served too)."""
        self._start()
        self.httpd.shared.clear()
        self.httpd.folders.clear()
        prefix = f"/{secrets.token_urlsafe(12)}/"
        self.httpd.folders[prefix] = folder
        return f"http://{lan_ip()}:{self.httpd.server_address[1]}{prefix}{urllib.parse.quote(entry)}"

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


files = FileServer()


# ── devices ──────────────────────────────────────────────────────────────
class Device:
    kind = "?"

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<{self.kind} {self.name}>"


class DLNA(Device):
    kind = "DLNA"

    def __init__(self, name, avt_url, rc_url, model=""):
        super().__init__(name)
        self.avt_url, self.rc_url, self.model = avt_url, rc_url, model

    def _soap(self, url, service, action, args):
        body = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in args)
        env = ('<?xml version="1.0" encoding="utf-8"?>'
               '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
        req = urllib.request.Request(url, env.encode(), {"Content-Type": 'text/xml; charset="utf-8"',
                                                         "SOAPAction": f'"{service}#{action}"'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read().decode("utf-8", "replace")

    def _avt(self, action, *args):
        return self._soap(self.avt_url, AVT, action, (("InstanceID", 0),) + args)

    def play(self, url, title="Video", mime="video/mp4", start=0, subtitle=None):
        meta = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                f'<item id="0" parentID="-1" restricted="1"><dc:title>{html.escape(title)}</dc:title>'
                '<upnp:class>object.item.videoItem.movie</upnp:class>'
                f'<res protocolInfo="http-get:*:{mime}:*">{html.escape(url)}</res></item></DIDL-Lite>')
        try:
            self._avt("Stop")
        except Exception:
            pass
        self._avt("SetAVTransportURI", ("CurrentURI", url), ("CurrentURIMetaData", meta))
        self._avt("Play", ("Speed", 1))
        if start and start > 5:
            for _ in range(10):                   # TVs only accept a seek once playback has begun
                time.sleep(1)
                try:
                    if self.status().get("state") == "PLAYING":
                        self.seek(start)
                        break
                except Exception:
                    continue

    def pause(self):
        self._avt("Pause")

    def resume(self):
        self._avt("Play", ("Speed", 1))

    def stop(self):
        self._avt("Stop")

    def seek(self, secs):
        s = int(max(0, secs))
        self._avt("Seek", ("Unit", "REL_TIME"), ("Target", f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"))

    def volume(self, pct):
        if self.rc_url:
            self._soap(self.rc_url, RC, "SetVolume", (("InstanceID", 0), ("Channel", "Master"),
                                                      ("DesiredVolume", int(max(0, min(100, pct))))))

    def status(self):
        def hms(t):
            try:
                h, m, s = t.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except Exception:
                return None
        st = self._avt("GetTransportInfo")
        pos = self._avt("GetPositionInfo")
        state = re.search(r"<CurrentTransportState>([^<]*)", st)
        rel = re.search(r"<RelTime>([^<]*)", pos)
        dur = re.search(r"<TrackDuration>([^<]*)", pos)
        return {"state": state.group(1) if state else "?",
                "pos": hms(rel.group(1)) if rel else None, "dur": hms(dur.group(1)) if dur else None}


class Chromecast(Device):
    kind = "Chromecast"

    def __init__(self, cc):
        super().__init__(cc.cast_info.friendly_name if hasattr(cc, "cast_info") else cc.name)
        self.cc = cc

    def _mc(self):
        self.cc.wait(timeout=10)
        return self.cc.media_controller

    def play(self, url, title="Video", mime="video/mp4", start=0, subtitle=None):
        mc = self._mc()
        live = "mpegurl" in mime.lower()
        kw = {"title": title, "current_time": None if live else (start or 0),
              "stream_type": "LIVE" if live else "BUFFERED"}
        if subtitle:
            kw.update(subtitles=subtitle, subtitles_lang="en-US", subtitles_mime="text/vtt")
        mc.play_media(url, mime, **kw)
        mc.block_until_active(timeout=15)

    def pause(self):
        self._mc().pause()

    def resume(self):
        self._mc().play()

    def stop(self):
        try:
            self._mc().stop()
        finally:
            self.cc.quit_app()

    def seek(self, secs):
        self._mc().seek(max(0, secs))

    def volume(self, pct):
        self.cc.wait(timeout=10)
        self.cc.socket_client.receiver_controller.set_volume(max(0.0, min(1.0, pct / 100)))

    def status(self):
        s = self._mc().status
        return {"state": s.player_state, "pos": s.adjusted_current_time, "dur": s.duration}


def chromecast_available():
    try:
        import pychromecast  # noqa: F401
        return True
    except Exception:
        return False


def _discover_dlna(timeout=4):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           f"MX: 2\r\nST: {RENDERER}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.5)
    locations = set()
    try:
        ip = lan_ip()
        if not ip.startswith("127."):
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        for _ in range(2):
            s.sendto(msg, SSDP)
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                locations.add(m.group(1).decode())
    except OSError as e:
        log.info("Cast: DLNA discovery failed: %s", e)
    finally:
        s.close()
    out = []
    for loc in locations:
        try:
            out.append(_dlna_device(loc))
        except Exception as e:
            log.info("Cast: couldn't read %s: %s", loc, e)
    return [d for d in out if d]


def _dlna_device(location):
    with urllib.request.urlopen(location, timeout=5) as r:
        root = ET.fromstring(r.read())
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    dev = root.find(".//d:device", ns)
    name = dev.findtext("d:friendlyName", "TV", ns)
    model = dev.findtext("d:modelName", "", ns)
    base = root.findtext("d:URLBase", "", ns) or location
    avt = rc = None
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        st = svc.findtext("d:serviceType", "", ns)
        ctl = urllib.parse.urljoin(base, svc.findtext("d:controlURL", "", ns))
        if st.startswith("urn:schemas-upnp-org:service:AVTransport"):
            avt = ctl
        elif st.startswith("urn:schemas-upnp-org:service:RenderingControl"):
            rc = ctl
    return DLNA(name, avt, rc, model) if avt else None


# ── making a video playable on the TV ───────────────────────────────────
# what each kind of receiver plays straight from the file
DIRECT = {
    "AirPlay": ({"mov,mp4,m4a,3gp,3g2,mj2"}, {"h264", "hevc"}, {"aac", "ac3", "eac3", "alac", "mp3"}),
    "Chromecast": ({"mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"}, {"h264", "vp8", "vp9", "hevc", "av1"},
                   {"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3"}),
}
TV_DIR = os.path.join(DATA_DIR, "tvcast")


def needs_conversion(info, kind):
    if kind not in DIRECT or not info:
        return False                           # DLNA TVs differ too much – let the TV try the file
    fmts, vcodecs, acodecs = DIRECT[kind]
    fmt = (info.get("format") or {}).get("format_name", "")
    v = [st.get("codec_name") for st in info.get("streams", []) if st.get("codec_type") == "video"
         and not (st.get("disposition") or {}).get("attached_pic")]
    a = [st.get("codec_name") for st in info.get("streams", []) if st.get("codec_type") == "audio"]
    return not (fmt in fmts and (not v or v[0] in vcodecs) and (not a or a[0] in acodecs))


class TVStream:
    """A growing HLS copy of a video, made for a TV, starting at `start` seconds."""

    def __init__(self, path, start=0):
        from . import fixvideo
        self.path, self.start = path, max(0, int(start or 0))
        self.tools, self.info = fixvideo.tools_for(path)
        self.dir = os.path.join(TV_DIR, secrets.token_hex(6))
        self.proc = None

    def run(self, ready, failed):
        """Start converting; ready(url) once the TV can begin playing."""
        import subprocess
        shutil = __import__("shutil")
        shutil.rmtree(TV_DIR, ignore_errors=True)          # one TV stream at a time
        os.makedirs(self.dir)
        streams = (self.info or {}).get("streams", [])
        v = next((st for st in streams if st.get("codec_type") == "video"
                  and not (st.get("disposition") or {}).get("attached_pic")), None)
        a = next((st for st in streams if st.get("codec_type") == "audio"), None)
        copy_v = v is not None and v.get("codec_name") == "h264" and (v.get("pix_fmt") or "yuv420p") == "yuv420p"
        cmd = [self.tools[0], "-hide_banner", "-nostdin", "-y"]
        if self.start:
            cmd += ["-ss", str(self.start)]
        cmd += ["-fflags", "+genpts", "-i", self.path, "-map", "0:v:0?", "-map", "0:a:0?"]
        cmd += (["-c:v", "copy"] if copy_v else
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                 "-force_key_frames", "expr:gte(t,n_forced*4)"])
        cmd += (["-c:a", "copy"] if a is not None and a.get("codec_name") == "aac" else
                ["-c:a", "aac", "-b:a", "192k", "-ac", "2"])
        pl = os.path.join(self.dir, "index.m3u8")
        cmd += ["-f", "hls", "-hls_time", "6", "-hls_playlist_type", "event",
                "-hls_segment_filename", os.path.join(self.dir, "seg%05d.ts"), pl]
        log.info("Cast: preparing %s for the TV (%s video)", os.path.basename(self.path),
                 "copying" if copy_v else "converting")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     **({"creationflags": 0x08000000} if os.name == "nt" else {}))

        def watch():
            t0 = time.time()
            while self.proc.poll() is None and time.time() - t0 < 120:
                try:
                    if open(pl).read().count("#EXTINF") >= 2:
                        ready(files.share_folder(self.dir, "index.m3u8"))
                        return
                except OSError:
                    pass
                time.sleep(0.5)
            if os.path.exists(pl) and "#EXTINF" in open(pl).read():
                ready(files.share_folder(self.dir, "index.m3u8"))
            else:
                failed((self.proc.stderr.read() or b"").decode(errors="replace")[-300:])
        threading.Thread(target=watch, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def discover(timeout=5):
    """All TVs and cast devices on the home network."""
    found = []
    threads = []

    def airplay_():
        try:
            from . import airplay
            found.extend(airplay.discover(timeout - 1))
        except Exception as e:
            log.info("Cast: AirPlay discovery failed: %s", e)

    def dlna():
        found.extend(_discover_dlna(timeout))

    def cc():
        if not chromecast_available():
            return
        try:
            import pychromecast
            casts, browser = pychromecast.get_chromecasts(timeout=timeout)
            browser.stop_discovery()
            found.extend(Chromecast(c) for c in casts)
        except Exception as e:
            log.info("Cast: Chromecast discovery failed: %s", e)
    for f in (dlna, cc, airplay_):
        t = threading.Thread(target=f, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout + 3)
    seen, out = set(), []
    for d in found:
        if d.name not in seen:
            seen.add(d.name)
            out.append(d)
    return sorted(out, key=lambda d: d.name.lower())
