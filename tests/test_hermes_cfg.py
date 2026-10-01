import os
import subprocess
import textwrap
import importlib.util

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load(name):
    import sys
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

# hermes_cfg.py hardcodes PATH = ~/.hermes/config.yaml; for the test we monkeypatch it to a temp file.
hc = _load("hermes_cfg")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(
        """
        model:
          provider: custom
          model: alfred
        agent:
          reasoning_effort: none
        delegation:
          max_concurrent_children: 8
        """
    ).lstrip())
    monkeypatch.setattr(hc, "PATH", str(p))
    return p


def test_get_returns_value(cfg):
    out = subprocess.run([os.sys.executable, os.path.join(ROOT, "hermes_cfg.py"),
                          "get", "agent.reasoning_effort"], capture_output=True, text=True)
    # hermes_cfg reads PATH from its module global, not env; so call the function directly instead
    assert hc.lookup(__import__("yaml").safe_load(cfg.read_text()), "agent.reasoning_effort") == "none"


def test_set_none_stays_string_not_null(cfg):
    # the bug: `hermes config set` writes "none" as YAML null. hermes_cfg must keep it the string "none".
    import yaml
    data = yaml.safe_load(cfg.read_text())
    before = data["agent"]["reasoning_effort"]
    # simulate what `set` does: call main with argv
    import sys
    sys.argv = ["hermes_cfg.py", "set", "agent.reasoning_effort", "none"]
    # PATH is patched to the temp file; run main
    hc.main()
    raw = cfg.read_text()
    data2 = yaml.safe_load(raw)
    # "none" must round-trip as the string "none", not None
    assert data2["agent"]["reasoning_effort"] == "none", f"got {data2['agent']['reasoning_effort']!r} (null bug)"
