#!/usr/bin/env python3
"""Nightly efficiency data (free sources). Only used for EXTREME-case flags (Sammy's rule).
NFL: nflverse team-week stats -> offense/defense EPA per play, last-4-weeks form.
Writes efficiency/nfl.json + efficiency.md (top/bottom teams, biggest form swings)."""
import csv, io, json, os, urllib.request, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
SEASON = 2026

def get_csv(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 eff"}), timeout=60) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8", "replace"))))

def f(x):
    try: return float(x)
    except Exception: return 0.0

def nfl():
    rows = [r for r in get_csv(f"https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{SEASON}.csv")
            if r.get("season_type", "REG") == "REG"]
    by = {}
    for r in rows:
        t, o, w = r["team"], r["opponent_team"], int(r["week"])
        plays = f(r.get("attempts")) + f(r.get("carries")) + f(r.get("sacks_suffered"))
        epa = f(r.get("passing_epa")) + f(r.get("rushing_epa"))
        by.setdefault(t, {"off": [], "def": []})["off"].append((w, epa, plays))
        by.setdefault(o, {"off": [], "def": []})["def"].append((w, epa, plays))
    out = []
    for t, d in by.items():
        def per(lst, last=None):
            l = sorted(lst)[-last:] if last else lst
            p = sum(x[2] for x in l); return round(sum(x[1] for x in l) / p, 3) if p else None
        out.append({"team": t, "off_epa": per(d["off"]), "def_epa": per(d["def"]), "off_epa_l4": per(d["off"], 4), "def_epa_l4": per(d["def"], 4),
                    "games": len(d["off"])})
    for x in out:
        x["net_epa"] = round((x["off_epa"] or 0) - (x["def_epa"] or 0), 3)
        x["form_swing"] = round(((x["off_epa_l4"] or 0) - (x["def_epa_l4"] or 0)) - x["net_epa"], 3)
    out.sort(key=lambda x: -x["net_epa"])
    return out

def main():
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%MZ")
    res = {"generated": now}
    try:
        res["nfl"] = nfl()
    except Exception as e:
        res["nfl_error"] = str(e)
    json.dump(res, open(os.path.join(HERE, "nfl.json"), "w"), indent=1)
    L = [f"# Efficiency data · {now}", "", "_Used only for extreme cases (Sammy's rule). Defense EPA: lower is better._", ""]
    if res.get("nfl"):
        n = res["nfl"]
        L += ["## NFL net EPA/play (offense minus defense)", "", "| Team | Net | Off | Def | Last 4 net | Form swing |", "|---|---|---|---|---|---|"]
        for x in n:
            L.append(f'| {x["team"]} | {x["net_epa"]:+.3f} | {x["off_epa"]:+.3f} | {x["def_epa"]:+.3f} | {(x["off_epa_l4"] or 0)-(x["def_epa_l4"] or 0):+.3f} | {x["form_swing"]:+.3f} |')
        hot = sorted(n, key=lambda x: -x["form_swing"])[:3]; cold = sorted(n, key=lambda x: x["form_swing"])[:3]
        L += ["", "**Trending up (last 4 weeks way above season):** " + ", ".join(f'{x["team"]} ({x["form_swing"]:+.2f})' for x in hot),
              "", "**Trending down:** " + ", ".join(f'{x["team"]} ({x["form_swing"]:+.2f})' for x in cold)]
    open(os.path.join(HERE, "efficiency.md"), "w").write("\n".join(L) + "\n")
    print("ok", list(res))

if __name__ == "__main__":
    main()
