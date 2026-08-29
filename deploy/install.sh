#!/usr/bin/env bash
# Install the RTX vLLM monitoring stack (node_exporter, nvidia_gpu_exporter,
# Prometheus, Grafana) as user-level systemd services in WSL2. No root needed.
#
# One-time prerequisite (for vLLM only, not this script):
#   sudo apt-get update && sudo apt-get install -y build-essential
set -euo pipefail

# --- pinned versions ---
NODE_EXPORTER=1.12.1
NVIDIA_GPU_EXPORTER=1.14.0
PROMETHEUS=3.14.0
GRAFANA=13.2.0
ARCH=linux-amd64

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # deploy/
ROOT="$(dirname "$HERE")"                              # repo root
OPT="$HOME/opt"
BIN="$OPT/bin"
mkdir -p "$BIN" "$OPT/src"

echo "==> node_exporter ${NODE_EXPORTER}"
curl -fsSL -o /tmp/ne.tgz "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER}/node_exporter-${NODE_EXPORTER}.${ARCH}.tar.gz"
tar xzf /tmp/ne.tgz -C /tmp
cp "/tmp/node_exporter-${NODE_EXPORTER}.${ARCH}/node_exporter" "$BIN/"

echo "==> nvidia_gpu_exporter ${NVIDIA_GPU_EXPORTER} (nvidia-smi backend)"
curl -fsSL -o /tmp/ngx.tgz "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${NVIDIA_GPU_EXPORTER}/nvidia_gpu_exporter_${NVIDIA_GPU_EXPORTER}_linux_x86_64.tar.gz"
tar xzf /tmp/ngx.tgz -C /tmp
cp /tmp/nvidia_gpu_exporter "$BIN/"

echo "==> Prometheus ${PROMETHEUS}"
curl -fsSL -o /tmp/p.tgz "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS}/prometheus-${PROMETHEUS}.${ARCH}.tar.gz"
tar xzf /tmp/p.tgz -C /tmp
rm -rf "$OPT/prometheus" && mkdir -p "$OPT/prometheus"
cp -r "/tmp/prometheus-${PROMETHEUS}.${ARCH}/"* "$OPT/prometheus/"
mkdir -p "$OPT/prometheus/data"

echo "==> Grafana ${GRAFANA}"
curl -fsSL -o /tmp/g.tgz "https://dl.grafana.com/oss/release/grafana-${GRAFANA}.${ARCH}.tar.gz"
tar xzf /tmp/g.tgz -C /tmp
rm -rf "$OPT/grafana" && mkdir -p "$OPT/grafana"
cp -r "/tmp/grafana-v${GRAFANA}/"* "$OPT/grafana/"

echo "==> writing configs"
mkdir -p "$OPT/prometheus"
cp "$HERE/prometheus.yml" "$OPT/prometheus/prometheus.yml"

mkdir -p "$OPT/grafana/conf/provisioning/datasources" "$OPT/grafana/conf/provisioning/dashboards"
cp "$HERE/grafana/provisioning/datasources/prometheus.yml" "$OPT/grafana/conf/provisioning/datasources/prometheus.yml"
sed "s|__HOME__|$HOME|g" "$HERE/grafana/provisioning/dashboards/dashboards.yml" \
  > "$OPT/grafana/conf/provisioning/dashboards/dashboards.yml"

echo "==> installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$HERE/systemd/"*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now node-exporter nvidia-gpu-exporter prometheus grafana harness-tokens 2>/dev/null || \
  systemctl --user enable --now node-exporter nvidia-gpu-exporter prometheus grafana

echo
echo "Done. Endpoints:"
echo "  node_exporter         http://localhost:9100/metrics"
echo "  nvidia_gpu_exporter   http://localhost:9835/metrics"
echo "  Prometheus            http://localhost:9090"
echo "  Grafana               http://localhost:3001  (admin/admin)"
echo "  harness_tokens        http://localhost:9257/metrics  (Pi/Codex/Hermes/Ollama)"
echo
echo "Next: provision the dashboard JSON into Grafana, then set up vLLM (see README)."
