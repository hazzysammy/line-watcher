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
            for side, ab in (("ml_home", ev["hab"]), ("ml_away", ev["aab"])):
                if o[side] is not None and b.get(side) is not None and th.get("ml") and ml_cents(o[side], b[side]) >= th["ml"]:
                    msgs.append(f'ML {ab} {fmt(b[side])} → {fmt(o[side])}')
                    b[side] = o[side]
            if msgs:
                op = b.get("open", {})
                alerts.append((3, "📈 Line move", f'{label(ev)}\n' + "\n".join(msgs) +
                               f'\n(first seen: {ev["hab"]} {fmt(op.get("spread_home"))}, O/U {op.get('total') or '—'})'))
            # watchlist rules
            for w in watch:
                if not w.get("active", True) or w.get("id") in fired: continue
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
