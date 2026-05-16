#!/usr/bin/env bash
# Postcheck git pre-push hook (v0). Drop into .git/hooks/pre-push.
set -euo pipefail
exec postcheck verify --since "@{push}"
