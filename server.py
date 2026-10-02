#!/usr/bin/env python3
"""model-launcher server: web UI + JSON API over a registry of engines.

Generalized from llm-launcher/server.py. The hardcoded TARGETS/UNITS/DEFAULT_EFFORT/ENGINE_STAGES
are replaced by the engines.json registry (registry.py). All switching is delegated to bin/llm-switch
(the single source of truth for the lifecycle and the Hermes profile); this server only drives it,
tracks the job, reports progress, and exposes the registry + a Tailscale-only, token-authenticated API.

Stdlib only. Binds to 127.0.0.1 + the detected Tailscale IP (never the LAN).
  GET  /                  web UI
  GET  /api/detect        hardware/prereq probe (read-only; the wizard's first screen)
  GET  /api/engines       registered engines + live status
  POST /api/engines       register an engine (token)
  PATCH/DELETE /api/engines/<id>
  POST /api/validate      run the three-step gate on one engine (token)
  GET  /api/status        active engine, job, progress, GPUs, RAM, Hermes
  POST /api/switch        trigger a switch (token)
  POST /api/effort        set Hermes thinking effort now (token)
  POST /api/hermes-sync   match Hermes to whatever is serving (token)
  GET  /api/journal       switch log since a cursor
  POST /api/config        set the bind list + the bearer token (token)
  POST /api/power         GPU power cap {"action": "on"|"off"|"set", "watts": int} (token)
Auth: read endpoints open; mutating endpoints require the bearer token (Authorization or
X-Launcher-Token). The token lives in the registry config block. Tailscale is the trust boundary.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import registry

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("LAUNCHER_PORT", "8790"))
BIND = os.environ.get("LAUNCHER_BIND", "127.0.0.1").split(",")
SWITCH = os.path.join(HERE, "bin", "llm-switch")
# GPU power cap: root-owned helper installed by `install.sh --power --apply`; status needs no root,
# changes go through a NOPASSWD sudo rule scoped to this one binary.
GPU_POWER = os.environ.get("LAUNCHER_GPU_POWER", "/usr/local/bin/gpu-power")
SUDO = os.environ.get("LAUNCHER_SUDO", "sudo -n").split()
REGPATH = os.environ.get("LAUNCHER_REGISTRY",
                         os.path.expanduser("~/.config/model-launcher/engines.json"))
LOGF = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "model-launcher-switch.log")

EFFORTS = [
    {"id": "off", "label": "Off", "hint": "No thinking; answers immediately"},
    {"id": "low", "label": "Low", "hint": "Brief thinking"},
    {"id": "medium", "label": "Medium", "hint": "Normal thinking"},
    {"id": "xhigh", "label": "XHigh", "hint": "Deepest thinking; slowest (the model's own default)"},
]

# Generic loading stages shown on the progress bar (the engine-specific regex detail from the original
# is engine-specific; for a generalized launcher we show the lifecycle stages and a live GPU/RAM
# sub-signal, and never advance on time alone).
GENERIC_STAGES = ["Stopping the current model", "Starting the engine",
                  "Loading model into memory", "Opening the API",
                  "Answering a test prompt", "Updating Hermes", "Ready"]

# registry (the single source of truth) loaded at boot; re-read when its mtime changes.
_reg_cache = {"mtime": None, "engines": [], "cfg": {}}
_lock = threading.Lock()
job = {"running": False, "target": None, "effort": None, "started": None, "ended": None,
       "exit": None, "proc": None, "gen": None, "changed_from": None, "leg_started": None}


def _load_registry(force=False):
    mt = os.path.getmtime(REGPATH) if os.path.exists(REGPATH) else None
    if mt != _reg_cache["mtime"] or force:
        engines = registry.load(REGPATH) if os.path.exists(REGPATH) else []
        # the config block (token) is a top-level "config" key, kept out of the engines list
        cfg = {}
        try:
            raw = json.load(open(REGPATH))
            if isinstance(raw, dict):
                cfg = raw.get("config", {}) or {}
        except Exception:
            cfg = {}
        _reg_cache.update(mtime=mt, engines=engines, cfg=cfg)
    return _reg_cache["engines"], _reg_cache["cfg"]


def _save_registry(engines, cfg=None):
    if cfg is not None:
        # merge engines + config into one file (config is a sibling of engines)
        payload = {"engines": _engine_dicts(engines), "config": cfg}
    else:
        payload = registry_payload(engines)
    # atomic write, same as registry.save but including the config block
    d = os.path.dirname(os.path.abspath(REGPATH))
    os.makedirs(d, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".engines.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, REGPATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    _load_registry(force=True)


def _engine_dicts(engines):
    return [{"id": e.id, "label": e.label, "unit": e.unit, "container": e.container,
             "port": e.port, "api_name": e.api_name, "health_url": e.health_url,
             "boot_budget_s": e.boot_budget_s, "gpu_free_mib": e.gpu_free_mib,
             "require_mount": e.require_mount,
             "hermes": {"reasoning_effort": e.hermes.reasoning_effort,
                        "max_concurrent_children": e.hermes.max_concurrent_children,
                        "child_timeout_seconds": e.hermes.child_timeout_seconds,
                        "stale_timeout_seconds": e.hermes.stale_timeout_seconds},
             "default": e.default} for e in engines]


def registry_payload(engines):
    return {"engines": _engine_dicts(engines)}


# ---- low-level helpers ----
def sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _http_get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def unit_state(u):
    return sh(["systemctl", "--user", "is-active", u]) or "unknown"


def hermes_settings():
    path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        import yaml
        c = yaml.safe_load(open(path)) or {}
        d, pr = c.get("delegation") or {}, (c.get("providers") or {}).get("custom") or {}
        ag = c.get("agent") or {}
        return {"reasoning": ag.get("reasoning_effort"), "children": d.get("max_concurrent_children"),
                "child_timeout": d.get("child_timeout_seconds"), "stale_timeout": pr.get("stale_timeout_seconds")}
    except Exception as e:
        return {"error": str(e)[:200]}


def gpus():
    out = []
    for line in sh(["nvidia-smi", "--query-gpu=index,memory.used,memory.total,temperature.gpu,power.draw",
                    "--format=csv,noheader,nounits"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 5:
            out.append({"index": int(p[0]), "used": int(p[1]), "total": int(p[2]), "temp": int(p[3]),
                        "power": float(p[4]) if p[4] not in ("[N/A]", "") else None})
    return out


def gpu_power():
    """Power-cap state from gpu-power, or available=False when the helper is not installed."""
    if not os.access(GPU_POWER, os.X_OK):
        return {"available": False}
    try:
        return dict(json.loads(sh([GPU_POWER, "status"])), available=True)
    except ValueError:
        return {"available": False, "error": "gpu-power status failed"}


def set_gpu_power(action, watts=None):
    if action not in ("on", "off", "set"):
        return 400, {"error": f"unknown action {action}"}
    if not os.access(GPU_POWER, os.X_OK):
        return 404, {"error": "gpu-power is not installed (run install.sh --power --apply)"}
    args = SUDO + [GPU_POWER, action]
    if action == "set":
        if not isinstance(watts, int) or isinstance(watts, bool):
            return 400, {"error": "watts must be an integer"}
        args.append(str(watts))
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return 500, {"error": str(e)[:200]}
    if r.returncode:
        msg = (r.stderr or r.stdout).strip()[:300]
        return 400, {"error": msg or f"gpu-power exit {r.returncode}"}
    return 200, {"ok": True, "power": dict(json.loads(r.stdout), available=True)}


def ram():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            m[line.split(":")[0]] = int(line.split()[1])
    return {"total_gb": round(m["MemTotal"] / 1048576, 1),
            "used_gb": round((m["MemTotal"] - m["MemAvailable"]) / 1048576, 1)}


def ram_used_gib():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            m[line.split(":")[0]] = int(line.split()[1])
    return (m["MemTotal"] - m["MemAvailable"]) / 1048576


def tail(n):
    try:
        with open(LOGF) as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def journal_since(unit, since):
    if not unit:
        return tail(200)
    args = ["journalctl", "--user", "-u", unit, "-n", "200", "-o", "cat", "--no-pager"]
    if since:
        args += ["--since", f"@{int(since)}"]
    return sh(args, timeout=15)


def detect():
    """Hardware + prerequisite probe. The wizard's first screen and the agent's first call."""
    import shutil
    tailscale_ip = None
    try:
        out = sh(["tailscale", "ip", "-4"], timeout=5)
        tailscale_ip = out.strip() or None
    except Exception:
        pass
    gpu_lines = sh(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader"]).splitlines()
    gpus_info = []
    for line in gpu_lines:
        p = [x.strip() for x in line.split(",")]
        if len(p) == 3:
            gpus_info.append({"index": int(p[0]), "name": p[1], "vram_mib": int(p[2])})
    def has(prog):
        return shutil.which(prog) is not None
    docker_nvidia = False
    if has("docker"):
        docker_nvidia = "nvidia" in sh(["docker", "info", "--format", "{{.RuntimeName}} {{.Runtimes}}"], timeout=5)
    hermes_cfg = os.path.exists(os.path.expanduser("~/.hermes/config.yaml"))
    return {
        "os": os.uname().sysname,
        "nvidia_gpus": gpus_info,
        "tailscale_ip": tailscale_ip,
        "docker": has("docker"),
        "docker_nvidia_runtime": docker_nvidia,
        "systemd_user": has("systemctl") and sh(["systemctl", "--user", "is-system-running"], timeout=5) != "",
        "ollama": has("ollama"),
        "hermes_config": hermes_cfg,
        "free_ports_sample": _free_ports(),
    }


def _free_ports():
    import socket
    free = []
    for port in (8081, 8082, 11434, 1234, 8790):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            free.append(port)
        except OSError:
            pass
        finally:
            s.close()
    return free


def progress(j):
    if not j.get("target"):
        return None
    log = "\n".join(tail(400))
    if "--- changed mind" in log:
        log = log.rsplit("--- changed mind", 1)[1]
    steps = GENERIC_STAGES
    idx = 0
    if re.search(r"^starting \S+", log, re.M):
        idx = max(idx, 1)
    if re.search(r"UP after", log, re.M):
        idx = max(idx, 3)
    if re.search(r"^smoke:", log, re.M):
        idx = max(idx, 4)
    ready = "DONE:" in log and j.get("exit") == 0
    if ready:
        idx = len(steps) - 1
    detail = ""
    if re.search(r"rolling back", log):
        detail = "target failed to start; rolling back to the default engine"
    # live GPU sub-signal while loading (never advances the step on time alone)
    sub = None
    if 1 <= idx < len(steps) - 1:
        used = sum(g["used"] for g in gpus())
        total = sum(g["total"] for g in gpus()) or 1
        sub = max(0.0, min(1.0, used / total))
    pct = (idx + (sub or 0)) / (len(steps) - 1) * 100
    return {"steps": steps, "index": idx, "sub": sub, "pct": round(pct, 1), "detail": detail, "ready": ready}


def status():
    engines, _ = _load_registry()
    states = {e.id: unit_state(e.unit) for e in engines if e.unit}
    active = next((k for k, v in states.items() if v in ("active", "activating")), None)
    port = int(os.environ.get("LAUNCHER_PORT", "8790"))
    serve_port = 8081
    for e in engines:
        if e.id == active:
            serve_port = e.port
            break
    models, up = [], False
    active_eng = next((e for e in engines if e.id == active), None)
    # try the engine's own health_url first (works for Ollama /api/tags, vLLM /v1/models, etc.)
    if active_eng and active_eng.health_url:
        try:
            data = _http_get_json(active_eng.health_url)
            up = True
            if isinstance(data, dict):
                # OpenAI-style /v1/models -> {"data":[{"id":..}]}
                if "data" in data and isinstance(data["data"], list):
                    models = [m.get("id") for m in data["data"] if isinstance(m, dict)]
                # Ollama /api/tags -> {"models":[{"name":..}]}
                elif "models" in data and isinstance(data["models"], list):
                    models = [m.get("name") for m in data["models"] if isinstance(m, dict)]
                # fallback: use the engine's registered api_name
                if not models:
                    models = [active_eng.api_name]
            elif isinstance(data, list):
                models = [data]
        except Exception:
            pass
    if not up:
        # last resort: the OpenAI endpoint on the serve port
        try:
            models = [m["id"] for m in _http_get_json(f"http://127.0.0.1:{serve_port}/v1/models")["data"]]
            up = True
        except Exception:
            pass
    busy = 0
    try:
        r = _http_get_json(f"http://127.0.0.1:{serve_port}/metrics")
        for line in json.dumps(r).split():
            pass
        # fall back to a raw text fetch for the metrics counters
        with urllib.request.urlopen(f"http://127.0.0.1:{serve_port}/metrics", timeout=3) as resp:
            for line in resp.read().decode().splitlines():
                if re.match(r"^vllm:num_requests_(running|waiting)\{", line):
                    busy += int(float(line.split()[-1]))
    except Exception:
        pass
    with _lock:
        j = {k: v for k, v in job.items() if k != "proc"}
    j["tail"] = tail(40)
    j["progress"] = progress(j) if (j["running"] or j["target"]) else None
    return {"time": time.time(), "active": active, "states": states, "up": up, "models": models,
            "busy": busy, "gpus": gpus(), "ram": ram(), "hermes": hermes_settings(), "job": j,
            "power": gpu_power(), "engines": _engine_dicts(engines), "efforts": EFFORTS}


def _validate_one(eng):
    """Three-step gate: launch, probe health, one smoke completion. Returns (ok, detail)."""
    # launch (best-effort; a failure here means the unit won't come up)
    if eng.unit:
        sh(["systemctl", "--user", "start", eng.unit], timeout=30)
    # probe health
    try:
        _http_get_json(eng.health_url)
    except Exception as e:
        return False, f"health probe failed: {e}"
    # smoke completion
    try:
        body = json.dumps({"model": eng.api_name,
                           "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
                           "max_tokens": 8, "temperature": 0}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{eng.port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)["choices"][0]["message"]["content"].strip()
        return True, f"smoke: {out}"
    except Exception as e:
        return False, f"smoke completion failed: {e}"


def run_switch(target, force, effort=None, preempt=False, gen=None):
    args = [SWITCH, target] + (["--force"] if force else []) + ([f"--effort={effort}"] if effort else []) + (["--preempt"] if preempt else [])
    env = dict(os.environ, LAUNCHER_REGISTRY=REGPATH, LAUNCHER_HERMES_CFG=os.path.join(HERE, "hermes_cfg.py"))
    with open(LOGF, "a" if preempt else "w") as log:
        if preempt:
            log.write(f"\n--- changed mind: switching to {target} instead ---\n")
        log.write(f"$ {' '.join(a)}   ({time.strftime('%H:%M:%S')})\n"); log.flush()
        p = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
        with _lock:
            job["proc"] = p
        rc = p.wait()
        log.write(f"[exit {rc}] {time.strftime('%H:%M:%S')}\n")
    with _lock:
        if job.get("gen") == gen:
            job.update(running=False, ended=time.time(), exit=rc, proc=None)


def start_switch(target, force, effort=None, preempt=False):
    engines, _ = _load_registry()
    ids = {e.id for e in engines}
    if target not in ids:
        return 400, {"error": f"unknown engine {target} (registered: {sorted(ids)})"}
    if effort and effort not in {e["id"] for e in EFFORTS}:
        return 400, {"error": f"unknown effort {effort}"}
    with _lock:
        taking_over = job["running"]
        if taking_over and not preempt:
            return 409, {"error": f"a switch to {job['target']} is already running"}
        if taking_over and job["target"] == target:
            return 200, {"ok": True, "target": target, "note": "already switching to that engine"}
        gen = time.time()
        started = job["started"] if taking_over else gen
        default_effort = "medium"
        for e in engines:
            if e.id == target:
                default_effort = "off" if e.hermes.reasoning_effort == "none" else e.hermes.reasoning_effort
        job.update(running=True, target=target, effort=effort or default_effort, started=started, ended=None,
                   exit=None, gen=gen, changed_from=job["target"] if taking_over else None, leg_started=gen)
    threading.Thread(target=run_switch, args=(target, force, effort, taking_over, gen), daemon=True).start()
    return 202, {"ok": True, "target": target}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        """Mutating endpoints require the bearer token from the registry config."""
        _, cfg = _load_registry()
        tok = cfg.get("token")
        if not tok:
            # no token set yet: allow (first-run, pre-config) so the wizard can set one
            return True
        auth = self.headers.get("Authorization", "")
        hdr = self.headers.get("X-Launcher-Token", "")
        return auth == f"Bearer {tok}" or hdr == tok

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            p = os.path.join(HERE, "index.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    html = f.read().decode("utf-8", "replace")
                # the browser UI authenticates its own mutating calls with the registry token,
                # injected into the page (only reachable over 127.0.0.1 + Tailscale). Direct/API
                # callers still send the token themselves; the page-bound copy is never logged.
                _, cfg = _load_registry()
                tok = cfg.get("token", "")
                html = html.replace("__UI_TOKEN__", tok)
                return self.send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return self.send(200, b"<h1>model-launcher</h1><p>web UI not built yet; use the JSON API.</p>", "text/html; charset=utf-8")
        if self.path == "/api/detect":
            return self.send(200, detect())
        if self.path == "/api/engines":
            engines, _ = _load_registry()
            states = {e.id: unit_state(e.unit) for e in engines if e.unit}
            return self.send(200, {"engines": _engine_dicts(engines), "states": states})
        if self.path == "/api/status":
            return self.send(200, status())
        if self.path.startswith("/api/journal"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            tgt = q.get("target", [None])[0]
            if tgt:
                engines, _ = _load_registry()
                eng = next((e for e in engines if e.id == tgt), None)
                return self.send(200, {"log": journal_since(eng.unit if eng else None, None)})
            return self.send(200, {"log": tail(100000)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/config":
            body = self._body()
            engines, cfg = _load_registry()
            if body.get("token"):
                cfg["token"] = body["token"]
            if body.get("bind"):
                cfg["bind"] = body["bind"]
            _save_registry(engines, cfg)
            return self.send(200, {"ok": True})
        if not self._authorized():
            return self.send(403, {"error": "missing or invalid bearer token"})
        try:
            body = self._body()
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if self.path == "/api/switch":
            return self.send(*start_switch(body.get("target"), bool(body.get("force")),
                                           body.get("effort"), bool(body.get("preempt"))))
        if self.path == "/api/effort":
            e = body.get("effort")
            if e not in {x["id"] for x in EFFORTS}:
                return self.send(400, {"error": f"unknown effort {e}"})
            with _lock:
                if job["running"]:
                    return self.send(409, {"error": "a switch is running; choose the effort for it instead"})
            out = sh([SWITCH, "effort", e], timeout=30)
            return self.send(200, {"ok": True, "message": out})
        if self.path == "/api/power":
            return self.send(*set_gpu_power(body.get("action"), body.get("watts")))
        if self.path == "/api/hermes-sync":
            out = sh([SWITCH, "hermes-sync"], timeout=60)
            return self.send(200, {"ok": True, "message": out})
        if self.path == "/api/validate":
            eid = body.get("id")
            engines, _ = _load_registry()
            eng = next((e for e in engines if e.id == eid), None)
            if not eng:
                return self.send(404, {"error": f"unknown engine {eid}"})
            ok, detail = _validate_one(eng)
            return self.send(200 if ok else 503, {"id": eid, "ok": ok, "detail": detail})
        if self.path == "/api/engines":
            # register a new engine
            rec = body
            try:
                eng = registry._engine_from_dict(rec)
            except Exception as e:
                return self.send(400, {"error": f"invalid engine record: {e}"})
            engines, cfg = _load_registry()
            if any(e.id == eng.id for e in engines):
                return self.send(409, {"error": f"engine {eng.id} already exists"})
            if eng.default and any(e.default for e in engines):
                for e in engines:
                    e.default = False
            engines.append(eng)
            _save_registry(engines, cfg)
            return self.send(201, {"ok": True, "id": eng.id})
        self.send(404, {"error": "not found"})

    def do_PATCH(self):
        if not self._authorized():
            return self.send(403, {"error": "missing or invalid bearer token"})
        m = re.match(r"^/api/engines/([a-z0-9_-]+)$", self.path)
        if not m:
            return self.send(404, {"error": "not found"})
        eid = m.group(1)
        body = self._body()
        engines, cfg = _load_registry()
        eng = next((e for e in engines if e.id == eid), None)
        if not eng:
            return self.send(404, {"error": f"unknown engine {eid}"})
        # merge allowed fields
        for k in ("label", "unit", "container", "api_name", "health_url"):
            if k in body:
                setattr(eng, k, body[k])
        for k in ("port", "boot_budget_s", "gpu_free_mib"):
            if k in body:
                setattr(eng, k, int(body[k]))
        if "hermes" in body and isinstance(body["hermes"], dict):
            for hk in ("reasoning_effort", "max_concurrent_children", "child_timeout_seconds", "stale_timeout_seconds"):
                if hk in body["hermes"]:
                    setattr(eng.hermes, hk, body["hermes"][hk])
        if "default" in body:
            if body["default"]:
                for e in engines:
                    e.default = False
            eng.default = bool(body["default"])
        _save_registry(engines, cfg)
        self.send(200, {"ok": True, "id": eid})

    def do_DELETE(self):
        if not self._authorized():
            return self.send(403, {"error": "missing or invalid bearer token"})
        m = re.match(r"^/api/engines/([a-z0-9_-]+)$", self.path)
        if not m:
            return self.send(404, {"error": "not found"})
        eid = m.group(1)
        engines, cfg = _load_registry()
        before = len(engines)
        engines = [e for e in engines if e.id != eid]
        if len(engines) == before:
            return self.send(404, {"error": f"unknown engine {eid}"})
        if not any(e.default for e in engines) and engines:
            engines[0].default = True
        _save_registry(engines, cfg)
        self.send(200, {"ok": True, "id": eid})


def main():
    # fail loudly at boot if the registry is missing or malformed (Foundation 2)
    if not os.path.exists(REGPATH):
        print(f"model-launcher: registry not found: {REGPATH} (run install.sh first)", flush=True)
        raise SystemExit(1)
    try:
        _load_registry(force=True)
    except Exception as e:
        print(f"model-launcher: registry invalid: {e}", flush=True)
        raise SystemExit(1)
    # on login/reboot, make Hermes match whatever is serving
    env = dict(os.environ, LAUNCHER_REGISTRY=REGPATH, LAUNCHER_HERMES_CFG=os.path.join(HERE, "hermes_cfg.py"))
    def _sync():
        try:
            subprocess.run([SWITCH, "hermes-sync"], timeout=60, env=env, capture_output=True)
        except Exception as e:
            print(f"hermes-sync at boot: {e}", flush=True)
    threading.Thread(target=_sync, daemon=True).start()
    servers = []
    for addr in BIND:
        try:
            s = ThreadingHTTPServer((addr.strip(), PORT), H)
        except OSError as e:
            print(f"cannot bind {addr}:{PORT}: {e}", flush=True); continue
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s); print(f"model-launcher on http://{addr.strip()}:{PORT}", flush=True)
    if not servers:
        print("model-launcher: no bind address succeeded", flush=True); raise SystemExit(1)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
