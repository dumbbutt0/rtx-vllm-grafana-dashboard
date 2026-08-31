#!/usr/bin/env python3
"""
harness_tokens.py — Prometheus exporter for per-tool token usage.

Reads token usage from local AI harnesses and serves it as Prometheus metrics,
with an "active" flag so Grafana panels hide tools that aren't currently running.

  * Pi          ~/.pi/agent/sessions/*/*.jsonl      usage{input,output,cacheRead,...}
  * Codex       ~/.codex/sessions + /mnt/c/Users/<u>/.codex/sessions (CLI + desktop app)
                rollout-*.jsonl  payload.info.total_token_usage{...}
  * Hermes      state.db (SQLite)  sessions{input_tokens,...}

Metrics (gauges; labels harness, model, direction):
  harness_tokens_total{harness, model, direction}
  harness_cost_usd_total{harness, model}
  harness_last_activity_seconds{harness}
  harness_active{harness}   1 if recently active / running, else 0

Performance: per-file results are cached by (mtime, size); only changed files are
re-parsed. Codex files are read tail-first (cumulative totals live near the end),
so a re-scan of hundreds of files stays fast.

Usage:
  python3 harness_tokens.py --listen :9257 [--window 900] [--hermes-db PATH]
"""
import argparse, glob, json, os, sqlite3, time, subprocess, shutil, tempfile
import sys, traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

DEFAULT_WINDOW = 900
CACHE_FILE = os.path.expanduser("~/.cache/harness_tokens_cache.json")

# global file cache: path -> {"mtime_ns": .., "size": .., "result": ..}
CACHE = {}


def _load_cache():
    global CACHE
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as fh:
                CACHE = json.load(fh)
    except Exception:
        CACHE = {}


def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(CACHE, fh)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def now():
    return time.time()


def file_sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def cached(path, parse_fn):
    sig = file_sig(path)
    if sig is None:
        return None
    key = path
    ent = CACHE.get(key)
    if isinstance(ent, dict) and ent.get("sig") == list(sig):
        return ent.get("result")
    result = parse_fn(path)
    CACHE[key] = {"sig": list(sig), "result": result}
    return result


