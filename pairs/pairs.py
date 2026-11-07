#!/usr/bin/env python3
"""'Player B scores after Player A scores' finder (Sammy's own stat).
For every men's forward on Sammy's FC27 list: ESPN game logs for the last 3 seasons, every competition (no friendlies).
Pair rule: Player A scored in a game that kicked off 2-36h BEFORE Player B's game -> did B score?
Keeps pairs with at least MIN_N cases and a hit rate >= 50%. Writes pairs/pairs.json and pairs/pairs.md."""
import json, os, time, unicodedata, urllib.request, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/soccer"
SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "por.1", "ned.1", "tur.1", "bel.1", "sco.1", "usa.1", "ksa.1", "mex.1", "bra.1", "arg.1"]
SEASONS = ["2024", "2025", "2026"]
POS = {"ST", "CF", "LW", "RW", "CAM", "LF", "RF"}
MIN_N, MIN_RATE, MIN_OVR = 8, 0.50, 79
CACHE = os.path.join(HERE, "cache"); os.makedirs(CACHE, exist_ok=True)

def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 pairs"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(1 + i)
    print("fail", url[:110], err); return None

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("ß", "ss")
    for x in (".", "'", "-"): s = s.replace(x, " ")
    return " ".join(s.split())

def team_key(s):
    s = norm(s)
    for w in ("fc ", " fc", "cf ", " cf", "afc ", "ac ", "sc ", "ssc ", "as ", "us ", "rc ", "rcd ", "real ", "club ", " sv", "1 "):
        s = s.replace(w, " ")
    return " ".join(s.split())

def main():
    pool = json.load(open(os.path.join(HERE, "pool.json")))
    fwd = [p for p in pool if p.get("pos") in POS and p.get("ovr", 0) >= MIN_OVR and "women" not in (p.get("team") or "").lower()]
    # ESPN teams for each league
    teams = {}
    for lg in LEAGUES:
        d = get(f"{SITE}/{lg}/teams") or {}
        for t in d.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            tm = t["team"]; teams[team_key(tm["displayName"])] = (tm["id"], lg, tm["displayName"])
            teams[team_key(tm.get("shortDisplayName", tm["displayName"]))] = (tm["id"], lg, tm["displayName"])
    def find_team(name):
        k = team_key(name)
        if k in teams: return teams[k]
        for tk, v in teams.items():
            if k and (k in tk or tk in k): return v
        return None
    rosters, players = {}, {}
    for p in fwd:
        t = find_team(p["team"] or "")
        if not t: continue
        if t[0] not in rosters:
            rosters[t[0]] = (get(f"{SITE}/{t[1]}/teams/{t[0]}/roster") or {}).get("athletes", [])
        for a in rosters[t[0]]:
            n1, n2 = norm(a.get("displayName")), norm(p["name"])
            if n1 == n2 or (n1.split()[-1] == n2.split()[-1] and n1[0] == n2[0]) or (len(n2.split()) == 1 and n2 in n1.split()):
                players[a["id"]] = {"name": p["name"], "team": t[2], "ovr": p["ovr"]}
                break
    print("forwards matched", len(players), "of", len(fwd))
    # game logs (cached for finished seasons)
    games = {}  # pid -> list of (kickoff datetime, scored bool, event id)
    for pid in players:
        out = []
        for s in SEASONS:
            cf = os.path.join(CACHE, f"{pid}_{s}.json")
            if s != SEASONS[-1] and os.path.exists(cf):
                out += json.load(open(cf)); continue
            base = get(f"{WEB}/athletes/{pid}/gamelog?season={s}") or {}
            lgs = [o["value"] for f in base.get("filters", []) if f.get("name") == "league" for o in f.get("options", [])]
            rows = []
            for lg in lgs or [None]:
                g = base if lg is None else get(f"{WEB}/athletes/{pid}/gamelog?season={s}&league={lg}") or {}
                if "friendly" in (lg or "") or "club.friendly" in (lg or ""): continue
                names = g.get("names", []); meta = g.get("events", {})
                for st in g.get("seasonTypes", []):
                    for c in st.get("categories", []):
                        for e in c.get("events", []):
                            m = meta.get(e["eventId"], {})
                            if "friendl" in (m.get("leagueName", "")).lower(): continue
                            row = dict(zip(names, e["stats"]))
                            rows.append([m.get("gameDate", ""), int(float(row.get("totalGoals", 0) or 0)) > 0, e["eventId"]])
                time.sleep(0.15)
            rows = list({r[2]: r for r in rows}.values())
            json.dump(rows, open(cf, "w"))
            out += rows
        games[pid] = sorted([(dt.datetime.fromisoformat(r[0].replace("Z", "+00:00")), r[1], r[2]) for r in out if r[0]])
    # pairs
    res = []
    ids = list(games)
    for b in ids:
        gb = games[b]
        if len(gb) < 15: continue
        base_rate = sum(1 for x in gb if x[1]) / len(gb)
        for a in ids:
            if a == b or players[a]["team"] == players[b]["team"]: continue
            ga = [x for x in games[a] if x[1]]
            if not ga: continue
            n = h = 0; j = 0
            for kb, sb, _ in gb:
                lo, hi = kb - dt.timedelta(hours=36), kb - dt.timedelta(hours=2.5)   # Sammy 11/7: A kicks off 2.5h+ before B
                if any(lo <= ka <= hi for ka, _, _ in ga):
                    n += 1; h += 1 if sb else 0
            if n >= MIN_N and h / n >= MIN_RATE:
                res.append({"A": players[a]["name"], "A_team": players[a]["team"], "B": players[b]["name"], "B_team": players[b]["team"],
                            "hits": h, "n": n, "rate": round(h / n, 3), "B_base": round(base_rate, 3), "lift": round(h / n - base_rate, 3)})
    res.sort(key=lambda x: (-x["rate"], -x["n"]))
    json.dump({"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ"), "players": len(players), "pairs": res}, open(os.path.join(HERE, "pairs.json"), "w"), indent=1)
    L = [f"# Player B scores after Player A scores (50%+, at least {MIN_N} cases)", "",
         "_A scored in a game that kicked off 2.5-36h before B's game. Base = B's normal scoring rate. Lift = how much better than normal. "
         "Many pairs are luck (we tested thousands), so lift and sample size matter more than the raw %._", "",
         "| B scores after A | Hit | Rate | B normal | Lift |", "|---|---|---|---|---|"]
    for r in res[:300]:
        L.append(f'| {r["B"]} ({r["B_team"]}) after {r["A"]} ({r["A_team"]}) | {r["hits"]}/{r["n"]} | {r["rate"]*100:.0f}% | {r["B_base"]*100:.0f}% | {r["lift"]*100:+.0f} |')
    open(os.path.join(HERE, "pairs.md"), "w").write("\n".join(L) + "\n")
    print("pairs", len(res))

if __name__ == "__main__":
    main()
