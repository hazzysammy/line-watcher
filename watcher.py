#!/usr/bin/env python3
"""Sammy's line & injury watcher.
Runs every 15 min on GitHub Actions. Pulls ESPN (DraftKings lines) + injury reports,
compares with the last run (state.json) and pushes alerts to your phone via ntfy."""
import json, os, sys, time, urllib.request, urllib.parse, datetime as dt
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

def american(p):
    p = min(max(p, 0.001), 0.999)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)

def close_bet(w, ps):
    """At game start: closing line value measured DK-to-DK, translated into the user's own book via the gap at bet time."""
    if ps.get("closed") or ps.get("price") is None or ps.get("dk_bet_price") is None:
        ps["closed"] = True; return None
    ps["closed"] = True
    myp, myl = w.get("my_price"), w.get("my_line")
    db, dbl, dc, dcl = ps["dk_bet_price"], ps.get("dk_bet_line"), ps["price"], ps.get("line")
    gap = prob(myp) - prob(db) if myp is not None else 0.0           # your book vs DK at bet time ("exchange rate")
    # convert the DK close back to YOUR line before comparing (each half point is worth ~k win %)
    oid = w["odd_id"]; adj = prob(dc)
    if dbl is not None and dcl is not None and dbl != dcl:
        k = CFG.get("half_point_pct", {}).get("prop" if "-all-" not in oid and "-home-" not in oid and "-away-" not in oid else "game", 2) / 100
        halves = (dcl - dbl) / 0.5
        if oid.endswith("-over"): adj = prob(dc) + k * halves
        elif oid.endswith("-under"): adj = prob(dc) - k * halves
        elif "-sp-" in oid: adj = prob(dc) - k * halves     # spread: you hold dbl; more points for you is better
    clv = adj - prob(db)                                             # DK-to-DK, at your line, in implied win %
    est = american(prob(dc) + gap)                                   # what your book likely closed at
    ps.update(gap_pct=round(gap * 100, 2), clv_pct=round(clv * 100, 2), my_book_close_est=est, dk_close_price=dc, dk_close_line=dcl)
    ln = ""
    if dbl is not None and dcl is not None and dbl != dcl:
        side = (w.get("side") or ("over" if w["odd_id"].endswith("over") else "under" if w["odd_id"].endswith("under") else "")).lower()
        better = (dcl > dbl) if side == "over" else (dcl < dbl) if side == "under" else None
        if "-sp-" in w["odd_id"]: better = dcl < dbl
        ln = f"\nLine moved {dbl:g} → {dcl:g}" + (" ✅ in your favor" if better else " ❌ against you" if better is not None else "")
    ln += "\n(close converted to your line)" if ln else ""
    verdict = "✅ beat the close" if clv > 0.005 else "❌ lost to the close" if clv < -0.005 else "➖ about even"
    return (2, "📏 Closing line", f'{w.get("label")}\nYou: {myl if myl is not None else ""} {fmt(myp)} · DK then {dbl if dbl is not None else ""} {fmt(db)} (gap {gap*100:+.1f}%)'
               f'\nDK close {dcl if dcl is not None else ""} {fmt(dc)} → ≈ {fmt(est)} at your book{ln}\nCLV {clv*100:+.1f}% {verdict}')

def grade_bet(w, ps, ev):
    """Grade from SportsGameOdds final results. Writes the result into ps and returns an alert."""
    o = (ev.get("odds") or {}).get(w["odd_id"]) or {}
    oid = w["odd_id"]; line = w.get("my_line"); res = None
    score = o.get("score")
    if o.get("cancelled"):
        res = "Void"
    elif "-ou-" in oid and score is not None and line is not None:
        d = score - line if oid.endswith("-over") else line - score
        res = "Win" if d > 0 else "Loss" if d < 0 else "Push"
    elif ("-sp-" in oid or "-ml-" in oid):
        per = oid.split("-")[-3]
        r = ((ev.get("results") or {}).get(per) or {})
        side = oid.split("-")[-1]; other = "home" if side == "away" else "away"
        a, b = (r.get(side) or {}).get("points"), (r.get(other) or {}).get("points")
        if a is not None and b is not None:
            m = a - b + (line or 0 if "-sp-" in oid else 0)
            if "-ml-" in oid and a == b: res = "Push" if w.get("draw_push") else "Loss"
            else: res = "Win" if m > 0 else "Loss" if m < 0 else "Push"
            score = f"{a}-{b}"
    if res is None:
        return None
    myp = w.get("my_price"); stake = w.get("stake")
    net = None
    if stake is not None and myp is not None:
        net = round(stake * (myp / 100 if myp > 0 else 100 / -myp), 2) if res == "Win" else (-stake if res == "Loss" else 0)
    ps.update(graded=True, result=res, final=score, net=net)
    icon = {"Win": "✅", "Loss": "❌", "Push": "➖", "Void": "➖"}[res]
    return (3, f"{icon} Bet graded: {res}", f'{w.get("label")}\nFinal: {score}' + (f'\nNet: {"+" if (net or 0) > 0 else ""}${net}' if net is not None else "")
               + (f'\nCLV {ps["clv_pct"]:+.1f}%' if ps.get("clv_pct") is not None else ""))

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

STRONG = ("ruled out", "questionable", "doubtful", "out for", "won't play", "will not play", "game-time", "inactive",
          "will play", "upgraded", "downgraded", "probable", "will start", "to start", "starting lineup", "out vs", "out against",
          "sit out", "rest ", "injur", "suspend", "scratch", "day-to-day", "sidelined", "will miss", "to miss", "concussion", "torn", "surgery")

def strong_hit(text):
    t = text.lower()
    return any(w in t for w in STRONG)

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
          "odds": None,
          "period": (e.get("status") or {}).get("period", 0), "clock": (e.get("status") or {}).get("displayClock", ""),
          "hs": num(h.get("score")), "as": num(a.get("score")),
          "hid": h["team"].get("id"), "aid": a["team"].get("id"),
          "indoor": (c.get("venue") or {}).get("indoor"),
          "city": ((c.get("venue") or {}).get("address") or {}).get("city", ""),
          "st": ((c.get("venue") or {}).get("address") or {}).get("state", ""),
          "venue": (c.get("venue") or {}).get("fullName", "")}
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

def band_note(p):
    """Sammy's own record by odds band (config profile)."""
    pr = CFG.get("profile", {})
    if p is None: return ""
    if p < CFG.get("parlay_cutoff", -650): return f"⚠ {fmt(p)} is shorter than your -650 parlay cutoff"
    if p <= -300: return f"heavy-fav band: {pr.get('heavy','')} of your legs win"
    if p <= -150: return f"✅ your best ML band (-150 to -299): {pr.get('fav','')}"
    if p < 100: return f"⚠ slight-fav band (-101 to -149): only {pr.get('slight','')} for you"
    if p < 200: return f"⚠ underdog band: {pr.get('dog','')} for you"
    return f"⚠ long-shot band: {pr.get('long','')} for you"

