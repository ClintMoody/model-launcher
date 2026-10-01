import os
import subprocess
import json
import tempfile
import importlib.util
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCKBIN = os.path.join(ROOT, "tests", "mockbin")

def run_switch(env_extra, *args):
    base = tempfile.mkdtemp(prefix="ml-test-")
    env = {
        "PATH": MOCKBIN + ":" + os.environ.get("PATH", ""),
        "XDG_RUNTIME_DIR": base,          # fresh lock dir per test (no stale flock)
        "MOCK_LOG": os.path.join(base, "mock-systemctl.log"),
        "MOCK_ACTIVE_UNITS": "other-svc.service",
        "LAUNCHER_REGISTRY": os.path.join(base, "test-engines.json"),
        "LAUNCHER_HERMES_CFG": os.path.join(ROOT, "hermes_cfg.py"),
        "LAUNCHER_PORT": "8081",
        "HOME": os.path.join(base, "home"),
    }
    env.update(env_extra)
    os.makedirs(os.path.join(env["HOME"], ".hermes"), exist_ok=True)
    with open(os.path.join(env["HOME"], ".hermes", "config.yaml"), "w") as f:
        f.write("model:\n  provider: custom\nagent:\n  reasoning_effort: none\ndelegation:\n  max_concurrent_children: 2\n  child_timeout_seconds: 3600\nproviders:\n  custom:\n    stale_timeout_seconds: 1800\n")
    # registry
    reg = {"engines": [
        {"id": "eng-a", "unit": "a-svc.service", "container": None, "port": 8081,
         "api_name": "alfred", "health_url": "http://127.0.0.1:8081/v1/models",
         "boot_budget_s": 2, "gpu_free_mib": 1, "require_mount": None,
         "hermes": {"reasoning_effort": "none", "max_concurrent_children": 8,
                    "child_timeout_seconds": 1200, "stale_timeout_seconds": 900},
         "default": True},
        {"id": "eng-b", "unit": "other-svc.service", "container": None, "port": 8081,
         "api_name": "alfred", "health_url": "http://127.0.0.1:8081/v1/models",
         "boot_budget_s": 2, "gpu_free_mib": 1, "require_mount": None,
         "hermes": {"reasoning_effort": "medium", "max_concurrent_children": 2,
                    "child_timeout_seconds": 3600, "stale_timeout_seconds": 1800},
         "default": False},
    ]}
    with open(env["LAUNCHER_REGISTRY"], "w") as f:
        json.dump(reg, f)
    # clear the mock log
    if os.path.exists(env["MOCK_LOG"]):
        os.remove(env["MOCK_LOG"])
    r = subprocess.run(["bash", os.path.join(ROOT, "bin", "llm-switch"), *args],
                       env=env, capture_output=True, text=True)
    return r, env

def test_switch_stops_other_starts_target_and_writes_hermes():
    r, env = run_switch({}, "eng-a")
    log = open(env["MOCK_LOG"]).read() if os.path.exists(env["MOCK_LOG"]) else ""
    # we are switching TO eng-a (a-svc.service); the currently-active other-svc.service must be stopped
    assert "stop other-svc.service" in log, f"expected to stop the active other unit; log={log}"
    assert "start a-svc.service" in log, f"expected to start the target unit; log={log}"
    # hermes profile for eng-a should have been written (reasoning_effort none -> none is no-op,
    # but delegation/concurrent should move to 8 if not already). Check the config got the eng-a values.
    cfg = open(os.path.join(env["HOME"], ".hermes", "config.yaml")).read()
    assert "max_concurrent_children: 8" in cfg, f"hermes profile not applied to config: {cfg}"
    assert "DONE: eng-a is serving" in r.stdout, r.stdout

def test_unknown_target_is_rejected():
    r, env = run_switch({}, "nope")
    assert r.returncode != 0
    assert "unknown target" in r.stdout

def test_status_lists_registered_engines():
    r, env = run_switch({}, "status")
    assert "eng-a" in r.stdout and "eng-b" in r.stdout, r.stdout
