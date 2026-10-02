# model-launcher

A generalized version of our LLM Launcher. Drop it on a Linux GPU box, register your inference
engines and models in one file (`engines.json`), and switch between them over Tailscale with a
measured progress bar. Your AI agent drives the whole thing through a JSON API, and if you run
Hermes, the launcher keeps your Hermes config in step with whichever model is serving (no gateway
restart). One release, stdlib-only, no bundled weights.

## What it is
- One stdlib Python process (`server.py`) serving a **web UI**, a **CLI** (`launcher`), and a
  **JSON API** over one port (default `8790`), bound to `127.0.0.1` + your Tailscale IP only.
- A **registry** (`engines.json`): each entry is an engine (its unit/container, port, model name,
  health endpoint, boot budget, and the Hermes profile to apply when it is active).
- A **switch engine** (`bin/llm-switch`): the same proven lifecycle we run in production (lock,
  busy guard, stop-all, wait-for-GPU, start, health poll, one smoke completion, rollback,
  `--preempt`, `--force`), now reading the registry instead of hardcoded maps.
- **GPU power cap (optional):** a web UI panel and `POST /api/power` that set one wattage cap for every
  NVIDIA GPU, live and persistent across reboots. Turning it off keeps the saved cap, so turning it back on
  restores it. See [GPU power cap](#gpu-power-cap).
- **Hermes plug-and-play:** after every switch, `hermes-sync` writes the active engine's Hermes
  profile into `~/.hermes/config.yaml` via `hermes_cfg.py`.

## Install
```
git clone https://github.com/ClintMoody/model-launcher.git
cd model-launcher
bash install.sh            # dry run: shows detected hardware + what it will do
bash install.sh --apply    # writes the systemd unit (or Docker compose), seeds engines.json
bash install.sh --power --apply   # same, plus the optional GPU power-cap control (asks for sudo)
```
Open the web UI at `http://<your-tailscale-ip>:8790`.

## Onboarding for an agent
Paste the prompt in `docs/onboarding-agent-checklist.md` to your agent; it reads `AGENTS.md` and
registers, validates, and switches your engines for you.

## GPU power cap
Optional, NVIDIA only. Install it with `install.sh --power --apply`, which uses sudo to install:
- **`/usr/local/bin/gpu-power`** (root-owned). Commands: `status`, `on`, `off`, `set <watts>`, `apply`.
  `status` is JSON and needs no root.
- **`/etc/default/nvidia-power-limit`**, which holds `ENABLED=0|1` and `CAP=<watts>`. A fresh install
  starts uncapped.
- **`gpu-power.service`** (system unit). It waits for the driver at boot, then runs `gpu-power apply`.
- **`/etc/sudoers.d/gpu-power`**, a NOPASSWD rule for the installing user, scoped to `gpu-power` only.

The panel appears in the web UI as soon as `gpu-power` is installed. It shows the cap state, a watts
slider bounded by the GPUs' own min/max, presets, and live draw for each GPU. From a shell:
```
sudo gpu-power set 300   # cap every GPU at 300 W, now and after reboots
sudo gpu-power off       # back to the driver default; 300 W is remembered
sudo gpu-power on        # restore the saved cap
```
On an RTX 3090 Ti pair running a 27B model with TP2 (vLLM), a 250 W cap against the stock 450 W cost
about 8% aggregate throughput at 4-16 streams, about 16% long-prompt prefill, and about 5% single-stream
decode.

`install.sh --uninstall --power --apply` removes the pieces and leaves the GPUs at their driver default.

## Environment variables
| Var | Default | Meaning |
|---|---|---|
| `LAUNCHER_PORT` | `8790` | the server port |
| `LAUNCHER_BIND` | `127.0.0.1,<tailscale-ip>` | comma-separated bind addresses (Tailscale IP auto-detected at install) |
| `LAUNCHER_REGISTRY` | `~/.config/model-launcher/engines.json` | the registry path |
| `LAUNCHER_TOKEN` | (set via the config endpoint) | the bearer token for mutating API calls |
| `LAUNCHER_GPU_POWER` | `/usr/local/bin/gpu-power` | the power-cap helper (the panel is hidden if it is missing) |
| `LAUNCHER_SUDO` | `sudo -n` | the prefix for power-cap changes (tests set it empty) |

## Layout
- `server.py` - stdlib HTTP server: UI + CLI + JSON API, registry writer, auth.
- `registry.py` - `engines.json` load/save, bash-array generation, Hermes-profile extraction.
- `bin/llm-switch` - the registry-driven switch engine (bash).
- `hermes_cfg.py` - the only reader/writer of `~/.hermes/config.yaml`.
- `install.sh` - dry-run-first installer (probes, writes the unit, seeds the registry; `--power` adds the cap control).
- `bin/gpu-power` - the GPU power-cap helper (installed root-owned to `/usr/local/bin`).
- `systemd/gpu-power.service` - re-applies the saved cap at boot.
- `check-secrets.sh` - secrets gate run before every push (wired into `gates.sh`).
- `AGENTS.md` - the contract an AI agent reads to drive the launcher.
- `docs/onboarding-agent-checklist.md` - the prompt to hand your agent.

## Gates
`bash gates.sh` runs compile, ruff, pytest, and the secrets grep. CI (if any) runs the same file.
