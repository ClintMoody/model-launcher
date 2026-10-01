#!/bin/bash
# model-launcher installer. DRY RUN by default; `--apply` writes.
#   - probes Tailscale, Docker, nvidia runtime, GPUs, free ports, Hermes config
#   - writes the systemd user unit (or prints the Docker compose path)
#   - seeds the registry (~/.config/model-launcher/engines.json) with an Ollama prefill if
#     Ollama is present, else a blank openai-compatible slot; the onboarding prompt/agent overrides it
#   - NEVER starts or stops a model server; use the web UI / CLI / API for that
# `--uninstall` removes the unit and the registry.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APPLY=0; UNINSTALL=0
for a in "$@"; do case "$a" in
  --apply) APPLY=1 ;;
  --uninstall) UNINSTALL=1 ;;
esac; done
REGDIR="$HOME/.config/model-launcher"
REG="$REGDIR/engines.json"
UNITDIR="$HOME/.config/systemd/user"
UNIT="$UNITDIR/model-launcher.service"
PYBIN="$(command -v python3 || echo /usr/bin/python3)"

if [ $UNINSTALL = 1 ]; then
  echo "uninstalling model-launcher"
  [ $APPLY = 1 ] || { echo "(dry run; re-run with --apply)"; exit 0; }
  systemctl --user disable --now model-launcher 2>/dev/null || true
  rm -f "$UNIT"
  echo "removed $UNIT (registry left in place at $REG)"
  systemctl --user daemon-reload 2>/dev/null || true
  exit 0
fi

echo "== model-launcher install (dry run by default) =="
# probe
ts_ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
docker_ok=0; command -v docker >/dev/null 2>&1 && docker_ok=1
nvidia_ok=0; command -v nvidia-smi >/dev/null 2>&1 && nvidia_ok=1
gpu_count="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -c . || true)"
ollama_ok=0; command -v ollama >/dev/null 2>&1 && ollama_ok=1
hermes_ok=0; [ -f "$HOME/.hermes/config.yaml" ] && hermes_ok=1
[ -z "$ts_ip" ] && echo "  Tailscale: NOT FOUND (the launcher binds to 127.0.0.1 only until Tailscale is up)" || echo "  Tailscale: $ts_ip"
[ $docker_ok = 1 ] && echo "  Docker: present" || echo "  Docker: NOT FOUND"
[ $nvidia_ok = 1 ] && echo "  NVIDIA GPUs: $gpu_count" || echo "  NVIDIA: nvidia-smi not found"
[ $ollama_ok = 1 ] && echo "  Ollama: present (will prefill the first engine)" || echo "  Ollama: not found (first engine will be a blank openai-compatible slot)"
[ $hermes_ok = 1 ] && echo "  Hermes: config found (hermes-sync will manage its model profile)" || echo "  Hermes: no config (hermes-sync will be a no-op)"

# decide bind
BIND="127.0.0.1"; [ -n "$ts_ip" ] && BIND="127.0.0.1,$ts_ip"
echo "  bind: $BIND   port: 8790"

if [ $APPLY = 0 ]; then
  echo
  echo "(dry run; re-run with --apply to write)"
  echo "  will install: $UNIT"
  echo "  will seed:    $REG"
  echo "  then: open http://${ts_ip:-127.0.0.1}:8790 and run the onboarding prompt (docs/onboarding-agent-checklist.md)"
  exit 0
fi

# apply
mkdir -p "$REGDIR" "$UNITDIR"
# seed the registry if absent
if [ ! -f "$REG" ]; then
  if [ $ollama_ok = 1 ]; then
    cat > "$REG" <<EOF
{
  "engines": [
    {
      "id": "ollama",
      "label": "Ollama (default)",
      "unit": "",
      "container": null,
      "port": 11434,
      "api_name": "llama3.1",
      "health_url": "http://127.0.0.1:11434/api/tags",
      "boot_budget_s": 300,
      "gpu_free_mib": 1500,
      "require_mount": null,
      "hermes": { "reasoning_effort": "medium", "max_concurrent_children": 2,
                  "child_timeout_seconds": 3600, "stale_timeout_seconds": 1800 },
      "default": true
    }
  ],
  "config": { "token": "" }
}
EOF
  else
    cat > "$REG" <<EOF
{
  "engines": [
    {
      "id": "engine",
      "label": "Engine 1 (openai-compatible)",
      "unit": "",
      "container": null,
      "port": 8081,
      "api_name": "local-model",
      "health_url": "http://127.0.0.1:8081/v1/models",
      "boot_budget_s": 900,
      "gpu_free_mib": 1500,
      "require_mount": null,
      "hermes": { "reasoning_effort": "medium", "max_concurrent_children": 2,
                  "child_timeout_seconds": 3600, "stale_timeout_seconds": 1800 },
      "default": true
    }
  ],
  "config": { "token": "" }
}
EOF
  fi
  echo "seeded registry: $REG"
fi
# write the unit
sed -e "s#%h#$HOME#g" -e "s#%h/.local/bin/python3#$PYBIN#g" "$HERE/systemd/model-launcher.service" > "$UNIT"
echo "wrote unit: $UNIT"
# copy the launcher to a stable location the unit points at
DEST="$HOME/.local/lib/model-launcher"
mkdir -p "$DEST"
for f in server.py registry.py hermes_cfg.py; do cp "$HERE/$f" "$DEST/$f"; done
mkdir -p "$HOME/.local/bin"
cp "$HERE/bin/llm-switch" "$HOME/.local/bin/llm-switch"
chmod +x "$HOME/.local/bin/llm-switch"
systemctl --user daemon-reload
systemctl --user enable --now model-launcher
echo "installed and started."
echo
echo "Next: open http://${ts_ip:-127.0.0.1}:8790 and paste the onboarding prompt to your agent (docs/onboarding-agent-checklist.md)."
