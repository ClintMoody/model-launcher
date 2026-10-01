import json
import os
import subprocess
import importlib.util
import sys
import tempfile
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCKBIN = os.path.join(ROOT, "tests", "mockbin")

def _start_server(base):
    """Start server.py against a temp registry + mock tools; return (proc, env)."""
    env = {
        "PATH": MOCKBIN + ":" + os.environ.get("PATH", ""),
        "XDG_RUNTIME_DIR": base,
        "LAUNCHER_REGISTRY": os.path.join(base, "engines.json"),
        "LAUNCHER_HERMES_CFG": os.path.join(ROOT, "hermes_cfg.py"),
        "LAUNCHER_PORT": "18790",
        "LAUNCHER_BIND": "127.0.0.1",
        "HOME": os.path.join(base, "home"),
    }
    os.makedirs(os.path.join(env["HOME"], ".hermes"), exist_ok=True)
    open(os.path.join(env["HOME"], ".hermes", "config.yaml"), "w").write(
        "model:\n  provider: custom\nagent:\n  reasoning_effort: none\ndelegation:\n  max_concurrent_children: 2\n  child_timeout_seconds: 3600\nproviders:\n  custom:\n    stale_timeout_seconds: 1800\n")
    # two-engine registry, no token yet
    reg = {"engines": [
        {"id": "eng-a", "unit": "a-svc.service", "port": 8081, "api_name": "alfred",
         "health_url": "http://127.0.0.1:8081/v1/models", "boot_budget_s": 3, "gpu_free_mib": 1,
         "hermes": {"reasoning_effort": "none", "max_concurrent_children": 8,
                    "child_timeout_seconds": 1200, "stale_timeout_seconds": 900}, "default": True},
        {"id": "eng-b", "unit": "b-svc.service", "port": 8081, "api_name": "alfred",
         "health_url": "http://127.0.0.1:8081/v1/models", "boot_budget_s": 3, "gpu_free_mib": 1,
         "hermes": {"reasoning_effort": "medium", "max_concurrent_children": 2,
                    "child_timeout_seconds": 3600, "stale_timeout_seconds": 1800}, "default": False},
    ], "config": {}}
    open(env["LAUNCHER_REGISTRY"], "w").write(json.dumps(reg))
    p = subprocess.Popen(["python3", os.path.join(ROOT, "server.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p, env

def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())

def _post(url, body, token=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

BASE = "http://127.0.0.1:18790"

@pytest.fixture
def server():
    base = tempfile.mkdtemp(prefix="ml-server-")
    p, env = _start_server(base)
    time.sleep(1.0)  # give the server a moment to bind
    yield env
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()

def test_detect_is_open_and_reports_hardware(server):
    code, body = _get(BASE + "/api/detect")
    assert code == 200
    assert "nvidia_gpus" in body and "tailscale_ip" in body

def test_mutating_requires_token_after_one_is_set(server):
    # first set a token (allowed before a token exists)
    code, _ = _post(BASE + "/api/config", {"token": "sekret123"})
    assert code == 200
    # now an unauthenticated mutating call must be 403
    code, body = _post(BASE + "/api/switch", {"target": "eng-b"})
    assert code == 403, (code, body)
    # and the same call WITH the token is allowed (202 or a switch error, not 403)
    code2, _ = _post(BASE + "/api/switch", {"target": "eng-b"}, token="sekret123")
    assert code2 != 403

def test_register_and_delete_engine(server):
    _post(BASE + "/api/config", {"token": "tok"})
    code, body = _post(BASE + "/api/engines",
                       {"id": "eng-c", "unit": "c-svc.service", "port": 8081, "api_name": "alfred"}, token="tok")
    assert code in (200, 201), (code, body)
    code, body = _get(BASE + "/api/engines")
    assert any(e["id"] == "eng-c" for e in body["engines"])
    # delete it (needs the DELETE method; urllib can do it)
    req = urllib.request.Request(BASE + "/api/engines/eng-c", method="DELETE")
    req.add_header("Authorization", "Bearer tok")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200

def test_unknown_engine_is_400(server):
    _post(BASE + "/api/config", {"token": "tok"})
    code, body = _post(BASE + "/api/switch", {"target": "nope"}, token="tok")
    assert code == 400 and "unknown engine" in body["error"]
