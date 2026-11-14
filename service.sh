#!/usr/bin/env bash
# service.sh — مدیریت سرویس systemd «سطح کاربر» (بدون نیاز به root برای اجرای معمولی).
#
#   ./service.sh install      نصب سرویس (اجرا نمی‌کند)
#   ./service.sh enable       اجرای خودکار با ورود/روشن‌شدن سیستم
#   ./service.sh disable      غیرفعال‌کردن اجرای خودکار
#   ./service.sh start|stop|restart|status
#   ./service.sh logs         مشاهده‌ی زنده‌ی گزارش‌ها (journalctl)
#   ./service.sh boot-on      اجرا حتی بدون ورود کاربر (loginctl enable-linger)
#   ./service.sh boot-off     برگرداندن linger
#   ./service.sh uninstall    حذف سرویس
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

have_systemd_user || die "systemd کاربر در این محیط در دسترس نیست (WSL/کانتینر؟). به‌جای آن از ./start.sh استفاده کنید."
UNIT="$(service_unit_path)"

install_unit() {
    require_venv
    ensure_dirs
    mkdir -p "$(dirname "$UNIT")"
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/systemd/$SERVICE_NAME.service.template" > "$UNIT"
    chmod 644 "$UNIT"
    systemctl --user daemon-reload
    ok "سرویس نصب شد: $UNIT"
}

cmd="${1:-}"
case "$cmd" in
    install)
        install_unit
        echo "برای شروع:  ./service.sh start    — برای اجرای خودکار:  ./service.sh enable"
        ;;
    enable)
        service_installed || install_unit
        systemctl --user enable "$SERVICE_NAME.service"
        ok "اجرای خودکار فعال شد (با ورود شما به سیستم شروع می‌شود)."
        linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)"
        if [ "$linger" != "yes" ]; then
            info "اگر می‌خواهید بعد از روشن‌شدن سیستم و «بدون ورود» هم اجرا شود:  ./service.sh boot-on"
        fi
        ;;
    disable)
        service_installed || die "سرویس نصب نیست."
        systemctl --user disable "$SERVICE_NAME.service"
        ok "اجرای خودکار غیرفعال شد (سرویس همچنان قابل اجرای دستی است)."
        ;;
    start)
        service_installed || install_unit
        if pid="$(running_pid)" && ! service_active; then
            die "دستیار با ./start.sh در حال اجراست (pid $pid). ابتدا ./stop.sh را بزنید تا با دو نمونه‌ی هم‌زمان نشست خراب نشود."
        fi
        systemctl --user start "$SERVICE_NAME.service"
        sleep 2
        systemctl --user is-active --quiet "$SERVICE_NAME.service" && ok "سرویس اجرا شد." || { err "سرویس بالا نیامد؛ گزارش:"; journalctl --user -u "$SERVICE_NAME.service" -n 20 --no-pager || true; exit 1; }
        ;;
    stop)
        systemctl --user stop "$SERVICE_NAME.service"; ok "سرویس متوقف شد." ;;
    restart)
        service_installed || install_unit
        systemctl --user restart "$SERVICE_NAME.service"; ok "سرویس دوباره راه‌اندازی شد." ;;
    status)
        systemctl --user status "$SERVICE_NAME.service" --no-pager || true ;;
    logs)
        exec journalctl --user -u "$SERVICE_NAME.service" -f -n 50 ;;
    boot-on)
        info "فعال‌سازی «اجرا بدون ورود کاربر» (linger). ممکن است رمز کاربر/sudo پرسیده شود."
        loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
        ok "فعال شد؛ سرویس بعد از بوت حتی بدون ورود شما اجرا می‌شود (اگر enable کرده باشید)." ;;
    boot-off)
        loginctl disable-linger "$USER" 2>/dev/null || sudo loginctl disable-linger "$USER"
        ok "غیرفعال شد." ;;
    uninstall)
        systemctl --user stop "$SERVICE_NAME.service" 2>/dev/null || true
        systemctl --user disable "$SERVICE_NAME.service" 2>/dev/null || true
        rm -f -- "$UNIT"
        systemctl --user daemon-reload
        ok "سرویس حذف شد (تنظیمات و داده‌های شما دست‌نخورده است)." ;;
    ""|-h|--help)
        sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
    *)
        die "دستور ناشناخته: $cmd  (راهنما: ./service.sh --help)" ;;
esac
