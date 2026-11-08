#!/usr/bin/env bash
# Install the RTX vLLM monitoring stack as user-level systemd services in WSL2.
set -euo pipefail

NODE_EXPORTER=1.12.1
NVIDIA_GPU_EXPORTER=1.14.0
PROMETHEUS=3.14.0
GRAFANA=13.2.0
GRAFANA_BUILD=32077357341
ARCH=linux-amd64

NODE_EXPORTER_SHA256=b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63
NVIDIA_GPU_EXPORTER_SHA256=faa18c7ca506fe1e2bd8c41a060ff27a08dd3652f59b236ae9647bc6a4c78478
PROMETHEUS_SHA256=f665c6da19eb7ba399c915d30c7d9793c9b417bf8a749b504bc470678631478d
GRAFANA_SHA256=4669384cdb0bb5b4a3f804927e57490d17f4cc47258cd1698fc124e99ee58265

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
OPT="$HOME/opt"
BIN="$OPT/bin"
PROMETHEUS_HOME="$OPT/prometheus-${PROMETHEUS}-${PROMETHEUS_SHA256:0:12}"
PROMETHEUS_CONFIG="$OPT/prometheus-config"
PROMETHEUS_DATA="$OPT/prometheus-data"
GRAFANA_HOME="$OPT/grafana-${GRAFANA}-${GRAFANA_BUILD}"
SYSTEMD_DIR="$HOME/.config/systemd/user"
APP_CONFIG_DIR="$HOME/.config/rtx-vllm-grafana"
TMP_DIR="$(mktemp -d -t rtx-monitoring-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

for command in curl tar systemctl sed python3 sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $command" >&2
    exit 1
  }
done

verify_sha256() {
  local expected="$1"
  local file="$2"
  printf '%s  %s\n' "$expected" "$file" | sha256sum --check --status || {
    echo "ERROR: checksum verification failed for $file" >&2
    exit 1
  }
}

mkdir -p "$BIN" "$OPT/src" "$SYSTEMD_DIR" "$APP_CONFIG_DIR"

echo "==> node_exporter ${NODE_EXPORTER}"
curl -fsSL -o "$TMP_DIR/node-exporter.tgz" "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER}/node_exporter-${NODE_EXPORTER}.${ARCH}.tar.gz"
verify_sha256 "$NODE_EXPORTER_SHA256" "$TMP_DIR/node-exporter.tgz"
tar xzf "$TMP_DIR/node-exporter.tgz" -C "$TMP_DIR"
install -m 0755 "$TMP_DIR/node_exporter-${NODE_EXPORTER}.${ARCH}/node_exporter" "$BIN/node_exporter"

echo "==> nvidia_gpu_exporter ${NVIDIA_GPU_EXPORTER}"
curl -fsSL -o "$TMP_DIR/nvidia-gpu-exporter.tgz" "https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v${NVIDIA_GPU_EXPORTER}/nvidia_gpu_exporter_${NVIDIA_GPU_EXPORTER}_linux_x86_64.tar.gz"
verify_sha256 "$NVIDIA_GPU_EXPORTER_SHA256" "$TMP_DIR/nvidia-gpu-exporter.tgz"
tar xzf "$TMP_DIR/nvidia-gpu-exporter.tgz" -C "$TMP_DIR"
install -m 0755 "$TMP_DIR/nvidia_gpu_exporter" "$BIN/nvidia_gpu_exporter"

echo "==> Prometheus ${PROMETHEUS}"
curl -fsSL -o "$TMP_DIR/prometheus.tgz" "https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS}/prometheus-${PROMETHEUS}.${ARCH}.tar.gz"
verify_sha256 "$PROMETHEUS_SHA256" "$TMP_DIR/prometheus.tgz"
tar xzf "$TMP_DIR/prometheus.tgz" -C "$TMP_DIR"
PROMETHEUS_MARKER="$PROMETHEUS_HOME/.rtx-install-complete"
if [[ ! -f "$PROMETHEUS_MARKER" ]]; then
  if [[ -e "$PROMETHEUS_HOME" ]]; then
    echo "ERROR: incomplete Prometheus installation exists at $PROMETHEUS_HOME" >&2
    exit 1
  fi
  PROMETHEUS_STAGE="$TMP_DIR/prometheus-stage"
  mkdir -p "$PROMETHEUS_STAGE"
  cp -R "$TMP_DIR/prometheus-${PROMETHEUS}.${ARCH}/." "$PROMETHEUS_STAGE/"
  touch "$PROMETHEUS_STAGE/.rtx-install-complete"
  mv "$PROMETHEUS_STAGE" "$PROMETHEUS_HOME"
