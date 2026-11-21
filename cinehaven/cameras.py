"""Your own security cameras, DVRs and NVRs.

Any camera or recorder that streams one of these can be watched:
  RTSP / RTSPS (nearly all IP cameras, DVRs and NVRs), HTTP MJPEG (older cameras, phone
  camera apps), HLS (.m3u8), RTMP / HTTP-FLV (Reolink and others), SRT, UDP/RTP.

Ways to add one:
  * Search my network – ONVIF discovery (the standard most cameras and recorders speak)
    plus a quick look for devices with an RTSP port open.
  * Pick the make and type the address, user name and password – the stream address is
    built from the maker's known format; a recorder adds one tile per channel.
  * "Find it for me" – asks the camera over ONVIF for its stream address, or tries the
    common address formats one after another until one plays.
  * Paste any stream address.

Passwords are kept in cameras.json in CineHaven's data folder, readable only by you.
Nothing is sent anywhere except to the cameras themselves.
"""
import base64
import concurrent.futures as cf
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from . import DATA_DIR
from .health import load_json, log

STORE = os.path.join(DATA_DIR, "cameras.json")

# key: (label, main stream, sub stream or None, default port)
# {a} = "user:password@" (or ""), {h} = host, {p} = port, {c} = channel number, {c2} = 2-digit channel
BRANDS = {
    "hikvision": ("Hikvision · Annke · HiLook · Safire",
                  "rtsp://{a}{h}:{p}/Streaming/Channels/{c}01", "rtsp://{a}{h}:{p}/Streaming/Channels/{c}02", 554),
    "dahua": ("Dahua · Amcrest · Lorex · IC Realtime",
              "rtsp://{a}{h}:{p}/cam/realmonitor?channel={c}&subtype=0",
              "rtsp://{a}{h}:{p}/cam/realmonitor?channel={c}&subtype=1", 554),
    "reolink": ("Reolink", "rtsp://{a}{h}:{p}/Preview_{c2}_main", "rtsp://{a}{h}:{p}/Preview_{c2}_sub", 554),
    "uniview": ("Uniview (UNV)", "rtsp://{a}{h}:{p}/unicast/c{c}/s0/live", "rtsp://{a}{h}:{p}/unicast/c{c}/s1/live", 554),
    "swann": ("Swann · older Lorex DVRs", "rtsp://{a}{h}:{p}/ch{c2}/0", "rtsp://{a}{h}:{p}/ch{c2}/1", 554),
    "tapo": ("TP-Link Tapo · VIGI", "rtsp://{a}{h}:{p}/stream1", "rtsp://{a}{h}:{p}/stream2", 554),
    "axis": ("Axis", "rtsp://{a}{h}:{p}/axis-media/media.amp?camera={c}",
             "rtsp://{a}{h}:{p}/axis-media/media.amp?camera={c}&resolution=640x360", 554),
    "foscam": ("Foscam", "rtsp://{a}{h}:{p}/videoMain", "rtsp://{a}{h}:{p}/videoSub", 88),
    "hanwha": ("Hanwha · Samsung Wisenet", "rtsp://{a}{h}:{p}/profile2/media.smp", "rtsp://{a}{h}:{p}/profile3/media.smp", 554),
    "vivotek": ("Vivotek", "rtsp://{a}{h}:{p}/live1s1.sdp", "rtsp://{a}{h}:{p}/live1s2.sdp", 554),
    "eufy": ("Eufy (RTSP switched on)", "rtsp://{a}{h}:{p}/live0", None, 554),
    "wyze": ("Wyze (RTSP firmware)", "rtsp://{a}{h}:{p}/live", None, 554),
    "ipwebcam": ("Phone camera app (IP Webcam)", "http://{a}{h}:{p}/video", None, 8080),
    "mjpeg": ("Other – MJPEG over HTTP", "http://{a}{h}:{p}/mjpg/video.mjpg", None, 80),
}
GUESS_ORDER = ("hikvision", "dahua", "reolink", "uniview", "tapo", "swann", "hanwha", "axis", "vivotek",
               "foscam", "eufy", "wyze")
