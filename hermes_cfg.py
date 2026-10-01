#!/usr/bin/env python3
"""Minimal, format-preserving editor for ~/.hermes/config.yaml, used by llm-switch.

Why not `hermes config set`: it writes the string "none" as a YAML null (`reasoning_effort:`), which Hermes
then reads as "unset" (provider default = thinking ON) instead of "thinking off". This edits only the value
text on the one line that holds the key, leaves every other byte alone, and verifies the result by
re-parsing the YAML.

usage: hermes_cfg.py get <dotted.key>
       hermes_cfg.py set <dotted.key> <value>     (value is written verbatim, e.g. none / 8 / medium)
"""
import os, re, sys, yaml

PATH = os.path.expanduser("~/.hermes/config.yaml")


def lookup(data, key):
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            raise KeyError(key)
        data = data[part]
    return data


def find_line(lines, key):
    """Index of the line holding the last component of a dotted key, walking block-mapping indentation."""
    parts, start, indent = key.split("."), 0, -1
    for depth, part in enumerate(parts):
        found = None
        for i in range(start, len(lines)):
            m = re.match(r"^( *)([^\s#][^:]*?):(\s|$)", lines[i])
            if not m:
                continue
            ind = len(m.group(1))
            if ind <= indent:  # left the parent block
                break
            if m.group(2).strip().strip("'\"") == part and (depth == 0 and ind == 0 or depth > 0 and ind > indent):
                found, indent, start = i, ind, i + 1
                break
        if found is None:
            raise KeyError(key)
    return found


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("get", "set"):
        print(__doc__); sys.exit(2)
    key = sys.argv[2]
    text = open(PATH).read()
    if sys.argv[1] == "get":
        v = lookup(yaml.safe_load(text), key)
        print("" if v is None else v); return
    value = sys.argv[3]
    lines = text.split("\n")
    i = find_line(lines, key)
    m = re.match(r"^( *[^:]+:)(\s*)(.*?)(\s+#.*)?$", lines[i])
    lines[i] = f"{m.group(1)} {value}{m.group(4) or ''}"
    new = "\n".join(lines)
    got = lookup(yaml.safe_load(new), key)
    want = yaml.safe_load(value)
    if got != want:
        sys.exit(f"verify failed for {key}: wrote {value!r}, parsed back {got!r}")
    tmp = PATH + ".tmp-llm-switch"
    with open(tmp, "w") as f:
        f.write(new)
    os.replace(tmp, PATH)


if __name__ == "__main__":
    main()
