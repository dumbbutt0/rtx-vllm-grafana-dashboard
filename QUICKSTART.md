# Quickstart: RTX monitoring on WSL2

This installs the monitoring dashboard on Ubuntu under WSL2. It does **not** install or start vLLM; the GPU, node, and local-tool panels work without it.

## 1. Check the host

Inside Ubuntu/WSL2:

```bash
ps -p 1 -o comm=
nvidia-smi
```

The first command must print `systemd`. The second must show an NVIDIA RTX GPU. If either fails, fix WSL2 systemd or NVIDIA CUDA passthrough before continuing.

Install the small set of bootstrap tools:

```bash
sudo apt-get update
sudo apt-get install -y git curl python3
```

## 2. Clone and install

```bash
git clone https://github.com/dumbbutt0/rtx-vllm-grafana-dashboard.git
cd rtx-vllm-grafana-dashboard
./deploy/install.sh
```

The installer downloads version-pinned archives, verifies their SHA-256 checksums, provisions Prometheus and Grafana, and starts five user services. It does not require root.

On first install it prints a random Grafana admin password. Save it. A mode-600 copy is stored locally at:

```text
~/.config/rtx-vllm-grafana/grafana.env
```

## 3. Verify

```bash
python3 scripts/smoke-test.py
```

Expected final line:

```text
PASS: monitoring stack is healthy
```

Then open [http://localhost:3001](http://localhost:3001) in Windows and sign in as `admin` with the generated password.

For a full live panel check:

```bash
GRAFANA_PASSWORD="$(sed -n 's/^GF_SECURITY_ADMIN_PASSWORD=//p' ~/.config/rtx-vllm-grafana/grafana.env)"
python3 scripts/validate-panels.py http://127.0.0.1:3001 "admin:${GRAFANA_PASSWORD}"
```

Panels for vLLM, Ollama, Pi, Hermes, or Claude Code may be empty when those optional tools are not running. Query errors are not expected.

## Optional: start vLLM

The monitoring stack is now complete. To add a local model and inference metrics, continue with the [vLLM setup in the README](README.md#vllm-setup--the-wsl2-gotchas-all-hit-in-practice). That setup additionally requires `uv`, build tools, model storage, and several gigabytes of download space.

## Troubleshooting

Check a failed service:

```bash
systemctl --user --no-pager --full status SERVICE_NAME
journalctl --user -u SERVICE_NAME --no-pager -n 100
```

The five core service names are `node-exporter`, `nvidia-gpu-exporter`, `prometheus`, `grafana`, and `harness-tokens`.

All HTTP endpoints bind to `127.0.0.1`. Do not expose them to a network without adding authentication and firewall policy.
