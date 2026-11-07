#!/usr/bin/env python3
"""Backtests of Sammy's research ideas on free ESPN data (closing spreads from ESPN's odds feed).
NBA 2024-25, 2025-26, 2026-27 so far; NFL 2024, 2025. Results -> backtest/results.md + results.json.
Each spot: ATS record, cover %, z vs a 50% coin flip, and a plain verdict (real / weak / noise)."""
import json, math, os, time, urllib.request, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache")
SITE = "https://site.api.espn.com/apis/site/v2/sports"; CORE = "https://sports.core.api.espn.com/v2/sports"

def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 bt"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(1 + i)
    return None

def games_nba(season_start, season_end, tag):
    cf = os.path.join(CACHE, f"nba_{tag}.json")
    old = json.load(open(cf)) if os.path.exists(cf) else {}
    d = season_start
    today = dt.date.today()
    while d <= min(season_end, today - dt.timedelta(days=1)):
        k = d.strftime("%Y%m%d")
        if k not in old:
            sb = get(f"{SITE}/basketball/nba/scoreboard?dates={k}") or {}
            day = []
            for e in sb.get("events", []):
                c = e["competitions"][0]
                if not c.get("status", {}).get("type", {}).get("completed"): continue
                if (e.get("season") or {}).get("type") not in (2, None): continue
                t = {x["homeAway"]: x for x in c["competitors"]}
                od = (get(f"{CORE}/basketball/leagues/nba/events/{e['id']}/competitions/{e['id']}/odds") or {}).get("items", [])
                sp = next((o.get("spread") for o in od if o.get("spread") is not None and "Live" not in (o.get("provider") or {}).get("name", "")), None)
                day.append({"id": e["id"], "date": e["date"], "home": t["home"]["team"]["abbreviation"], "away": t["away"]["team"]["abbreviation"],
                            "hs": float(t["home"].get("score") or 0), "as": float(t["away"].get("score") or 0), "spread": sp})
                time.sleep(0.05)
            old[k] = day
        d += dt.timedelta(days=1)
    json.dump(old, open(cf, "w"))
    return [g for k in sorted(old) for g in old[k]]

def zscore(w, n): return (w / n - 0.5) / math.sqrt(0.25 / n) if n else 0

def verdict(w, n):
    if n < 30: return "too few games"
    z = zscore(w, n)
    return "REAL (strong)" if abs(z) >= 2.5 else "weak lean" if abs(z) >= 1.5 else "noise"

def nba_tests(games):
    games = [g for g in games if g["spread"] is not None]
    games.sort(key=lambda g: g["date"])
    hist = {}
    rows = {}
    def add(name, covered):
        r = rows.setdefault(name, [0, 0, 0])
        if covered is None: r[2] += 1
        elif covered: r[0] += 1
        else: r[1] += 1
    for g in games:
        when = dt.datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        margin_home = g["hs"] - g["as"]
        res_home = margin_home + g["spread"]          # spread is the home line
        cov = {"home": None if res_home == 0 else res_home > 0, "away": None if res_home == 0 else res_home < 0}
        for side, team, opp in (("home", g["home"], g["away"]), ("away", g["away"], g["home"])):
            line = g["spread"] if side == "home" else -g["spread"]
            prev = hist.get(team, [])
            c = cov[side]
            add("All teams (baseline)", c)
            if line > 0: add("Underdogs (baseline)", c)
            if prev:
                p = prev[-1]; rest = (when - p["when"]).total_seconds() / 86400
                if p["margin"] >= 20: add("After a 20+ pt WIN", c)
                if p["margin"] <= -20: add("After a 20+ pt LOSS", c)
                if p["line"] <= -8 and p["margin"] < 0 and line > 0: add("Lost as 8+ fav, now an underdog", c)
                if p["opp"] == opp and rest <= 4:
                    add("Immediate rematch: won game 1" if p["margin"] > 0 else "Immediate rematch: lost game 1", c)
                if rest < 1.6: add("2nd night of back-to-back", c)
                if len(prev) >= 2 and (when - prev[-2]["when"]).total_seconds() / 86400 <= 3.6: add("3rd game in 4 nights", c)
                if rest >= 3: add("3+ days rest", c)
            met = [x for x in prev if x["opp"] == opp]
            if met and met[-1]["margin"] <= -15 and (when - met[-1]["when"]).days > 4: add("Revenge: lost last meeting by 15+", c)
            if side == "away" and g["home"] in ("DEN", "UTAH"):
                add("Road team at altitude (DEN/UTA)", c)
                if prev and (when - prev[-1]["when"]).total_seconds() / 86400 < 1.6: add("Road team at altitude on a back-to-back", c)
        for side, team, opp, m, line in (("home", g["home"], g["away"], margin_home, g["spread"]), ("away", g["away"], g["home"], -margin_home, -g["spread"])):
            hist.setdefault(team, []).append({"when": when, "opp": opp, "margin": m, "line": line})
    out = []
    for k, (w, l, p) in rows.items():
        n = w + l
        out.append({"spot": k, "w": w, "l": l, "push": p, "cover": round(w / n, 3) if n else None, "z": round(zscore(w, n), 2), "verdict": verdict(w, n)})
    return sorted(out, key=lambda x: (x["spot"] not in ("All teams (baseline)", "Underdogs (baseline)"), -abs(x["z"])))