def news_for(ev, log):
    """Real news lines (not game labels) mentioning either team."""
    keys = set()
    for t in (ev["home"], ev["away"]):
        w = t.split()
        keys.add(w[-1].lower()); keys.add(" ".join(w[:-1]).lower()); keys.add(t.lower())
    keys = {k for k in keys if len(k) > 3}
    hits = []
    for a in log[-80:]:
        if not any(x in a["title"] for x in ("news", "injury", "Weather", "pulled")): continue
        for line in a["body"].split("\n"):
            l = line.replace("🚨⭐ ", "").strip()
            if not l or (" @ " in l and "PT)" in l) or l.startswith(("📊", "•", "🧠", "👉")): continue   # skip game labels / our own notes
            if any(k in l.lower() for k in keys) and l not in hits: hits.append(l[:90])
    return hits[-2:]

def _short(t): return t.split()[-1]

def explain_move(ev, mv, o, op, sport, log, state=None):
    """Compact: what moved, current lines, does it fit our criteria, what to do."""
    if not mv: return ""
    out, take, do = [], [], None
    money_on = None
    for kind, old, new in mv:
        if kind == "spread":
            money_on = ev["home"] if new - old < 0 else ev["away"]
            out.append(f"Spread {ev['hab']} {fmt(old)} → {fmt(new)} (money on {_short(money_on)})")
        elif kind == "total":
            out.append(f"Total {old:g} → {new:g} ({'under' if new < old else 'over'} money)")
        else:
            team = ev["home"] if kind == "ml_home" else ev["away"]
            out.append(f"ML {_short(team)} {fmt(old)} → {fmt(new)}")
    if op and op.get("spread_home") is not None and op.get("spread_home") != o.get("spread_home") and not any(k == "spread" and op.get("spread_home") == old for k, old, _ in mv):
        out.append(f"(opened {ev['hab']} {fmt(op.get('spread_home'))})")
    out.append(f"Now: {ev['hab']} {fmt(o.get('spread_home'))} · O/U {o.get('total') or '—'} · ML {ev['aab']} {fmt(o.get('ml_away'))} / {ev['hab']} {fmt(o.get('ml_home'))}")

    # 1) our backtested NBA trend
    if sport == "nba" and state is not None and money_on:
        try:
            tr = team_trends(ev, state)
        except Exception:
            tr = {"home": [], "away": []}
        for side in ("home", "away"):
            if not tr.get(side): continue
            tm = ev[side]; sp = o.get("spread_home"); line = sp if side == "home" else (-sp if sp is not None else None)
            sig = tr[side][0].split(" (")[0]
            if tm == money_on:
                take.append(f"✅ Fits our trend: {_short(tm)} {sig} (55.7% ATS) + money agrees")
                do = f"BET {_short(tm)} {fmt(line)} 0.5u (Trend Alert Pick)"
            else:
                take.append(f"⚠ Trend team {_short(tm)} ({sig}) — money went the OTHER way")
                do = f"WATCH: {_short(tm)} is now a better number ({fmt(line)}), but trend + market disagree → no bet"
    # 2) Alert Picks already on this game
    afp = os.path.join(HERE, "props", "autoflags.json")
    try:
        flags = json.load(open(afp)) if os.path.exists(afp) else []
    except Exception:
        flags = []
    for f in flags:
        if str(f.get("espn_event")) != str(ev["id"]) or f.get("result") or f.get("silent"): continue
        if f.get("stat") == "spread" and money_on:
            good = f.get("player") == money_on
            take.append(f"🤖 Your Alert Pick {_short(f['player'])} {fmt(f.get('line'))}: move is {'WITH it ✅' if good else 'AGAINST it ❌'}")
        elif f.get("stat") not in ("spread", "live_ml"):
            take.append(f"🤖 Prop Alert Pick on this game: {f.get('player')} {f.get('side')} {f.get('line')} {f.get('stat')}")
    # 3) news vs no news
    hits = news_for(ev, log)
    if hits:
        take.append("📰 Cause: " + " / ".join(hits))
        do = do or "PASS — news is already priced in; don't chase the move"
    else:
        big = any((k == "spread" and abs(n - o_) >= 2) or (k == "total" and abs(n - o_) >= 3) for k, o_, n in mv)
        if big: take.append("No news found → likely sharp money")
    for kind, old, new in mv:
        if kind.startswith("ml_"):
            bn = band_note(new)
            if bn: take.append(bn)
    if is_fav(ev["home"]) or is_fav(ev["away"]): take.append("⭐ your team")
    if not [t for t in take if t.startswith(("✅", "⚠ Trend", "🤖"))]:
        take.insert(0, "❌ Doesn't meet any of our criteria")
    nxt = {"nba": "statuses ~30–60 min before tip", "nfl": "inactives 90 min before kickoff",
           "soccer": "lineups ~60 min before kickoff", "ncaab": "lineups at tip",
           "ncaaf": "late injury news", "nhl": "goalies ~1 hr before"}.get(sport, "news")
    do = do or f"PASS — just watch {nxt}; I'll ping if news makes it a bet"
    return "\n" + "\n".join(out) + "\n🧠 " + " · ".join(take) + "\n👉 " + do

PLAYERS = {}
def load_players():
    global PLAYERS
    f = os.path.join(HERE, "props", "players.json")
    if not PLAYERS and os.path.exists(f):
        try: PLAYERS = json.load(open(f))
        except Exception: PLAYERS = {}
    return PLAYERS

