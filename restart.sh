#!/usr/bin/env bash
# restart.sh — راه‌اندازی مجدد (اعمال تغییرات .env).
set -Eeuo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/stop.sh"
exec "$DIR/start.sh" "$@"
