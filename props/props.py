#!/usr/bin/env python3
"""Sammy's NBA prop model + Alert Picks.
Runs on GitHub Actions (free). For every NBA player with a DraftKings prop today:
  projection = projected minutes x per-minute rate (season blended with last 5 games)
               x opponent defense factor, with minutes/usage boosts when teammates are OUT and a back-to-back trim.
Compares projection with DK lines (SportsGameOdds), writes props/edges.json + edges.md,
creates 🤖 Alert Picks (2+ signals) in props/autoflags.json and grades old ones from ESPN box scores."""
import json, os, time, math, unicodedata, urllib.request, datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
PT = ZoneInfo("America/Los_Angeles")
SGO_KEY = os.environ.get("SGO_API_KEY", "")
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
CFG = json.load(open(os.path.join(HERE, "props_config.json")))
calls = {"espn": 0, "sgo": 0}

def get(url, sgo=False):
    hdr = {"User-Agent": "Mozilla/5.0 props-model"}
    if sgo: hdr["x-api-key"] = SGO_KEY
    for i in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
                calls["sgo" if sgo else "espn"] += 1
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(1.5)
    print("fetch failed", url[:120], err); return None

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    for x in (" jr.", " jr", " sr.", " iii", " ii", " iv", ".", "'"): s = s.replace(x, "")
    return " ".join(s.split())

def prob(a): return 100 / (a + 100) if a > 0 else -a / (-a + 100)
def fmt(a): return ("+" if a > 0 else "") + f"{a:g}"
def phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))
def num(x):
    try: return float(str(x).replace("+", ""))
    except Exception: return None

STATS = {  # SGO statID -> (gamelog fields, sd model a*proj+b)
    "points": (["points"], 0.26, 2.2),
    "rebounds": (["totalRebounds"], 0.30, 1.1),
    "assists": (["assists"], 0.32, 1.0),
    "threePointersMade": (["3pm"], 0.45, 0.6),
    "points+rebounds+assists": (["points", "totalRebounds", "assists"], 0.20, 3.2),
}

def gamelog(aid):
    g = get(f"{WEB}/athletes/{aid}/gamelog")
    if not g: return []
    names = g.get("names", [])
    meta = g.get("events", {})
    games = []
    for st in g.get("seasonTypes", []):
        if "Regular" not in st.get("displayName", "") and "Cup" not in st.get("displayName", ""): continue
        for cat in st.get("categories", []):
            for e in cat.get("events", []):
                row = dict(zip(names, e["stats"]))
                try:
                    m = float(row.get("minutes", 0))
                    three = row.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0").split("-")[0]
                    games.append({"date": meta.get(e["eventId"], {}).get("gameDate", ""), "min": m,
                                  "points": float(row.get("points", 0)), "totalRebounds": float(row.get("totalRebounds", 0)),
                                  "assists": float(row.get("assists", 0)), "3pm": float(three)})
                except Exception:
                    pass
    games = [x for x in games if x["min"] > 0]
    games.sort(key=lambda x: x["date"], reverse=True)
    return games

