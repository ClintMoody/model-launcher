#!/bin/bash
# Fail if anything that looks like a credential is about to be committed. Run before every push.
cd "$(dirname "$0")"
hits=$(git ls-files -co --exclude-standard -z | xargs -0 grep -IlE '(hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|X-Launcher-Token: *[A-Za-z0-9]{16,})' 2>/dev/null || true)
# also scan the live (gitignored) registry for a token that might have been committed by mistake
if [ -f engines.json ]; then
  hits="$hits $(grep -IlE '"token"[[:space:]]*:[[:space:]]*"[A-Za-z0-9]{16,}"' engines.json 2>/dev/null || true)"
fi
hits=$(echo $hits)
if [ -n "$hits" ]; then echo "SECRET-LIKE CONTENT in: $hits"; exit 1; fi
echo "secret scan: clean"
