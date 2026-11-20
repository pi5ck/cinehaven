"""Search everywhere a film can be watched legally.

One query fans out to:
  * official full-movie uploads on YouTube (studio / distributor channels only)
  * every streaming service in the user's countries via JustWatch – free (with ads)
    services first, then subscription, rent and buy
(The Internet Archive search, which plays inside CineHaven, runs alongside in app.py.)
Pirate sites are never searched.
"""
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import watch, youtube

FLAG = {c: chr(0x1F1E6 + ord(c[0]) - 65) + chr(0x1F1E6 + ord(c[1]) - 65) for c in watch.COUNTRIES}
FREE_KINDS = ("FREE", "ADS")


def _relevant(query, titles):
    """Titles containing every word searched for (or the closest few if none do)."""
    want = watch._norm(query).split()
    good = [t for t in titles if all(w in watch._norm(t["title"]).split() for w in want)]
    good.sort(key=lambda t: watch._norm(t["title"]) != " ".join(want))      # exact title first
    return good


def _streaming(query, countries, limit=8):
    titles = {}
    # the user's countries first, then big catalogues to identify films not offered locally
    for c in dict.fromkeys(list(countries) + ["US", "GB"]):
        try:
            for t in _relevant(query, watch.search(query, c, limit=20)):
                titles.setdefault(t["id"], t)
        except Exception:
            continue
        if len(titles) >= limit:
            break
    titles = list(titles.values())[:limit]

    def with_offers(t):
        services, free = {}, False
        for c in countries:
            try:
                offs = watch.offers(t["id"], c)
            except Exception:
                continue
            for o in offs:
                free = free or o["kind"] in FREE_KINDS
                label = f"{o['service']} · {'Free' if o['kind'] in FREE_KINDS else o['label']}"
                u, flags = services.get(label, (o["url"], ""))
                services[label] = (u, flags + FLAG[c])
        order = lambda kv: (0 if "· Free" in kv[0] else 1, kv[0])  # noqa: E731
        if not services:      # not offered here right now – still show it, with a search link
            q = urllib.parse.quote(t["title"])
            services = {"Not streaming here right now – find it": (
                f"https://www.justwatch.com/{countries[0].lower()}/search?q={q}", "")}
        return dict(t, services=dict(sorted(services.items(), key=order)), free=free)

    with ThreadPoolExecutor(max_workers=6) as pool:
        res = list(pool.map(with_offers, titles))
    res.sort(key=lambda t: not t["free"])
    return res


def everywhere(query, countries):
    """-> {"youtube": [...], "streaming": [...], "errors": [...]}"""
    out = {"youtube": [], "streaming": [], "errors": []}
    with ThreadPoolExecutor(max_workers=2) as pool:
        yt = pool.submit(youtube.search, query)
        st = pool.submit(_streaming, query, countries)
        for key, fut in (("youtube", yt), ("streaming", st)):
            try:
                out[key] = fut.result(timeout=45)
            except Exception as e:
                out["errors"].append(f"{key}: {e}")
    return out
