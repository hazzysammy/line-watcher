#!/usr/bin/env python3
"""Sammy's line & injury watcher.
Runs every 15 min on GitHub Actions. Pulls ESPN (DraftKings lines) + injury reports,
compares with the last run (state.json) and pushes alerts to your phone via ntfy."""
import json, os, sys, time, urllib.request, datetime as dt
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(HERE, "state.json")
CFG = json.load(open(os.path.join(HERE, "config.json")))
WATCH_F = os.path.join(HERE, "watchlist.json")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
API = "https://site.api.espn.com/apis/site/v2/sports/"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 line-watcher"})
    for i in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            err = e; time.sleep(2)
    print("fetch failed", url, err); return None

def ml_cents(a, b):
    """distance between two American odds in 'cents' (handles crossing even)."""
    def lin(x): return x - 100 if x >= 100 else x + 100   # +130 -> 30, -130 -> -30
    return abs(lin(a) - lin(b))

def prob(x):
    """American odds -> implied win probability (0-1)."""
    return 100 / (x + 100) if x > 0 else -x / (-x + 100)

def num(x):
    try: return float(str(x).replace("+", ""))
    except Exception: return None

def fmt(x):
    if x is None: return "—"
    x = float(x); s = ("%g" % x)
    return ("+" + s) if x > 0 else s

def is_fav(name):
    n = name.lower()
    return any(f.lower() in n for f in CFG["favorites"])

def norm(x):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", x.lower()) if not unicodedata.combining(c))

def lineup_wanted(ev):
    if is_fav(ev["home"]) or is_fav(ev["away"]): return True
    lt = [norm(t) for t in CFG.get("lineup_teams", [])]
    return any(t in norm(ev["home"]) or t in norm(ev["away"]) for t in lt)

NEWS_WORDS = ["out", "ruled", "questionable", "doubtful", "probable", "game-time", "gtd", "inactive",
              "scratch", "won't play", "will play", "will not play", "to play", "return", "available",
              "unavailable", "injur", "illness", "sore", "strain", "sprain", "rest", "dnp", "start",
              "lineup", "benched", "suspend", "miss", "sidelined", "listed", "upgrade", "downgrade",
              "concussion", "hamstring", "ankle", "knee", "back", "groin", "calf", "out for"]

def news_hit(text):
    t = " " + text.lower() + " "
    return any((" " + w) in t for w in NEWS_WORDS)

def parse_event(e, lg):
    c = e["competitions"][0]
    teams = {x["homeAway"]: x for x in c["competitors"]}
    h, a = teams.get("home"), teams.get("away")
    if not h or not a: return None
    rank = lambda t: (t.get("curatedRank") or {}).get("current", 99)
    ev = {"id": e["id"], "lg": lg["key"], "sport": lg["sport"], "date": e["date"],
          "state": e["status"]["type"]["state"],
          "home": h["team"]["displayName"], "away": a["team"]["displayName"],
          "hab": h["team"].get("abbreviation", ""), "aab": a["team"].get("abbreviation", ""),
          "ranked": min(rank(h), rank(a)) <= 25,
          "score": f'{a["team"].get("abbreviation","")} {a.get("score","")}-{h.get("score","")} {h["team"].get("abbreviation","")}',
          "odds": None}
    od = (c.get("odds") or [None])[0]
    if od:
        ml = od.get("moneyline") or {}
        ps = od.get("pointSpread") or {}
        cur = lambda d, k: (d.get(k) or {}).get("close") or (d.get(k) or {}).get("open") or {}
        o = {"spread_home": num(cur(ps, "home").get("line")) if ps else None,
             "total": num(od.get("overUnder")),
             "ml_home": num(cur(ml, "home").get("odds")) if ml else num((od.get("homeTeamOdds") or {}).get("moneyLine")),
             "ml_away": num(cur(ml, "away").get("odds")) if ml else num((od.get("awayTeamOdds") or {}).get("moneyLine")),
             "ml_draw": num(cur(ml, "draw").get("odds")) if ml.get("draw") else num((od.get("drawOdds") or {}).get("moneyLine"))}
        if o["spread_home"] is None and od.get("spread") is not None:
            o["spread_home"] = num(od.get("spread"))
        if any(v is not None for v in o.values()):
            ev["odds"] = o
    return ev

