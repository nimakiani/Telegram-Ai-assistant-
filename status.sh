#!/usr/bin/env bash
# status.sh — نمایش وضعیت دستیار، سرویس systemd، تنظیمات و آخرین گزارش‌ها.
#   ./status.sh          خلاصه‌ی وضعیت
#   ./status.sh --logs   دنبال‌کردن زنده‌ی گزارش‌ها (Ctrl+C برای خروج)
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

if [ "${1:-}" = "--logs" ]; then
    if service_active && command -v journalctl >/dev/null 2>&1; then
        exec journalctl --user -u "$SERVICE_NAME.service" -f -n 50
    fi
    [ -f "$APP_LOG" ] || die "هنوز فایل گزارشی وجود ندارد."
    exec tail -n 50 -f "$APP_LOG"
fi

echo "── وضعیت دستیار ──"
if pid="$(running_pid)"; then
    started="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//' || true)"
    ok "در حال اجرا (pid $pid، شروع: ${started:-؟})"
else
    err "در حال اجرا نیست."
fi

echo; echo "── سرویس systemd ──"
if have_systemd_user; then
    if service_installed; then
        printf 'نصب‌شده: بله | فعال‌اکنون: %s | اجرای خودکار با بوت: %s\n' \
            "$(systemctl --user is-active "$SERVICE_NAME.service" 2>/dev/null || true)" \
            "$(systemctl --user is-enabled "$SERVICE_NAME.service" 2>/dev/null || true)"
        if command -v loginctl >/dev/null 2>&1; then
            printf 'اجرا بدون ورود کاربر (linger): %s\n' "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo '؟')"
        fi
    else
        echo "نصب نشده (اختیاری):  ./service.sh install"
    fi
else
    echo "systemd کاربر در این محیط در دسترس نیست."
fi

echo; echo "── تنظیمات ──"
if [ -x "$VENV_PY" ]; then
    set +e; "$VENV_PY" main.py --check; set -e
else
    err "نصب انجام نشده است (./install.sh)."
fi
[ -f .env ] && printf 'مجوز .env: %s\n' "$(stat -c '%a' .env)"
sess="$(find data/session -maxdepth 1 -name '*.session' 2>/dev/null | head -n1 || true)"
[ -n "$sess" ] && printf 'فایل نشست: وجود دارد (مجوز %s)\n' "$(stat -c '%a' "$sess")" || echo "فایل نشست: وجود ندارد"
[ -f data/assistant.db ] && printf 'دیتابیس: %s\n' "$(du -h data/assistant.db | cut -f1)"

echo; echo "── آخرین گزارش‌ها ──"
tail_logs 12 || true
