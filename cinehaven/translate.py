"""Subtitle translation to English (Google Translate web endpoint, MyMemory fallback)."""
import json
import threading
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
_cache = {}
_lock = threading.Lock()
ENGLISH = {"en", "eng", "english"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd"}


def is_english(lang):
    return (lang or "").lower().split("-")[0] in ENGLISH


def _google(text):
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=en&q="
           + urllib.parse.quote(text))
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=10) as r:
        data = json.loads(r.read())
    first = data[0]
    if isinstance(first, list):
        return first[0], (first[1] if len(first) > 1 else None)
    return first, None


def _mymemory(text, src="autodetect"):
    url = ("https://api.mymemory.translated.net/get?langpair=" + urllib.parse.quote(f"{src}|en")
           + "&q=" + urllib.parse.quote(text[:500]))
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=10) as r:
        data = json.loads(r.read())
    return data["responseData"]["translatedText"], None


def to_english(text):
    """Translate one subtitle line. Returns English text (or the original on failure)."""
    text = (text or "").strip()
    if not text:
        return ""
    with _lock:
        if text in _cache:
            return _cache[text]
    out = None
    for fn in (_google, _mymemory):
        try:
            out, _src = fn(text)
            if out:
                break
        except Exception:
            continue
    out = out or text
    with _lock:
        _cache[text] = out
    return out


def ass_escape(text):
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "\\N".join(text.splitlines())
