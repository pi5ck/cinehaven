"""'Describe what you feel like watching' – film suggestions from AI running on this computer.

Small local models invent film titles, so they never pick the films themselves here:

1. A local chat model (through Ollama, https://ollama.com) reads the request and picks
   JustWatch genres to include and leave out.
2. Real, well-rated films in those genres come from JustWatch.
3. A local embedding model ranks them by how closely each plot matches the request.
4. The best matches come back with where they can be watched legally.

Only genre and title lookups leave the computer: the request itself stays on it.
"""
import json
import os
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import DATA_DIR, watch, websearch

OLLAMA = "http://127.0.0.1:11434"
CHAT_MODELS = ("qwen2.5:7b", "llama3.1:8b", "qwen2.5:3b", "llama3.2:3b", "gemma2:2b", "qwen2.5:1.5b")
EMBED_MODELS = ("nomic-embed-text", "mxbai-embed-large", "all-minilm")
EMBED_CACHE = os.path.join(DATA_DIR, "embed_cache.json")

GENRES = {"act": "Action & Adventure", "ani": "Animation", "cmy": "Comedy", "crm": "Crime",
          "doc": "Documentary", "drm": "Drama", "fml": "Kids & Family", "fnt": "Fantasy",
          "hrr": "Horror", "hst": "History", "msc": "Music & Musical", "rma": "Romance",
          "scf": "Science-Fiction", "spt": "Sport", "trl": "Mystery & Thriller", "war": "War & Military",
          "wsn": "Western"}
# fallback when no chat model is available: words in the request → genres
KEYWORDS = {"act": "action adventure fight chase heist", "ani": "animated animation cartoon pixar anime",
            "cmy": "funny comedy laugh hilarious witty", "crm": "crime gangster mafia detective heist",
            "doc": "documentary true real-life", "drm": "drama heartfelt emotional moving quiet touching",
            "fml": "family kids children", "fnt": "fantasy magic dragon wizard fairy",
            "hrr": "horror scary terrifying ghost haunted", "hst": "history historical period",
            "msc": "music musical singing band", "rma": "romance romantic love",
            "scf": "space sci-fi science future alien robot", "spt": "sport sports football boxing",
            "trl": "thriller mystery tense suspense twist", "war": "war soldier military",
            "wsn": "western cowboy"}
GENRE_PROMPT = ("Pick film genres for this request. Genres (code: name): "
                + ", ".join(f"{k}: {v}" for k, v in GENRES.items())
                + '. Reply ONLY with JSON like {{"include":["drm"],"exclude":["hrr"]}}. '
                  "Use 1 to 3 codes in include. Request: {q}")


class NoModel(Exception):
    pass


