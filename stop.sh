#!/usr/bin/env bash
# stop.sh — توقف تمیز دستیار (SIGTERM؛ پاسخ‌های در حال ارسال تمام و اتصال بسته می‌شود).
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

if service_active; then
    info "توقف سرویس systemd…"
    systemctl --user stop "$SERVICE_NAME.service"
fi

if ! pid="$(running_pid)"; then
    ok "دستیار در حال اجرا نیست."
    exit 0
fi

info "ارسال درخواست توقف به pid $pid …"
kill -TERM "$pid" 2>/dev/null || true
for _ in $(seq 1 30); do
    sleep 0.5
    if ! is_running; then ok "دستیار متوقف شد."; exit 0; fi
done
warn "برنامه در ۱۵ ثانیه خاموش نشد؛ توقف اجباری."
kill -KILL "$pid" 2>/dev/null || true
sleep 1
is_running && die "توقف ممکن نشد (pid $pid)." || ok "دستیار متوقف شد."
