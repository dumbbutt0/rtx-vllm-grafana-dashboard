#!/usr/bin/env python3
"""Validate every panel in rtx-vllm.json by executing its queries through
Grafana's OWN query engine (/api/ds/query), not raw Prometheus. This exercises
the datasource-UID wiring, PromQL parsing, and returns actual data frames.

Usage: python3 grafana_panel_validate.py [base_url] [user:pass]
"""
import json, sys, urllib.request, urllib.parse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3001"
AUTH = sys.argv[2] if len(sys.argv) > 2 else "admin:admin"

d = json.load(open("dashboards/rtx-vllm.json", encoding="utf-8"))

panels = []
def walk(p):
    if p.get("type") == "row":
        panels.append(("ROW: " + p.get("title", ""), None))
    elif p.get("title"):
        exprs = [t["expr"] for t in p.get("targets", []) or [] if t.get("expr")]
        if exprs:
            panels.append((p.get("title", ""), exprs))
    for c in p.get("panels", []) or []:
        walk(c)
for p in d["panels"]:
    walk(p)

def run_query(expr):
    expr = expr.replace("$__rate_interval", "5m").replace("$__range", "1h")
    # substitute multi-node/GPU template vars with their "All" value (Grafana
    # does this at render time; the validator must mirror it).
    expr = expr.replace('"$host"', '".*"').replace("$host", ".*")
    expr = expr.replace('"$gpu"', '".*"').replace("$gpu", ".*")
    payload = {
        "queries": [{
            "refId": "A",
            "datasource": {"uid": "prometheus", "type": "prometheus"},
            "expr": expr,
            "instant": True,
        }],
        "from": "now-5m",
        "to": "now",
    }
    req = urllib.request.Request(
        BASE + "/api/ds/query",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Basic " + __import__("base64").b64encode(AUTH.encode()).decode()},
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:
        return ("ERROR", str(e))
    res = r.get("results", {}).get("A", {})
    if res.get("status") != 200:
        return ("ERROR", res.get("error", "status %s" % res.get("status")))
    frames = res.get("frames", [])
    # count data rows across frames: values live in frame["data"]["values"][0]
    rows = 0
    for f in frames:
        vals = (f.get("data", {}) or {}).get("values", []) or []
        if vals:
            first_col = vals[0]
            if isinstance(first_col, list):
                rows = max(rows, len(first_col))
    return ("OK" if rows > 0 else "EMPTY", rows)

# walk and validate
summary = {"HAS_DATA": 0, "EMPTY": 0, "ERROR": 0, "TOTAL": 0}
print("=== GRAFANA-ENGINE PANEL VALIDATION ===\n")
for title, exprs in panels:
    if exprs is None:
        print("\n" + title)
        continue
    best = ("EMPTY", 0)
    errs = []
    for e in exprs:
        st, detail = run_query(e)
        if st == "OK":
            best = ("OK", detail)
            break
        elif st == "ERROR":
            errs.append(detail)
    if best[0] == "OK":
        status = "HAS-DATA (%d)" % best[1]
        summary["HAS_DATA"] += 1
    elif errs:
        status = "ERROR: " + str(errs[0])[:60]
        summary["ERROR"] += 1
    else:
        status = "EMPTY"
        summary["EMPTY"] += 1
    summary["TOTAL"] += 1
    print("  [%-22s] %s" % (status, title))

print("\n=== SUMMARY ===")
print("  panels with data : %d" % summary["HAS_DATA"])
print("  panels empty     : %d" % summary["EMPTY"])
print("  panels in error  : %d" % summary["ERROR"])
print("  total            : %d" % summary["TOTAL"])
