"""AirPlay video sender – Apple TV, Macs, and AirPlay 2 TVs (Samsung, LG, Sony, Vizio…).

CineHaven's own implementation, using only the `cryptography` package:

* Discovery: Bonjour (_airplay._tcp) via zeroconf.
* Pairing: HomeKit-style pairing, as modern receivers require.
    - transient pairing (fixed PIN 3939) when the receiver lets everyone on the network play;
    - PIN pairing when it asks for a code: the receiver shows a 4-digit code, the user types it
      into CineHaven once, and the keys are remembered.
  SRP-6a (3072-bit, SHA-512), Ed25519, X25519, ChaCha20-Poly1305 and HKDF-SHA512.
* Control: the AirPlay video endpoints (/play, /rate, /scrub, /playback-info, /stop) over the
  encrypted connection. The receiver fetches the video itself from CineHaven's file server.
"""
import hashlib
import json
import os
import plistlib
import secrets
import socket
import threading
import time
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import DATA_DIR
from .health import log

KEYS_FILE = os.path.join(DATA_DIR, "airplay_keys.json")
TRANSIENT_PIN = "3939"
USER_AGENT = "AirPlay/550.10"

# RFC 5054 3072-bit group, generator 5
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF")
N = int(N_HEX, 16)
G = 5
N_BYTES = 384


class PinRequired(Exception):
    """The receiver wants a PIN: it is showing one on screen now."""


class AuthFailed(Exception):
    pass


# ── TLV8 ────────────────────────────────────────────────────────────────
T_METHOD, T_ID, T_SALT, T_PUBKEY, T_PROOF, T_ENC, T_STATE, T_ERROR, T_SIG, T_FLAGS = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0A, 0x13)


def tlv(*items):
    out = bytearray()
    for t, v in items:
        if isinstance(v, int):
            v = bytes([v])
        if not v:
            out += bytes([t, 0])
        for i in range(0, len(v), 255):
            chunk = v[i:i + 255]
            out += bytes([t, len(chunk)]) + chunk
    return bytes(out)


def untlv(data):
    out, i, last = {}, 0, None
    while i + 2 <= len(data):
        t, n = data[i], data[i + 1]
        v = data[i + 2:i + 2 + n]
        if t == last and t in out:
            out[t] += v           # continuation of a value longer than 255 bytes
        else:
            out[t] = v
        last = t
        i += 2 + n
    return out


# ── crypto helpers ──────────────────────────────────────────────────────
def _hkdf(ikm, salt, info, n=32):
    return HKDF(algorithm=hashes.SHA512(), length=n, salt=salt.encode(), info=info.encode()).derive(ikm)


def _h(*parts):
    h = hashlib.sha512()
    for p in parts:
        h.update(p)
    return h.digest()


def _b(n, pad=None):
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return raw.rjust(pad, b"\0") if pad else raw


def _i(b):
    return int.from_bytes(b, "big")


def _nonce(label):
    return b"\0\0\0\0" + label.encode()


class SRPClient:
    def __init__(self, username, password):
        self.I, self.p = username.encode(), password.encode()
        self.a = _i(secrets.token_bytes(32))
        self.A = pow(G, self.a, N)

    def process(self, salt, B_bytes):
        B = _i(B_bytes)
        if B % N == 0:
            raise AuthFailed("bad SRP value from receiver")
        k = _i(_h(_b(N), _b(G, N_BYTES)))
        u = _i(_h(_b(self.A, N_BYTES), _b(B, N_BYTES)))
        x = _i(_h(salt, _h(self.I + b":" + self.p)))
        S = pow((B - k * pow(G, x, N)) % N, self.a + u * x, N)
        self.K = _h(_b(S))
        hn, hg = _h(_b(N)), _h(_b(G))
        self.M1 = _h(bytes(a ^ b for a, b in zip(hn, hg)), _h(self.I), salt, _b(self.A), B_bytes, self.K)
        self.M2 = _h(_b(self.A), self.M1, self.K)
        return _b(self.A), self.M1