NFL_XY = {"ARI": (33.53, -112.26), "ATL": (33.76, -84.40), "BAL": (39.28, -76.62), "BUF": (42.77, -78.79), "CAR": (35.23, -80.85), "CHI": (41.86, -87.62),
          "CIN": (39.10, -84.52), "CLE": (41.51, -81.70), "DAL": (32.75, -97.09), "DEN": (39.74, -105.02), "DET": (42.34, -83.05), "GB": (44.50, -88.06),
          "HOU": (29.68, -95.41), "IND": (39.76, -86.16), "JAX": (30.32, -81.64), "KC": (39.05, -94.48), "LV": (36.09, -115.18), "LAC": (33.95, -118.34),
          "LAR": (33.95, -118.34), "MIA": (25.96, -80.24), "MIN": (44.97, -93.26), "NE": (42.09, -71.26), "NO": (29.95, -90.08), "NYG": (40.81, -74.07),
          "NYJ": (40.81, -74.07), "PHI": (39.90, -75.17), "PIT": (40.45, -80.02), "SF": (37.40, -121.97), "SEA": (47.60, -122.33), "TB": (27.98, -82.50),
          "TEN": (36.17, -86.77), "WSH": (38.91, -76.86)}

def miles(a, b):
    (la1, lo1), (la2, lo2) = NFL_XY.get(a, (0, 0)), NFL_XY.get(b, (0, 0))
    p = math.pi / 180
    h = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 3959 * 2 * math.asin(math.sqrt(h))

def games_nfl(year):
    cf = os.path.join(CACHE, f"nfl_{year}.json")
    if os.path.exists(cf) and year < dt.date.today().year: return json.load(open(cf))
    out = []
    for wk in range(1, 19):
        sb = get(f"{SITE}/football/nfl/scoreboard?dates={year}&seasontype=2&week={wk}") or {}
        for e in sb.get("events", []):
            c = e["competitions"][0]
            if not c.get("status", {}).get("type", {}).get("completed"): continue
            t = {x["homeAway"]: x for x in c["competitors"]}
            od = (get(f"{CORE}/football/leagues/nfl/events/{e['id']}/competitions/{e['id']}/odds") or {}).get("items", [])
            sp = next((o.get("spread") for o in od if o.get("spread") is not None and "Live" not in (o.get("provider") or {}).get("name", "")), None)
            out.append({"id": e["id"], "date": e["date"], "week": wk, "home": t["home"]["team"]["abbreviation"], "away": t["away"]["team"]["abbreviation"],
                        "hs": float(t["home"].get("score") or 0), "as": float(t["away"].get("score") or 0), "spread": sp, "neutral": c.get("neutralSite", False)})
    json.dump(out, open(cf, "w"))
    return out

def nfl_tests(games):
    games = [g for g in games if g["spread"] is not None]; games.sort(key=lambda g: g["date"])
    last = {}; rows = {}
    def add(k, c):
        r = rows.setdefault(k, [0, 0]); r[0 if c else 1] += 1
    for g in games:
        when = dt.datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        res_home = (g["hs"] - g["as"]) + g["spread"]
        if res_home == 0: continue
        for side, team in (("home", g["home"]), ("away", g["away"])):
            c = res_home > 0 if side == "home" else res_home < 0
            line = g["spread"] if side == "home" else -g["spread"]
            add("All NFL teams (baseline)", c)
            p = last.get(team)
            short = p and (when - p).total_seconds() / 86400 <= 4.5
            far = side == "away" and miles(g["away"], g["home"]) >= 1500
            if side == "away" and line > 0: add("Road underdogs (baseline)", c)
            if short: add("Short week (Thursday after Sunday)", c)
            if short and side == "away" and line > 0: add("Short week road underdog", c)
            if short and far and line > 0: add("Short week + 1,500+ mile road trip underdog", c)
            if far and line > 0: add("1,500+ mile road trip underdog (any rest)", c)
        last[g["home"]] = when; last[g["away"]] = when
    return [{"spot": k, "w": w, "l": l, "cover": round(w / (w + l), 3), "z": round(zscore(w, w + l), 2), "verdict": verdict(w, w + l)} for k, (w, l) in rows.items()]

def main():
    nba = games_nba(dt.date(2024, 10, 22), dt.date(2025, 4, 13), "2425") + games_nba(dt.date(2025, 10, 21), dt.date(2026, 4, 12), "2526") \
        + games_nba(dt.date(2026, 10, 20), dt.date(2027, 4, 12), "2627")
    nfl = games_nfl(2024) + games_nfl(2025)
    res = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ"), "nba_games": len(nba), "nfl_games": len(nfl),
           "nba": nba_tests(nba), "nfl": nfl_tests(nfl)}
    json.dump(res, open(os.path.join(HERE, "results.json"), "w"), indent=1)
    L = [f'# Backtests · {res["generated"]}', "", f'NBA games with closing spreads: {res["nba_games"]} · NFL: {res["nfl_games"]}', "",
         "_Cover % against the closing spread. You need about 52.4% to profit at -110. z = how far from a coin flip (2.5+ = hard to be luck)._", ""]
    for lg in ("nba", "nfl"):
        L += [f"## {lg.upper()}", "", "| Spot | ATS | Cover % | z | Verdict |", "|---|---|---|---|---|"]
        for r in res[lg]:
            L.append(f'| {r["spot"]} | {r["w"]}-{r["l"]} | {r["cover"]*100:.1f}% | {r["z"]:+.2f} | {r["verdict"]} |' if r["cover"] is not None else f'| {r["spot"]} | — | — | — | — |')
        L.append("")
    open(os.path.join(HERE, "results.md"), "w").write("\n".join(L))
    print("ok", res["nba_games"], res["nfl_games"])

if __name__ == "__main__":
    main()
