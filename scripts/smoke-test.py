#!/usr/bin/env python3
"""Live post-install smoke test for the five-service monitoring stack."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

ERRORS: list[str] = []


def fetch(url: str, contains: str | None = None) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read()
            if response.status != 200:
                ERRORS.append(f"{url}: HTTP {response.status}")
            if contains is not None and contains.encode() not in body:
                ERRORS.append(f"{url}: response did not contain {contains!r}")
            return body
    except (OSError, urllib.error.URLError) as exc:
        ERRORS.append(f"{url}: {exc}")
        return b""


fetch("http://127.0.0.1:9100/metrics", "node_exporter_build_info")
fetch("http://127.0.0.1:9835/metrics", "nvidia_smi_")
fetch("http://127.0.0.1:9257/metrics", "harness_source_success")
fetch("http://127.0.0.1:9090/-/ready", "Prometheus")
grafana_body = fetch("http://127.0.0.1:3001/api/health")
if grafana_body:
    try:
        health = json.loads(grafana_body)
        if health.get("database") != "ok":
            ERRORS.append(f"Grafana database health is {health.get('database')!r}")
    except (ValueError, TypeError) as exc:
        ERRORS.append(f"Grafana health response is not valid JSON: {exc}")

targets_body = fetch("http://127.0.0.1:9090/api/v1/targets")
if targets_body:
    try:
        payload = json.loads(targets_body)
        active = payload["data"]["activeTargets"]
        required_jobs = {"node-exporter", "gpu-exporter", "harness-tokens"}
        states: dict[str, list[str]] = {}
        for target in active:
            job = target.get("labels", {}).get("job", "unknown")
            states.setdefault(job, []).append(target.get("health", "unknown"))
        for job in sorted(required_jobs):
            health = states.get(job, [])
            if not health:
                ERRORS.append(f"Prometheus has no active target for required job {job}")
            elif any(state == "down" for state in health):
                ERRORS.append(f"Prometheus job {job} has a down target: {health}")
            elif any(state == "unknown" for state in health):
                print(f"required target {job}: reachable directly; awaiting first Prometheus scrape")
        for job in ("vllm", "claude-code"):
            health = states.get(job, [])
            status = ",".join(health) if health else "not configured"
            print(f"optional target {job}: {status}")
    except (KeyError, TypeError, ValueError) as exc:
        ERRORS.append(f"Prometheus targets response is invalid: {exc}")

if ERRORS:
    for error in ERRORS:
        print(f"FAIL: {error}", file=sys.stderr)
    raise SystemExit(1)

print("PASS: monitoring stack is healthy")
