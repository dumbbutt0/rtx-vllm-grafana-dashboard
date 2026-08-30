#!/usr/bin/env python3
"""Build rtx-vllm.json from the upstream dgx-spark-vllm.json.

Adaptations for a consumer NVIDIA RTX (GeForce) GPU in WSL2:
  1. Relabel dgx_spark="true" -> rtx_gpu="true".
  2. Reconcile vLLM metric-name drift (verified against vllm==0.28.0 source).
  3. Remove GPU panels that don't exist on GeForce cards (tlimit, throttle, nvme).
  4. Add replacement GPU panels (power headroom, VRAM used/total, mem stat).
  5. Add a "GPU ACTIVITY" row (per-process compute contexts).
  6. Fix the request-hopper panel (num_requests_swapped was removed in V1).
"""
import json
import copy
import os
import re

# Resolve paths relative to the repo root so the script runs from any cwd.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "dashboards", "dgx-spark-vllm.json")
DST = os.path.join(REPO, "dashboards", "rtx-vllm.json")

d = json.load(open(SRC, encoding="utf-8"))

# --- 1 & 2: global label + metric drift replacements ---
# Verified against LIVE vllm==0.28.0 /metrics (2026-08-28). Prometheus auto-appends
# _total to Counter names, so the original dashboard's _total suffixes are CORRECT
# and must be preserved. Only two genuine renames occurred:
REPLACEMENTS = [
    ("dgx_spark=\"true\"", "rtx_gpu=\"true\""),
    # REAL vLLM V1 renames (verified against live /metrics):
    ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"),
    ("vllm:time_per_output_token_seconds", "vllm:request_time_per_output_token_seconds"),
    # leftover DGX branding in descriptive text (description + legend):
    ("DGX Spark/Qwen", "RTX/Qwen"),
    ("DGX energy", "GPU energy"),
    # BUG FIX: SM clock divided by 1e9 but kept "hertz" unit → rendered "210 mHz".
    # Drop the /1e9 so Grafana's native hertz formatter shows "2.10 GHz" correctly.
    ("nvidia_smi_clocks_current_sm_clock_hz{rtx_gpu=\"true\"} / 1e9", "nvidia_smi_clocks_current_sm_clock_hz{rtx_gpu=\"true\"}"),
    ("nvidia_smi_clocks_max_sm_clock_hz{rtx_gpu=\"true\"} / 1e9", "nvidia_smi_clocks_max_sm_clock_hz{rtx_gpu=\"true\"}"),
]


def apply_repl(s, repls):
    for old, new in repls:
        s = s.replace(old, new)
    return s


# Gate every vllm:* metric on the target being up, so the whole vLLM section
# hides the instant no model is serving (vllm counters would otherwise linger
# in Prometheus for up to ~5 min after vLLM stops).
#
# PromQL gotcha: `[...]` range selectors may ONLY follow a bare vector selector,
# so `rate((metric and gate)[5m])` is invalid ("ranges only allowed for vector
# selectors"). Two cases, handled in a single left-to-right regex pass:
#   1. range-vector functions rate()/increase()/… -> append the gate AFTER the call
#   2. bare gauges (vllm:X or vllm:X{labels})      -> wrap the selector with the gate
VLLM_SEL = r'vllm:[a-zA-Z0-9_:]+(?:\{[^}]*\})?'
_RANGE_FN = r'(?:rate|increase|irate|delta|idelta|deriv|predict_linear)'
_GATE = ' and on(instance, job) up{job="vllm"} == 1'

_gate_re = re.compile(
    r'(' + _RANGE_FN + r'\(\s*' + VLLM_SEL + r'\s*\[[^\]]*\]\s*\))'  # full range call
    r'|' + VLLM_SEL + r'(?!\s*\[)'                                     # bare selector (not a range)
)


def gate_vllm(expr):
    def repl(m):
        if m.group(1):  # range-vector function call → gate after the call
            return m.group(1) + _GATE
        # bare instant-vector selector → wrap
        return '(%s%s)' % (m.group(0), _GATE)
    return _gate_re.sub(repl, expr)


