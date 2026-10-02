# AGENTS.md — operating contract for any AI agent driving the model-launcher

You are being asked to onboard or drive a **model-launcher** on this machine. This file is your
complete contract. Read it fully before running any command or calling any endpoint.

## What the model-launcher is
A single stdlib Python process (`server.py`) that serves a web UI, a CLI, and a JSON API over one
port (default **8790**). It switches between **engines** (inference servers) registered in one file,
`engines.json` (the **registry**). One engine owns the GPU(s) at a time. If the machine runs Hermes,
the launcher keeps Hermes' model profile in step with whichever engine is serving (no gateway
restart needed).

- **Registry:** `~/.config/model-launcher/engines.json` (path may be overridden by `LAUNCHER_REGISTRY`).
  Each engine record: `id`, `label`, `unit` (systemd user unit), optional `container` (docker), `port`,
  `api_name` (the OpenAI `/v1` model name), `health_url`, `boot_budget_s`, `gpu_free_mib`, optional
  `require_mount`, a `hermes` block (`reasoning_effort`, `max_concurrent_children`, `child_timeout_seconds`,
  `stale_timeout_seconds`), and an optional `default` flag (exactly one engine is the rollback target).
  There is also a top-level `config` block holding the bearer `token`.
- **Serving port:** the engine's `port` (usually 8081). Clients point at that port + the engine's `api_name`.
- **Switch engine:** `bin/llm-switch` (the single source of truth for the lifecycle: lock, busy guard,
  stop-all, wait-for-GPU, start, health poll, one smoke completion, rollback, `--preempt`, `--force`).
  The server delegates to it; you do not call it directly, you use the API.

## The API (base `http://<host>:8790`)
Read endpoints are **open**. Every **mutating** endpoint requires the bearer token, sent as
`Authorization: Bearer <token>` (or `X-Launcher-Token: <token>`). Until a token is set (first boot),
mutating calls are allowed so you can set one.

| Method & path | Purpose | Auth |
|---|---|---|
| `GET /api/detect` | hardware + prerequisite probe (GPUs, Tailscale IP, Docker, nvidia runtime, Ollama, Hermes config, free ports) | open |
| `GET /api/engines` | list registered engines + live unit states | open |
| `GET /api/status` | active engine, job, progress, GPUs, RAM, Hermes values | open |
| `GET /api/journal` | the switch log | open |
| `POST /api/engines` | register an engine (body = one engine record). 409 if the id already exists. | token |
| `PATCH /api/engines/<id>` | update fields on one engine. 404 if unknown. | token |
| `DELETE /api/engines/<id>` | remove an engine (re-assigns default if it was the default). | token |
| `POST /api/validate` `{id}` | run the **three-step gate** on one engine: launch, probe `health_url`, send one smoke completion using its `api_name`. Returns `{ok, detail}`; 200 if green, 503 if a step failed. | token |
| `POST /api/switch` `{target, force?, preempt?, rollback?}` | switch to a registered engine (same flags as `llm-switch`). 202 when it starts; 409 if another switch is running (use `preempt`); 400 if the id is unknown. | token |
| `POST /api/effort` `{effort}` | set Hermes thinking effort now (off/low/medium/xhigh). | token |
| `POST /api/hermes-sync` | match Hermes to whatever is serving. | token |
| `POST /api/power` `{action: "on"\|"off"\|"set", watts?}` | GPU power cap for every GPU, live and kept across reboots. `set` needs integer `watts` within the GPUs' min/max (400 otherwise). `off` keeps the saved cap for `on`. 404 if `gpu-power` is not installed. | token |
| `POST /api/config` `{bind?, token?}` | set the bind list and/or the bearer token. **First call after install.** | open until a token is set |

**Status codes to expect:** `200` ok · `201` created · `202` switch accepted · `400` bad/unknown ·
`403` missing or wrong token · `404` unknown engine · `409` conflict (already switching / id exists) ·
`503` engine unhealthy (validate failed).

## The onboarding procedure (what you do, in order)
1. `GET /api/detect` — confirm the box can run engines. If a prerequisite is missing, tell the user
   exactly what to install (e.g. "Tailscale not found" / "nvidia Docker runtime missing") and stop.
2. `POST /api/config` with a token you generate (long random string). Store the token; you will need it
   for every later mutating call.
3. For **each engine the user runs**: `POST /api/engines` with its record (id, unit or container, port,
   api_name, health_url, boot budget, hermes block). The user tells you what he runs; you fill it in.
4. For each engine: `POST /api/validate`. If it returns `503`, read `detail`, fix the cause (wrong unit
   name, wrong port, model not loaded, missing mount), and re-validate until it returns `200` (green).
5. `POST /api/switch` to the user's default engine, then `POST /api/hermes-sync` so Hermes follows it.
6. `GET /api/status` and report to the user: which engine is active, the served model, the Hermes
   profile, and the result of each validate.

## GPU power cap
`GET /api/status` carries a `power` block: `{available, enabled, cap, gpus: [{index, limit, default, min, max, draw}]}`.
If `available` is false, the optional control was not installed (`install.sh --power --apply`), so
do not call `/api/power`. Change the cap only when the user asks. It trades speed for heat and power
draw, and lowering it slows every engine.

## Invariants (do not violate)
- **Exactly one engine is `default`** (the rollback target). If you set a new default, clear the old one.
- **One engine owns the GPU(s) at a time.** Never register two engines as simultaneously active; the
  switch engine enforces this, and you drive one switch at a time (`409` if you try to start a second).
- **The registry is written only by the server** (atomic). Never edit `engines.json` by hand mid-run.
- **Hermes config is written only via `hermes_cfg.py`** (never a raw edit, never `hermes config set` —
  the latter writes the string `none` as a YAML null, which turns thinking back ON).
- **Auth is the Tailscale boundary + the bearer token.** The server binds to `127.0.0.1` + the Tailscale
  IP only, never the LAN. Keep the token out of any log, file, or message.

## Gotchas
- The `hermes_cfg.py` writer is a **line editor**: it only edits keys that already exist in
  `~/.hermes/config.yaml`. If a key is missing (e.g. `child_timeout_seconds`), the set fails silently in
  the log. A fresh Hermes install may not have all four keys; the first `hermes-sync` after install is
  where you'll see "FAILED to set" for a missing one — add the key to the config, then re-sync.
- `boot_budget_s` is the max seconds to wait for an engine's health endpoint before the switch rolls
  back to the default. Set it generously for large models (minutes), small for fast ones.
- A `POST /api/validate` that returns `503` means one of the three steps failed; the `detail` names
  which. Do not mark an engine green on a 503.
- After a reboot, the default engine comes back via systemd and `hermes-sync` runs at boot, so Hermes
  matches whatever is serving. You do not need to re-register anything.

## How to verify you are done
`GET /api/status` shows the target engine `active`, `up: true`, the expected model in `models`, and
Hermes values matching that engine's `hermes` block. All registered engines that you validated return
`200` from `POST /api/validate`.