# ── encrypted HTTP connection ───────────────────────────────────────────
class Connection:
    """One TCP connection to the receiver; HTTP/1.1, encrypted after pairing."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.out_key = self.in_key = None
        self.out_n = self.in_n = 0
        self.raw = b""
        self.plain = b""
        self.lock = threading.Lock()
        self.local_ip = self.sock.getsockname()[0]

    def encrypt_with(self, out_key, in_key):
        self.out_key, self.in_key = ChaCha20Poly1305(out_key), ChaCha20Poly1305(in_key)

    def _send(self, data):
        if not self.out_key:
            self.sock.sendall(data)
            return
        out = bytearray()
        for i in range(0, len(data), 1024):
            block = data[i:i + 1024]
            aad = len(block).to_bytes(2, "little")
            nonce = b"\0\0\0\0" + self.out_n.to_bytes(8, "little")
            self.out_n += 1
            out += aad + self.out_key.encrypt(nonce, block, aad)
        self.sock.sendall(bytes(out))

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("receiver closed the connection")
        if not self.in_key:
            self.plain += chunk
            return
        self.raw += chunk
        while len(self.raw) >= 2:
            n = int.from_bytes(self.raw[:2], "little")
            if len(self.raw) < 2 + n + 16:
                break
            aad, body = self.raw[:2], self.raw[2:2 + n + 16]
            self.raw = self.raw[2 + n + 16:]
            nonce = b"\0\0\0\0" + self.in_n.to_bytes(8, "little")
            self.in_n += 1
            self.plain += self.in_key.decrypt(nonce, body, aad)

    def request(self, method, path, body=b"", ctype=None, headers=None, timeout=15):
        with self.lock:
            self.sock.settimeout(timeout)
            h = {"Host": f"{self.host}:{self.port}", "User-Agent": USER_AGENT, "Content-Length": str(len(body))}
            if ctype:
                h["Content-Type"] = ctype
            h.update(headers or {})
            req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
            self._send(req.encode() + body)
            while b"\r\n\r\n" not in self.plain:
                self._fill()
            head, self.plain = self.plain.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            status = int(lines[0].split()[1])
            hdrs = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            n = int(hdrs.get("content-length", 0))
            while len(self.plain) < n:
                self._fill()
            data, self.plain = self.plain[:n], self.plain[n:]
            return status, hdrs, data

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ── remembered pairings ─────────────────────────────────────────────────
def _keys():
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _identity():
    """CineHaven's own long-term pairing identity (created once)."""
    k = _keys()
    if "self" not in k:
        sk = Ed25519PrivateKey.generate()
        k["self"] = {"id": str(uuid.uuid4()).upper(),
                     "sk": sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                            serialization.NoEncryption()).hex()}
        _save(k)
    return k["self"]["id"], Ed25519PrivateKey.from_private_bytes(bytes.fromhex(k["self"]["sk"]))


def _save(k):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(k, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, KEYS_FILE)


def _pub(sk):
    return sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# ── pairing ─────────────────────────────────────────────────────────────
HKP_NORMAL, HKP_TRANSIENT = "3", "4"


def _post_tlv(conn, path, payload, hkp):
    st, _h_, data = conn.request("POST", path, payload, "application/octet-stream",
                                 {"X-Apple-HKP": hkp, "Connection": "keep-alive"})
    if st != 200:
        raise AuthFailed(f"{path} answered HTTP {st}")
    d = untlv(data)
    if T_ERROR in d:
        raise AuthFailed(f"{path} refused (error {d[T_ERROR][0]})")
    return d


def pair_transient(conn):
    """Everyone-on-the-network receivers: pair with the fixed PIN and encrypt the connection."""
    d = _post_tlv(conn, "/pair-setup", tlv((T_METHOD, 0), (T_STATE, 1), (T_FLAGS, 0x10)), HKP_TRANSIENT)
    srp = SRPClient("Pair-Setup", TRANSIENT_PIN)
    A, M1 = srp.process(d[T_SALT], d[T_PUBKEY])
    d = _post_tlv(conn, "/pair-setup", tlv((T_STATE, 3), (T_PUBKEY, A), (T_PROOF, M1)), HKP_TRANSIENT)
    if d.get(T_PROOF) != srp.M2:
        raise AuthFailed("receiver's proof didn't match")
    conn.encrypt_with(_hkdf(srp.K, "Control-Salt", "Control-Write-Encryption-Key"),
                      _hkdf(srp.K, "Control-Salt", "Control-Read-Encryption-Key"))


def pin_start(conn):
    """Ask the receiver to show a PIN on screen."""
    st, _h_, _d = conn.request("POST", "/pair-pin-start", b"", None, {"X-Apple-HKP": HKP_NORMAL})
    if st != 200:
        raise AuthFailed(f"the receiver didn't show a code (HTTP {st})")


