# Onboarding your model-launcher (paste this to your agent)

> I have a **model-launcher** running on this machine at `http://127.0.0.1:8790` (or my Tailscale IP
> on port 8790). Read `AGENTS.md` in the launcher repo for the full contract, then onboard it for me.
>
> 1. `GET /api/detect` and confirm the prerequisites are met. If anything is missing, tell me exactly
>    what to install and stop.
> 2. Generate a strong random token and `POST /api/config` with it. Remember the token.
> 3. For each engine I run, register it with `POST /api/engines`. **Here are my engines**
>    (type, how I start/stop it, the port, the model name it serves, and any systemd unit or docker
>    container):
>
>    ```
>    - [engine 1]: <e.g. Ollama, port 11434, serves "llama3.1">
>    - [engine 2]: <e.g. vLLM docker container "my-vllm", port 8081, serves "alfred", boot ~15 min>
>    ```
>
> 4. For each, run `POST /api/validate` and fix any failure it reports (it names the failing step)
>    until each returns green.
> 5. `POST /api/switch` to my default engine, then `POST /api/hermes-sync` so my Hermes follows it.
> 6. `GET /api/status` and report: which engine is active, the served model, my Hermes profile, and
>    the result of each validate.
>
> Do not edit `engines.json` by hand; use the API. Keep the token out of anything you print.

## What the agent will do (so you know what to expect)
- It reads `AGENTS.md` (the contract) first, then drives everything through the JSON API.
- It will **launch each engine, probe its health, and send one real completion** before calling it
  good. A red engine is reported with the exact failing step and a log tail, not silently skipped.
- It sets the bearer token once, then uses it for every mutating call.
- If you run Hermes, it ends with `hermes-sync`, so your agent's model follows whichever engine is
  serving, with no gateway restart.

## After onboarding
- Open the web UI at `http://<tailscale-ip>:8790` for the human view (engine cards, effort picker,
  switch button, measured progress bar).
- To switch later, use the web UI, or `bin/llm-switch <engine-id>`, or `POST /api/switch`.
- To add an engine later, `POST /api/engines` then `POST /api/validate`.