fi
mkdir -p "$PROMETHEUS_CONFIG/targets"
if [[ -d "$OPT/prometheus/data" ]] && [[ ! -e "$PROMETHEUS_DATA" ]]; then
  echo "==> retaining legacy Prometheus data in place"
  ln -s "$OPT/prometheus/data" "$PROMETHEUS_DATA"
fi
mkdir -p "$PROMETHEUS_DATA"
install -m 0644 "$HERE/prometheus.yml" "$PROMETHEUS_CONFIG/prometheus.yml"
install -m 0644 "$HERE/targets/"*.yml "$PROMETHEUS_CONFIG/targets/"
ln -sfn "$PROMETHEUS_HOME" "$OPT/prometheus-current"

echo "==> Grafana ${GRAFANA}"
curl -fsSL -o "$TMP_DIR/grafana.tgz" "https://dl.grafana.com/grafana/release/${GRAFANA}/grafana_${GRAFANA}_${GRAFANA_BUILD}_linux_amd64.tar.gz"
verify_sha256 "$GRAFANA_SHA256" "$TMP_DIR/grafana.tgz"
tar xzf "$TMP_DIR/grafana.tgz" -C "$TMP_DIR"
GRAFANA_MARKER="$GRAFANA_HOME/.rtx-install-complete"
if [[ ! -f "$GRAFANA_MARKER" ]]; then
  if [[ -e "$GRAFANA_HOME" ]]; then
    echo "ERROR: incomplete Grafana installation exists at $GRAFANA_HOME" >&2
    exit 1
  fi
  GRAFANA_STAGE="$TMP_DIR/grafana-stage"
  mkdir -p "$GRAFANA_STAGE"
  cp -R "$TMP_DIR/grafana-${GRAFANA}/." "$GRAFANA_STAGE/"
  touch "$GRAFANA_STAGE/.rtx-install-complete"
  mv "$GRAFANA_STAGE" "$GRAFANA_HOME"
fi
GRAFANA_DB_MIGRATED=0
if [[ -f "$OPT/grafana/data/grafana.db" ]] && [[ ! -e "$GRAFANA_HOME/data" ]]; then
  echo "==> retaining legacy Grafana data and credentials in place"
  ln -s "$OPT/grafana/data" "$GRAFANA_HOME/data"
  GRAFANA_DB_MIGRATED=1
fi

echo "==> provisioning Grafana"
mkdir -p "$GRAFANA_HOME/conf/provisioning/datasources" "$GRAFANA_HOME/conf/provisioning/dashboards"
install -m 0644 "$HERE/grafana/provisioning/datasources/prometheus.yml" "$GRAFANA_HOME/conf/provisioning/datasources/prometheus.yml"
sed "s|__HOME__|$HOME|g" "$HERE/grafana/provisioning/dashboards/dashboards.yml" > "$GRAFANA_HOME/conf/provisioning/dashboards/dashboards.yml"
install -m 0644 "$ROOT/dashboards/rtx-vllm.json" "$GRAFANA_HOME/conf/provisioning/dashboards/rtx-vllm.json"
ln -sfn "$GRAFANA_HOME" "$OPT/grafana-current"

GRAFANA_PASSWORD_FILE="$APP_CONFIG_DIR/grafana.env"
if [[ ! -f "$GRAFANA_PASSWORD_FILE" ]] && [[ "$GRAFANA_DB_MIGRATED" -eq 0 ]]; then
  GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
  printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$GRAFANA_ADMIN_PASSWORD" > "$GRAFANA_PASSWORD_FILE"
  chmod 600 "$GRAFANA_PASSWORD_FILE"
  echo "Grafana initial admin password: $GRAFANA_ADMIN_PASSWORD"
  echo "Save it now. It is also stored with mode 600 in $GRAFANA_PASSWORD_FILE"
elif [[ "$GRAFANA_DB_MIGRATED" -eq 1 ]]; then
  echo "Grafana database migrated; existing login credentials were retained."
fi

echo "==> installing systemd user units"
for unit in "$HERE/systemd/"*.service; do
  name="$(basename "$unit")"
  sed "s|__REPO_ROOT__|$ROOT|g" "$unit" > "$SYSTEMD_DIR/$name"
done
systemctl --user daemon-reload

services=(node-exporter nvidia-gpu-exporter prometheus grafana harness-tokens)
systemctl --user enable "${services[@]}"
systemctl --user restart "${services[@]}"
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
