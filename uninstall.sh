#!/usr/bin/env bash
# uninstall.sh — حذف برنامه (سرویس systemd + محیط مجازی). اطلاعات شخصی فقط با تأیید صریح شما پاک می‌شود.
#
#   ./uninstall.sh                 حذف برنامه؛ درباره‌ی داده‌ها (.env، Session، دیتابیس) جداگانه می‌پرسد
#   ./uninstall.sh --keep-data     حذف برنامه و نگه‌داشتن همه‌ی داده‌ها بدون پرسش
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

KEEP_DATA=0
for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=1 ;;
        -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "گزینه‌ی ناشناخته: $arg" ;;
    esac
done

# حذف امن: فقط مسیرهایی که دقیقاً داخل پوشه‌ی پروژه‌اند (هرگز sudo، هرگز مسیر خالی یا ریشه)
safe_remove() {
    local target="$1"
    [ -n "$target" ] && [ -e "$target" ] || return 0
    case "$target" in
        "$PROJECT_DIR"/*) ;;
        *) die "از حذف مسیر خارج از پروژه خودداری شد: $target" ;;
    esac
    [ "$target" != "$PROJECT_DIR/" ] && [ "$target" != "$PROJECT_DIR" ] || die "مسیر نامعتبر."
    rm -rf -- "$target"
}

echo "این کار برنامه را متوقف می‌کند و سرویس systemd و محیط مجازی (venv) را حذف می‌کند."
confirm "ادامه می‌دهید؟" n || { echo "لغو شد."; exit 0; }

"$PROJECT_DIR/stop.sh" || true

if have_systemd_user && service_installed; then
    systemctl --user disable --now "$SERVICE_NAME.service" 2>/dev/null || true
    rm -f -- "$(service_unit_path)"
    systemctl --user daemon-reload
    ok "سرویس systemd حذف شد."
fi

safe_remove "$VENV_DIR"; ok "محیط مجازی حذف شد."
find "$PROJECT_DIR" -name '__pycache__' -type d -not -path "$VENV_DIR/*" -prune -exec rm -rf -- {} + 2>/dev/null || true

DELETE_DATA=0
if [ "$KEEP_DATA" = 0 ] && { [ -f .env ] || [ -d data ] || [ -d backups ]; }; then
    echo
    warn "اطلاعات شخصی شما: .env (کلیدها)، data/ (Session تلگرام، دیتابیس مکالمات، گزارش‌ها) و backups/"
    echo "اگر پاک شوند قابل بازگشت نیستند (مگر پشتیبان خودتان را جای دیگری داشته باشید)."
    if is_tty; then
        read -r -p "برای پاک‌کردن همه‌ی این اطلاعات عبارت «yes» را کامل بنویسید (هر چیز دیگر = نگه‌داشتن): " answer || answer=""
        [ "$answer" = "yes" ] && DELETE_DATA=1
    fi
fi

if [ "$DELETE_DATA" = 1 ]; then
    safe_remove "$PROJECT_DIR/data"
    safe_remove "$PROJECT_DIR/backups"
    safe_remove "$PROJECT_DIR/.env"
    ok "اطلاعات شخصی حذف شد."
    cat <<MSG

⚠️ توصیه‌ی امنیتی: نشست (Session) روی سرور تلگرام هنوز فعال است. برای بستن کامل آن:
   تلگرام ← Settings ← Devices ← نشست «Telegram AI Assistant» را Terminate کنید.
MSG
else
    ok "اطلاعات شما نگه داشته شد (.env، data/، backups/). با نصب دوباره همان تنظیمات و Session استفاده می‌شود."
fi

echo
echo "حذف برنامه تمام شد. برای حذف کدها می‌توانید پوشه‌ی پروژه را خودتان پاک کنید:"
echo "  $PROJECT_DIR"
