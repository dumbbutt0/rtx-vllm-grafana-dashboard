#!/usr/bin/env python3
"""Offline release-integrity checks. No running Grafana or GPU is required."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
errors = []

def require(condition, message):
    if not condition:
        errors.append(message)

dashboard_path = ROOT / "dashboards" / "rtx-vllm.json"
dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
panels = dashboard.get("panels", [])
ids = [p.get("id") for p in panels if p.get("id") is not None]
require(len(ids) == len(set(ids)), "dashboard panel IDs must be unique")
require(all({"h", "w", "x", "y"} <= set(p.get("gridPos", {})) for p in panels),
        "every top-level panel must have a complete gridPos")
dashboard_text = dashboard_path.read_text(encoding="utf-8")
require("dgx_spark" not in dashboard_text, "generated RTX dashboard contains stale dgx_spark selectors")
require('"uid": "prometheus"' in dashboard_text, "dashboard must use the provisioned prometheus UID")

prometheus = (ROOT / "deploy" / "prometheus.yml").read_text(encoding="utf-8")
for name in ("vllm.yml", "node-exporter.yml", "gpu-exporter.yml"):
    require(f'targets/{name}' in prometheus, f"Prometheus config does not reference targets/{name}")
    require((ROOT / "deploy" / "targets" / name).is_file(), f"missing deploy/targets/{name}")

installer = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
for fragment in (
    'install -m 0644 "$HERE/targets/"*.yml',
    'install -m 0644 "$ROOT/dashboards/rtx-vllm.json"',
    'systemctl --user is-active --quiet',
    '127.0.0.1:9257',
):
    require(fragment in installer, f"installer missing required release wiring: {fragment}")

services = list((ROOT / "deploy" / "systemd").glob("*.service"))
for service in services:
    text = service.read_text(encoding="utf-8")
    if "__REPO_ROOT__" in text:
        require(service.name == "harness-tokens.service",
                f"unexpected unresolved template token in {service.name}")
for service_name in ("node-exporter.service", "nvidia-gpu-exporter.service", "prometheus.service", "grafana.service"):
    text = (ROOT / "deploy" / "systemd" / service_name).read_text(encoding="utf-8")
    require("127.0.0.1" in text, f"{service_name} must bind explicitly to loopback")

require((ROOT / "package-lock.json").is_file(), "package-lock.json is required for reproducible validation")

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)
print(f"release checks passed: {len(panels)} top-level panels, {len(services)} service units")
