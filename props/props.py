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
    "points": (["points"], 0.30, 3.0),
    "rebounds": (["totalRebounds"], 0.35, 1.5),
    "assists": (["assists"], 0.38, 1.3),
    "threePointersMade": (["3pm"], 0.55, 0.8),
    "points+rebounds+assists": (["points", "totalRebounds", "assists"], 0.24, 4.0),
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

def run(teams=None, note=None):
    """teams=None: full slate. teams={display names}: news-triggered re-price of just those games."""
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
            od = (c.get("odds") or [{}])[0]
            gsp = od.get("spread")
            if teams and not ({tm["home"]["displayName"], tm["away"]["displayName"]} & set(teams)): continue
            slate[e["id"]] = {"id": e["id"], "start": e["date"], "spread": abs(float(gsp)) if gsp not in (None, "") else 0, "home": tm["home"]["displayName"], "away": tm["away"]["displayName"],
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
    if len(players) > 100:
        json.dump({k: {"ppg": v["ppg"], "mpg": v["mpg"], "team": v["team"]} for k, v in players.items() if v.get("gp")},
                  open(os.path.join(HERE, "players.json"), "w"))
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
    if teams:
        sgo_events = []
        for g in slate.values():
            tid = norm(g["home"]).upper().replace(" ", "_") + "_NBA"
            d = (get(f"https://api.sportsgameodds.com/v2/events?leagueID=NBA&teamID={tid}&oddsAvailable=true&startsAfter={t0}&startsBefore={t1}&limit=1", sgo=True) or {}).get("data", [])
            sgo_events += d
    else:
        sg = get(f"https://api.sportsgameodds.com/v2/events?leagueID=NBA&oddsAvailable=true&startsAfter={t0}&startsBefore={t1}&limit=20", sgo=True) or {}
        sgo_events = sg.get("data", [])
    snap_f = os.path.join(HERE, "lines.json")
    snap = json.load(open(snap_f)) if os.path.exists(snap_f) else {}
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
            if line is None or po is None or pu is None or not dk.get("available", True): continue
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
            team_gp = max([p["gp"] for p in players.values() if p["team"] == info["team"]] or [1])
            outs = [n for n in out.get(team, []) if norm(n) != norm(nm) and players.get(norm(n), {}).get("gp", 0) >= 0.6 * team_gp]
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
            heavy_last = b2b and gl and gl[0]["min"] >= 38
            if b2b and mins >= 28 and not heavy_last: mins *= 0.98
            dfac = 1 + 0.5 * ((pa.get(opp, lg_pa) / lg_pa) - 1)
            proj = rate * mins * (dfac if stat != "rebounds" else 1)
            sd = a_sd * proj + b_sd
            p_over = 1 - phi((line - proj) / sd)
            io, iu = prob(po), prob(pu)
            nv_over = io / (io + iu)
            p_over = CFG["model_weight"] * p_over + (1 - CFG["model_weight"]) * nv_over   # trust the market most
            side = "over" if p_over > nv_over else "under"
            edge = (p_over - nv_over) if side == "over" else ((1 - p_over) - (1 - nv_over))
            price = po if side == "over" else (pu if pu is not None else None)
            l5avg = sum(sum(x[f] for f in fields) for x in l5) / len(l5)
            open_line = num(od.get("openBookOverUnder")); open_over = num(od.get("openBookOdds"))
            moved = None
            if open_line is not None and abs(line - open_line) >= 0.5: moved = "up" if line > open_line else "down"
            elif open_over is not None and abs(prob(po) - prob(open_over)) >= 0.03: moved = "up" if prob(po) > prob(open_over) else "down"
            prev = snap.get(oid)
            unchanged = bool(prev) and prev[0] == line and abs(prob(prev[1]) - prob(po)) < 0.02
            snap[oid] = [line, po, now.strftime("%Y-%m-%dT%H:%M:%SZ")]
            edges.append({"heavy_last": heavy_last, "unchanged": unchanged, "news": note if (teams and team in teams) else None, "game_spread": g["spread"], "starter": mins >= 28,
                          "game": f'{g["aab"]} @ {g["hab"]}', "espn_event": g["id"], "start": g["start"], "player": nm, "espn_id": info["id"],
                          "team": team, "stat": stat, "line": line, "side": side, "price": price, "proj": round(proj, 1),
                          "p_model": round(p_over if side == "over" else 1 - p_over, 3), "p_book": round(nv_over if side == "over" else 1 - nv_over, 3),
                          "edge": round(edge, 3), "l5": round(l5avg, 1), "season_avg": round(sum(sum(x[f] for f in fields) for x in gl) / len(gl), 1),
                          "mins": round(mins, 1), "teammates_out": boost_note, "b2b": b2b, "opp_def": round(dfac, 3),
                          "moved": moved, "open_line": open_line, "odd_id": oid.replace("-over", "-" + side)})
    edges.sort(key=lambda x: -x["edge"])
    json.dump(snap, open(snap_f, "w"))
    if not teams:
        json.dump({"generated": now.astimezone(PT).strftime("%Y-%m-%d %H:%M PT"), "calls": calls, "edges": edges},
                  open(os.path.join(HERE, "edges.json"), "w"), indent=1)
        write_md(edges, now)
    new = make_flags(edges, now)
    grade_flags(now)
    print("done", len(edges), "props", calls, "new flags", len(new))
    return new

def main():
    run()

def suspicious(e):
    """Line far from BOTH his season average and last-5 average -> the book likely knows something (minutes limit, injury return)."""
    base = max(e["line"], 1.0)
    return abs(e["season_avg"] - e["line"]) / base > CFG["suspicious_gap"] and abs(e["l5"] - e["line"]) / base > CFG["suspicious_gap"]

def against(e):
    return bool(e["moved"]) and ((e["side"] == "over" and e["moved"] == "down") or (e["side"] == "under" and e["moved"] == "up"))

def blowout_risk(e):
    """Big spread -> starters may sit the 4th quarter: no OVERS on starters."""
    return e.get("game_spread", 0) >= CFG["blowout_spread"] and e.get("starter") and e["side"] == "over"

def is_flag(e):
    """Need 2+ of OUR signals, and the model must at least agree (secondary). Hard guards still apply."""
    return len([x for x in primary(e) if not x.startswith("fits your history")]) >= 2 and e["edge"] >= CFG["model_confirm_edge"] and e["price"] is not None and not suspicious(e) \
        and not against(e) and not blowout_risk(e) and CFG["price_min"] <= e["price"] <= CFG["price_max"]

PROFILE = {"prop_record": "24-10"}

def primary(e):
    """OUR criteria first (Sammy's history, situation, news, market). The model is only the tie-breaker."""
    p = []
    side_over = e["side"] == "over"
    # 1) his history: player stat props are a strong market; price band matters
    if e["price"] is not None and -200 <= e["price"] <= -110:
        p.append(f'fits your history: stat props {PROFILE["prop_record"]}, favorite-priced')
    # 2) news / role change
    if e.get("news") and e.get("unchanged"):
        p.append(f'⚡ {e["news"]}, and DK hasn\'t moved this line yet')
    if e["teammates_out"] and side_over:
        p.append(f'role up: {e["teammates_out"]}')
    # 3) recent form clearly past the line
    if (side_over and e["l5"] >= e["line"] * 1.10) or (not side_over and e["l5"] <= e["line"] * 0.90):
        p.append(f'last 5 avg {e["l5"]} vs line {e["line"]:g}')
    if (side_over and e["season_avg"] >= e["line"] * 1.08) or (not side_over and e["season_avg"] <= e["line"] * 0.92):
        p.append(f'season avg {e["season_avg"]}')
    # 4) market agreeing (sharp money same side)
    if e["moved"] and ((side_over and e["moved"] == "up") or (not side_over and e["moved"] == "down")):
        p.append("market moving our way")
    # 5) situation
    if side_over and e.get("heavy_last") and e["stat"] in ("points", "points+rebounds+assists"):
        p.append("our stat: 38+ min last night → +2.0 pts next night (n=208)")
    if not side_over and e.get("game_spread", 0) >= CFG["blowout_spread"] and e.get("starter"):
        p.append(f'blowout risk: {e["game_spread"]:g}-pt spread, starters sit late (under)')
    return p

def signals(e):
    return primary(e) + ([f'model agrees: proj {e["proj"]} (+{e["edge"]*100:.0f}% edge)'] if e["edge"] >= CFG["model_confirm_edge"] else [])

STAT_NAME = {"points": "pts", "rebounds": "reb", "assists": "ast", "threePointersMade": "3PM", "points+rebounds+assists": "PRA"}

def write_md(edges, now):
    L = [f"# NBA prop edges · {now.astimezone(PT):%a %b %-d %-I:%M %p} PT", "",
         "_Model = projected minutes x per-minute rate (season + last 5), adjusted for teammates out, opponent defense, back-to-backs. "
         "Edge = model win % minus DraftKings' no-vig %. Not picks by themselves — 🤖 = Alert Pick (2+ signals)._", "",
         "| | Game | Player | Prop | Line | Side | Price | Proj | L5 | Edge | Notes |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in [x for x in edges if x["edge"] >= CFG["show_edge"]][:40]:
        sig = signals(e); flag = "🤖" if is_flag(e) else ""
        notes = "; ".join(filter(None, ["⚠ blowout risk (starter over, big spread)" if blowout_risk(e) else "", "⚠ line far off his averages — check news (minutes limit?)" if suspicious(e) else "", e["teammates_out"], "B2B" if e["b2b"] else "", f'moved {e["moved"]}' if e["moved"] else ""]))
        if suspicious(e): flag = "⚠"
        elif against(e): notes = "market moving AGAINST this side; " + notes
        L.append(f'| {flag} | {e["game"]} | {e["player"]} | {STAT_NAME[e["stat"]]} | {e["line"]:g} | {e["side"]} | {fmt(e["price"]) if e["price"] is not None else "—"} | {e["proj"]} | {e["l5"]} | +{e["edge"]*100:.1f}% | {notes} |')
    open(os.path.join(HERE, "edges.md"), "w").write("\n".join(L) + "\n")

def make_flags(edges, now):
    f = os.path.join(HERE, "autoflags.json")
    flags = json.load(open(f)) if os.path.exists(f) else []
    have = {x["key"] for x in flags}
    new = []
    day_count = {}
    for x in flags: day_count[x["start"][:10]] = day_count.get(x["start"][:10], 0) + 1
    for e in edges:
        sig = signals(e)
        if not is_flag(e): continue
        key = f'{e["start"][:10]}|{e["player"]}|{e["stat"]}|{e["side"]}'
        if key in have: continue
        day_count[e["start"][:10]] = day_count.get(e["start"][:10], 0) + 1
        strong = [x for x in primary(e) if not x.startswith("fits your history")]
        units = 1.0 if ((e.get("news") and e.get("unchanged")) or (len(strong) >= 3 and e["edge"] >= 0.04)) else 0.5
        flags.append({"key": key, "created": now.astimezone(PT).strftime("%Y-%m-%d %H:%M PT"), "game": e["game"], "start": e["start"],
                      "espn_event": e["espn_event"], "espn_id": e["espn_id"], "player": e["player"], "stat": e["stat"],
                      "side": e["side"], "line": e["line"], "price": e["price"], "proj": e["proj"], "edge": e["edge"],
                      "signals": sig, "units": units, "sent": False, "result": None, "trigger": "news" if e.get("news") else "model"})
        have.add(key); new.append(flags[-1])
    json.dump(flags, open(f, "w"), indent=1)
    return new

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
        if x["stat"] == "live_ml":
            comp = s.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
            sc = {c.get("homeAway"): float(c.get("score", 0) or 0) for c in comp}
            if len(sc) < 2: continue
            mine, other = (sc["home"], sc["away"]) if x["side"] == "home" else (sc["away"], sc["home"])
            x["result"] = "Win" if mine > other else "Loss"; x["final"] = f"{int(mine)}-{int(other)}"
            x["net_units"] = round(x["units"], 2) if x["result"] == "Win" else -x["units"]   # assumes ~even live price
            continue
        if x["stat"] == "spread":
            comp = s.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
            sc = {c.get("homeAway"): float(c.get("score", 0) or 0) for c in comp}
            if len(sc) < 2: continue
            mine, other = (sc["home"], sc["away"]) if x["side"] == "home" else (sc["away"], sc["home"])
            d = mine - other + x["line"]
            x["result"] = "Win" if d > 0 else "Loss" if d < 0 else "Push"; x["final"] = f"{int(mine)}-{int(other)}"
            x["net_units"] = round(x["units"] * 100 / 110, 2) if x["result"] == "Win" else (-x["units"] if x["result"] == "Loss" else 0)
            continue
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
