#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
exec python3 "$PROJECT_ROOT/scripts/review_apply.py" "$@"