def walk_exprs(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                v = apply_repl(v, REPLACEMENTS)
                if k == "expr":
                    v = gate_vllm(v)
                obj[k] = v
            else:
                walk_exprs(v)
    elif isinstance(obj, list):
        for item in obj:
            walk_exprs(item)


walk_exprs(d)

# --- 3/4/5: GPU hardware row surgery ---
panels = d["panels"]
REMOVE_IDS = {108, 112, 115, 116, 117}  # tlimit x2, throttle x2, nvme

new_panels = []
for p in panels:
    if p.get("id") in REMOVE_IDS:
        continue
    # reposition the stats that had tlimit removed between them
    if p.get("id") == 109:  # GPU POWER -> x=6
        p["gridPos"]["x"] = 6
    if p.get("id") == 110:  # GPU UTILIZATION -> x=12
        p["gridPos"]["x"] = 12
    if p.get("id") == 118:  # GPU MEMORY/ENCODER -> move up to y=88
        p["gridPos"]["y"] = 88
    # rename the GPU hardware row
    if p.get("type") == "row" and "GPU HARDWARE" in (p.get("title") or ""):
        p["title"] = "RTX GPU HARDWARE (nvidia_gpu_exporter)"
    # fix request hopper (swapped removed in V1)
    if p.get("title") == "REQUEST HOPPER: RUNNING / WAITING / SWAPPED":
        p["targets"] = [t for t in p.get("targets", []) if "num_requests_swapped" not in (t.get("expr") or "")]
        p["title"] = "REQUEST HOPPER: RUNNING / WAITING"
    new_panels.append(p)

panels[:] = new_panels

# --- build new GPU panels by cloning existing templates (guarantees valid v41 schema) ---
by_id = {p["id"]: p for p in panels}
stat_tpl = by_id[107]   # GPU TEMPERATURE (stat)
ts_tpl = by_id[111]     # GPU TEMPERATURE OVER TIME (timeseries)
ts2_tpl = by_id[118]    # GPU MEMORY/ENCODER (timeseries, 2 targets)

max_id = max(p["id"] for p in panels)

def next_id():
    global max_id
    max_id += 1
    return max_id

def clone(src, title, grid, targets, unit=None, legend=None):
    p = copy.deepcopy(src)
    p["id"] = next_id()
    p["title"] = title
    p["gridPos"] = {"h": grid[0], "w": grid[1], "x": grid[2], "y": grid[3]}
    for t in p.get("targets", []):
        t["expr"] = ""  # clear; we rebuild targets below
    p["targets"] = []
    for i, (expr, leg) in enumerate(targets):
        ref = chr(ord("A") + i)
        p["targets"].append({"expr": expr, "refId": ref, "legendFormat": leg,
                             "editorMode": "code", "range": True})
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    return p

# New stat: GPU MEMORY USED
panels.append(clone(stat_tpl, "GPU MEMORY USED",
                    [6, 6, 18, 68],
                    [("nvidia_smi_memory_used_bytes{rtx_gpu=\"true\"}", "{{host_id}}")],
                    unit="decbytes"))

# New timeseries: POWER HEADROOM (%)
panels.append(clone(ts_tpl, "GPU POWER HEADROOM (%)",
                    [7, 12, 12, 74],
                    [("(1 - nvidia_smi_power_draw_watts{rtx_gpu=\"true\"} / nvidia_smi_power_limit_watts{rtx_gpu=\"true\"}) * 100", "{{host_id}}")],
                    unit="percent"))

# New timeseries: VRAM USED vs TOTAL
panels.append(clone(ts2_tpl, "VRAM: USED vs TOTAL",
                    [7, 12, 0, 88],
                    [("nvidia_smi_memory_used_bytes{rtx_gpu=\"true\"}", "{{host_id}} used"),
                     ("nvidia_smi_memory_total_bytes{rtx_gpu=\"true\"}", "{{host_id}} total")],
                    unit="decbytes"))

# --- 5: GPU ACTIVITY row ---
# insert after the last GPU panel (y=88 + 7 = 95)
row = {"id": next_id(), "type": "row", "title": "GPU ACTIVITY (per-process)",
       "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 95}, "panels": []}

panels.append(row)
panels.append(clone(stat_tpl, "ACTIVE GPU PROCESSES",
                    [5, 6, 0, 96],
                    [("nvidia_smi_compute_apps{rtx_gpu=\"true\"}", "{{host_id}}")],
                    unit="short"))
panels.append(clone(ts_tpl, "GPU PROCESS COUNT OVER TIME",
                    [7, 18, 6, 96],
                    [("nvidia_smi_compute_apps{rtx_gpu=\"true\"}", "{{host_id}}")],
                    unit="short"))

# --- 6: TOKEN USAGE BY TOOL row ---
# Two things the user wants glanceable:
#   1. WHAT apps are active right now  -> "ACTIVE APPS NOW" (count) + gated per-tool panels
#   2. HOW MANY tokens were used       -> "TOTAL TOKENS IN/OUT" (sum over active tools)
row = {"id": next_id(), "type": "row", "title": "APPS IN USE & TOKENS (harness_tokens)",
       "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 102}, "panels": []}
panels.append(row)

# Row 1: always-on summary stats (y=103, h=6, four panels of w=6)
panels.append(clone(stat_tpl, "ACTIVE APPS NOW",
                    [6, 6, 0, 103],
                    [("count(harness_active == 1)", "harnesses")],
                    unit="short"))
panels.append(clone(stat_tpl, "TOTAL TOKENS IN (active)",
                    [6, 6, 6, 103],
                    [("sum(harness_tokens_total{direction=\"input\"} and on(harness) harness_active == 1)", "{{harness}}")],
                    unit="short"))
panels.append(clone(stat_tpl, "TOTAL TOKENS OUT (active)",
                    [6, 6, 12, 103],
                    [("sum(harness_tokens_total{direction=\"output\"} and on(harness) harness_active == 1)", "{{harness}}")],
                    unit="short"))
panels.append(clone(stat_tpl, "OLLAMA MODELS LOADED",
                    [6, 6, 18, 103],
                    [("count(ollama_model_loaded)", "serving")],
                    unit="short"))

# Row 2: per-tool detail (hide-when-inactive). 4 columns of w=6:
#   pi / codex / hermes -> tokens + cost;  ollama -> loaded model (no token logs)
tools = [("pi", "Pi"), ("codex", "Codex"), ("hermes", "Hermes")]
for idx, (tool, label) in enumerate(tools):
    x = idx * 6
    panels.append(clone(ts_tpl, "%s — TOKENS (input/output)" % label,
                        [7, 6, x, 110],
                        [("harness_tokens_total{harness=\"%s\",direction=\"input\"} and on(harness) harness_active{harness=\"%s\"} == 1" % (tool, tool), "{{harness}} input"),
                         ("harness_tokens_total{harness=\"%s\",direction=\"output\"} and on(harness) harness_active{harness=\"%s\"} == 1" % (tool, tool), "{{harness}} output")],
                        unit="short"))
    panels.append(clone(stat_tpl, "%s — COST (USD)" % label,
                        [5, 6, x, 117],
                        [("harness_cost_usd_total{harness=\"%s\"} and on(harness) harness_active{harness=\"%s\"} == 1" % (tool, tool), "{{harness}}")],
                        unit="currencyUSD"))

# Ollama (server): no per-request token logs; show which model is serving.
# ollama_model_loaded only exists while a model is loaded -> hides when idle.
panels.append(clone(ts_tpl, "Ollama — SERVING",
                        [7, 6, 18, 110],
                        [("ollama_model_loaded", "{{model}}")],
                        unit="short"))
panels.append(clone(stat_tpl, "Ollama — MODELS INSTALLED",
                        [5, 6, 18, 117],
                        [("ollama_models_installed", "models")],
                        unit="short"))

# --- 7: remove dead sections from the previous (DGX) version ---
#  - the 4 cloud-equivalent cost cards (always $0.00; superseded by real token tracking)
#  - the 2 spec-decode panels (feature never enabled; always empty)
COST_LEGENDS = {"Claude Opus 5", "Claude Sonnet 5", "DeepSeek V4 Flash", "GPU energy"}

def is_cost_card(p):
    if p.get("title"):
        return False
    return any((t.get("legendFormat") or "") in COST_LEGENDS for t in p.get("targets", []) or [])

def is_spec_decode(p):
    return "SPEC DECODE" in (p.get("title") or "")

d["panels"] = [p for p in d["panels"] if not is_cost_card(p) and not is_spec_decode(p)]

# reflow: compact the grid gap-free (removing the top cards left holes)
_panels = d["panels"]
_panels.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
_reflowed = []
_i = 0
_new_y = 0
while _i < len(_panels):
    _y = _panels[_i]["gridPos"]["y"]
    _row = []
    _j = _i
    while _j < len(_panels) and _panels[_j]["gridPos"]["y"] == _y:
        _row.append(_panels[_j]); _j += 1
    _i = _j
    _h = max(p["gridPos"]["h"] for p in _row)
    for p in _row:
        p["gridPos"]["y"] = _new_y
        _reflowed.append(p)
    _new_y += _h
d["panels"] = _reflowed

# --- 8: COST TRACKING at the top (replaces the removed cloud cost cards) ---
# Real per-tool spend from harness_cost_usd_total — cumulative (a ledger), so it
# stays visible even when a tool is idle (you want total spend, not "is it running").
_cost_row = {"id": next_id(), "type": "row", "title": "COST TRACKING (per tool)",
             "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}, "panels": []}
_cost_defs = [
    ("Pi COST", 'harness_cost_usd_total{harness="pi"}', "{{harness}}"),
    ("Codex COST", 'harness_cost_usd_total{harness="codex"}', "{{harness}}"),
    ("Hermes COST", 'harness_cost_usd_total{harness="hermes"}', "{{harness}}"),
    ("TOTAL COST (all tools)", 'sum(harness_cost_usd_total)', "total"),
]
_cost_stats = []
for _i, (_t, _expr, _leg) in enumerate(_cost_defs):
    _p = clone(stat_tpl, _t, [6, 6, _i * 6, 1], [(_expr, _leg)], unit="currencyUSD")
    # drop the temperature thresholds inherited from the GPU-temp stat template
    _p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"color": "#76B900", "value": None}]}
    _cost_stats.append(_p)

# --- 8b: LOCAL MODEL row (which model is serving + token tracker) ---
_model_row = {"id": next_id(), "type": "row", "title": "LOCAL MODEL (vLLM / Ollama)",
              "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 7}, "panels": []}

def _model_name_stat(title, expr, legend):
    # a stat that renders the series NAME (the model) as its text, not a number
    _p = clone(stat_tpl, title, [6, 6, 0, 8], [(expr, legend)])
    _p["options"]["textMode"] = "name"
    _p["options"]["reduceOptions"] = {"calcs": ["lastNotNull"], "fields": "", "values": False}
    return _p

_model_panels = [
    # vLLM: model name (basename), hides when vLLM is down (no series)
    _model_name_stat("VLLM MODEL",
        gate_vllm('label_replace(vllm:num_requests_running, "model", "$1", "model_name", ".*/([^/]+)$")'),
        "{{model}}"),
    # vLLM token tracker (cumulative) — gated so it hides when vLLM stops
    clone(stat_tpl, "VLLM TOKENS IN", [6, 6, 0, 8],
          [(gate_vllm("vllm:prompt_tokens_total"), "{{model}}")], unit="short"),
    clone(stat_tpl, "VLLM TOKENS OUT", [6, 6, 0, 8],
          [(gate_vllm("vllm:generation_tokens_total"), "{{model}}")], unit="short"),
    # Ollama: loaded model name, hides when no model loaded
    _model_name_stat("OLLAMA MODEL", "ollama_model_loaded", "{{model}}"),
]
for _i, _p in enumerate(_model_panels):
    _p["gridPos"] = {"h": 6, "w": 6, "x": _i * 6, "y": 8}

# shift everything down by the cost section + local-model section height
_SHIFT = 14
for _p in d["panels"]:
    _p["gridPos"]["y"] += _SHIFT

d["panels"] = [_cost_row] + _cost_stats + [_model_row] + _model_panels + d["panels"]

# --- metadata ---
d["title"] = "NVIDIA RTX GPU — vLLM Throughput, GPU Hardware, Activity & Tool Tokens"
if d.get("tags"):
    d["tags"] = [t for t in d["tags"] if t != "dgx-spark"] + ["rtx", "vllm", "gpu-activity"]
else:
    d["tags"] = ["rtx", "vllm", "gpu-activity"]

json.dump(d, open(DST, "w", encoding="utf-8"), indent=2)
print("wrote", DST)

# --- validation report ---
d2 = json.load(open(DST, encoding="utf-8"))
txt = json.dumps(d2)
print("dgx_spark remaining:", txt.count("dgx_spark"))
print("rtx_gpu present:", txt.count("rtx_gpu"))
import re
panels2 = d2["panels"]
print("total panels:", len(panels2))
rows = [p["title"] for p in panels2 if p.get("type") == "row"]
print("rows:", rows)
# residual drifted metric names that should be gone
for stale in ["gpu_cache_usage_perc", "time_per_output_token_seconds", "_total", "num_requests_swapped"]:
    n = txt.count(stale)
    if n:
        print("WARN: residual %r x%d" % (stale, n))