def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(PT).date()
    # ---- slate (ESPN), yesterday for back-to-backs
    slate = {}
    for d in (today, today + dt.timedelta(days=1)):
        sb = get(f"{ESPN}/scoreboard?dates={d:%Y%m%d}") or {}
        for e in sb.get("events", []):
            if e["status"]["type"]["state"] != "pre": continue
            start = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if (start - now).total_seconds() > CFG["lookahead_hours"] * 3600: continue
            c = e["competitions"][0]
            tm = {x["homeAway"]: x["team"] for x in c["competitors"]}
            slate[e["id"]] = {"id": e["id"], "start": e["date"], "home": tm["home"]["displayName"], "away": tm["away"]["displayName"],
                              "hid": tm["home"]["id"], "aid": tm["away"]["id"], "hab": tm["home"]["abbreviation"], "aab": tm["away"]["abbreviation"]}
    played_yday = set()
    yb = get(f"{ESPN}/scoreboard?dates={today - dt.timedelta(days=1):%Y%m%d}") or {}
    for e in yb.get("events", []):
        for x in e["competitions"][0]["competitors"]: played_yday.add(x["team"]["displayName"])
    print("games", len(slate))

    # ---- season averages for every player (one call) + injuries
    by = get(f"{WEB}/statistics/byathlete?region=us&lang=en&isqualified=false&page=1&limit=700&season={CFG['season']}&seasontype=2") or {}
    players = {}  # norm name -> info
    for a in by.get("athletes", []):
        ath = a["athlete"]; cats = {c["name"]: c["values"] for c in a["categories"]}
        g = cats.get("general", []); o = cats.get("offensive", [])
        players[norm(ath["displayName"])] = {"id": ath["id"], "name": ath["displayName"], "team": ath.get("teamShortName"),
                                             "gp": g[0] if g else 0, "mpg": g[1] if len(g) > 1 else 0, "ppg": o[0] if o else 0}
    inj = get(f"{ESPN}/injuries") or {}
    out = {}  # team display name -> list of (name, status)
    for t in inj.get("injuries", []):
        for it in t.get("injuries", []):
            if it.get("status") in ("Out", "Doubtful"):
                out.setdefault(t["displayName"], []).append(it["athlete"]["displayName"])
    # opponent defense: points allowed vs league average
    pa = {}
    for g in slate.values():
        for tid, nm in ((g["hid"], g["home"]), (g["aid"], g["away"])):
            if nm in pa: continue
            t = (get(f"{ESPN}/teams/{tid}") or {}).get("team", {})
            for it in t.get("record", {}).get("items", []):
                if it.get("type") == "total":
                    for s in it.get("stats", []):
                        if s["name"] == "avgPointsAgainst": pa[nm] = s["value"]
    lg_pa = CFG["league_avg_pa"]

    # ---- DK props (SportsGameOdds): one call for the whole window
    t0 = now.strftime("%Y-%m-%dT%H:%M:%SZ"); t1 = (now + dt.timedelta(hours=CFG["lookahead_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    sg = get(f"https://api.sportsgameodds.com/v2/events?leagueID=NBA&oddsAvailable=true&startsAfter={t0}&startsBefore={t1}&limit=20", sgo=True) or {}
    sgo_events = sg.get("data", [])
    print("sgo events", len(sgo_events))

    edges, logs = [], {}
    for se in sgo_events:
        hn, an = se["teams"]["home"]["names"]["long"], se["teams"]["away"]["names"]["long"]
        g = next((x for x in slate.values() if norm(x["home"]).endswith(norm(hn).split()[-1]) and norm(x["away"]).endswith(norm(an).split()[-1])), None)
        if not g: continue
        team_of = {pid: p.get("teamID") for pid, p in (se.get("players") or {}).items()}
        pname = {pid: p.get("name") for pid, p in (se.get("players") or {}).items()}
        for oid, od in (se.get("odds") or {}).items():
            if od.get("periodID") != "game" or od.get("betTypeID") != "ou" or od.get("sideID") != "over": continue
            stat = od.get("statID"); ent = od.get("statEntityID")
            if stat not in STATS or ent in ("all", "home", "away"): continue
            dk = (od.get("byBookmaker") or {}).get("draftkings") or {}
            uo = ((se["odds"].get(od.get("opposingOddID", "")) or {}).get("byBookmaker") or {}).get("draftkings") or {}
            line = num(dk.get("overUnder")); po = num(dk.get("odds")); pu = num(uo.get("odds"))
            if line is None or po is None or not dk.get("available", True): continue
            nm = pname.get(ent, ""); info = players.get(norm(nm))
            if not info or info["gp"] < CFG["min_gp"]: continue
            if info["id"] not in logs: logs[info["id"]] = gamelog(info["id"])
            gl = logs[info["id"]]
            if len(gl) < CFG["min_gp"]: continue
            tid = team_of.get(ent, "")
            team = g["home"] if tid.startswith(norm(g["home"]).upper().replace(" ", "_")) else g["away"]
            opp = g["away"] if team == g["home"] else g["home"]
            fields, a_sd, b_sd = STATS[stat]
            # per-minute rates: season and last 5
            tot_min = sum(x["min"] for x in gl); l5 = gl[:5]; l5_min = sum(x["min"] for x in l5)
            rate_s = sum(sum(x[f] for f in fields) for x in gl) / tot_min
            rate_5 = sum(sum(x[f] for f in fields) for x in l5) / l5_min if l5_min else rate_s
            rate = 0.6 * rate_s + 0.4 * rate_5
            mins = 0.6 * (tot_min / len(gl)) + 0.4 * (l5_min / len(l5))
            # teammates out: share their minutes/usage
            outs = [n for n in out.get(team, []) if norm(n) != norm(nm)]
            out_min = sum(players.get(norm(n), {}).get("mpg", 0) for n in outs if players.get(norm(n), {}).get("mpg", 0) >= 15)
            out_pts = sum(players.get(norm(n), {}).get("ppg", 0) for n in outs if players.get(norm(n), {}).get("mpg", 0) >= 15)
            boost_note = ""
            if out_min > 0 and mins >= 15:
                add = min(CFG["max_min_boost"], out_min * 0.5 * mins / 160)
                mins += add
                usage = min(CFG["max_usage_boost"], 1 + 0.35 * out_pts / 110)
                if stat != "rebounds": rate *= usage
                boost_note = f"{', '.join(o for o in outs if players.get(norm(o), {}).get('mpg', 0) >= 15)} out"
            mins = min(mins, 40)
            b2b = team in played_yday
            if b2b and mins >= 28: mins *= 0.96
            dfac = 1 + 0.5 * ((pa.get(opp, lg_pa) / lg_pa) - 1)
            proj = rate * mins * (dfac if stat != "rebounds" else 1)
            sd = a_sd * proj + b_sd
            p_over = 1 - phi((line - proj) / sd)
            io, iu = prob(po), (prob(pu) if pu is not None else 1 - prob(po) + 0.045)
            nv_over = io / (io + iu)
            side = "over" if p_over > nv_over else "under"
            edge = (p_over - nv_over) if side == "over" else ((1 - p_over) - (1 - nv_over))
            price = po if side == "over" else (pu if pu is not None else None)
            l5avg = sum(sum(x[f] for f in fields) for x in l5) / len(l5)
            open_line = num(od.get("openBookOverUnder")); open_over = num(od.get("openBookOdds"))
            moved = None
            if open_line is not None and line != open_line: moved = "up" if line > open_line else "down"
            elif open_over is not None and po != open_over: moved = "up" if prob(po) > prob(open_over) else "down"
            edges.append({"game": f'{g["aab"]} @ {g["hab"]}', "espn_event": g["id"], "start": g["start"], "player": nm, "espn_id": info["id"],
                          "team": team, "stat": stat, "line": line, "side": side, "price": price, "proj": round(proj, 1),
                          "p_model": round(p_over if side == "over" else 1 - p_over, 3), "p_book": round(nv_over if side == "over" else 1 - nv_over, 3),
                          "edge": round(edge, 3), "l5": round(l5avg, 1), "season_avg": round(sum(sum(x[f] for f in fields) for x in gl) / len(gl), 1),
                          "mins": round(mins, 1), "teammates_out": boost_note, "b2b": b2b, "opp_def": round(dfac, 3),
                          "moved": moved, "open_line": open_line, "odd_id": oid.replace("-over", "-" + side)})
    edges.sort(key=lambda x: -x["edge"])
    json.dump({"generated": now.astimezone(PT).strftime("%Y-%m-%d %H:%M PT"), "calls": calls, "edges": edges},
              open(os.path.join(HERE, "edges.json"), "w"), indent=1)
    write_md(edges, now)
    make_flags(edges, now)
    grade_flags(now)
    print("done", len(edges), "props", calls)

def signals(e):
    s = []
    if e["edge"] >= CFG["flag_edge"]: s.append(f'model edge +{e["edge"]*100:.0f}% (proj {e["proj"]} vs {e["line"]})')
    if (e["side"] == "over" and e["l5"] > e["line"]) or (e["side"] == "under" and e["l5"] < e["line"]):
        s.append(f'last 5 avg {e["l5"]}')
    if e["teammates_out"] and e["side"] == "over": s.append(e["teammates_out"])
    if e["moved"] and ((e["side"] == "over" and e["moved"] == "up") or (e["side"] == "under" and e["moved"] == "down")):
        s.append("market moving our way")
    return s

STAT_NAME = {"points": "pts", "rebounds": "reb", "assists": "ast", "threePointersMade": "3PM", "points+rebounds+assists": "PRA"}

def write_md(edges, now):
    L = [f"# NBA prop edges · {now.astimezone(PT):%a %b %-d %-I:%M %p} PT", "",
         "_Model = projected minutes x per-minute rate (season + last 5), adjusted for teammates out, opponent defense, back-to-backs. "
         "Edge = model win % minus DraftKings' no-vig %. Not picks by themselves — 🤖 = Alert Pick (2+ signals)._", "",
         "| | Game | Player | Prop | Line | Side | Price | Proj | L5 | Edge | Notes |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in [x for x in edges if x["edge"] >= CFG["show_edge"]][:40]:
        sig = signals(e); flag = "🤖" if len(sig) >= 2 and e["edge"] >= CFG["flag_edge"] else ""
        notes = "; ".join(filter(None, [e["teammates_out"], "B2B" if e["b2b"] else "", f'moved {e["moved"]}' if e["moved"] else ""]))
        L.append(f'| {flag} | {e["game"]} | {e["player"]} | {STAT_NAME[e["stat"]]} | {e["line"]:g} | {e["side"]} | {fmt(e["price"]) if e["price"] is not None else "—"} | {e["proj"]} | {e["l5"]} | +{e["edge"]*100:.1f}% | {notes} |')
    open(os.path.join(HERE, "edges.md"), "w").write("\n".join(L) + "\n")

def make_flags(edges, now):
    f = os.path.join(HERE, "autoflags.json")
    flags = json.load(open(f)) if os.path.exists(f) else []
    have = {x["key"] for x in flags}
    for e in edges:
        sig = signals(e)
        if e["edge"] < CFG["flag_edge"] or len(sig) < 2 or e["price"] is None: continue
        if not (CFG["price_min"] <= e["price"] <= CFG["price_max"]): continue
        key = f'{e["start"][:10]}|{e["player"]}|{e["stat"]}|{e["side"]}'
        if key in have: continue
        units = 1.0 if (e["edge"] >= 0.10 and len(sig) >= 3) else 0.5
        flags.append({"key": key, "created": now.astimezone(PT).strftime("%Y-%m-%d %H:%M PT"), "game": e["game"], "start": e["start"],
                      "espn_event": e["espn_event"], "espn_id": e["espn_id"], "player": e["player"], "stat": e["stat"],
                      "side": e["side"], "line": e["line"], "price": e["price"], "proj": e["proj"], "edge": e["edge"],
                      "signals": sig, "units": units, "sent": False, "result": None})
        have.add(key)
    json.dump(flags, open(f, "w"), indent=1)

def grade_flags(now):
    f = os.path.join(HERE, "autoflags.json")
    flags = json.load(open(f)) if os.path.exists(f) else []
    boxes = {}
    for x in flags:
        if x["result"] or dt.datetime.fromisoformat(x["start"].replace("Z", "+00:00")) > now - dt.timedelta(hours=3): continue
        ev = x["espn_event"]
        if ev not in boxes: boxes[ev] = get(f"{ESPN}/summary?event={ev}")
        s = boxes[ev] or {}
        if (s.get("header", {}).get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state")) != "post": continue
        val = None; played = False
        for t in s.get("boxscore", {}).get("players", []):
            st = t["statistics"][0]; keys = st.get("keys", [])
            for a in st.get("athletes", []):
                if a["athlete"]["id"] != x["espn_id"]: continue
                row = dict(zip(keys, a.get("stats", [])))
                if not row or row.get("minutes") in (None, "", "0"): break
                played = True
                v = {"points": float(row.get("points", 0)), "rebounds": float(row.get("rebounds", 0)), "assists": float(row.get("assists", 0)),
                     "threePointersMade": float(str(row.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0")).split("-")[0])}
                v["points+rebounds+assists"] = v["points"] + v["rebounds"] + v["assists"]
                val = v[x["stat"]]
        if not played:
            x["result"] = "Void (DNP)"; x["final"] = None; x["net_units"] = 0; continue
        d = val - x["line"] if x["side"] == "over" else x["line"] - val
        x["result"] = "Win" if d > 0 else "Loss" if d < 0 else "Push"
        x["final"] = val
        p = x["price"]
        x["net_units"] = round(x["units"] * (p / 100 if p > 0 else 100 / -p), 2) if x["result"] == "Win" else (-x["units"] if x["result"] == "Loss" else 0)
        x["graded_sent"] = False
    json.dump(flags, open(f, "w"), indent=1)

if __name__ == "__main__":
    main()