def pair_with_pin(conn, device_id, pin):
    """Full pairing with the code shown on the receiver; the keys are remembered."""
    my_id, my_sk = _identity()
    d = _post_tlv(conn, "/pair-setup", tlv((T_METHOD, 0), (T_STATE, 1)), HKP_NORMAL)
    srp = SRPClient("Pair-Setup", pin)
    A, M1 = srp.process(d[T_SALT], d[T_PUBKEY])
    d = _post_tlv(conn, "/pair-setup", tlv((T_STATE, 3), (T_PUBKEY, A), (T_PROOF, M1)), HKP_NORMAL)
    if d.get(T_PROOF) != srp.M2:
        raise AuthFailed("wrong code")
    enc_key = _hkdf(srp.K, "Pair-Setup-Encrypt-Salt", "Pair-Setup-Encrypt-Info")
    x = _hkdf(srp.K, "Pair-Setup-Controller-Sign-Salt", "Pair-Setup-Controller-Sign-Info")
    ltpk = _pub(my_sk)
    sig = my_sk.sign(x + my_id.encode() + ltpk)
    sub = tlv((T_ID, my_id.encode()), (T_PUBKEY, ltpk), (T_SIG, sig))
    enc = ChaCha20Poly1305(enc_key).encrypt(_nonce("PS-Msg05"), sub, None)
    d = _post_tlv(conn, "/pair-setup", tlv((T_STATE, 5), (T_ENC, enc)), HKP_NORMAL)
    sub = untlv(ChaCha20Poly1305(enc_key).decrypt(_nonce("PS-Msg06"), d[T_ENC], None))
    acc_id, acc_ltpk, acc_sig = sub[T_ID], sub[T_PUBKEY], sub[T_SIG]
    ax = _hkdf(srp.K, "Pair-Setup-Accessory-Sign-Salt", "Pair-Setup-Accessory-Sign-Info")
    Ed25519PublicKey.from_public_bytes(acc_ltpk).verify(acc_sig, ax + acc_id + acc_ltpk)
    k = _keys()
    k.setdefault("devices", {})[device_id] = {"id": acc_id.decode(errors="replace"), "ltpk": acc_ltpk.hex()}
    _save(k)


def pair_verify(conn, device_id):
    """Resume a remembered pairing and encrypt the connection."""
    dev = _keys().get("devices", {}).get(device_id)
    if not dev:
        raise PinRequired()
    my_id, my_sk = _identity()
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    d = _post_tlv(conn, "/pair-verify", tlv((T_STATE, 1), (T_PUBKEY, eph_pub)), HKP_NORMAL)
    srv_pub = d[T_PUBKEY]
    shared = eph.exchange(X25519PublicKey.from_public_bytes(srv_pub))
    key = _hkdf(shared, "Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info")
    sub = untlv(ChaCha20Poly1305(key).decrypt(_nonce("PV-Msg02"), d[T_ENC], None))
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(dev["ltpk"])).verify(
        sub[T_SIG], srv_pub + sub[T_ID] + eph_pub)
    sig = my_sk.sign(eph_pub + my_id.encode() + srv_pub)
    enc = ChaCha20Poly1305(key).encrypt(_nonce("PV-Msg03"), tlv((T_ID, my_id.encode()), (T_SIG, sig)), None)
    _post_tlv(conn, "/pair-verify", tlv((T_STATE, 3), (T_ENC, enc)), HKP_NORMAL)
    conn.encrypt_with(_hkdf(shared, "Control-Salt", "Control-Write-Encryption-Key"),
                      _hkdf(shared, "Control-Salt", "Control-Read-Encryption-Key"))


def forget(device_id):
    k = _keys()
    if k.get("devices", {}).pop(device_id, None) is not None:
        _save(k)


