#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cao_home() {
  if [[ -n "${CAO_HOME_DIR:-}" ]]; then printf '%s\n' "$CAO_HOME_DIR"; else dirname "$(cao config path)"; fi
}
cao_has_command() { cao "$1" --help 2>/dev/null | grep -Eq "(^|[[:space:]])$2([[:space:]]|$)"; }
