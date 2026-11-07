"""Sweat meter: how cleanly did a bet win (or how close did it lose)?
Pure functions on an ESPN summary JSON. Used by the watcher (Alert Picks) and by Claude's scans (Sammy's bets, Claude Picks, Suggested Bets).

bet = {"sport": "nba"|"ncaab"|"nfl"|"ncaaf"|"soccer", "market": "points"|"rebounds"|"assists"|"threePointersMade"|"points+rebounds+assists"
       |"spread"|"total"|"ml"|"team_total"|"goal"|"sot"|..., "side": "over"|"under"|"home"|"away", "line": float, "player_id": str|None}
returns {"margin", "margin_rel", "locked_at" (0-1 share of game when it was locked for good, None if it never locked early),
         "pct_winning" (share of game time the bet was in a winning spot), "cashed_min" (real minutes from tip to lock), "label", "note"}
"""
import datetime as dt
PER = {"nba": (4, 12), "ncaab": (2, 20), "wnba": (4, 10), "nfl": (4, 15), "ncaaf": (4, 15), "soccer": (2, 45)}
TOL = {"points": 2, "rebounds": 1, "assists": 1, "threePointersMade": 0.5, "points+rebounds+assists": 2,
       "spread": 2, "total": 3, "ml": 2, "team_total": 2, "goal": 0, "sot": 0.5}

def _clock(p):
    try:
        m, s = (p.get("clock", {}).get("displayValue") or "0:00").split(":")
        return int(m) + float(s) / 60
    except Exception:
        return 0.0

def _frac(sport, period, clock_left):
    n, ln = PER.get(sport, (4, 12))
    el = (period - 1) * ln + (ln - clock_left)
    return min(1.0, max(0.0, el / (n * ln)))

def _wall(p):
    w = p.get("wallclock")
    return dt.datetime.fromisoformat(w.replace("Z", "+00:00")) if w else None

def _player_timeline(plays, sport, pid, market):
    """cumulative value of the stat after each play -> [(frac, value, wallclock)]"""
    v = 0.0; out = []
    for p in plays:
        parts = [((x.get("athlete") or {}).get("id")) for x in (p.get("participants") or [])]
        if pid not in parts: continue
        t = (p.get("type") or {}).get("text", "").lower(); add = 0
        sv = p.get("scoreValue") or 0
        if p.get("scoringPlay") and parts and parts[0] == pid:
            if market in ("points", "points+rebounds+assists"): add += sv
            if market == "threePointersMade" and sv == 3: add += 1
        if "rebound" in t and parts and parts[0] == pid and market in ("rebounds", "points+rebounds+assists"): add += 1
        if p.get("scoringPlay") and len(parts) > 1 and parts[1] == pid and market in ("assists", "points+rebounds+assists"): add += 1
        if add:
            v += add
            out.append((_frac(sport, (p.get("period") or {}).get("number", 1), _clock(p)), v, _wall(p)))
    return out

def _score_timeline(plays, sport):
    out = []; last = None
    for p in plays:
        h, a = p.get("homeScore"), p.get("awayScore")
        if h is None or a is None: continue
        if (h, a) != last:
            out.append((_frac(sport, (p.get("period") or {}).get("number", 1), _clock(p)), float(h), float(a), _wall(p)))
            last = (h, a)
    return out

def _label(win, margin, market, locked_at, line):
    tol = TOL.get(market, 1)
    rel = abs(margin) / max(abs(line), 1) if line else None
    if win is None: return "➖ Push"
    if not win: return "💀 Bad beat" if abs(margin) <= tol else "❌ Clear loss"
    close = margin <= tol or (rel is not None and rel < 0.06)
    late = locked_at is None or locked_at >= 0.9
    if close and late: return "🔴 Sweat"
    if (locked_at is not None and locked_at <= 0.75) or (rel is not None and rel >= 0.2 and margin > 2 * tol): return "🟢 Easy"
    return "🟡 Normal" if not close else "🔴 Sweat"