def tail_lines(path, n=200):
    """Yield up to n lines from the end of a file, without reading it all."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = 8192
            blocks = []
            remaining = size
            while remaining > 0 and len(blocks) < n:
                seek = max(0, remaining - chunk)
                f.seek(seek)
                data = f.read(remaining - seek)
                blocks.append(data)
                remaining = seek
            data = b"".join(reversed(blocks))
            lines = data.decode("utf-8", "replace").splitlines()
            return lines[-n:]
    except Exception:
        return []


def process_running(name):
    try:
        out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


def newest_mtime(paths):
    m = 0
    for p in paths:
        try:
            m = max(m, os.path.getmtime(p))
        except OSError:
            pass
    return m


# ---------------- collectors ----------------
# each returns (list_of (harness, model, totals_dict), last_activity)

def _pi_parse(path):
    d = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "cost": 0}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                u = obj.get("usage")
                if not u:
                    continue
                d["input"] += u.get("input", 0)
                d["output"] += u.get("output", 0)
                d["cacheRead"] += u.get("cacheRead", 0)
                d["cacheWrite"] += u.get("cacheWrite", 0)
                d["reasoning"] += u.get("reasoning", 0)
                d["cost"] += (u.get("cost") or {}).get("total", 0)
    except Exception:
        pass
    return d


def collect_pi():
    agg = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "cost": 0}
    last = 0
    base = os.path.expanduser("~/.pi/agent/sessions")
    if not os.path.isdir(base):
        return [("pi", "pi", agg)], 0   # no sessions dir yet → graceful empty
    files = glob.glob(base + "/**/*.jsonl", recursive=True)
    for f in files:
        last = max(last, newest_mtime([f]))
        d = cached(f, _pi_parse)
        if d:
            for k in agg:
                agg[k] += d[k]
    return [("pi", "pi", agg)], last


def _codex_parse(path):
    """Per-turn token usage for a rollout (sane numbers). Uses `last_token_usage`
    (the most recent turn's delta), NOT `total_token_usage` which is a cumulative
    counter that grows implausibly large on long-running desktop-app rollouts."""
    d = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "cost": 0}
    for line in tail_lines(path, 400):
        if "last_token_usage" not in line:
            continue
        try:
            obj = json.loads(line)
            payload = obj.get("payload", {})
            u = ((payload.get("info") or {}).get("last_token_usage")
                 or (payload.get("info") or {}).get("total_token_usage"))
            if u:
                d["input"] = u.get("input_tokens", d["input"])
                d["output"] = u.get("output_tokens", d["output"])
                d["cacheRead"] = u.get("cached_input_tokens", d["cacheRead"])
                d["cacheWrite"] = u.get("cache_write_input_tokens", d["cacheWrite"])
                d["reasoning"] = u.get("reasoning_output_tokens", d["reasoning"])
        except Exception:
            continue
    return d


def _windows_homes():
    """Yield candidate Windows user-profile roots visible from WSL (/mnt/c/Users/*).
    Used to auto-discover the Codex desktop app and Hermes state on the Windows
    host without hardcoding a username."""
    try:
        base = "/mnt/c/Users"
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if name.lower() in ("public", "default", "all users", "default user"):
                continue
            if os.path.isdir(p):
                yield p
    except OSError:
        return


def codex_roots():
    """Return all plausible Codex session roots: WSL home, every Windows user
    profile, plus archived_sessions — overridable via CODEX_HOME (colon-separated)."""
    roots = []
    env = os.environ.get("CODEX_HOME", "")
    for p in env.split(":") if env else []:
        if p:
            roots.append(os.path.join(p, "sessions"))
    roots.append(os.path.expanduser("~/.codex/sessions"))
    for win in _windows_homes():
        roots.append(os.path.join(win, ".codex", "sessions"))
    # archived_sessions under each candidate home
    homes = [os.path.expanduser("~/.codex")]
    homes += [os.path.join(w, ".codex") for w in _windows_homes()]
    for home in homes:
        roots.append(os.path.join(home, "archived_sessions"))
    # de-dup, keep order
    seen = set()
    out = []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def hermes_db_candidates():
    """Candidate Hermes state.db paths, in order, overridable via HERMES_HOME."""
    env = os.environ.get("HERMES_HOME", "")
    if env:
        yield os.path.join(env, "state.db")
    yield os.path.expanduser("~/.hermes/state.db")
    for win in _windows_homes():
        yield os.path.join(win, "AppData", "Local", "hermes", "state.db")


def collect_codex():
    files = []
    for r in codex_roots():
        if os.path.isdir(r):
            files.extend(glob.glob(r + "/**/*.jsonl", recursive=True))

    # "recent activity": only sum latest-turn tokens from rollouts touched in
    # the last 24h (keeps the number sane and meaningful).
    cutoff = now() - 24 * 3600
    recent = [f for f in files if newest_mtime([f]) > cutoff]
    last = newest_mtime(files)

    agg = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "cost": 0}
    for f in recent:
        d = cached(f, _codex_parse)
        if d:
            for k in agg:
                agg[k] += d[k]
    return [("codex", "codex", agg)], last


def _hermes_query(db_path):
    tmp = tempfile.mkdtemp(prefix="hermes-tok-")
    try:
        for suffix in ["", "-wal", "-shm"]:
            src = db_path + suffix
            if os.path.exists(src):
                shutil.copy(src, tmp + "/state.db" + suffix)
        con = sqlite3.connect("file:" + tmp + "/state.db?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute("""
            SELECT model, SUM(input_tokens), SUM(output_tokens),
                   SUM(cache_read_tokens), SUM(cache_write_tokens),
                   SUM(reasoning_tokens), SUM(estimated_cost_usd), MAX(last_activity_at)
            FROM sessions
            WHERE input_tokens > 0 OR output_tokens > 0
            GROUP BY model
        """)
        rows = cur.fetchall()
        con.close()
        return rows
    except Exception:
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def collect_hermes(db_path):
    if not db_path or not os.path.exists(db_path):
        return [], 0
    # Throttle: re-query the SQLite DB at most once per 60s (copying the WAL
    # across /mnt/c is the slow part; token totals don't change that fast).
    global _hermes_last_query, _hermes_cache
    if not hasattr(collect_hermes, "_t"):
        collect_hermes._t = 0
        collect_hermes._result = None
    if (now() - collect_hermes._t) < 60 and collect_hermes._result is not None:
        return collect_hermes._result
    rows = _hermes_query(db_path)
    out = []
    last = 0
    for model, inp, outp, cre, cwr, rs, cost, la in rows:
        m = model or "unknown"
        out.append((m, {"input": inp or 0, "output": outp or 0, "cacheRead": cre or 0,
                        "cacheWrite": cwr or 0, "reasoning": rs or 0, "cost": cost or 0}))
        if la:
            last = max(last, la)
    collect_hermes._t = now()
    collect_hermes._result = (out, last)
    return out, last


# ---------------- rendering ----------------
def esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# ---------------- Ollama (server — no token logs, only presence + loaded models) ----------------
def collect_ollama():
    """Returns (active, installed, loaded_names).
    active = 1 only when at least one model is loaded (actually serving)."""
    try:
        json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=3))
    except Exception:
        return 0, 0, []
    installed = 0
    loaded = []
    try:
        tags = json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3))
        installed = len(tags.get("models", []))
    except Exception:
        pass
    try:
        ps = json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=3))
        for m in ps.get("models", []):
            loaded.append(m.get("name", "unknown"))
    except Exception:
        pass
    return (1 if loaded else 0), installed, loaded


def ollama_lines(active, installed, loaded):
    lines = []
    lines.append("# HELP ollama_active 1 if Ollama is serving at least one loaded model.")
    lines.append("# TYPE ollama_active gauge")
    lines.append("ollama_active %d" % active)
    lines.append("# HELP ollama_models_installed Number of models installed in Ollama.")
    lines.append("# TYPE ollama_models_installed gauge")
    lines.append("ollama_models_installed %d" % installed)
    lines.append("# HELP ollama_loaded_models Number of models currently loaded.")
    lines.append("# TYPE ollama_loaded_models gauge")
    lines.append("ollama_loaded_models %d" % len(loaded))
    lines.append("# HELP ollama_model_loaded 1 if the model is currently loaded.")
    lines.append("# TYPE ollama_model_loaded gauge")
    for m in loaded:
        lines.append('ollama_model_loaded{model="%s"} 1' % esc(m))
    return lines


def render(entries, active, window):
    lines = []
    lines.append("# HELP harness_tokens_total Cumulative tokens per local AI harness.")
    lines.append("# TYPE harness_tokens_total gauge")
    lines.append("# HELP harness_cost_usd_total Cumulative estimated cost per harness.")
    lines.append("# TYPE harness_cost_usd_total gauge")
    lines.append("# HELP harness_last_activity_seconds Unix time of last recorded activity.")
    lines.append("# TYPE harness_last_activity_seconds gauge")
    lines.append("# HELP harness_active 1 if the harness is currently active (recent activity or running process).")
    lines.append("# TYPE harness_active gauge")

    seen_harness = set()
    for harness, model, d in entries:
        seen_harness.add(harness)
        for direction in ["input", "output", "cacheRead", "cacheWrite", "reasoning"]:
            lines.append('harness_tokens_total{harness="%s",model="%s",direction="%s"} %d'
                         % (esc(harness), esc(model), direction, d[direction]))
        lines.append('harness_cost_usd_total{harness="%s",model="%s"} %.6f'
                     % (esc(harness), esc(model), d["cost"]))

    for harness, la in active.items():
        lines.append('harness_last_activity_seconds{harness="%s"} %d' % (esc(harness), int(la)))

    proc_map = {"pi": "pi", "codex": "codex"}
    t0 = now()
    for harness, la in active.items():
        val = 0
        if la and (t0 - la) < window:
            val = 1
        if harness in proc_map and process_running(proc_map[harness]):
            val = 1
        lines.append('harness_active{harness="%s"} %d' % (esc(harness), val))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/metrics":
            self.send_response(404); self.end_headers(); return
        try:
            data = self.server.render_fn().encode()
        except Exception:
            # Never fail the scrape: on catastrophic error, return a valid
            # minimal body flagging the failure so Prometheus records it cleanly.
            traceback.print_exc()
            data = (
                "# HELP harness_scrape_error 1 when the collector could not render metrics.\n"
                "# TYPE harness_scrape_error gauge\n"
                "harness_scrape_error 1\n"
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-scrape; harmless

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default=":9257")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--hermes-db", default=None)
    args = ap.parse_args()

    hermes_db = args.hermes_db
    if not hermes_db:
        for cand in hermes_db_candidates():
            if os.path.exists(cand):
                hermes_db = cand
                break

    host, port = args.listen.rsplit(":", 1)
    port = int(port)

    _load_cache()

    SOURCES = ["pi", "codex", "hermes", "ollama"]

    def render_fn():
        entries = []
        active = {}
        source_ok = {s: True for s in SOURCES}

        def guarded(name, fn, *a):
            try:
                return fn(*a)
            except Exception:
                source_ok[name] = False
                traceback.print_exc()
                return None

        pe = guarded("pi", collect_pi)
        if pe:
            entries.extend(pe[0]); active["pi"] = pe[1]
        else:
            active["pi"] = 0

        ce = guarded("codex", collect_codex)
        if ce:
            entries.extend(ce[0]); active["codex"] = ce[1]
        else:
            active["codex"] = 0

        he = guarded("hermes", collect_hermes, hermes_db)
        if he:
            for model, d in he[0]:
                entries.append(("hermes", model, d))
            active["hermes"] = he[1]
        else:
            active["hermes"] = 0

        try:
            _save_cache()
        except Exception:
            pass

        out = render(entries, active, args.window)

        oa, oi, ol = guarded("ollama", collect_ollama) or (0, 0, [])
        out += "\n".join(ollama_lines(oa, oi, ol)) + "\n"
        out += 'harness_active{harness="ollama"} %d\n' % oa

        # Per-source health, so a silent failure is still visible in the dashboard.
        out += "# HELP harness_source_success 1 if this tool's data was read OK, 0 if it errored.\n"
        out += "# TYPE harness_source_success gauge\n"
        for s in SOURCES:
            out += 'harness_source_success{harness="%s"} %d\n' % (s, 1 if source_ok[s] else 0)

        return out

    # Warm the cache once before serving so the first scrape isn't a ~45s
    # blocking parse of hundreds of session files. A failure here must not
    # crash the service — log it and continue serving (render_fn will retry).
    print("warming cache (first collection pass)...")
    t0 = now()
    try:
        render_fn()
        print("warm-up complete in %.1fs" % (now() - t0))
    except Exception:
        traceback.print_exc()
        print("warm-up failed (will retry on first scrape)")

    srv = HTTPServer((host, port), Handler)
    srv.render_fn = render_fn
    print("harness_tokens serving on %s:%d" % (host, port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
