#!/usr/bin/env python3
"""Data relay: Claude writes relay/request.json, GitHub fetches each URL, results go to relay/out/.
ESPN web pages are reduced to their embedded JSON (page.content); API URLs are saved as JSON."""
import json, os, re, sys, time, urllib.request, hashlib
HERE = os.path.dirname(os.path.abspath(__file__))
req = json.load(open(os.path.join(HERE, "request.json")))
out_dir = os.path.join(HERE, "out"); os.makedirs(out_dir, exist_ok=True)
for f in os.listdir(out_dir):
    os.remove(os.path.join(out_dir, f))
SGO = os.environ.get("SGO_API_KEY", "")
res = {}
for item in req.get("items", []):
    name, url = item["name"], item["url"]
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    if "sportsgameodds.com" in url and SGO: hdr["x-api-key"] = SGO
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        if "__espnfitt__" in body:
            m = re.search(r"__espnfitt__'\]=(\{.*?\});</script>", body, re.S)
            data = json.loads(m.group(1))["page"]["content"]
        else:
            try: data = json.loads(body)
            except Exception: data = {"text": body[:200000]}
        json.dump(data, open(os.path.join(out_dir, name + ".json"), "w"))
        res[name] = "ok"
    except Exception as e:
        res[name] = "ERR " + str(e)[:200]
    time.sleep(item.get("sleep", 0.4))
json.dump({"id": req.get("id"), "done": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": res}, open(os.path.join(out_dir, "_status.json"), "w"), indent=1)
print(res)