def news_meaning(text, player, teams, sport, tl=None):
    """Plain-English: what this news means and what to do next."""
    t = text.lower()
    info = load_players().get(norm(player)) if sport == "nba" else None
    team = None
    if info and tl:
        for x in list(tl):
            if x in tl:
                ev = tl[x][0]; ab = ev["hab"] if x == ev["home"] else ev["aab"]
                if ab == info.get("team"): team = x
    if not team: team = teams[0] if len(teams) == 1 else None
    tn = team.split()[-1] if team else "his team"
    who = f"{player} ({info['ppg']:.1f} ppg, {info['mpg']:.0f} min)" if info else player
    big = bool(info and info.get("ppg", 0) >= 18)
    if any(k in t for k in ("ruled out", "won't play", "will not play", "will miss", "is out", "out for", "sit out", "unavailable", "inactive", "→ out")):
        m = f"🧠 Means: {tn} play WITHOUT {who}." + (" That's a top scorer. Expect the spread to swing away from them and their team total to drop." if big else "")
        d = ("👉 Do: if the line hasn't moved, look at the OTHER side's spread and the " + tn + " team-total UNDER. "
             "His minutes go to teammates, so check their points/assists OVERS before DK adjusts.") if sport in ("nba", "ncaab") else \
            ("👉 Do: if the line hasn't moved, look at the other side or the " + tn + " team total UNDER. Players who replace him get more volume (their yards/receptions overs).") if sport in ("nfl", "ncaaf") else \
            "👉 Do: check the lineup ~1 hr before kickoff and whether the price moved."
        return m + "\n" + d
    if any(k in t for k in ("will start", "to start", "starting", "good to go", "will play", "available", "probable", "upgraded", "returns", "return", "cleared", "removed from report")):
        m = f"🧠 Means: {who} PLAYS" + (" or gets a bigger role." if "start" in t else ".")
        d = "👉 Do: if he was doubtful/questionable, the line may move back toward " + tn + ". His minutes go UP (his overs); the teammates who covered for him go DOWN (their unders)."
        return m + "\n" + d
    if any(k in t for k in ("questionable", "game-time", "doubtful", "day-to-day", "limited", "uncertain", "downgraded")):
        lean = "leans OUT" if "doubtful" in t or "downgraded" in t else "50/50"
        return (f"🧠 Means: {who} is uncertain ({lean}).\n"
                "👉 Do: don't bet his props or this game yet. Final status usually comes 30–60 min before tip/kickoff, and the watcher will ping you the moment it changes.")
    return ""

def add_flag(rec):
    afp = os.path.join(HERE, "props", "autoflags.json")
    flags = json.load(open(afp)) if os.path.exists(afp) else []
    if any(x["key"] == rec["key"] for x in flags): return False
    flags.append(rec); json.dump(flags, open(afp, "w"), indent=1); return True

def team_trends(ev, state):
    """Our NBA research (own-stats.md, 2025-26): after a 20+ pt WIN covers 61% (n=217);
    immediate rematch: winner of game 1 covers 58.5%, loser 43.6%."""
    day = dt.datetime.now(PT).strftime("%Y-%m-%d")
    cache = state.setdefault("team_hist", {})
    out = {}
    for side, tid, opp in (("home", ev["hid"], ev["aid"]), ("away", ev["aid"], ev["hid"])):
        k = f"{tid}|{day}"
        if k not in cache:
            sc = get(API + "basketball/nba/teams/" + str(tid) + "/schedule") or {}
            done = []
            for e in sc.get("events", []):
                c = e["competitions"][0]
                if not c.get("status", {}).get("type", {}).get("completed"): continue
                me = next((x for x in c["competitors"] if x["team"]["id"] == str(tid)), None)
                ot = next((x for x in c["competitors"] if x["team"]["id"] != str(tid)), None)
                if not me or not ot: continue
                sv = lambda x: num((x.get("score") or {}).get("value") if isinstance(x.get("score"), dict) else x.get("score")) or 0
                done.append({"date": e["date"], "opp": ot["team"]["id"], "margin": sv(me) - sv(ot)})
            done.sort(key=lambda x: x["date"])
            cache[k] = done[-5:]
        last = cache[k]
        sig = []
        if last:
            g = last[-1]
            if g["margin"] >= 20: sig.append(f'coming off a {g["margin"]:.0f}-pt WIN (backtest: 55.7% ATS over 567 games, z 2.7)')
            # rematch angle removed 11/6: 2-season backtest = noise (winners 47.3%, losers 53.3%)
        out[side] = sig
    for k2 in [k2 for k2 in cache if not k2.endswith(day)]: cache.pop(k2)
    return out

