#!/usr/bin/env bash
# start.sh — اجرای دستیار.
#
#   ./start.sh              اجرا در پس‌زمینه (یا شروع سرویس systemd اگر نصب شده باشد)
#   ./start.sh --foreground اجرا در همین ترمینال (Ctrl+C برای توقف)
#   ./start.sh --setup      راهنمای راه‌اندازی/ورود دوباره به تلگرام
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

MODE="background"
for arg in "$@"; do
    case "$arg" in
        --foreground|-f) MODE="foreground" ;;
        --setup) MODE="setup" ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "گزینه‌ی ناشناخته: $arg" ;;
    esac
done

require_venv
ensure_dirs
secure_files

if [ "$MODE" = "setup" ]; then
    if is_running; then
        warn "دستیار در حال اجراست. برای ورود دوباره ابتدا آن را متوقف کنید:  ./stop.sh"
        exit 1
    fi
    [ -f .env ] || cp .env.example .env
    chmod 600 .env
    "$VENV_PY" main.py --setup
    secure_files
    exit $?
fi

if is_running; then
    ok "دستیار همین الان در حال اجراست (pid $(running_pid))."
    exit 0
fi

# بررسی تنظیمات و نشست پیش از اجرا
set +e
"$VENV_PY" main.py --check
check_code=$?
set -e
if [ "$check_code" -ne 0 ]; then
    if is_tty; then
        info "راه‌اندازی هنوز کامل نیست؛ راهنما اجرا می‌شود…"
        "$VENV_PY" main.py --setup
        secure_files
    else
        die "راه‌اندازی کامل نیست. ابتدا در ترمینال اجرا کنید:  ./start.sh --setup"
    fi
fi

if [ "$MODE" = "foreground" ]; then
    exec "$VENV_PY" main.py
fi

if service_installed && have_systemd_user; then
    info "شروع سرویس systemd…"
    systemctl --user start "$SERVICE_NAME.service"
else
    : >> "$STDOUT_LOG"; chmod 600 "$STDOUT_LOG"
    info "اجرا در پس‌زمینه…"
    setsid nohup "$VENV_PY" "$PROJECT_DIR/main.py" >> "$STDOUT_LOG" 2>&1 < /dev/null &
fi

# چند ثانیه صبر می‌کنیم تا مطمئن شویم برنامه واقعاً بالا آمده است
for _ in $(seq 1 20); do
    sleep 0.5
    if is_running; then
        ok "دستیار اجرا شد (pid $(running_pid)).  وضعیت: ./status.sh   توقف: ./stop.sh"
        exit 0
    fi
done
err "برنامه بالا نیامد. آخرین خطوط گزارش:"
tail_logs 15 >&2 || true
exit 1
