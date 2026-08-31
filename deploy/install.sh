#!/usr/bin/env bash
# Install the RTX vLLM monitoring stack as user-level systemd services in WSL2.
set -euo pipefail

NODE_EXPORTER=1.12.1
NVIDIA_GPU_EXPORTER=1.14.0
PROMETHEUS=3.14.0
GRAFANA=13.2.0
ARCH=linux-amd64

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OPT="$HOME/opt"
BIN="$OPT/bin"
PROMETHEUS_HOME="$OPT/prometheus-${PROMETHEUS}"
PROMETHEUS_CONFIG="$OPT/prometheus-config"
PROMETHEUS_DATA="$OPT/prometheus-data"
GRAFANA_HOME="$OPT/grafana-${GRAFANA}"
SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_CONFIG_DIR="$HOME/.config/rtx-vllm-grafana"
TMP_DIR="$(mktemp -d -t rtx-monitoring-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

for command in curl tar systemctl sed python3; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

mkdir -p "$BIN" "$OPT/src" "$SYSTEMD_DIR" "$APP_CONFIG_DIR"

echo "==> node_exporter ${NODE_EXPORTER}"
curl -fsSL -o "$TMP_DIR/node-exporter.tgz" "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER}/node_exporter-${NODE_EXPORTER}.${ARCH}.tar.gz"
tar xzf "$TMP_DIR/node-exporter.tgz" -C "$TMP_DIR"
install -m 0755 "$TMP_DIR/node_exporter-${NODE_EXPORTER}.${ARCH}/node_exporter" "$BIN/node_exporter"

echo "==> nvidia_gpu_exporter ${NVIDIA_GPU_EXPORTER}"
curl -fsSL -o "$TMP_DIR/nvidia-gpu-exporter.tgz" "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${NVIDIA_GPU_EXPORTER}/nvidia_gpu_exporter_${NVIDIA_GPU_EXPORTER}_linux_x86_64.tar.gz"
tar xzf "$TMP_DIR/nvidia-gpu-exporter.tgz" -C "$TMP_DIR"
install -m 0755 "$TMP_DIR/nvidia_gpu_exporter" "$BIN/nvidia_gpu_exporter"

echo "==> Prometheus ${PROMETHEUS}"
curl -fsSL -o "$TMP_DIR/prometheus.tgz" "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS}/prometheus-${PROMETHEUS}.${ARCH}.tar.gz"
tar xzf "$TMP_DIR/prometheus.tgz" -C "$TMP_DIR"
mkdir -p "$PROMETHEUS_HOME" "$PROMETHEUS_CONFIG/targets" "$PROMETHEUS_DATA"
cp -R "$TMP_DIR/prometheus-${PROMETHEUS}.${ARCH}/." "$PROMETHEUS_HOME/"
install -m 0644 "$HERE/prometheus.yml" "$PROMETHEUS_CONFIG/prometheus.yml"
install -m 0644 "$HERE/targets/"*.yml "$PROMETHEUS_CONFIG/targets/"
ln -sfn "$PROMETHEUS_HOME" "$OPT/prometheus-current"

echo "==> Grafana ${GRAFANA}"
curl -fsSL -o "$TMP_DIR/grafana.tgz" "https://dl.grafana.com/oss/release/grafana-${GRAFANA}.${ARCH}.tar.gz"
tar xzf "$TMP_DIR/grafana.tgz" -C "$TMP_DIR"
mkdir -p "$GRAFANA_HOME"
cp -R "$TMP_DIR/grafana-v${GRAFANA}/." "$GRAFANA_HOME/"

echo "==> provisioning Grafana"
mkdir -p "$GRAFANA_HOME/conf/provisioning/datasources" "$GRAFANA_HOME/conf/provisioning/dashboards"
install -m 0644 "$HERE/grafana/provisioning/datasources/prometheus.yml" "$GRAFANA_HOME/conf/provisioning/datasources/prometheus.yml"
sed "s|__HOME__|$HOME|g" "$HERE/grafana/provisioning/dashboards/dashboards.yml" > "$GRAFANA_HOME/conf/provisioning/dashboards/dashboards.yml"
install -m 0644 "$ROOT/dashboards/rtx-vllm.json" "$GRAFANA_HOME/conf/provisioning/dashboards/rtx-vllm.json"
ln -sfn "$GRAFANA_HOME" "$OPT/grafana-current"

GRAFANA_PASSWORD_FILE="$APP_CONFIG_DIR/grafana.env"
if [[ ! -f "$GRAFANA_PASSWORD_FILE" ]]; then
  GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
  printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$GRAFANA_ADMIN_PASSWORD" > "$GRAFANA_PASSWORD_FILE"
  chmod 600 "$GRAFANA_PASSWORD_FILE"
  echo "Grafana initial admin password: $GRAFANA_ADMIN_PASSWORD"
  echo "Save it now. It is also stored with mode 600 in $GRAFANA_PASSWORD_FILE"
fi

echo "==> installing systemd user units"
for unit in "$HERE/systemd/"*.service; do
  name="$(basename "$unit")"
  sed "s|__REPO_ROOT__|$ROOT|g" "$unit" > "$SYSTEMD_DIR/$name"
done
systemctl --user daemon-reload

services=(node-exporter nvidia-gpu-exporter prometheus grafana harness-tokens)
systemctl --user enable --now "${services[@]}"
for service in "${services[@]}"; do
  if ! systemctl --user is-active --quiet "$service"; then
    echo "ERROR: $service did not start" >&2
    systemctl --user --no-pager --full status "$service" >&2 || true
    exit 1
  fi
done

echo
echo "Installation complete. Local-only endpoints:"
echo "  node_exporter         http://127.0.0.1:9100/metrics"
echo "  nvidia_gpu_exporter   http://127.0.0.1:9835/metrics"
echo "  Prometheus            http://127.0.0.1:9090"
echo "  Grafana               http://127.0.0.1:3001"
echo "  harness_tokens        http://127.0.0.1:9257/metrics"
echo
echo "vLLM is optional and is not enabled by this installer. See README.md."