# ── the device ──────────────────────────────────────────────────────────
class AirPlay:
    kind = "AirPlay"

    def __init__(self, name, host, port, model="", device_id=None, props=None):
        self.name, self.host, self.port, self.model = name, host, port, model
        self.device_id = device_id or name
        self.props = props or {}
        self.conn = None
        self.session = str(uuid.uuid4()).upper()
        self.ask_pin = None              # set by the UI: callable() -> PIN string (or None to cancel)
        self._keepalive = None
        self._pair_lock = threading.Lock()

    def __repr__(self):
        return f"<AirPlay {self.name} ({self.model})>"

    def _connect(self):
        """Open an encrypted connection, pairing first if needed (one attempt at a time)."""
        with self._pair_lock:
            return self._connect_locked()

    def connect(self):
        self._connect()

    def _connect_locked(self):
        if self.conn:
            return self.conn
        conn = Connection(self.host, self.port)
        try:
            if _keys().get("devices", {}).get(self.device_id):
                try:
                    pair_verify(conn, self.device_id)
                    self.conn = conn
                    return conn
                except Exception as e:
                    log.info("AirPlay: saved pairing with %s no longer works (%s) – pairing again", self.name, e)
                    forget(self.device_id)
                    conn.close()
                    conn = Connection(self.host, self.port)
            try:
                pair_transient(conn)
                self.conn = conn
                return conn
            except (AuthFailed, ConnectionError, OSError, KeyError) as e:
                log.info("AirPlay: %s needs a code (%s)", self.name, e)
                transient_err = str(e)
                conn.close()
            # PIN pairing: the receiver shows a code, the user types it in CineHaven
            conn = Connection(self.host, self.port)
            try:
                pin_start(conn)
            except AuthFailed as e:
                if "403" in transient_err or "403" in str(e):
                    raise AuthFailed("refused (HTTP 403): AirPlay access is limited to the owner's devices")
                raise
            pin = self.ask_pin() if self.ask_pin else None
            if not pin:
                raise AuthFailed("pairing cancelled")
            pair_with_pin(conn, self.device_id, pin.strip())
            conn.close()
            conn = Connection(self.host, self.port)
            pair_verify(conn, self.device_id)
            self.conn = conn
            return conn
        except Exception:
            conn.close()
            raise

    def _req(self, method, path, body=b"", ctype=None):
        conn = self._connect()
        try:
            return conn.request(method, path, body, ctype, {"X-Apple-Session-ID": self.session})
        except (OSError, ConnectionError):
            self.conn = None          # connection dropped – reconnect once
            conn = self._connect()
            return conn.request(method, path, body, ctype, {"X-Apple-Session-ID": self.session})

    def play(self, url, title="Video", mime="video/mp4", start=0, subtitle=None):
        body = plistlib.dumps({"Content-Location": url, "Start-Position": 0.0,
                               "Start-Position-Seconds": float(start or 0),
                               "X-Apple-Session-ID": self.session, "uuid": self.session,
                               "streamType": 1, "mediaType": "file", "volume": 1.0, "rate": 1.0,
                               "clientProcName": "CineHaven", "SenderMACAddress": "",
                               "title": title}, fmt=plistlib.FMT_BINARY)
        st, _h_, data = self._req("POST", "/play", body, "application/x-apple-binary-plist")
        if st not in (200, 204):
            raise RuntimeError(f"{self.name} refused the video (HTTP {st})")
        if start and start > 5:
            for _ in range(20):           # seek once the receiver has the duration
                time.sleep(0.5)
                s = self.status()
                if s.get("dur"):
                    self.seek(start)
                    break
        self._start_keepalive()

    def _start_keepalive(self):
        """The receiver stops if the controlling connection goes quiet – poll it."""
        if self._keepalive:
            return
        stop = threading.Event()
        self._keepalive = stop

        def run():
            while not stop.wait(5):
                try:
                    self.status()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def pause(self):
        self._req("POST", "/rate?value=0.000000")

    def resume(self):
        self._req("POST", "/rate?value=1.000000")

    def seek(self, secs):
        self._req("POST", f"/scrub?position={max(0.0, float(secs)):.6f}")

    def volume(self, pct):
        # AirPlay video volume is set on the receiver's own remote; -144 dB…0 dB for audio streams only
        pass

    def stop(self):
        if self._keepalive:
            self._keepalive.set()
            self._keepalive = None
        try:
            self._req("POST", "/stop")
        finally:
            if self.conn:
                self.conn.close()
                self.conn = None

    def status(self):
        st, _h_, data = self._req("GET", "/playback-info")
        if st != 200 or not data:
            return {"state": "?"}
        try:
            p = plistlib.loads(data)
        except Exception:
            return {"state": "?"}
        rate = p.get("rate", 0)
        state = "PLAYING" if rate and rate > 0 else ("BUFFERING" if p.get("readyToPlay") is False else "PAUSED")
        if p.get("duration") is None and not p.get("readyToPlay", True):
            state = "BUFFERING"
        return {"state": state, "pos": p.get("position"), "dur": p.get("duration")}

    def server_info(self):
        st, _h_, data = self._req("GET", "/server-info")
        return plistlib.loads(data) if st == 200 and data else {}


# ── discovery ───────────────────────────────────────────────────────────
VIDEO_FEATURE = 0x1         # features bit 0: video


def discover(timeout=4):
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        log.info("AirPlay: zeroconf isn't installed – AirPlay discovery off")
        return []
    found = {}

    class Listener:
        def add_service(self, zc, t, name):
            info = zc.get_service_info(t, name, 2500)
            if not info:
                return
            props = {k.decode(): (v or b"").decode(errors="replace") for k, v in (info.properties or {}).items()}
            addrs = [a for a in info.parsed_addresses() if ":" not in a]
            if not addrs:
                return
            feats = props.get("features", "0").split(",")[0]
            try:
                f = int(feats, 16)
            except ValueError:
                f = 0
            if not f & VIDEO_FEATURE:
                return                     # speakers (HomePod, AirPort) can't show video
            nice = name.split("._airplay")[0]
            found[nice] = AirPlay(nice, addrs[0], info.port, props.get("model", ""),
                                  props.get("deviceid") or props.get("pi") or nice, props)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_airplay._tcp.local.", Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return list(found.values())
