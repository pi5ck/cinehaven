"""Watch together: friends on other computers watch the same film in step.

Everyone in a room has the same short room code. Messages travel through a public
publish/subscribe relay (ntfy.sh by default, or your own ntfy server), end-to-end
encrypted with a key made from the room code – the relay only ever sees random bytes
on a topic name that is itself derived from the code. Nothing is stored there
("Cache: no"), and each person plays their own copy of the film; only pause, play,
position and chat messages are exchanged.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import random
import secrets
import threading
import time
import urllib.request

DEFAULT_SERVER = "https://ntfy.sh"
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"           # no 0/O/1/I/L mix-ups
UA = "CineHaven-Together"


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(11))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def clean_code(text):
    t = "".join(c for c in (text or "").upper() if c.isalnum())
    t = t.replace("O", "0").replace("I", "1").replace("L", "1")
    t = "".join(c for c in t if c in ALPHABET)
    return f"{t[:4]}-{t[4:8]}-{t[8:]}" if len(t) == 11 else None


class _Crypto:
    """Encrypt-then-MAC with HMAC-SHA256 (counter-mode keystream + 128-bit tag); standard library only."""

    def __init__(self, code):
        k = hashlib.pbkdf2_hmac("sha256", code.encode(), b"cinehaven-together-v1", 60000, 64)
        self.ek, self.mk = k[:32], k[32:]
        self.topic = "chw" + hashlib.sha256(b"topic" + k).hexdigest()[:29]

    def _stream(self, nonce, n):
        out, i = bytearray(), 0
        while len(out) < n:
            out += hmac.new(self.ek, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
            i += 1
        return bytes(out[:n])

    def seal(self, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        nonce = os.urandom(16)
        ct = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self.mk, nonce + ct, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + ct + tag).decode()

    def open(self, text):
        try:
            raw = base64.urlsafe_b64decode(text.encode())
        except Exception:
            return None
        if len(raw) < 33:
            return None
        nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
        if not hmac.compare_digest(tag, hmac.new(self.mk, nonce + ct, hashlib.sha256).digest()[:16]):
            return None
        try:
            return json.loads(bytes(a ^ b for a, b in zip(ct, self._stream(nonce, len(ct)))))
        except ValueError:
            return None


class Room:
    """One room. on_message(dict) is called from a background thread for every message from others;
    on_status(text_or_None) when the connection drops or comes back."""

    def __init__(self, code, name, on_message, on_status=None, server=None):
        self.code = code
        self.name = name
        self.me = secrets.token_hex(4)
        self.server = (server or DEFAULT_SERVER).rstrip("/")
        self.crypto = _Crypto(code)
        self.on_message = on_message
        self.on_status = on_status or (lambda s: None)
        self.alive = True
        self.connected = False
        self._out = queue.Queue()
        self._last_send = 0.0
        self._resp = None
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._sender, daemon=True).start()

    # -- sending: in order, at most ~1 per second, newer "state" replaces an unsent older one
    def send(self, msg):
        msg = dict(msg, id=self.me, name=self.name, at=time.time())
        self._out.put(msg)

    def _sender(self):
        while self.alive:
            try:
                msg = self._out.get(timeout=1)
            except queue.Empty:
                continue
            if msg.get("t") == "state":                     # coalesce a burst of seeks into the last one
                time.sleep(0.4)
                pending = []
                while not self._out.empty():
                    pending.append(self._out.get_nowait())
                later = [m for m in pending if m.get("t") == "state"]
                if later:
                    msg = later[-1]
                for m in pending:
                    if m.get("t") != "state":
                        self._out.put(m)
            wait = 1.0 - (time.time() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            self._post(msg)

    def _post(self, msg, tries=3):
        body = self.crypto.seal(msg).encode()
        for i in range(tries):
            try:
                req = urllib.request.Request(f"{self.server}/{self.crypto.topic}", data=body, method="POST",
                                             headers={"User-Agent": UA, "Cache": "no", "Firebase": "no",
                                                      "Content-Type": "text/plain"})
                urllib.request.urlopen(req, timeout=15).read()
                self._last_send = time.time()
                return True
            except Exception as e:
                if getattr(e, "code", None) == 429:          # relay's rate limit – back off
                    time.sleep(6 * (i + 1))
                else:
                    time.sleep(1 + i)
        self.on_status("Couldn't send to the room – check your internet connection")
        return False

    # -- receiving: one long-lived streaming request, reconnecting with back-off
    def _listen(self):
        delay = 1
        while self.alive:
            try:
                url = f"{self.server}/{self.crypto.topic}/json?since={int(time.time()) - 2}"
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=90) as r:
                    self._resp = r
                    if not self.connected:
                        self.connected = True
                        self.on_status(None)
                    delay = 1
                    for line in r:
                        if not self.alive:
                            return
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("event") != "message":
                            continue
                        msg = self.crypto.open(ev.get("message") or "")
                        if msg and msg.get("id") != self.me:
                            try:
                                self.on_message(msg)
                            except Exception:
                                pass
            except Exception:
                if not self.alive:
                    return
                if self.connected:
                    self.connected = False
                    self.on_status("Reconnecting to the room…")
                time.sleep(delay + random.random())
                delay = min(delay * 2, 30)

    def close(self):
        if not self.alive:
            return
        try:
            self._post(dict(t="bye", id=self.me, name=self.name, at=time.time()), tries=1)
        except Exception:
            pass
        self.alive = False
        try:
            if self._resp:
                self._resp.close()
        except Exception:
            pass


def topic_for(code):
    """For tests: the relay topic a code maps to."""
    return _Crypto(code).topic

