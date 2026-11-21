"""Profiles: everyone in the house gets their own Recently played and resume points.
Kids profiles get a safe, simple CineHaven: only family films and children's channels,
nothing can be opened from outside, an optional daily screen-time limit – and leaving
needs the grown-ups' PIN."""
import hashlib
import hmac
import os
import re
import secrets
import time

# what belongs to each person rather than the whole household
SCOPED = ("history", "resume", "together_name", "together_last", "discover_category", "live_section",
          "screen_time")
EMOJIS = ["🙂", "😎", "🦊", "🐼", "🦁", "🐸", "🐙", "🦄", "🐶", "🐱", "🚀", "⚽", "🎨", "🌻", "🍕", "🎸",
          "👑", "🐢", "🦖", "🌈"]
COLORS = ["#3584e4", "#e66100", "#2ec27e", "#9141ac", "#e01b24", "#f5c211", "#1c71d8", "#c64600"]

# Words that keep a title out of kids profiles even if a catalogue calls it "family"
NOT_FOR_KIDS = re.compile(
    r"\b(sex|sexual|nude|nudity|naked|erotic|adult|xxx|porn|horror|terror|zombie|vampire|murder|kill|killer|"
    r"blood|gore|massacre|slasher|devil|satan|demon|exorcis|drug|cocaine|marijuana|reefer|opium|war|nazi|"
    r"hitler|battle|combat|gangster|mob|crime|prison|noir|torture|suicide|madness|strip|burlesque|vice|"
    r"seduc|lust|sin\b|hell|ghoul|monster|creature|mutilat|cannibal|corpse|dead|death)", re.I)
# Old cartoons with racist caricatures or wartime hate (the "Censored Eleven" and similar)
OFFENSIVE = re.compile(
    r"(\bjaps?\b|\bnips\b|sambo|bosko|coal black|jungle jitters|pingo pongo|f[uü]e?hrer|uncle tom|clean pastures|"
    r"tokio jokio|jivin|angel puss|tin pan alley cats|rabbit stew|hallelujah land|meetin'? time|"
    r"education for death|herr meets hare|commando duck|spinach fer britain|seein'? red|scrap the|"
    r"you're a sap|mammy|pickaninny|darkie|injun|redskin|blackface|minstrel|"
    r"elephants dream|sintel|tears of steel|cosmos laundromat|agent 327)", re.I)   # open films made for grown-ups


def ensure(settings, default_name="Me"):
    """Older installs: everything so far becomes the first (grown-up) profile."""
    if settings.get("profiles"):
        return False
    settings["profiles"] = [{"id": "p1", "name": default_name, "emoji": "🙂", "color": COLORS[0], "kid": False}]
    settings["profile"] = "p1"
    settings["profile_data"] = {}
    return True


def all_(settings):
    return settings.get("profiles") or []


def get(settings, pid=None):
    pid = pid or settings.get("profile")
    return next((p for p in all_(settings) if p["id"] == pid), (all_(settings) or [None])[0])


def is_kid(settings):
    p = get(settings)
    return bool(p and p.get("kid"))


def switch(settings, pid):
    """Put away the current person's things, bring out the other person's."""
    cur = settings.get("profile")
    if cur == pid:
        return
    data = settings.setdefault("profile_data", {})
    if cur:
        data[cur] = {k: settings[k] for k in SCOPED if k in settings}
    for k in SCOPED:
        settings.pop(k, None)
    settings.update(data.pop(pid, {}))
    settings["profile"] = pid


def add(settings, name, emoji, kid=False, limit=0):
    pid = "p" + secrets.token_hex(3)
    p = {"id": pid, "name": name.strip()[:20] or "New profile", "emoji": emoji,
         "color": COLORS[len(all_(settings)) % len(COLORS)], "kid": bool(kid), "limit": int(limit or 0)}
    settings.setdefault("profiles", []).append(p)
    if kid:
        settings.setdefault("profile_data", {})[pid] = {"discover_category": "kids", "live_section": "kids"}
    return p


def remove(settings, pid):
    settings["profiles"] = [p for p in all_(settings) if p["id"] != pid]
    settings.get("profile_data", {}).pop(pid, None)


# ── the grown-ups' PIN ───────────────────────────────────────────────────
def has_pin(settings):
    return bool(settings.get("parent_pin"))


def _hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 120000).hex()


def set_pin(settings, pin):
    salt = secrets.token_hex(16)
    settings["parent_pin"] = {"salt": salt, "hash": _hash(pin, salt)}


_fails = {"n": 0, "until": 0}


def check_pin(settings, pin):
    """True/False; after 5 wrong tries, a one-minute wait (returns None while waiting)."""
    if time.time() < _fails["until"]:
        return None
    p = settings.get("parent_pin")
    if not p:
        return True
    ok = hmac.compare_digest(_hash(pin, p["salt"]), p["hash"])
    if ok:
        _fails["n"] = 0
    else:
        _fails["n"] += 1
        if _fails["n"] >= 5:
            _fails["n"] = 0
            _fails["until"] = time.time() + 60
    return ok


def pin_ok_format(pin):
    return bool(re.fullmatch(r"\d{4,8}", pin or ""))


# ── screen time for kids ─────────────────────────────────────────────────
def _today():
    return time.strftime("%Y-%m-%d")


def screen_time(settings):
    st = settings.get("screen_time") or {}
    if st.get("date") != _today():
        st = {"date": _today(), "secs": 0, "bonus": 0}
        settings["screen_time"] = st
    return st


def minutes_left(settings):
    """None = no limit for this profile."""
    p = get(settings)
    if not p or not p.get("kid") or not p.get("limit"):
        return None
    st = screen_time(settings)
    return (p["limit"] + st.get("bonus", 0)) - st["secs"] / 60


def add_watch(settings, secs):
    st = screen_time(settings)
    st["secs"] = st.get("secs", 0) + secs


def add_bonus(settings, minutes):
    st = screen_time(settings)
    st["bonus"] = st.get("bonus", 0) + minutes


def kid_safe(r, hidden=()):
    """A catalogue result fit for a kids profile."""
    t = r.get("title") or ""
    return not NOT_FOR_KIDS.search(t) and not OFFENSIVE.search(t) and r.get("id") not in hidden


def default_name():
    try:
        import pwd
        full = pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
        name = (full or pwd.getpwuid(os.getuid()).pw_name).split()[0]
        return name[:1].upper() + name[1:]
    except Exception:
        pass
    return "Me"