SCHEMES = ("rtsp://", "rtsps://", "http://", "https://", "rtmp://", "rtmps://", "srt://", "udp://", "rtp://")


class AuthError(Exception):
    pass


# ── saved cameras ─────────────────────────────────────────────────────────
def load():
    return load_json(STORE, {"cameras": []}).get("cameras", [])


def save(cams):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cameras": cams}, f, indent=1)
    os.replace(tmp, STORE)
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def new_id():
    return secrets.token_hex(4)


# ── addresses ─────────────────────────────────────────────────────────────
def _auth(user, password):
    if not user:
        return ""
    q = lambda s: urllib.parse.quote(s or "", safe="")  # noqa: E731
    return f"{q(user)}:{q(password)}@" if password else f"{q(user)}@"


def _host(h):
    h = h.strip()
    h = re.sub(r"^[a-z]+://", "", h).split("/")[0]
    if ":" in h and h.count(":") == 1:                 # "192.168.1.20:8554" – port typed with the host
        h = h.split(":")[0]
    try:
        if isinstance(ipaddress.ip_address(h.strip("[]")), ipaddress.IPv6Address):
            return f"[{h.strip('[]')}]"
    except ValueError:
        pass
    return h


def build(brand, host, port, user, password, channel=1):
    """(main, sub) stream addresses for a make of camera/recorder."""
    _label, main, sub, default_port = BRANDS[brand]
    v = {"a": _auth(user, password), "h": _host(host), "p": int(port or default_port),
         "c": int(channel), "c2": f"{int(channel):02d}"}
    return main.format(**v), (sub.format(**v) if sub else None)


def with_auth(url, user, password):
    """Put a user name and password into a stream address (replacing any already there)."""
    if not user:
        return url
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = _auth(user, password) + host + (f":{p.port}" if p.port else "")
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def redact(url):
    """The address with the password hidden, for showing on screen."""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1•••@", url or "")


def looks_like_stream(text):
    return (text or "").strip().lower().startswith(SCHEMES)


# ── testing an address ────────────────────────────────────────────────────
def _ffprobe():
    from . import fixvideo
    t = fixvideo._static() or fixvideo._system()
    return t[1] if t else shutil.which("ffprobe")


def test(url, timeout=8):
    """Open the stream briefly. Returns {'codec','w','h'} or raises (AuthError for a wrong password)."""
    exe = _ffprobe()
    if not exe:
        raise RuntimeError("FFmpeg isn't available to test the stream")
    us = str(int(timeout * 1e6))
    opts = ["-rtsp_transport", "tcp", "-timeout", us] if url.startswith(("rtsp", "rtsps")) else ["-rw_timeout", us]
    try:
        r = subprocess.run([exe, "-v", "error", *opts, "-analyzeduration", "3000000", "-probesize", "2000000",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                            "-of", "json", url], capture_output=True, text=True, timeout=timeout + 6)
    except subprocess.TimeoutExpired:
        raise RuntimeError("no answer from the camera") from None
    err = (r.stderr or "").strip()
    if "401" in err or "Unauthorized" in err or "403" in err:
        raise AuthError("the user name or password is wrong")
    try:
        s = json.loads(r.stdout or "{}").get("streams") or []
    except ValueError:
        s = []
    if r.returncode == 0 and s:
        return {"codec": s[0].get("codec_name", "?"), "w": s[0].get("width"), "h": s[0].get("height")}
    if "Connection refused" in err:
        raise RuntimeError("the camera refused the connection on that port")
    if "404" in err or "Not Found" in err or "461" in err:
        raise RuntimeError("the camera doesn't have a stream at that address")
    raise RuntimeError(err.splitlines()[-1] if err else "it didn't send any video")


def describe(info):
    names = {"h264": "H.264", "hevc": "H.265", "mjpeg": "MJPEG", "mpeg4": "MPEG-4", "av1": "AV1"}
    res = f"{info['w']}×{info['h']} " if info.get("w") else ""
    return res + names.get(info.get("codec"), (info.get("codec") or "").upper())


