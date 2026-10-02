"""GPU power cap: the gpu-power helper (against the mock nvidia-smi) and the /api/power endpoint."""
import json
import os
import subprocess
import tempfile
import time

import pytest

from tests.test_server import BASE, _get, _post, _start_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPU_POWER = os.path.join(ROOT, "bin", "gpu-power")
MOCK_SMI = os.path.join(ROOT, "tests", "mockbin", "nvidia-smi")


def _power_env(base):
    return {"GPU_POWER_CONF": os.path.join(base, "nvidia-power-limit"),
            "GPU_POWER_SMI": MOCK_SMI,
            "MOCK_SMI_STATE": os.path.join(base, "smi-state"),
            "GPU_POWER_ALLOW_NONROOT": "1"}


def _gp(env, *args):
    r = subprocess.run([GPU_POWER, *args], env=dict(os.environ, **env),
                       capture_output=True, text=True)
    return r.returncode, (json.loads(r.stdout) if r.returncode == 0 else r.stderr)


@pytest.fixture
def penv():
    return _power_env(tempfile.mkdtemp(prefix="ml-power-"))


def test_status_starts_uncapped(penv):
    code, st = _gp(penv, "status")
    assert code == 0 and st["enabled"] is False
    assert [g["limit"] for g in st["gpus"]] == [450, 450]
    assert st["gpus"][1]["draw"] is None          # [N/A] draw becomes null, JSON stays valid


def test_set_off_on_roundtrip(penv):
    code, st = _gp(penv, "set", "250")
    assert code == 0 and st["enabled"] and st["cap"] == 250
    assert [g["limit"] for g in st["gpus"]] == [250, 250]
    code, st = _gp(penv, "off")
    assert code == 0 and not st["enabled"] and st["cap"] == 250     # cap remembered
    assert [g["limit"] for g in st["gpus"]] == [450, 450]          # back to driver default
    code, st = _gp(penv, "on")
    assert code == 0 and st["enabled"] and [g["limit"] for g in st["gpus"]] == [250, 250]
    assert "ENABLED=1" in open(penv["GPU_POWER_CONF"]).read()


def test_rejects_out_of_range_and_junk(penv):
    assert _gp(penv, "set", "5000")[0] != 0
    assert _gp(penv, "set", "99")[0] != 0
    assert _gp(penv, "set", "25O")[0] != 0
    assert _gp(penv, "on")[0] != 0                 # nothing saved yet
    assert _gp(penv, "bogus")[0] != 0


def test_refuses_writes_without_root(penv):
    env = dict(penv, GPU_POWER_ALLOW_NONROOT="0")
    if os.geteuid() == 0:
        pytest.skip("running as root")
    code, err = _gp(env, "set", "300")
    assert code != 0 and "root" in err


@pytest.fixture
def power_server():
    base = tempfile.mkdtemp(prefix="ml-power-srv-")
    extra = dict(_power_env(base), LAUNCHER_GPU_POWER=GPU_POWER, LAUNCHER_SUDO="")
    p, env = _start_server(base, extra)
    time.sleep(1.0)
    yield env
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


def test_api_power_status_and_auth(power_server):
    code, st = _get(BASE + "/api/status")
    assert code == 200 and st["power"]["available"] is True and st["power"]["enabled"] is False
    _post(BASE + "/api/config", {"token": "tok"})
    code, _ = _post(BASE + "/api/power", {"action": "set", "watts": 300})
    assert code == 403
    code, body = _post(BASE + "/api/power", {"action": "set", "watts": 300}, token="tok")
    assert code == 200 and body["power"]["cap"] == 300 and body["power"]["gpus"][0]["limit"] == 300
    code, body = _post(BASE + "/api/power", {"action": "off"}, token="tok")
    assert code == 200 and body["power"]["gpus"][0]["limit"] == 450


def test_api_power_validation(power_server):
    _post(BASE + "/api/config", {"token": "tok"})
    assert _post(BASE + "/api/power", {"action": "set", "watts": 9000}, token="tok")[0] == 400
    assert _post(BASE + "/api/power", {"action": "set", "watts": "300"}, token="tok")[0] == 400
    assert _post(BASE + "/api/power", {"action": "explode"}, token="tok")[0] == 400