def line_context(teams, team_line):
    """For news about a team: show its current line vs when we first saw it."""
    out = []
    for t in teams:
        if t not in team_line: continue
        ev, o, op = team_line[t]
        sp, so = o.get("spread_home"), op.get("spread_home")
        tot, to = o.get("total"), op.get("total")
        moved = (sp is not None and so is not None and abs(sp - so) >= 1) or (tot is not None and to is not None and abs(tot - to) >= 1.5)
        txt = f'📊 {ev["away"].split()[-1]} @ {ev["home"].split()[-1]}: {ev["hab"]} {fmt(sp)}, O/U {tot or "—"}'
        txt += f' (first seen {ev["hab"]} {fmt(so)}, O/U {to or "—"})'
        txt += " → already moved; the news may be priced in." if moved else " → ⏱️ hasn't moved yet. If this player matters, the early window is NOW."
        if txt not in out: out.append(txt)
    return out

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
    weather_games = []
    team_line = {}
    soccer_events = []
    live_games = []
    nba_today = []
    triggers = []   # (teams, note) from NBA news -> instant prop re-price

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
            if lg["sport"] == "nba" and ev["state"] == "in":
                live_games.append(ev)
            if lg["sport"] == "nba" and ev["state"] == "pre" and 0 < hrs < 14:
                nba_today.append(ev)
            if lg["sport"] == "soccer" and -6 < hrs < 40:
                soccer_events.append((lg, ev))
            if 0 < hrs < 36 and ev["state"] == "pre":
                playing_soon.setdefault(lg["sport"], set()).update([ev["home"], ev["away"]])
            if lg["sport"] in ("nfl", "ncaaf") and 0 < hrs <= 30 and ev["indoor"] is False and ev["state"] == "pre" and (ev["odds"] or is_fav(ev["home"]) or is_fav(ev["away"])) and (lg.get("watch") != "ranked" or ev["ranked"] or ev["odds"]):
                weather_games.append(ev)
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
            for tnm in (ev["home"], ev["away"]):
                team_line[tnm] = (ev, o, (base.get(key) or {}).get("open") or o)
            b = base.get(key)
            if not b:
                base[key] = dict(o, open=dict(o), date=ev["date"]); continue
            msgs = []; mv = []
            if o["spread_home"] is not None and b.get("spread_home") is not None and th.get("spread") and abs(o["spread_home"] - b["spread_home"]) >= th["spread"]:
                msgs.append(f'Spread {ev["hab"]} {fmt(b["spread_home"])} → {fmt(o["spread_home"])}')
                mv.append(("spread", b["spread_home"], o["spread_home"]))
                b["spread_home"] = o["spread_home"]
            if o["total"] is not None and b.get("total") is not None and th.get("total") and abs(o["total"] - b["total"]) >= th["total"]:
                msgs.append(f'Total {b['total']:g} → {o['total']:g}')
                mv.append(("total", b["total"], o["total"]))
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
                    mv.append((side, b[side], o[side]))
                    b[side] = o[side]
            if msgs:
                op = b.get("open", {})
                alerts.append((3, "📈 Line move", label(ev)
                               + explain_move(ev, mv, o, op, lg["sport"], state.get("alert_log", []) + [{"title": a[1], "body": a[2]} for a in alerts], state)))
            # watchlist rules
            for w in watch:
                if w.get("type") in ("prop", "bet") or not w.get("active", True) or w.get("id") in fired: continue
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
            if not strong_hit(a.get("headline", "") + " " + (a.get("description") or "")[:160]): continue
            cats = " ".join((c.get("description") or "") for c in a.get("categories", []) if c.get("type") in ("team", "athlete"))
            blob = (text + " " + cats).lower()
            teams = [t for t in soon if t.lower() in blob] + [nick[n] for n in nick if (" " + n) in (" " + blob) and nick[n] not in blob]
            fav = is_fav(blob)
            if not teams and not fav: continue
            star = "🚨⭐ " if fav else ""
            alerts.append((4, f"📰 {sport.split('_')[0].upper()} news", f'{star}{a.get("headline","")}\n{(a.get("description") or "")[:220]}'
                           + (f'\nPlays within 36h: {", ".join(sorted(set(teams)))}' if teams else "")
                           + ("\n" + "\n".join(line_context(sorted(set(teams)), team_line)) if line_context(sorted(set(teams)), team_line) else "\n→ Check the line NOW before it moves.")))
    state["news_seen"] = sorted(news_seen)[-1500:]

    # player -> team map from ESPN injury reports (RotoWire headlines only name the player)
    player_team = {}
    inj_cache = {}
    for sport, path in CFG["injury_feeds"].items():
        j = get(API + path + "/injuries"); inj_cache[sport] = j
        for t in (j or {}).get("injuries", []):
            for it in t.get("injuries", []):
                nm = (it.get("athlete") or {}).get("displayName")
                if nm: player_team.setdefault(sport, {})[nm.lower()] = t.get("displayName", "")

    # ---------- RotoWire news (usually faster than ESPN) ----------
    import xml.etree.ElementTree as ET, email.utils
    rw_first = "rw_seen" not in state
    rw_seen = set(state.setdefault("rw_seen", []))
    for sport, code in CFG.get("rotowire_feeds", {}).items():
        try:
            req = urllib.request.Request("https://www.rotowire.com/rss/news.php?sport=" + code, headers={"User-Agent": "Mozilla/5.0 line-watcher"})
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            print("rotowire failed", code, e); continue
        soon = playing_soon.get(sport, set())
        nick = {t.split()[-1].lower(): t for t in soon}
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip(); desc = (it.findtext("description") or "").strip()
            gid = (it.findtext("guid") or title)
            if gid in rw_seen: continue
            rw_seen.add(gid)
            if rw_first: continue
            try:
                pub = email.utils.parsedate_to_datetime(it.findtext("pubDate"))
                if (utcnow - pub).total_seconds() > 3 * 3600: continue
            except Exception:
                pass
            text = title + " " + desc
            if not strong_hit(text): continue
            blob = text.lower()
            clean = " " + blob.replace(",", " ").replace(".", " ").replace("'", " ").replace(":", " ") + " "
            city = {" ".join(t.split()[:-1]).lower(): t for t in soon if len(t.split()) > 1}
            teams = {t for t in soon if t.lower() in blob} | {nick[n] for n in nick if (" " + n + " ") in clean} \
                    | ({city[c] for c in city if c and (" " + c + " ") in clean} if sport in ("nba", "nfl") else set())
            pl = title.split(":")[0].strip().lower()
            if pl in player_team.get(sport, {}) and player_team[sport][pl] in soon: teams.add(player_team[sport][pl])
            teams = sorted(teams)
            fav = is_fav(blob)
            strong = any(w in blob for w in ("ruled out", "questionable", "doubtful", "out for", "won't play", "will not play", "game-time",
                                             "inactive", "will play", "upgraded", "downgraded", "probable", "will start", "to start", "starting lineup", "out vs", "out against", "sit out", "rest"))
            if not teams and not fav and not (strong and sport in ("nba", "nfl")): continue
            ctx = line_context(teams, team_line)
            pinfo = load_players().get(norm(title.split(":")[0].strip())) if sport == "nba" else None
            if pinfo:
                real = [x for x in team_line if (team_line[x][0]["hab"] if x == team_line[x][0]["home"] else team_line[x][0]["aab"]) == pinfo.get("team")]
                if real: teams = real
            ctx = line_context(teams, team_line)
            mean = news_meaning(title + " " + desc, title.split(":")[0].strip(), teams, sport, team_line)
            if mean: ctx = [mean] + ctx
            if sport == "nba" and teams: triggers.append((teams, title.split(":")[0] + ": " + title.split(":", 1)[-1].strip()[:40]))
            alerts.append((4, f"⚡ {sport.upper()} news (RotoWire)", f'{"🚨⭐ " if fav else ""}{title}\n{desc[:240]}'
                           + (f'\nPlays within 36h: {", ".join(teams)}' if teams else "")
                           + ("\n" + "\n".join(ctx) if ctx else "\n→ Check the line NOW before it moves.")))
    state["rw_seen"] = sorted(rw_seen)[-3000:]

    # ---------- weather for outdoor football ----------
    geo = state.setdefault("geo", {})
    wxc = state.setdefault("wx_checked", {})
    for ev in weather_games:
        if time.time() - wxc.get(ev["id"], 0) < CFG.get("weather_check_minutes", 60) * 60: continue
        wxc[ev["id"]] = time.time()
        loc = f'{ev["city"]}, {ev["st"]}'.strip(", ")
        if not loc: continue
        if loc not in geo:
            g = get("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(ev["city"]))
            r0 = ((g or {}).get("results") or [None])[0]
            geo[loc] = [r0["latitude"], r0["longitude"]] if r0 else None
        if not geo[loc]: continue
        lat, lon = geo[loc]
        w = get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,precipitation,snowfall,wind_speed_10m,wind_gusts_10m&wind_speed_unit=mph&temperature_unit=fahrenheit&precipitation_unit=inch&timezone=UTC&forecast_days=3")
        h = (w or {}).get("hourly") or {}
        ko = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        idx = [i for i, t in enumerate(h.get("time", [])) if 0 <= (dt.datetime.fromisoformat(t + "+00:00") - ko).total_seconds() < 3 * 3600]
        if not idx: continue
        mx = lambda k: max(h[k][i] or 0 for i in idx)
        wind, gust, rain, snow, temp = mx("wind_speed_10m"), mx("wind_gusts_10m"), mx("precipitation"), mx("snowfall"), min(h["temperature_2m"][i] for i in idx)
        t = CFG.get("weather", {})
        flags = []
        if wind >= t.get("wind_mph", 15) or gust >= t.get("gust_mph", 28): flags.append(f"💨 wind {wind:.0f} mph (gusts {gust:.0f})")
        if rain >= t.get("rain_in_hr", 0.15): flags.append(f"🌧 heavy rain {rain:.2f} in/hr")
        if snow >= t.get("snow_in_hr", 0.2): flags.append(f"❄️ snow {snow:.1f} in/hr")
        if temp <= t.get("cold_f", 20): flags.append(f"🥶 {temp:.0f}°F")
        sev = len(flags) + (1 if wind >= 20 or gust >= 35 else 0)
        key = ev["id"] + ":wx"
        if flags and state.setdefault("wx", {}).get(key, 0) < sev:
            state["wx"][key] = sev
            o = ev["odds"] or {}
            alerts.append((4 if sev >= 2 else 3, "🌦 Weather game", f'{label(ev)}\n{ev["venue"]} — ' + ", ".join(flags)
                           + f'\nTotal {o.get("total") or "—"} · {ev["hab"]} {fmt(o.get("spread_home"))}'
                           + "\nWind/rain usually push totals and passing props DOWN — check before the market adjusts."))

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

    # ---------- DraftKings props / halves / quarters via SportsGameOdds (needs SGO_API_KEY) ----------
    skey = os.environ.get("SGO_API_KEY", "").strip()
    props = [w for w in watch if w.get("type") in ("prop", "bet") and w.get("active", True)
             and not state.get("props", {}).get(w["id"], {}).get("graded")]
    pstate = state.setdefault("props", {})
    month = now.strftime("%Y-%m")
    usage = state.setdefault("sgo_usage", {})
    if skey and props:
        SGO = "https://api.sportsgameodds.com/v2/events"
        def sgo(q):
            if usage.get(month, 0) >= CFG.get("sgo_monthly_budget", 1200):
                print("SGO monthly budget reached"); return []
            req = urllib.request.Request(SGO + "?" + q, headers={"x-api-key": skey, "User-Agent": "line-watcher"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    d = json.load(r).get("data", [])
            except Exception as e:
                print("sgo failed", e); return []
            usage[month] = usage.get(month, 0) + len(d)
            return d
        evids = state.setdefault("sgo_events", {})   # "LEAGUE|TEAM|date" -> eventID
        day = now.strftime("%Y%m%d")
        groups = {}
        for w in props:
            ps0 = pstate.get(w["id"], {})
            last = ps0.get("checked", 0)
            every = CFG.get("prop_check_minutes", 60)
            st = ps0.get("starts")
            if w.get("type") == "bet" and st:
                mins = (dt.datetime.fromisoformat(st.replace("Z", "+00:00")) - utcnow).total_seconds() / 60
                if mins <= 75: every = CFG.get("close_check_minutes", 10)
                if mins < 0: every = CFG.get("grade_check_minutes", 30)
            if time.time() - last < every * 60: continue
            groups.setdefault((w["league"], w["team_id"]), []).append(w)
        for (league, team_id), ws in groups.items():
            ck = f"{league}|{team_id}|{day}"
            if ck not in evids:
                t0 = utcnow.strftime("%Y-%m-%dT%H:%M:%SZ"); t1 = (utcnow + dt.timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
                d = sgo(f"leagueID={league}&teamID={team_id}&startsAfter={t0}&startsBefore={t1}&limit=1")
                d = [e for e in d if team_id in (e["teams"]["home"]["teamID"], e["teams"]["away"]["teamID"])]
                evids[ck] = d[0]["eventID"] if d else None
                evdata = d[0] if d else None
            else:
                evdata = None
            if not evids[ck]:
                continue
            if evdata is None:
                d = sgo("eventIDs=" + evids[ck])
                evdata = d[0] if d else None
            if not evdata: continue
            if (evdata.get("status") or {}).get("started"):
                for w in ws:
                    ps = pstate.setdefault(w["id"], {}); ps["checked"] = time.time()
                    if w.get("type") == "bet":
                        a = close_bet(w, ps)
                        if a: alerts.append(a)
                        if (evdata.get("status") or {}).get("ended"):
                            g = grade_bet(w, ps, evdata)
                            if g: alerts.append(g)
                    else:
                        ps["graded"] = True      # watch-only prop: nothing more to do after tip
                continue
            for w in ws:
                pstate.setdefault(w["id"], {})["starts"] = (evdata.get("status") or {}).get("startsAt")
                ps = pstate.setdefault(w["id"], {}); ps["checked"] = time.time()
                o = (evdata.get("odds") or {}).get(w["odd_id"]) or {}
                bk = (o.get("byBookmaker") or {}).get(CFG.get("prop_book", "draftkings")) or {}
                if not bk.get("available", True) or not bk:
                    if ps.get("line") is not None and w["id"] + ":off" not in fired:
                        alerts.append((4, "⛔ Prop pulled", f'{w.get("label")}\nDK took this market off the board — check news.'))
                        fired.add(w["id"] + ":off")
                    continue
                fired.discard(w["id"] + ":off")
                line = num(bk.get("overUnder") or bk.get("spread")); price = num(bk.get("odds"))
                pl, pp = ps.get("line"), ps.get("price")
                ps.update(line=line, price=price)
                if w.get("type") == "bet":
                    if "dk_bet_price" not in ps:
                        ps["dk_bet_price"] = w.get("dk_bet_price", price); ps["dk_bet_line"] = w.get("dk_bet_line", line)
                    continue
                mv = []
                if pl is not None and line != pl: mv.append(f"line {pl:g} → {line:g}")
                if pp is not None and price is not None and abs(prob(price) - prob(pp)) * 100 >= CFG.get("prop_price_pct", 4):
                    mv.append(f"price {fmt(pp)} → {fmt(price)}")
                if mv:
                    alerts.append((3, "📊 Prop move (DK)", f'{w.get("label")}\n' + ", ".join(mv) + f'  (now {line if line is not None else ""} {fmt(price)})'))
                for fld, cur in (("line", line), ("price", price)):
                    op, val = w.get(fld + "_op"), w.get(fld + "_value")
                    if op and cur is not None and w["id"] + ":" + fld not in fired and ((cur <= val) if op == "<=" else (cur >= val)):
                        alerts.append((4, "🎯 Target hit (DK)", f'{w.get("label")}\nNow {line if line is not None else ""} {fmt(price)} — target {fld} {op} {fmt(val) if fld=="price" else val}'))
                        fired.add(w["id"] + ":" + fld)
        for k in [k for k in evids if not k.endswith(day)]:
            evids.pop(k)

    # injuries
    for sport, path in CFG["injury_feeds"].items():
        j = inj_cache.get(sport) if sport in inj_cache else get(API + path + "/injuries")
        if not j: continue
        soon = playing_soon.get(sport, set())
        cur = {}
        for t in j.get("injuries", []):
            team = t.get("displayName", "")
            for it in t.get("injuries", []):
                nm = (it.get("athlete") or {}).get("displayName")
                cm = (it.get("shortComment") or "").strip()
                if nm: cur[f"{team}|{nm}"] = it.get("status", "") + (" ¦ " + cm[:110] if cm else "")
        prev = inj_prev.get(sport)
        if prev is not None:
            lines = []
            for k, s in cur.items():
                team, nm = k.split("|", 1)
                if team not in soon and not is_fav(team): continue
                pv = prev.get(k)
                pst = pv.split(" ¦ ")[0] if pv else None
                cst = s.split(" ¦ ")[0]
                note = s.split(" ¦ ")[1] if " ¦ " in s else ""
                if pst == cst: continue
                if cst.lower() == "active" and pst is None: continue          # newly listed as Active = noise
                if (pst or "").lower() in ("injured reserve", "out for season") and cst.lower() in ("injured reserve", "out for season"): continue
                star = "🚨⭐ " if is_fav(team) else ""
                lines.append(f'{star}{nm} ({team}): {pst or "NEW"} → {cst}' + (f' — {note}' if note else ""))
            for k, pv in prev.items():
                team, nm = k.split("|", 1)
                pst = pv.split(" ¦ ")[0]
                if k not in cur and (team in soon or is_fav(team)) and pst.lower() not in ("active", "injured reserve", "out for season"):
                    lines.append(f'{"🚨⭐ " if is_fav(team) else ""}{nm} ({team}): {pst} → removed from report (likely available)')
            if lines:
                big = sorted({l.split("(")[1].split(")")[0] for l in lines if ("→ Out" in l or "→ Doubtful" in l) and "(" in l})
                ctx = line_context(big, team_line)
                if sport == "nba":
                    for l in lines:
                        if "→ Out" not in l or "(" not in l: continue
                        pl = l.replace("🚨⭐ ", "").split(" (")[0]; tm = l.split("(")[1].split(")")[0]
                        info = load_players().get(norm(pl))
                        if not info or info.get("ppg", 0) < CFG.get("star_ppg", 18) or tm not in team_line: continue
                        ev, o, op = team_line[tm]
                        st = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                        if not (0 < (st - dt.datetime.now(dt.timezone.utc)).total_seconds() < 14 * 3600): continue
                        sp, so = o.get("spread_home"), op.get("spread_home")
                        if sp is None or so is None or abs(sp - so) >= 0.5: continue      # already moved = priced in
                        opp_home = tm != ev["home"]
                        opp = ev["home"] if opp_home else ev["away"]
                        line = sp if opp_home else -sp
                        afp = os.path.join(HERE, "props", "autoflags.json")
                        flags = json.load(open(afp)) if os.path.exists(afp) else []
                        key = f'{ev["date"][:10]}|SPREAD|{opp}'
                        if any(x["key"] == key for x in flags): continue
                        flags.append({"key": key, "created": now.strftime("%Y-%m-%d %H:%M PT"), "game": f'{ev["aab"]} @ {ev["hab"]}', "start": ev["date"],
                                      "espn_event": ev["id"], "espn_id": None, "player": opp, "stat": "spread", "side": "home" if opp_home else "away",
                                      "line": line, "price": -110, "proj": None, "edge": 0,
                                      "signals": [f'{pl} ({info["ppg"]:.1f} ppg) ruled OUT for {tm.split()[-1]}', "spread hasn't moved yet",
                                                  f'spreads are your best market ({CFG.get("profile", {}).get("spread", "")})'],
                                      "units": 1.0, "sent": False, "result": None, "trigger": "news"})
                        json.dump(flags, open(afp, "w"), indent=1)
                for l in lines[:4]:
                    if "→ Out" in l or "→ Doubtful" in l or "removed from report" in l:
                        pl = l.replace("🚨⭐ ", "").split(" (")[0]; tm = l.split("(")[1].split(")")[0] if "(" in l else ""
                        mm = news_meaning(l, pl, [tm] if tm else [], sport)
                        if mm: ctx = [f"• {pl}: " + mm.replace(chr(10), " ")] + ctx
                if sport == "nba":
                    for l in lines:
                        if "(" in l and any(k in l for k in ("→ Out", "→ Doubtful", "→ Questionable", "removed from report")):
                            triggers.append(([l.split("(")[1].split(")")[0]], l.split(" (")[0].replace("🚨⭐ ", "") + " " + l.split("): ")[-1].split(" —")[0]))
                alerts.append((4, f"🏥 {sport.upper()} injury update", "\n".join(lines[:15]) + (f"\n+{len(lines)-15} more" if len(lines) > 15 else "")
                               + ("\n" + "\n".join(ctx) if ctx else "")))
        inj_prev[sport] = cur

    # ---------- 📈 trend-based spread Alert Picks (our own stats + market agreement) ----------
    for ev in nba_today:
        if not ev["odds"] or ev["id"] not in base: continue
        tr = team_trends(ev, state)
        o = ev["odds"]; op = base[ev["id"]].get("open", o)
        sp, so = o.get("spread_home"), op.get("spread_home")
        if sp is None or so is None: continue
        for side in ("home", "away"):
            good = [s for s in tr[side] if "→ fade" not in s]
            fade_opp = [s for s in tr["away" if side == "home" else "home"] if "→ fade" in s]
            sigs = good + [f'opponent {s}' for s in fade_opp]
            moved_our_way = (so - sp >= 1) if side == "home" else (sp - so >= 1)
            if moved_our_way: sigs.append(f"market moved our way ({fmt(so)} → {fmt(sp)} {ev['hab']})")
            if len(sigs) >= 2 and (good or fade_opp):
                team = ev[side]; line = sp if side == "home" else -sp
                if add_flag({"key": f'{ev["date"][:10]}|SPREAD|{team}', "created": now.strftime("%Y-%m-%d %H:%M PT"), "game": f'{ev["aab"]} @ {ev["hab"]}',
                             "start": ev["date"], "espn_event": ev["id"], "espn_id": None, "player": team, "stat": "spread", "side": side,
                             "line": line, "price": -110, "proj": None, "edge": 0, "units": 0.5, "sent": False, "result": None, "trigger": "trend",
                             "signals": sigs + [f'spreads are your best market ({CFG.get("profile", {}).get("spread", "")})']}):
                    print("trend flag", team)

    # ---------- 🔴 live Alert Picks: big pregame favorite trailing early ----------
    lb = state.setdefault("live_budget", {}); dkey = now.strftime("%Y-%m-%d")
    for ev in live_games:
        b0 = (base.get(ev["id"]) or {}).get("open") or {}
        sp0 = b0.get("spread_home")
        if sp0 is None or ev["hs"] is None or ev["as"] is None: continue
        fav = "home" if sp0 < 0 else "away"
        if abs(sp0) < CFG.get("live_fav_spread", 6): continue
        diff = (ev["hs"] - ev["as"]) if fav == "home" else (ev["as"] - ev["hs"])
        if ev["period"] in (1, 2) and diff <= -CFG.get("live_deficit", 10) and lb.get(dkey, 0) < CFG.get("live_budget_per_day", 10):
            team = ev[fav]
            rec = {"key": f'{ev["date"][:10]}|LIVE|{team}', "created": now.strftime("%Y-%m-%d %H:%M PT"), "game": f'{ev["aab"]} @ {ev["hab"]}',
                   "start": ev["date"], "espn_event": ev["id"], "espn_id": None, "player": team, "stat": "live_ml", "side": fav,
                   "line": 0, "price": None, "proj": None, "edge": 0, "units": 0.5, "sent": False, "result": None, "trigger": "live",
                   "signals": [f'{team.split()[-1]} were {abs(sp0):g}-pt favorites before tip', f'now down {-diff:.0f} in Q{ev["period"]} ({ev["clock"]})',
                               "live lines overreact to early deficits for strong favorites; check the live ML/spread, and only bet if it's plus money or near pick'em"]}
            if add_flag(rec):
                lb[dkey] = lb.get(dkey, 0) + 1
    for k in [k for k in lb if k != dkey]: lb.pop(k)

    # ---------- 🔗 Player A -> Player B scorer pairs (>=50%) ----------
    pf = os.path.join(HERE, "pairs", "pairs.json")
    if os.path.exists(pf) and soccer_events:
        pairs = json.load(open(pf)).get("pairs", [])
        ps = state.setdefault("pairs_sent", [])
        bteam = {}
        for lg, ev in soccer_events:
            for t in (ev["home"], ev["away"]): bteam.setdefault(t, []).append((lg, ev))
        utc = dt.datetime.now(dt.timezone.utc)
        # Sammy 11/7: only NOTIFY when both A and B are on that day's FC27 list (pairs/today_list.json, written with each daily list,
        # keyed by event id). Everything else is still tracked silently. Women's games never count (pairs are men's players).
        tlf = os.path.join(HERE, "pairs", "today_list.json")
        TL = (json.load(open(tlf)).get("events", {}) if os.path.exists(tlf) else {})
        def on_list(name, eid):
            return any(norm(name) == norm(x) or norm(name).split()[-1] == norm(x).split()[-1] and norm(name)[0] == norm(x)[0] for x in TL.get(str(eid), []))
        womens = lambda path: any(k in path for k in (".w.", "nwsl", "wchampions", "women", "esp.w", "fra.w", "ger.w", "ita.w"))
        silent_log = state.setdefault("pairs_silent", [])
        for p in pairs:
            for la, ea in bteam.get(p["A_team"], []):
                for lb, eb in bteam.get(p["B_team"], []):
                    ka = dt.datetime.fromisoformat(ea["date"].replace("Z", "+00:00")); kb = dt.datetime.fromisoformat(eb["date"].replace("Z", "+00:00"))
                    gap = (kb - ka).total_seconds() / 3600
                    need = 2.5 if any(x in la["path"] for x in ("uefa", ".fa", "cup")) else 2.0
                    if not (need <= gap <= 36) or eb["state"] != "pre": continue
                    if womens(la["path"]) or womens(lb["path"]): continue
                    both = on_list(p["A"], ea["id"]) and on_list(p["B"], eb["id"])
                    tag = f'{p["A"]}>{p["B"]}>{ea["id"]}'
                    txt = (f'{p["B"]} scores after {p["A"]} scores: {p["hits"]}/{p["n"]} ({p["rate"]*100:.0f}%) · {p["B"]} normally {p["B_base"]*100:.0f}%'
                           f'\nA: {ea["away"]} @ {ea["home"]} {ka.astimezone(PT):%a %-I:%M %p} · B: {eb["away"]} @ {eb["home"]} {kb.astimezone(PT):%a %-I:%M %p} PT')
                    if ea["state"] == "pre" and tag + ":pre" not in ps and not both:
                        silent_log.append({"tag": tag, "day": ka.astimezone(PT).strftime("%Y-%m-%d"), "A": p["A"], "B": p["B"], "note": "not both on today's list: tracked, no ping"})
                        ps.append(tag + ":pre")
                    if ea["state"] == "pre" and tag + ":pre" not in ps:
                        alerts.append((3, "🔗 Scorer pair today", txt + "\n→ A's game ends before B's kicks off. Wait for A to score, then bet B, or parlay both (small stakes: anytime goals are your weak market)."))
                        ps.append(tag + ":pre")
                    if ea["state"] == "post" and tag + ":post" not in ps:
                        sm = get(API + la["path"] + "/summary?event=" + ea["id"]) or {}
                        scored = False
                        for k in sm.get("keyEvents", []) or []:
                            if "goal" in ((k.get("type") or {}).get("text", "") + " " + (k.get("text") or "")).lower() and "own goal" not in (k.get("text") or "").lower():
                                for pa in k.get("participants", []) or []:
                                    nm = ((pa.get("athlete") or {}).get("displayName") or "")
                                    if nm and norm(nm).split()[-1] == norm(p["A"]).split()[-1]: scored = True
                        ps.append(tag + ":post")
                        if scored:
                            # shrink the pair rate toward B's normal rate (small samples) -> fair price
                            w8 = p["n"] / (p["n"] + 25)     # e.g. 29 cases -> trust the pair 54%, B's normal rate 46%
                            pr = w8 * p["rate"] + (1 - w8) * p["B_base"]
                            fair = american(pr)
                            add_flag({"key": f'{eb["date"][:10]}|GOAL|{p["B"]}', "created": now.strftime("%Y-%m-%d %H:%M PT"),
                                      "game": f'{eb["away"]} @ {eb["home"]}', "start": eb["date"], "espn_event": eb["id"], "league": lb["path"],
                                      "espn_id": None, "player": p["B"], "stat": "goal", "side": "anytime", "line": 0.5, "price": None,
                                      "fair": fair, "proj": None, "edge": 0, "units": 0.5, "sent": False, "result": None, "trigger": "pair",
                                      "signals": [f'{p["A"]} scored today', f'{p["B"]} scores after {p["A"]} scores: {p["hits"]}/{p["n"]} ({p["rate"]*100:.0f}%) vs his normal {p["B_base"]*100:.0f}%',
                                                  f'our honest estimate (small samples shrunk): {pr*100:.0f}% → TAKE at {fmt(fair)} or better',
                                                  "⚠ anytime goals are your weak market (7-9): 0.5u max; confirm he's in the XI (~1 hr before)"],
                                      "silent": not both})
                            if both: alerts.append((5, f'⚽ {p["A"]} scored → TAKE {p["B"]} to score', txt + f'\n✅ Trigger hit. Bet {p["B"]} anytime goal at {fmt(fair)} or better (0.5u, $5). Check he\'s in the starting XI first.'))
        state["pairs_sent"] = ps[-800:]; state["pairs_silent"] = silent_log[-500:]

    # ---------- ⚡ news-triggered prop re-price (instant, budget-capped) ----------
    if triggers and os.environ.get("SGO_API_KEY"):
        day = now.strftime("%Y-%m-%d")
        tb = state.setdefault("trig_budget", {})
        teams_now = {}
        for tms, note in triggers:
            for t in tms:
                if t in team_line and t not in teams_now:
                    ev = team_line[t][0]
                    st = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                    if 0 < (st - dt.datetime.now(dt.timezone.utc)).total_seconds() < 14 * 3600 and ev["sport"] == "nba":
                        teams_now[t] = note
        if teams_now and tb.get(day, 0) < CFG.get("trigger_budget_per_day", 25):
            try:
                sys.path.insert(0, os.path.join(HERE, "props"))
                import props as P
                games = {}
                for t, note in teams_now.items():
                    ev = team_line[t][0]; games.setdefault(ev["id"], (set(), note))[0].update({ev["home"], ev["away"]})
                for gid, (tms, note) in games.items():
                    if tb.get(day, 0) >= CFG.get("trigger_budget_per_day", 25): break
                    tb[day] = tb.get(day, 0) + 1
                    P.run(teams=tms, note=note)
            except Exception as e:
                print("trigger re-price failed", e)
        for k in [k for k in tb if k < day]: tb.pop(k)

    # ---------- 📒 daily silent "model only" report (9 AM PT) ----------
    afp0 = os.path.join(HERE, "props", "autoflags.json")
    if now.hour >= 9 and state.get("mo_report") != now.strftime("%Y-%m-%d") and os.path.exists(afp0):
        fl = json.load(open(afp0))
        def rec(rows):
            w_ = sum(1 for r in rows if r.get("result") == "Win"); l_ = sum(1 for r in rows if r.get("result") == "Loss")
            u_ = round(sum(r.get("net_units", 0) or 0 for r in rows if r.get("result")), 2)
            return f"{w_}-{l_} ({u_:+g}u)"
        yday = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        mo = [r for r in fl if r.get("silent") or r.get("trigger") == "model"]
        ed = [r for r in fl if not (r.get("silent") or r.get("trigger") == "model")]
        mo_y = [r for r in mo if r["start"][:10] <= yday and dt.datetime.fromisoformat(r["start"].replace("Z", "+00:00")).astimezone(PT).strftime("%Y-%m-%d") == yday]
        alerts.append((2, "📒 Daily Alert Pick report", f"Model-only picks (silent) yesterday: {rec(mo_y)} · all-time: {rec(mo)}\n"
                       f"Edge Alert Picks (the ones you get pinged for) all-time: {rec(ed)}\nFull list: Sammy's Bet Tracker → Alert Pick track record"))
        state["mo_report"] = now.strftime("%Y-%m-%d")

    # ---------- grade finished Alert Picks hourly (ESPN only, free) ----------
    if time.time() - state.get("last_grade", 0) > 3600 and os.path.exists(os.path.join(HERE, "props", "autoflags.json")):
        try:
            sys.path.insert(0, os.path.join(HERE, "props"))
            import props as P2
            P2.grade_flags(dt.datetime.now(dt.timezone.utc))
            try:
                import runpy; runpy.run_path(os.path.join(HERE, "grade", "patterns.py"))
            except Exception as e: print("patterns error", e)
            state["last_grade"] = time.time()
        except Exception as e:
            print("grading failed", e)

    # ---------- 🤖 Alert Picks from the prop model (props/autoflags.json) ----------
    afp = os.path.join(HERE, "props", "autoflags.json")
    if os.path.exists(afp):
        sent = set(state.setdefault("af_sent", [])); graded = set(state.setdefault("af_graded", []))
        SN = {"points": "points", "rebounds": "rebounds", "assists": "assists", "threePointersMade": "3-pointers", "points+rebounds+assists": "pts+reb+ast"}
        for x in json.load(open(afp)):
            nm = (f'{x["player"]} to score (anytime), take at {fmt(x["fair"])} or better' if x["stat"] == "goal" else f'{x["player"]} LIVE moneyline' if x["stat"] == "live_ml" else f'{x["player"]} {fmt(x["line"])} ({fmt(x["price"])})' if x["stat"] == "spread"
                  else f'{x["player"]} {x["side"].upper()} {x["line"]:g} {SN.get(x["stat"], x["stat"])} ({fmt(x["price"])})')
            if x.get("silent"):
                sent.add(x["key"]); graded.add(x["key"]) if x.get("result") else None
                continue
            if x["key"] not in sent and not x.get("result"):
                st = dt.datetime.fromisoformat(x["start"].replace("Z", "+00:00"))
                if st > dt.datetime.now(dt.timezone.utc) or x.get("trigger") == "live":
                    u = x["units"]
                    alerts.append((5 if x.get("trigger") == "news" else 4, "⚡🤖 Alert Pick: NEWS, bet before it moves" if x.get("trigger") == "news" else "🎯🤖 Alert Pick" if x.get("trigger") == "edge" else "🔴🤖 LIVE Alert Pick" if x.get("trigger") == "live" else "⚽🤖 Pair Alert Pick" if x.get("trigger") == "pair" else "📈🤖 Trend Alert Pick" if x.get("trigger") == "trend" else "🤖 Alert Pick (paper)", f'{nm} · {x["game"]}\n'
                                   + (f'Model {x["proj"]} vs line {x["line"]:g} (secondary check)\n' if x.get("proj") is not None else "")
                                   + "\n".join("• " + s for s in x["signals"])
                                   + f'\nSize: {u:g}u (${u*10:g}) · Code-only pick, NOT a Claude Pick. Claude confirms or kills it next scan.'))
                sent.add(x["key"])
            if x.get("result") and x["key"] not in graded:
                icon = {"Win": "✅", "Loss": "❌"}.get(x["result"], "➖")
                alerts.append((2, f"{icon} Alert Pick graded: {x['result']}", f'{nm}\nFinal: {x.get("final")} · {x.get("net_units", 0):+g}u'
                               + (f'\n{x["sweat"]["label"]} · margin {x["sweat"].get("margin")}' + (f' · {x["sweat"]["note"]}' if x["sweat"].get("note") else "") if (x.get("sweat") or {}).get("label") else "")))
                graded.add(x["key"])
        state["af_sent"] = sorted(sent)[-500:]; state["af_graded"] = sorted(graded)[-500:]


    # prune old events
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    for k in [k for k, v in wxc.items() if time.time() - v > 3 * 86400]:
        wxc.pop(k)
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

    log = state.setdefault("alert_log", [])
    for p, t, b in alerts:
        log.append({"at": now.strftime("%Y-%m-%d %H:%M PT"), "title": t, "body": b})
    state["alert_log"] = log[-300:]
    with open(os.path.join(HERE, "alerts.md"), "w") as f:
        f.write("# Watcher alert log (newest first)\n\n")
        for a in reversed(state["alert_log"]):
            f.write(f'**{a["at"]} · {a["title"]}**  \n' + a["body"].replace("\n", "  \n") + "\n\n")
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