# ── ONVIF ─────────────────────────────────────────────────────────────────
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(root, name):
    return [e for e in root.iter() if _local(e.tag) == name]


def _security(user, password):
    if not user:
        return ""
    nonce = secrets.token_bytes(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + (password or "").encode()).digest()).decode()
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    return ('<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
            f"<Username>{esc(user)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
            f'#PasswordDigest">{digest}</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
            f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"{created}</Created></UsernameToken></Security></s:Header>")


def _soap(url, body, user, password, timeout=6):
    env = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
           'xmlns:tt="http://www.onvif.org/ver10/schema">' + _security(user, password) + f"<s:Body>{body}</s:Body></s:Envelope>")
    req = urllib.request.Request(url, env.encode(), {"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return ET.fromstring(r.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="ignore")
        if e.code in (400, 401, 403) and re.search(r"NotAuthorized|not authorized|Unauthorized|Sender", text, re.I):
            raise AuthError("the user name or password is wrong") from None
        raise


def onvif_streams(xaddr, user, password):
    """[{'name','main','w','h'}] – every stream profile the camera/recorder offers, best first."""
    info = {}
    try:
        d = _soap(xaddr, "<tds:GetDeviceInformation/>", user, password)
        for k in ("Manufacturer", "Model"):
            e = _find(d, k)
            info[k] = e[0].text if e and e[0].text else ""
    except AuthError:
        raise
    except Exception:
        pass
    media = xaddr
    try:
        caps = _soap(xaddr, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
                     user, password)
        for m in _find(caps, "Media"):
            x = [e for e in m if _local(e.tag) == "XAddr"]
            if x and x[0].text:
                media = x[0].text.strip()
                break
    except AuthError:
        raise
    except Exception:
        pass
    # some recorders report their own internal address; keep the host we reached it on
    reached = urllib.parse.urlsplit(xaddr)
    mp = urllib.parse.urlsplit(media)
    if mp.hostname != reached.hostname:
        media = urllib.parse.urlunsplit((mp.scheme, reached.netloc, mp.path, mp.query, ""))
    profiles = _soap(media, "<trt:GetProfiles/>", user, password)
    out = []
    for p in _find(profiles, "Profiles"):
        token = p.get("token")
        name = next((e.text for e in p if _local(e.tag) == "Name"), token)
        w = h = 0
        for r in _find(p, "Resolution"):
            for e in r:
                if _local(e.tag) == "Width":
                    w = int(e.text or 0)
                elif _local(e.tag) == "Height":
                    h = int(e.text or 0)
            break
        try:
            su = _soap(media, '<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>'
                              f'<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
                              f'<trt:ProfileToken>{token}</trt:ProfileToken></trt:GetStreamUri>', user, password)
            uri = next((e.text for e in _find(su, "Uri") if e.text), None)
        except AuthError:
            raise
        except Exception:
            uri = None
        if uri:
            up = urllib.parse.urlsplit(uri.strip())
            if up.hostname != reached.hostname:        # same: recorder may report an internal address
                uri = urllib.parse.urlunsplit((up.scheme, f"{reached.hostname}:{up.port or 554}", up.path, up.query, ""))
            out.append({"name": name, "main": with_auth(uri.strip(), user, password), "w": w, "h": h, "token": token})
    out.sort(key=lambda s: -(s["w"] * s["h"]))
    for s in out:
        s["maker"] = " ".join(v for v in (info.get("Manufacturer"), info.get("Model")) if v)
    return out


def _onvif_addrs(host):
    h = _host(host)
    return [f"http://{h}{'' if p == 80 else f':{p}'}/onvif/device_service" for p in (80, 8000, 8080, 2020, 8899, 888)]


def find_onvif(host, user, password, progress=None):
    """Try ONVIF on the usual ports. Returns streams, or [] if the device doesn't speak it."""
    for x in _onvif_addrs(host):
        if progress:
            progress(f"Asking {urllib.parse.urlsplit(x).netloc} over ONVIF…")
        try:
            s = onvif_streams(x, user, password)
            if s:
                return s
        except AuthError:
            raise
        except Exception:
            continue
    return []


# ── searching the network ─────────────────────────────────────────────────
PROBE = ('<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
         'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/'
         'discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>uuid:{id}</w:MessageID>'
         '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action '
         'e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
         '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>')


def discover_onvif(timeout=4.0, target=("239.255.255.250", 3702)):
    """[{'host','xaddr','name'}] for cameras/recorders that answer an ONVIF search."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.4)
        msg = PROBE.format(id=uuid.uuid4()).encode()
        s.sendto(msg, target)
        end, resent = time.time() + timeout, False
        while time.time() < end:
            if not resent and time.time() > end - timeout / 2:
                s.sendto(msg, target)            # UDP can get lost; ask twice
                resent = True
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode(errors="ignore")
            xs = re.findall(r"XAddrs>\s*([^<]+)<", text)
            x = next((u for u in (xs[0].split() if xs else []) if addr[0] in u), (xs[0].split() or [None])[0] if xs else None)
            scopes = " ".join(re.findall(r"Scopes>([^<]*)<", text)).split()
            pick = lambda kind: next((urllib.parse.unquote(sc.rsplit("/", 1)[-1]) for sc in scopes  # noqa: E731
                                      if sc.startswith(f"onvif://www.onvif.org/{kind}/")), "")
            name, hw = pick("name"), pick("hardware")
            label = name if name and hw and hw in name else " · ".join(v for v in (name, hw) if v)
            if x and addr[0] not in found:
                found[addr[0]] = {"host": addr[0], "xaddr": x, "name": label.replace("_", " ") or "Camera"}
    finally:
        s.close()
    return list(found.values())


def _open(host, port, t=0.35):
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def scan_rtsp(skip=()):
    """Devices on this computer's home network with a camera port open (RTSP 554/8554)."""
    from .cast import lan_ip
    ip = lan_ip()
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    except ValueError:
        return []
    if not net.is_private:
        return []
    hosts = [str(h) for h in net.hosts() if str(h) not in skip and str(h) != ip]

    def check(h):
        for p in (554, 8554):
            if _open(h, p):
                return {"host": h, "port": p, "name": f"Camera or recorder at {h}"}
        return None
    with cf.ThreadPoolExecutor(64) as ex:
        return [r for r in ex.map(check, hosts) if r]


def search(progress=None):
    if progress:
        progress("Asking cameras and recorders to answer (ONVIF)…")
    found = discover_onvif()
    if progress:
        progress("Looking for camera ports on your network…")
    found += scan_rtsp(skip={f["host"] for f in found})
    return found


# ── "find it for me" ──────────────────────────────────────────────────────
def find_stream(host, user, password, channel=1, port=None, progress=None):
    """Work out a camera's stream address. Returns (brand or 'onvif', main, sub, info)."""
    auth_failed = False
    try:
        streams = find_onvif(host, user, password, progress)
    except AuthError:
        streams, auth_failed = [], True
    if streams:
        main = streams[0]["main"]
        sub = streams[-1]["main"] if len(streams) > 1 else None
        try:
            return "onvif", main, sub, test(main)
        except AuthError:
            auth_failed = True
        except Exception:
            pass
    ports = [port] if port else [p for p in (554, 8554, 88) if _open(_host(host), p, 1.0)]
    if not ports:
        raise RuntimeError("no camera answered at that address – check it, and that the camera is switched on")
    for p in ports:
        for b in GUESS_ORDER:
            main, sub = build(b, host, p, user, password, channel)
            if progress:
                progress(f"Trying the {BRANDS[b][0].split(' ·')[0]} format…")
            try:
                return b, main, sub, test(main, timeout=5)
            except AuthError:
                auth_failed = True
            except Exception:
                continue
    if auth_failed:
        raise AuthError("the camera answered, but the user name or password is wrong")
    raise RuntimeError("the camera answered but none of the usual stream addresses worked – "
                       "paste the address from its manual or app instead")
