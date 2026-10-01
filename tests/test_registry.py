import json
import os
import subprocess
import importlib.util

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load_module(name):
    import sys as _sys
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    _sys.modules[name] = m  # register before exec so @dataclass (3.14) can resolve it
    spec.loader.exec_module(m)
    return m

reg = _load_module("registry")


def _write_registry(tmp_path, engines):
    p = tmp_path / "engines.json"
    p.write_text(json.dumps({"engines": engines}))
    return str(p)


def test_load_and_bash_arrays(tmp_path):
    p = _write_registry(tmp_path, [
        {"id": "a", "unit": "u-a.service", "container": "c-a", "port": 8081,
         "api_name": "alfred", "health_url": "http://127.0.0.1:8081/v1/models",
         "boot_budget_s": 600, "default": True},
        {"id": "b", "unit": "u-b.service", "port": 8082, "api_name": "alfred",
         "health_url": "http://127.0.0.1:8082/v1/models", "boot_budget_s": 1500},
    ])
    engines = reg.load(p)
    assert [e.id for e in engines] == ["a", "b"]
    assert engines[0].default is True and engines[1].default is False
    out = reg.to_bash_arrays(engines)
    assert "UNIT[\'a\']=\'u-a.service\'" in out
    assert "BOOT[\'b\']=1500" in out
    assert "APINAME[\'a\']=\'alfred\'" in out
    assert "DEFAULT=\'a\'" in out
    r = subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_hermes_lines_match_known_27b_profile(tmp_path):
    p = _write_registry(tmp_path, [
        {"id": "a", "unit": "u.service", "api_name": "alfred",
         "hermes": {"reasoning_effort": "none", "max_concurrent_children": 8,
                    "child_timeout_seconds": 1200, "stale_timeout_seconds": 900}},
    ])
    lines = reg.to_hermes_lines("a", reg.load(p))
    assert lines == [
        "agent.reasoning_effort none",
        "delegation.max_concurrent_children 8",
        "delegation.child_timeout_seconds 1200",
        "providers.custom.stale_timeout_seconds 900",
    ]


def test_malformed_json_raises(tmp_path):
    p = tmp_path / "engines.json"
    p.write_text("{ not valid json ")
    with pytest.raises(Exception) as ei:
        reg.load(str(p))
    assert "json" in str(ei.value).lower()


def test_invalid_id_rejected(tmp_path):
    p = _write_registry(tmp_path, [{"id": "bad id; rm -rf /", "unit": "u"}])
    with pytest.raises(ValueError):
        reg.load(p)


def test_hostile_id_cannot_inject_into_bash(tmp_path):
    p = _write_registry(tmp_path, [{"id": "ok", "unit": "u"}])
    e = reg.Engine(id="ok", unit="u")
    out = reg.to_bash_arrays([e])
    assert "$(rm" not in out and "`rm" not in out


def test_duplicate_ids_rejected(tmp_path):
    p = _write_registry(tmp_path, [
        {"id": "a", "unit": "u1"}, {"id": "a", "unit": "u2"},
    ])
    with pytest.raises(ValueError):
        reg.load(p)


def test_two_defaults_rejected_on_save(tmp_path):
    p = str(tmp_path / "out.json")
    with pytest.raises(ValueError):
        reg.save(p, [reg.Engine(id="a", default=True), reg.Engine(id="b", default=True)])