def sweat(summary, bet):
    sport = bet["sport"]; m = bet["market"]; side = bet["side"]; line = float(bet.get("line") or 0)
    plays = summary.get("plays") or []
    comp = (summary.get("header", {}).get("competitions") or [{}])[0]
    tip = None
    try: tip = dt.datetime.fromisoformat(comp.get("date").replace("Z", "+00:00"))
    except Exception: pass
    res = {"margin": None, "margin_rel": None, "locked_at": None, "pct_winning": None, "cashed_min": None, "label": None, "note": ""}
    if m in ("spread", "ml", "total", "team_total"):
        tl = _score_timeline(plays, sport)
        if not tl:   # football: use scoringPlays
            tl = [(_frac(sport, (p.get("period") or {}).get("number", 1), _clock(p)), float(p.get("homeScore", 0)), float(p.get("awayScore", 0)), None)
                  for p in summary.get("scoringPlays", [])]
        sc = {c.get("homeAway"): float(c.get("score", 0) or 0) for c in comp.get("competitors", [])}
        if len(sc) < 2: return res
        def val(h, a):
            if m == "total": return (h + a - line) if side == "over" else (line - h - a)
            if m == "team_total":
                t = h if bet.get("team_side") == "home" else a
                return (t - line) if side == "over" else (line - t)
            mine, oth = (h, a) if side == "home" else (a, h)
            return mine - oth + (line if m == "spread" else 0)
        final = val(sc["home"], sc["away"])
        res["margin"] = round(final, 1)
        # share of game time in a winning spot + moment it went winning for good
        prev_f, prev_v, win_time, lock = 0.0, val(0, 0), 0.0, None
        for f, h, a, w in tl:
            if prev_v > 0: win_time += f - prev_f
            v = val(h, a)
            if v > 0 and prev_v <= 0: lock = (f, w)
            if v <= 0: lock = None
            prev_f, prev_v = f, v
        if prev_v > 0: win_time += 1 - prev_f
        res["pct_winning"] = round(win_time, 2)
        if final > 0 and lock and not (m == "total" and side == "under") and not (m == "team_total" and side == "under"):
            res["locked_at"] = round(lock[0], 2)
            if lock[1] and tip: res["cashed_min"] = round((lock[1] - tip).total_seconds() / 60)
        win = None if final == 0 else final > 0
    elif m == "goal":
        mins = []
        for k in summary.get("keyEvents", []) or []:
            tx = ((k.get("type") or {}).get("text", "") + " " + (k.get("text") or "")).lower()
            if "goal" in tx and "own goal" not in tx:
                for pa in k.get("participants", []) or []:
                    if ((pa.get("athlete") or {}).get("id")) == bet.get("player_id"):
                        try: mins.append(int(str((k.get("clock") or {}).get("displayValue", "90")).split("'")[0].split("+")[0]))
                        except Exception: mins.append(90)
        win = bool(mins); res["margin"] = len(mins) - 0.5
        if mins: res["locked_at"] = round(min(mins) / 90, 2); res["note"] = f"scored {min(mins)}'"
    else:   # player stat prop
        tl = _player_timeline(plays, sport, bet.get("player_id"), m)
        final = tl[-1][1] if tl else 0.0
        if bet.get("final") is not None: final = float(bet["final"])   # trust box score
        d = (final - line) if side == "over" else (line - final)
        res["margin"] = round(d, 1); win = None if d == 0 else d > 0
        if side == "over" and win:
            hit = next(((f, w) for f, v, w in tl if v > line), None)
            if hit:
                res["locked_at"] = round(hit[0], 2)
                if hit[1] and tip: res["cashed_min"] = round((hit[1] - tip).total_seconds() / 60)
        if side == "under" and tl:
            # how long it stayed comfortably under: share of game before the player got within tolerance of the line
            near = next((f for f, v, w in tl if v >= line - TOL.get(m, 1)), None)
            res["pct_winning"] = round(near if near is not None else 1.0, 2)
    res["margin_rel"] = round(res["margin"] / abs(line), 3) if res["margin"] is not None and line else None
    res["label"] = _label(win, res["margin"] or 0, m, res["locked_at"], line)
    if res["locked_at"] is not None:
        res["note"] = (res["note"] + " " if res["note"] else "") + f"locked {int(res['locked_at']*100)}% into the game"
    return res


# ---------- reasoning tags: which line of thinking produced the pick ----------
TAGS = [
    ("📈 20+ pt win trend", ("20-pt win", "pt win", "coming off a")),
    ("⚡ News not priced", ("ruled out", "news", "hasn't moved", "unpriced")),
    ("🔁 Role up (teammate out)", ("teammates out", "likely starts", "role up", "role-up")),
    ("⏱ 38+ min last night", ("38+ min", "min last night", "heavy minutes")),
    ("📊 Recent form (L5)", ("last 5", "l5")),
    ("🤖 Model projection", ("model", "proj")),
    ("📚 Fits your history", ("fits your history", "your history", "best market", "strongest market")),
    ("💸 Line move our way", ("moved our way", "market moving our way", "line moved")),
    ("🧤 Keeper / lineup change", ("keeper", "goalkeeper", "backup")),
    ("🎯 Shots-on-target rate", ("shot on target", "shots on target", "sot")),
    ("⚽ Scorer pair", ("scored →", "pair")),
    ("🔴 Live: fav trailing", ("trailing", "down 10", "live")),
    ("🧪 Blowout spot (untested)", ("blowout",)),
    ("😤 Revenge / motivation", ("revenge", "motivation")),
    ("🛌 Rest / schedule spot", ("b2b", "back-to-back", "rested", "rest")),
    ("🩹 Opponent injuries", ("out (", "defenders out", "missing")),
]
def tags_for(text):
    t = (text or "").lower()
    out = [name for name, keys in TAGS if any(k in t for k in keys)]
    return out or ["❔ Other / gut"]