def relevant(ev, lg):
    mode = lg.get("watch", "all")
    if is_fav(ev["home"]) or is_fav(ev["away"]): return True
    if mode == "all": return True
    if mode == "ranked": return ev["ranked"]
    return False   # "favorites"

def label(ev):
    t = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(PT)
    star = "🚨⭐ " if (is_fav(ev["home"]) or is_fav(ev["away"])) else ""
    return f'{star}{ev["lg"].upper()} {ev["away"]} @ {ev["home"]} ({t.strftime("%a %-I:%M %p")} PT)'

def team_line(o, ev, side):
    s = o.get("spread_home")
    if side == "home": return s, o.get("ml_home")
    return (-s if s is not None else None), o.get("ml_away")

def main():
    state = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {}
    first_run = not state.get("seeded")
    base = state.setdefault("base", {})        # event id -> baseline odds (last alerted)
    seen_odds = state.setdefault("had_odds", {})
    inj_prev = state.setdefault("inj", {})
    fired = set(state.setdefault("fired", []))
    queue = state.setdefault("queue", [])
    watch = json.load(open(WATCH_F)) if os.path.exists(WATCH_F) else []
    alerts = []   # (priority, title, body)
    now = dt.datetime.now(PT)
    days = [(now + dt.timedelta(days=d)).strftime("%Y%m%d") for d in range(CFG.get("days_ahead", 2))]
    playing_soon = {}   # sport -> set of team names playing within 36h
    soon_lineups = []

    for lg in CFG["leagues"]:
        evs = {}
        for d in days:
            url = API + lg["path"] + "/scoreboard?dates=" + d + ("&" + lg["query"] if lg.get("query") else "")
            j = get(url)
            for e in (j or {}).get("events", []):
                ev = parse_event(e, lg)
                if ev: evs[ev["id"]] = ev
        th = CFG["thresholds"][lg["sport"]]
        for ev in evs.values():
            start = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            hrs = (start - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
            if hrs < 36:
                playing_soon.setdefault(lg["sport"], set()).update([ev["home"], ev["away"]])
            if lg["sport"] == "soccer" and 0 < hrs <= CFG.get("lineup_lookahead_hrs", 1.25) and ev["state"] == "pre" and lineup_wanted(ev):
                soon_lineups.append((lg, ev))
            if ev["state"] != "pre" or not relevant(ev, lg):
                continue
            o, key = ev["odds"], ev["id"]
            # line pulled off the board (often = injury news)
            if not o:
                if seen_odds.get(key) and key + ":off" not in fired:
                    alerts.append((4, "⛔ Line pulled", f'{label(ev)}\nDK line came OFF the board — check injury/lineup news.'))
                    fired.add(key + ":off")
                continue
            if key + ":off" in fired:
                alerts.append((4, "✅ Line back up", f'{label(ev)}\nReposted: {ev["hab"]} {fmt(o["spread_home"])}, O/U {o["total"] or "—"}, ML {ev["aab"]} {fmt(o["ml_away"])} / {ev["hab"]} {fmt(o["ml_home"])}'))
                fired.discard(key + ":off")
            seen_odds[key] = True
            b = base.get(key)
            if not b:
                base[key] = dict(o, open=dict(o), date=ev["date"]); continue
            msgs = []
            if o["spread_home"] is not None and b.get("spread_home") is not None and th.get("spread") and abs(o["spread_home"] - b["spread_home"]) >= th["spread"]:
                msgs.append(f'Spread {ev["hab"]} {fmt(b["spread_home"])} → {fmt(o["spread_home"])}')
                b["spread_home"] = o["spread_home"]
            if o["total"] is not None and b.get("total") is not None and th.get("total") and abs(o["total"] - b["total"]) >= th["total"]:
                msgs.append(f'Total {b['total']:g} → {o['total']:g}')
                b["total"] = o["total"]
            cut = CFG.get("parlay_cutoff", -650)
            for side, ab in (("ml_home", ev["hab"]), ("ml_away", ev["aab"])):
                if o[side] is None or b.get(side) is None or not th.get("ml_pct"): continue
                # ignore huge-favorite / huge-dog noise: skip when the favorite is shorter than the cutoff before AND after
                fav_now = min(v for v in (o["ml_home"], o["ml_away"]) if v is not None)
                fav_then = min(v for v in (b.get("ml_home"), b.get("ml_away")) if v is not None)
                if fav_now < cut and fav_then < cut:
                    b[side] = o[side]; continue
                if abs(prob(o[side]) - prob(b[side])) * 100 >= th["ml_pct"]:
                    msgs.append(f'ML {ab} {fmt(b[side])} → {fmt(o[side])}')
                    b[side] = o[side]
            if msgs:
                op = b.get("open", {})
                alerts.append((3, "📈 Line move", f'{label(ev)}\n' + "\n".join(msgs) +
                               f'\n(first seen: {ev["hab"]} {fmt(op.get("spread_home"))}, O/U {op.get('total') or '—'})'))
            # watchlist rules
            for w in watch:
                if w.get("type") == "prop" or not w.get("active", True) or w.get("id") in fired: continue
                tm = w["team"].lower()
                side = "home" if tm in ev["home"].lower() or tm == ev["hab"].lower() else \
                       "away" if tm in ev["away"].lower() or tm == ev["aab"].lower() else None
                if not side: continue
                sp, ml = team_line(o, ev, side)
                val = {"spread": sp, "ml": ml, "total": o.get("total")}.get(w["field"])
                if val is None: continue
                hit = val <= w["value"] if w["op"] == "<=" else val >= w["value"]
                if hit:
                    alerts.append((4, "🎯 Watchlist hit", f'{w.get("label", w["id"])}\n{label(ev)}\n{w["team"]} {w["field"]} now {fmt(val)} (trigger {w["op"]} {fmt(w["value"])})'))
                    fired.add(w["id"])
            # hard flag: parlay cutoff
            for side, nm in (("ml_home", ev["home"]), ("ml_away", ev["away"])):
                v = o.get(side)
                if v is not None and v < CFG.get("parlay_cutoff", -650) and is_fav(nm) and key + side + ":cut" not in fired:
                    alerts.append((2, "⚠ Too short for parlay", f'{label(ev)}\n{nm} ML {fmt(v)} is shorter than {CFG["parlay_cutoff"]}.'))
                    fired.add(key + side + ":cut")

    # ---------- breaking news (ESPN news feeds) ----------
    news_first = "news_seen" not in state
    news_seen = set(state.setdefault("news_seen", []))
    utcnow = dt.datetime.now(dt.timezone.utc)
    for sport, path in CFG.get("news_feeds", {}).items():
        j = get(API + path + "/news?limit=50")
        soon = playing_soon.get(sport.split("_")[0], set())
        nick = {t.split()[-1].lower(): t for t in soon}
        for a in (j or {}).get("articles", []):
            aid = str(a.get("id") or a.get("headline"))
            if aid in news_seen: continue
            news_seen.add(aid)
            if news_first: continue
            try:
                pub = dt.datetime.fromisoformat(a.get("published", "").replace("Z", "+00:00"))
                if (utcnow - pub).total_seconds() > 3 * 3600: continue
            except Exception:
                pass
            text = (a.get("headline", "") + " " + a.get("description", ""))
            if not news_hit(text): continue
            cats = " ".join((c.get("description") or "") for c in a.get("categories", []) if c.get("type") in ("team", "athlete"))
            blob = (text + " " + cats).lower()
            teams = [t for t in soon if t.lower() in blob] + [nick[n] for n in nick if (" " + n) in (" " + blob) and nick[n] not in blob]
            fav = is_fav(blob)
            if not teams and not fav: continue
            star = "🚨⭐ " if fav else ""
            alerts.append((4, f"📰 {sport.split('_')[0].upper()} news", f'{star}{a.get("headline","")}\n{(a.get("description") or "")[:220]}'
                           + (f'\nPlays within 36h: {", ".join(sorted(set(teams)))}' if teams else "")
                           + "\n→ Check the line NOW before it moves."))
    state["news_seen"] = sorted(news_seen)[-1500:]

    # ---------- soccer lineups (~1h before kickoff) ----------
    watch_players = [norm(p) for p in CFG.get("lineup_players", [])]
    for lg, ev in soon_lineups:
        if ev["id"] + ":lu" in fired: continue
        j = get(API + lg["path"] + "/summary?event=" + ev["id"])
        rosters = (j or {}).get("rosters", [])
        xi_lines, warn = [], []
        for r in rosters:
            team = (r.get("team") or {}).get("displayName", "?")
            ro = r.get("roster", [])
            starters = [p for p in ro if p.get("starter")]
            if len(starters) < 11: continue
            names = [(p.get("athlete") or {}).get("displayName", "?") for p in starters]
            gk = [(p.get("athlete") or {}).get("displayName") for p in starters if (p.get("position") or {}).get("abbreviation") in ("G", "GK")]
            xi_lines.append(f'{"🚨⭐ " if is_fav(team) else ""}{team}: ' + ", ".join(n.split()[-1] for n in names) + (f' (GK {gk[0]})' if gk else ""))
            allnames = {norm((p.get("athlete") or {}).get("displayName", "")): p for p in ro}
            for wp in watch_players:
                hit = [n for n in allnames if wp == n or wp.split()[-1] == n.split()[-1] and wp.split()[0][0] == n.split()[0][0]]
                if not hit: continue
                if not allnames[hit[0]].get("starter"):
                    warn.append(f'🔄 {allnames[hit[0]]["athlete"]["displayName"]} ({team}) on the BENCH')
        if len(xi_lines) == 2:
            fired.add(ev["id"] + ":lu")
            alerts.append((4, "📋 Lineups posted", f'{label(ev)}\n' + "\n".join(xi_lines) + ("\n" + "\n".join(warn) if warn else "")))

    # ---------- player props (optional: needs ODDS_API_KEY secret) ----------
    okey = os.environ.get("ODDS_API_KEY", "").strip()
    props = [w for w in watch if w.get("type") == "prop" and w.get("active", True)]
    pstate = state.setdefault("props", {})
    if okey and props:
        OA = "https://api.the-odds-api.com/v4/sports/"
        evcache = {}
        groups = {}
        for w in props:
            last = pstate.get(w["id"], {}).get("checked", 0)
            if time.time() - last < CFG.get("prop_check_minutes", 60) * 60: continue
            groups.setdefault((w["sport_key"], w["team"], w["market"]), []).append(w)
        for (sk, team, market), ws in groups.items():
            if sk not in evcache:
                evcache[sk] = get(OA + sk + "/events?apiKey=" + okey) or []
            evm = [e for e in evcache[sk] if team.lower() in (e.get("home_team", "") + e.get("away_team", "")).lower()]
            evm = [e for e in evm if 0 < (dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) - utcnow).total_seconds() < 14 * 3600]
            if not evm:
                continue
            j = get(OA + sk + "/events/" + evm[0]["id"] + "/odds?apiKey=" + okey + "&regions=us&markets=" + market
                    + "&bookmakers=" + CFG.get("prop_book", "draftkings") + "&oddsFormat=american")
            outs = [o for b in (j or {}).get("bookmakers", []) for m in b.get("markets", []) for o in m.get("outcomes", [])]
            for w in ws:
                ps = pstate.setdefault(w["id"], {})
                ps["checked"] = time.time()
                mine = [o for o in outs if norm(w["player"]) in norm(o.get("description", "")) and o.get("name") == w.get("side", "Over")]
                if not mine: continue
                line, price = mine[0].get("point"), mine[0].get("price")
                prev = ps.get("line")
                ps.update(line=line, price=price)
                if prev is not None and line != prev:
                    alerts.append((3, "📊 Prop line move", f'{w.get("label", w["player"])}\n{w["player"]} {w.get("side","Over")} {prev} → {line} ({fmt(price)})'))
                if w.get("op") in ("<=", ">=") and w["id"] + ":hit" not in fired and line is not None:
                    if (line <= w["value"]) if w["op"] == "<=" else (line >= w["value"]):
                        alerts.append((4, "🎯 Prop target hit", f'{w.get("label", w["player"])}\n{w["player"]} {w.get("side","Over")} {line} ({fmt(price)}) — target {w["op"]} {w["value"]}'))
                        fired.add(w["id"] + ":hit")

    # injuries
    for sport, path in CFG["injury_feeds"].items():
        j = get(API + path + "/injuries")
        if not j: continue
        soon = playing_soon.get(sport, set())
        cur = {}
        for t in j.get("injuries", []):
            team = t.get("displayName", "")
            for it in t.get("injuries", []):
                nm = (it.get("athlete") or {}).get("displayName")
                if nm: cur[f"{team}|{nm}"] = it.get("status", "")
        prev = inj_prev.get(sport)
        if prev is not None:
            lines = []
            for k, s in cur.items():
                team, nm = k.split("|", 1)
                if team not in soon and not is_fav(team): continue
                if k not in prev:
                    lines.append(f'{"🚨⭐ " if is_fav(team) else ""}{nm} ({team}): NEW → {s}')
                elif prev[k] != s:
                    lines.append(f'{"🚨⭐ " if is_fav(team) else ""}{nm} ({team}): {prev[k]} → {s}')
            for k, s in prev.items():
                team, nm = k.split("|", 1)
                if k not in cur and (team in soon or is_fav(team)):
                    lines.append(f'{"🚨⭐ " if is_fav(team) else ""}{nm} ({team}): {s} → OFF report (likely playing)')
            if lines:
                alerts.append((4, f"🏥 {sport.upper()} injury update", "\n".join(lines[:15]) + (f"\n+{len(lines)-15} more" if len(lines) > 15 else "")))
        inj_prev[sport] = cur

    # prune old events
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    for k in [k for k, v in base.items() if v.get("date", "9") < cutoff]:
        base.pop(k, None); seen_odds.pop(k, None)
    state["fired"] = sorted(fired)[-2000:]
    state["seeded"] = True
    state["last_run"] = now.isoformat()

    stamp = now.strftime("%-I:%M %p PT")
    if first_run:
        alerts = [(3, "✅ Watcher is live", f"Seeded {len(base)} games. You'll get alerts for line moves, pulled lines, injuries and watchlist hits. ({stamp})")]
    msgs = [(p, t, b + f"\n⏱ {stamp}") for p, t, b in alerts]

    qs, qe = CFG.get("quiet_hours", [23, 7])
    quiet = now.hour >= qs or now.hour < qe
    if quiet:
        queue.extend([list(m) for m in msgs if m[0] < 5]); msgs = [m for m in msgs if m[0] >= 5]
    elif queue:
        msgs = [tuple(q) for q in queue] + msgs; state["queue"] = queue = []
    state["queue"] = queue[-50:]

    for p, t, b in msgs:
        print("ALERT", t, "|", b.replace("\n", " | "))
        if TOPIC:
            try:
                payload = json.dumps({"topic": TOPIC, "title": t, "message": b, "priority": p}).encode()
                req = urllib.request.Request("https://ntfy.sh/", data=payload,
                        headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15)
            except Exception as e:
                print("ntfy failed", e)
    json.dump(state, open(STATE_F, "w"), indent=1, sort_keys=True)
    print(f"done {stamp}: {len(msgs)} alerts sent, {len(state['queue'])} queued")

if __name__ == "__main__":
    main()