def _post(path, payload, timeout=180):
    req = urllib.request.Request(OLLAMA + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ollama_binary():
    import shutil
    for p in (shutil.which("ollama"), os.path.expanduser("~/.ollama-local/bin/ollama"),
              os.path.expanduser("~/.local/bin/ollama"), "/usr/local/bin/ollama",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
              "/Applications/Ollama.app/Contents/Resources/ollama"):
        if p and os.path.isfile(p):
            return p
    return None


def _start_ollama():
    """Start the Ollama server in the background if it's installed but not running."""
    import subprocess
    import time
    exe = _ollama_binary()
    if not exe:
        return False
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434")
    local = os.path.expanduser("~/.ollama-local/models")
    if os.path.isdir(local) and "OLLAMA_MODELS" not in env:
        env["OLLAMA_MODELS"] = local
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([exe, "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(OLLAMA + "/api/version", timeout=1).close()
            return True
        except Exception:
            continue
    return False


def _installed():
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception:
            if attempt or not _start_ollama():
                break
    raise NoModel("Suggestions need the free local AI Ollama (ollama.com). Install it, then run:  "
                  "ollama pull nomic-embed-text  and  ollama pull qwen2.5:3b")


def _pick(names, wanted):
    for w in wanted:
        for n in names:
            if n == w or n.split(":")[0] == w.split(":")[0] and (":" not in w or n == w):
                return n
    return None


def models():
    """(chat model or None, embedding model) – raises NoModel without an embedding model."""
    names = _installed()
    embed = _pick(names, EMBED_MODELS)
    if not embed:
        raise NoModel("No AI embedding model is installed. In a terminal run:  ollama pull nomic-embed-text")
    chat = _pick(names, CHAT_MODELS) or next(
        (n for n in names if not any(w in n for w in ("coder", "embed", "minilm"))), None)
    return chat, embed


def genres_for(query, chat):
    """-> (include, exclude) JustWatch genre codes."""
    inc, exc = [], []
    if chat:
        try:
            r = _post("/api/generate", {"model": chat, "prompt": GENRE_PROMPT.format(q=query), "stream": False,
                                        "format": "json", "options": {"temperature": 0}}, timeout=90)
            d = json.loads(r.get("response") or "{}")
            inc = [g for g in d.get("include", []) if g in GENRES][:3]
            exc = list(dict.fromkeys(g for g in d.get("exclude", []) if g in GENRES and g not in inc))
        except Exception:
            pass
    words = set(re.findall(r"[a-z\-]+", query.lower()))
    for code, kw in KEYWORDS.items():            # words the user actually typed always count
        if words & set(kw.split()) and code not in inc and code not in exc:
            inc.append(code)
    for neg in re.findall(r"\b(?:no|not|without|nothing)\s+([a-z\-]+)", query.lower()):
        for code, kw in KEYWORDS.items():
            if neg in kw.split():
                exc.append(code)
                if code in inc:
                    inc.remove(code)
    return inc or ["drm"], exc


def _pool(include, country):
    """Real, well-regarded films in any of the genres (deduplicated), cached for 12 hours."""
    import time
    cache = watch._load_cache()
    ck = "pool:%s:%s" % (country, ",".join(sorted(include)))
    hit = cache.get(ck)
    if hit and time.time() - hit["t"] < watch.CACHE_TTL:
        return [dict(t) for t in hit["films"]]
    g = json.dumps(include)
    queries = [
        ("IMDB_SCORE", 100, "imdbVotes:{min:25000}"),       # acclaimed
        ("POPULAR", 60, "imdbVotes:{min:5000}"),             # popular now
    ]
    films = {}
    for sort, n, extra in queries:
        q = ("query($c:Country!){popularTitles(country:$c,first:%d,sortBy:%s,filter:{genres:%s,"
             "objectTypes:[MOVIE],%s}){edges{node{%s}}}}" % (n, sort, g, extra, watch._FIELDS))
        try:
            for e in watch._gql(q, {"c": country})["popularTitles"]["edges"]:
                t = watch._title(e["node"])
                if t["title"] and t["description"]:
                    films.setdefault(t["id"], t)
        except Exception:
            if sort == "IMDB_SCORE":
                raise
            continue
    films = list(films.values())
    cache[ck] = {"t": time.time(), "films": films}
    watch._save_cache()
    return films


_cache_lock = threading.Lock()


def _embed(model, texts):
    r = _post("/api/embed", {"model": model, "input": texts}, timeout=300)
    return r["embeddings"]


def _film_vectors(model, films):
    """Embeddings of each film's plot, cached on disk (plots don't change)."""
    with _cache_lock:
        try:
            with open(EMBED_CACHE) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    key = lambda t: f"{model}|{t['id']}"   # noqa: E731
    todo = [t for t in films if key(t) not in cache]
    for i in range(0, len(todo), 32):
        chunk = todo[i:i + 32]
        vecs = _embed(model, [f"search_document: {t['title']} ({t['year']}). {', '.join(GENRES.get(g, g) for g in t['genres'])}. "
                              f"{t['description']}" for t in chunk])
        for t, v in zip(chunk, vecs):
            cache[key(t)] = [round(x, 5) for x in v]
    if todo:
        with _cache_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = EMBED_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, EMBED_CACHE)
    return [cache[key(t)] for t in films]


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb or 1)


def _why(t):
    d = t["description"].strip()
    first = re.split(r"(?<=[.!?])\s", d, maxsplit=1)[0]
    return first if len(first) <= 160 else first[:157].rsplit(" ", 1)[0] + "…"


STOP = set("""a an the and or but of on in at to for with about from into set film films movie movies
something some any like want feel feeling watch watching show me i im i'm my we our that this is are be
very really quite kind sort type story about one who where when nothing not no without good great
best new old please""".split())
_GENRE_WORDS = {w for kw in KEYWORDS.values() for w in kw.split()}


def _content_words(query):
    """Words that describe the story itself ('train', 'father', 'space'), not the mood or genre."""
    return [w for w in re.findall(r"[a-z]+", query.lower())
            if len(w) > 2 and w not in STOP and w not in _GENRE_WORDS]


def _stem(w):
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def suggest(query, countries, n=12, progress=None):
    """-> (films, model description). Each film has "why", "services", "free", "match"."""
    say = progress or (lambda _m: None)
    chat, embed = models()
    say("Understanding what you'd like…")
    include, exclude = genres_for(query, chat)
    say("Finding films: " + ", ".join(GENRES[g] for g in include) + "…")
    films = _pool(include, "US")        # the largest catalogue; titles are global, offers come later
    words = _content_words(query)
    have = {t["id"] for t in films}
    for w in words[:4]:                          # films named after the story's subject
        try:
            for t in watch.search(w, "US", limit=15):
                if t["kind"] == "movie" and t["id"] not in have and t["description"] \
                        and set(t["genres"]) & set(include):
                    films.append(t)
                    have.add(t["id"])
        except Exception:
            continue
    films = [t for t in films if not set(t["genres"]) & set(exclude)
             and not any(w in (t["title"] or "").lower() for w in watch.ADULT)]
    if not films:
        return [], embed
    say(f"Comparing {len(films)} films with your description…")
    qv = _embed(embed, [f"search_query: {query}"])[0]
    vecs = _film_vectors(embed, films)
    for t, v in zip(films, vecs):
        text = {_stem(w) for w in re.findall(r"[a-z]+", (t["title"] + " " + t["description"]).lower())}
        hits = sum(1 for w in words if _stem(w) in text)
        t["match"] = _cos(qv, v) + 0.03 * hits + 0.01 * ((t.get("imdb") or 6.5) - 6.5)
    films.sort(key=lambda t: -t["match"])
    best = films[:n]
    say("Checking where to watch them…")
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(lambda t: websearch.with_offers(t, countries), best))
    for t in out:
        t["why"] = _why(t)
    used = embed + (f" + {chat}" if chat else "")
    return out, used
