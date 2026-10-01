#!/bin/bash
# Gates for the model-launcher repo. Run with: bash gates.sh
# Order: syntax/build, lint, test, secrets. Any non-zero exit = FAIL.
set -uo pipefail
PY="${MODEL_LAUNCHER_PY:-/usr/bin/python3}"  # interpreter that has pytest (the hermes venv does not)
cd "$(dirname "$0")"
fail=0

echo "== build: python3 -m compileall =="
present=()
for f in server.py registry.py hermes_cfg.py; do [ -f "$f" ] && present+=("$f"); done
if [ ${#present[@]} -gt 0 ]; then "$PY" -m compileall -q "${present[@]}" || { echo "FAIL: compileall"; fail=1; }; else echo "skip: no source modules yet"; fi

echo "== lint: ruff check . =="
if command -v ruff >/dev/null 2>&1; then
  ruff check . || { echo "FAIL: ruff"; fail=1; }
else
  echo "skip: ruff not installed"
fi

echo "== test: pytest -q =="
"$PY" -m pytest -q 2>/dev/null || { echo "FAIL: pytest"; fail=1; }

echo "== secrets: grep for credentials (must be empty) =="
hits=$(git ls-files -co --exclude-standard -z | xargs -0 grep -IlE '(hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|X-Launcher-Token: *[A-Za-z0-9]{20,})' 2>/dev/null || true)
if [ -n "$hits" ]; then echo "SECRET-LIKE CONTENT in: $hits"; fail=1; else echo "secrets: clean"; fi

[ $fail = 0 ] && echo "ALL GATES PASS" || echo "GATES FAILED"
exit $fail
