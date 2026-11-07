"""Which reasoning patterns produce EASY wins? Reads graded Alert Picks (props/autoflags.json) + grade/ledger.json
(Sammy's bets, Claude Picks, Suggested Bets graded by Claude) -> grade/patterns.json + grade/patterns.md"""
import json, os, collections
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
rows = []
af = os.path.join(ROOT, "props", "autoflags.json")
for x in (json.load(open(af)) if os.path.exists(af) else []):
    sw = x.get("sweat") or {}
    if not sw.get("label") or not x.get("result") or str(x["result"]).startswith("Void"): continue
    rows.append({"type": "🤖 Alert Pick" + (" (silent)" if x.get("silent") else ""), "pick": f'{x["player"]} {x["side"]} {x.get("line")} {x["stat"]}',
                 "date": x["start"][:10], "result": x["result"], "units": x.get("net_units", 0), "tags": x.get("tags") or [], **{k: sw.get(k) for k in ("label", "margin", "margin_rel", "locked_at", "cashed_min")}})
lf = os.path.join(HERE, "ledger.json")
rows += [r for r in (json.load(open(lf)) if os.path.exists(lf) else []) if r.get("label")]
def summ(rs):
    n = len(rs); w = sum(1 for r in rs if r["result"] == "Win"); l = sum(1 for r in rs if r["result"] == "Loss")
    lab = collections.Counter(r["label"] for r in rs)
    locks = [r["locked_at"] for r in rs if r.get("locked_at") is not None]
    rel = [r["margin_rel"] for r in rs if r.get("margin_rel") is not None]
    return {"n": n, "W": w, "L": l, "easy": lab["🟢 Easy"], "normal": lab["🟡 Normal"], "sweat": lab["🔴 Sweat"], "bad_beat": lab["💀 Bad beat"], "clear_loss": lab["❌ Clear loss"],
            "easy_pct": round(lab["🟢 Easy"] / n, 2) if n else 0, "avg_lock": round(sum(locks) / len(locks), 2) if locks else None,
            "avg_margin_rel": round(sum(rel) / len(rel), 3) if rel else None, "units": round(sum(r.get("units") or 0 for r in rs), 2)}
by_tag = collections.defaultdict(list); by_type = collections.defaultdict(list)
for r in rows:
    by_type[r["type"]].append(r)
    for t in r["tags"]: by_tag[t].append(r)
out = {"all": summ(rows), "by_tag": {t: summ(v) for t, v in by_tag.items()}, "by_type": {t: summ(v) for t, v in by_type.items()}, "rows": rows}
json.dump(out, open(os.path.join(HERE, "patterns.json"), "w"), indent=1)
L = ["# 🧘 Easy wins vs sweats", "", "🟢 Easy = locked by 75% of the game or won by 20%+ of the line · 🟡 Normal · 🔴 Sweat = won by a hair late · 💀 Bad beat = lost by a hair · ❌ Clear loss", "",
     "## By reasoning pattern (sorted by easy-win rate, min 1 graded)", "", "| Pattern | n | W-L | 🟢 | 🟡 | 🔴 | 💀 | ❌ | Easy % | Avg lock | Avg margin | Units |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
for t, s in sorted(out["by_tag"].items(), key=lambda kv: (-kv[1]["easy_pct"], -kv[1]["n"])):
    L.append(f'| {t} | {s["n"]} | {s["W"]}-{s["L"]} | {s["easy"]} | {s["normal"]} | {s["sweat"]} | {s["bad_beat"]} | {s["clear_loss"]} | {int(s["easy_pct"]*100)}% | '
             f'{(str(int(s["avg_lock"]*100))+"%") if s["avg_lock"] is not None else "—"} | {(f"{s["avg_margin_rel"]*100:+.0f}%") if s["avg_margin_rel"] is not None else "—"} | {s["units"]:+g}u |')
L += ["", "_Small samples lie: treat any pattern with under 15 graded plays as a hint, not a verdict._"]
open(os.path.join(HERE, "patterns.md"), "w").write("\n".join(L))
print(out["all"])
