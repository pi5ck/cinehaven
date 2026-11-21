"""Sport: live scores and fixtures, plus where to watch each match legally.

Scores come from ESPN's public scoreboard feed. CineHaven never streams
matches itself – live sport is sold to broadcasters per country, so the
"Watch" buttons open the official broadcaster or World Rugby's RugbyPass TV.
"""
import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

API = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

# (key, label, [(espn path, short league name)])
SPORTS = [
    ("rugby", "🏉 Rugby", [
        ("rugby/17567", "Nations Championship"),
        ("rugby/244293", "Rugby Championship"),
        ("rugby/180659", "Six Nations"),
        ("rugby/270557", "URC"),
        ("rugby/271937", "Champions Cup"),
        ("rugby/164205", "Rugby World Cup"),
    ]),
    ("football", "⚽ Football", [
        ("soccer/rsa.1", "SA Premiership"),
        ("soccer/eng.1", "Premier League"),
        ("soccer/uefa.champions", "Champions League"),
        ("soccer/esp.1", "LaLiga"),
        ("soccer/fifa.world", "World Cup"),
    ]),
]

# Official places to watch, per home country. Only services we know hold rights.
BROADCASTERS = {
    "ZA": [("SuperSport on DStv Stream", "https://now.dstv.com/livetv", "Subscription")],
}
RUGBYPASS = ("RugbyPass TV", "https://rugbypass.tv", "Free in countries without a TV deal")


def _get(path):
    req = urllib.request.Request(API.format(path=path), headers={"User-Agent": "CineHaven",
                                                                  "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _small(url):
    """ESPN crests are 500 px; ask their resizer for 80 px ones."""
    pre = "https://a.espncdn.com/"
    if url.startswith(pre) and "/combiner/" not in url:
        return f"{pre}combiner/i?img=/{url[len(pre):]}&w=80&h=80"
    return url


def _match(ev, league, sport):
    comp = ev["competitions"][0]
    st = ev["status"]["type"]
    teams = sorted(comp["competitors"], key=lambda t: t.get("homeAway") != "home")
    side = [{"name": t["team"].get("displayName") or t["team"].get("name", "?"),
             "short": t["team"].get("abbreviation", ""),
             "logo": _small(t["team"].get("logo", "")),
             "score": t.get("score", "")} for t in teams]
    state = st.get("state", "pre")                      # pre / in / post
    when = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
    if state == "in":
        status = st.get("description") or "Live"
    elif state == "post":
        status = "Full time" if st.get("completed") else (st.get("description") or "Finished")
    else:
        status = when.astimezone().strftime("%a %d %b · %H:%M")
    link = next((l["href"] for l in ev.get("links", []) if "match" in l.get("href", "")), "")
    return {"id": ev["id"], "sport": sport, "league": league, "state": state, "status": status,
            "when": when, "home": side[0], "away": side[1] if len(side) > 1 else side[0],
            "venue": (comp.get("venue") or {}).get("fullName", ""), "link": link,
            "tv": [n for b in comp.get("broadcasts", []) for n in b.get("names", [])]}


def matches(sport_key):
    """All of today's/this round's matches for one sport, live first, then upcoming, then results."""
    _k, _l, leagues = next(s for s in SPORTS if s[0] == sport_key)

    def one(item):
        path, name = item
        try:
            return [_match(e, name, sport_key) for e in _get(path).get("events", [])]
        except Exception:
            return []
    with ThreadPoolExecutor(6) as ex:
        out = [m for chunk in ex.map(one, leagues) for m in chunk]
    order = {"in": 0, "pre": 1, "post": 2}
    now = datetime.now(timezone.utc)
    out.sort(key=lambda m: (order.get(m["state"], 3),
                            abs((m["when"] - now).total_seconds()) if m["state"] != "pre" else m["when"].timestamp()))
    return out


def where_to_watch(m, home):
    """[(label, url, note)] official options for this match in the user's countries."""
    out = []
    for cc in home:
        for b in BROADCASTERS.get(cc, []):
            if b not in out:
                out.append(b)
    if m["sport"] == "rugby":
        out.append(RUGBYPASS)
    from .watch import COUNTRIES
    for cc in home:
        q = f'{m["home"]["name"]} vs {m["away"]["name"]} {m["league"]} TV channel {COUNTRIES.get(cc, cc)}'
        out.append((f"Find the broadcaster in {COUNTRIES.get(cc, cc)}",
                    "https://duckduckgo.com/?q=" + urllib.parse.quote(q), "Web search"))
    return out


def highlights_query(m):
    return f'{m["home"]["name"]} vs {m["away"]["name"]} highlights {m["league"]} {m["when"].year}'
