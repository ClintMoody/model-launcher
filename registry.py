#!/usr/bin/env python3
"""registry.py: the single source of truth for what the launcher can switch to.

An engine is a record in engines.json: the unit/container that owns it, its port, the model
name it serves, its health endpoint, its boot budget, and the Hermes profile to apply when it
is active. The switch engine (bin/llm-switch) reads this via to_bash_arrays(); the server
(server.py) is the only writer of the file (atomic save).

Stdlib only (json, os, tempfile, dataclasses). No pyyaml.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Optional

# strict id charset: lowercase letters, digits, dash, underscore. Prevents bash injection
# through the arrays to_bash_arrays() emits (an id is used as an array key and in unit names).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# paths/units we interpolate into bash must not carry shell metacharacters
_SAFE_RE = re.compile(r"^[A-Za-z0-9._/: -]*$")


@dataclass
class HermesProfile:
    reasoning_effort: str = "medium"
    max_concurrent_children: int = 2
    child_timeout_seconds: int = 3600
    stale_timeout_seconds: int = 1800

    def to_lines(self) -> list[str]:
        return [
            f"agent.reasoning_effort {self.reasoning_effort}",
            f"delegation.max_concurrent_children {self.max_concurrent_children}",
            f"delegation.child_timeout_seconds {self.child_timeout_seconds}",
            f"providers.custom.stale_timeout_seconds {self.stale_timeout_seconds}",
        ]


@dataclass
class Engine:
    id: str
    label: str = ""
    unit: str = ""                      # systemd user unit name that owns this engine
    container: Optional[str] = None     # docker container name (for the SIGKILL path)
    port: int = 8081
    api_name: str = "alfred"            # the OpenAI /v1 model name; the smoke test uses this
    health_url: str = ""
    boot_budget_s: int = 600
    gpu_free_mib: int = 1500
    require_mount: Optional[str] = None # refuse to switch if this path is not mounted
    hermes: HermesProfile = field(default_factory=HermesProfile)
    default: bool = False               # exactly one engine may be the rollback target

    def __post_init__(self):
        if not _ID_RE.match(self.id):
            raise ValueError(f"invalid engine id {self.id!r}: must match {_ID_RE.pattern}")
        if self.unit and not _SAFE_RE.match(self.unit):
            raise ValueError(f"invalid unit name {self.unit!r}")
        if self.container and not _SAFE_RE.match(self.container):
            raise ValueError(f"invalid container name {self.container!r}")
        if not self.health_url:
            self.health_url = f"http://127.0.0.1:{self.port}/v1/models"


def _engine_from_dict(d: dict) -> Engine:
    h = d.get("hermes", {}) or {}
    hermes = HermesProfile(
        reasoning_effort=str(h.get("reasoning_effort", "medium")),
        max_concurrent_children=int(h.get("max_concurrent_children", 2)),
        child_timeout_seconds=int(h.get("child_timeout_seconds", 3600)),
        stale_timeout_seconds=int(h.get("stale_timeout_seconds", 1800)),
    )
    return Engine(
        id=str(d["id"]),
        label=str(d.get("label", "")),
        unit=str(d.get("unit", "")),
        container=d.get("container"),
        port=int(d.get("port", 8081)),
        api_name=str(d.get("api_name", "alfred")),
        health_url=str(d.get("health_url", "")),
        boot_budget_s=int(d.get("boot_budget_s", 600)),
        gpu_free_mib=int(d.get("gpu_free_mib", 1500)),
        require_mount=d.get("require_mount"),
        hermes=hermes,
        default=bool(d.get("default", False)),
    )


def load(path: str) -> list[Engine]:
    """Load the registry. Raises ValueError with a clear message on any malformation."""
    if not os.path.exists(path):
        raise ValueError(f"registry not found: {path}")
    with open(path) as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON in registry {path}: {e}") from e
    if isinstance(raw, dict):
        raw = raw.get("engines", [])
    if not isinstance(raw, list):
        raise ValueError("registry must be a list of engine objects")
    engines = [_engine_from_dict(d) for d in raw]
    ids = [e.id for e in engines]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate engine id(s) in registry: {sorted(ids)}")
    if sum(1 for e in engines if e.default) > 1:
        raise ValueError("at most one engine may be marked default (the rollback target)")
    return engines


def save(path: str, engines: list[Engine]) -> None:
    """Atomic write: temp file in the same dir, then os.replace. The server is the only caller."""
    defaults = sum(1 for e in engines if e.default)
    if defaults > 1:
        raise ValueError("refusing to save: more than one engine marked default")
    payload = {"engines": [
        {
            "id": e.id, "label": e.label, "unit": e.unit, "container": e.container,
            "port": e.port, "api_name": e.api_name, "health_url": e.health_url,
            "boot_budget_s": e.boot_budget_s, "gpu_free_mib": e.gpu_free_mib,
            "require_mount": e.require_mount,
            "hermes": {
                "reasoning_effort": e.hermes.reasoning_effort,
                "max_concurrent_children": e.hermes.max_concurrent_children,
                "child_timeout_seconds": e.hermes.child_timeout_seconds,
                "stale_timeout_seconds": e.hermes.stale_timeout_seconds,
            },
            "default": e.default,
        } for e in engines
    ]}
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".engines.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def to_bash_arrays(engines: list[Engine]) -> str:
    """Emit the declare -A lines bin/llm-switch evals. Quoted so no value can inject."""
    lines = ["declare -A UNIT BOOT CONTAINER APINAME HEALTHURL"]
    for e in engines:
        lines.append(
            f"UNIT['{e.id}']='{e.unit}' "
            f"BOOT['{e.id}']={e.boot_budget_s} "
            f"CONTAINER['{e.id}']='{e.container or ''}' "
            f"APINAME['{e.id}']='{e.api_name}' "
            f"HEALTHURL['{e.id}']='{e.health_url}'"
        )
    default = next((e.id for e in engines if e.default), None)
    if default:
        lines.append(f"DEFAULT='{default}'")
    return "\n".join(lines) + "\n"


def to_hermes_lines(engine_id: str, engines: list[Engine]) -> list[str]:
    """The key/value lines apply_hermes consumes, for the named engine."""
    for e in engines:
        if e.id == engine_id:
            return e.hermes.to_lines()
    raise ValueError(f"unknown engine id: {engine_id}")


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "engines.json"
    print(to_bash_arrays(load(path)))
