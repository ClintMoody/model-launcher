import json
import os
import subprocess
import importlib.util
import sys
import tempfile
import time
import urllib.request

import pytest
import threading
import http.server
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCKBIN = os.path.join(ROOT, "tests", "mockbin")

def _start_server(base, extra=None):
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
    env.update(extra or {})
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


# ---- adversarial-review regression tests ---------------------------------------

def _start_server_with(tmp, port, reg_obj, extra_env=None):
    env = {
        "PATH": MOCKBIN + ":" + os.environ.get("PATH", ""),
        "XDG_RUNTIME_DIR": tmp,
        "LAUNCHER_REGISTRY": os.path.join(tmp, "engines.json"),
        "LAUNCHER_HERMES_CFG": os.path.join(ROOT, "hermes_cfg.py"),
        "LAUNCHER_PORT": str(port), "LAUNCHER_BIND": "127.0.0.1",
        "HOME": os.path.join(tmp, "home"),
    }
    os.makedirs(os.path.join(env["HOME"], ".hermes"), exist_ok=True)
    with open(os.path.join(env["HOME"], ".hermes", "config.yaml"), "w") as f:
        f.write("model:\n  provider: custom\nagent:\n  reasoning_effort: none\n")
    with open(env["LAUNCHER_REGISTRY"], "w") as f:
        json.dump(reg_obj, f)
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen(["python3", os.path.join(ROOT, "server.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2)
    return p


def _http(method, url, body=None, tok=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    if tok:
        h["X-Launcher-Token"] = tok
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_ui_token_injection_and_auth(tmp_path):
    """Web UI must be able to drive mutating endpoints once a token is set (defect fix)."""
    base = tmp_path / "ui"
    base.mkdir(exist_ok=True)
    tok = "ui-secret-token-1234"
    p = _start_server_with(str(base), 18795, {
        "engines": [
            {"id": "a", "label": "A", "unit": "a-svc.service", "port": 8081, "api_name": "m",
             "health_url": "http://127.0.0.1:8081/v1/models", "boot_budget_s": 3,
             "gpu_free_mib": 1,
             "hermes": {"reasoning_effort": "none", "max_concurrent_children": 8,
                        "child_timeout_seconds": 1200, "stale_timeout_seconds": 900},
             "default": True}],
        "config": {"token": tok},
    }, extra_env={"MOCK_ACTIVE_UNITS": "a-svc.service"})
    try:
        base_url = "http://127.0.0.1:18795"
        # direct API without token -> 403
        s, _ = _http("POST", base_url + "/api/switch", {"target": "a"})
        assert s == 403, f"expected 403 without token, got {s}"
        # direct API with token -> 202
        s, _ = _http("POST", base_url + "/api/switch", {"target": "a"}, tok)
        assert s == 202, f"expected 202 with token, got {s}"
        # served page must carry the token for the UI's own fetches
        with urllib.request.urlopen(base_url + "/", timeout=10) as r:
            page = r.read().decode()
        assert tok in page, "token not injected into served html"
        assert "X-Launcher-Token" in page, "UI does not send the token header"
        # UI-style hermes-sync (token header) -> 200
        s, _ = _http("POST", base_url + "/api/hermes-sync", {}, tok)
        assert s == 200, f"expected 200 for UI hermes-sync, got {s}"
    finally:
        p.terminate()


class _OllamaMock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": "llama3.1"}, {"name": "mistral"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def test_status_reports_ollama_up_and_models(tmp_path):
    """status() must treat a non-OpenAI engine (Ollama /api/tags) as up with its model list (defect fix)."""
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); mock_port = s.getsockname()[1]; s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", mock_port), _OllamaMock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = tmp_path / "oll"
    base.mkdir(exist_ok=True)
    p = _start_server_with(str(base), 18796, {
        "engines": [
            {"id": "ollama", "label": "Ollama", "unit": "ollama-svc.service", "port": mock_port,
             "api_name": "llama3.1", "health_url": f"http://127.0.0.1:{mock_port}/api/tags",
             "boot_budget_s": 300, "gpu_free_mib": 1,
             "hermes": {"reasoning_effort": "medium", "max_concurrent_children": 2,
                        "child_timeout_seconds": 3600, "stale_timeout_seconds": 1800},
             "default": True}],
        "config": {"token": ""},
    }, extra_env={"MOCK_ACTIVE_UNITS": "ollama-svc.service"})
    try:
        with urllib.request.urlopen("http://127.0.0.1:18796/api/status", timeout=10) as r:
            d = json.load(r)
        assert d["active"] == "ollama"
        assert d["up"] is True, f"Ollama not reported up: {d.get('up')}"
        assert d["models"] == ["llama3.1", "mistral"], f"wrong models: {d.get('models')}"
    finally:
        p.terminate()
        srv.shutdown()
