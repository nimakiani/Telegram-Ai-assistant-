#!/usr/bin/env bash
# scripts/common.sh — توابع مشترک اسکریپت‌های مدیریتی (فقط source می‌شود، مستقیم اجرا نمی‌شود).

# پوشه‌ی پروژه = یک سطح بالاتر از scripts/ ؛ همه‌ی اسکریپت‌ها از هر مسیری قابل اجرا هستند.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="telegram-ai-assistant"
VENV_DIR="$PROJECT_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"
LOCK_FILE="$PROJECT_DIR/data/assistant.lock"
STDOUT_LOG="$PROJECT_DIR/data/logs/stdout.log"
APP_LOG="$PROJECT_DIR/data/logs/assistant.log"

if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_OFF=""
fi

info() { printf '%s\n' "${C_BLUE}•${C_OFF} $*"; }
ok()   { printf '%s\n' "${C_GREEN}✔${C_OFF} $*"; }
warn() { printf '%s\n' "${C_YELLOW}⚠${C_OFF} $*" >&2; }
err()  { printf '%s\n' "${C_RED}✘${C_OFF} $*" >&2; }
die()  { err "$*"; exit 1; }

is_tty() { [ -t 0 ] && [ -t 1 ]; }

# پرسش بله/خیر؛ پیش‌فرض خیر مگر اینکه $2 برابر y باشد. در حالت غیرتعاملی پیش‌فرض را برمی‌گرداند.
confirm() {
    local prompt="$1" default="${2:-n}" reply
    if [ "${ASSUME_YES:-0}" = "1" ]; then return 0; fi
    if ! is_tty; then [ "$default" = "y" ]; return; fi
    if [ "$default" = "y" ]; then prompt="$prompt [Y/n] "; else prompt="$prompt [y/N] "; fi
    read -r -p "$prompt" reply || reply=""
    reply="${reply:-$default}"
    case "$reply" in y|Y|yes|YES|بله) return 0 ;; *) return 1 ;; esac
}

require_venv() {
    [ -x "$VENV_PY" ] || die "محیط مجازی پایتون پیدا نشد. ابتدا نصب را انجام دهید:  ./install.sh"
}

have_systemd_user() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

service_unit_path() { printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME.service"; }
service_installed() { [ -f "$(service_unit_path)" ]; }
service_active() { have_systemd_user && systemctl --user is-active --quiet "$SERVICE_NAME.service" 2>/dev/null; }

# pid برنامه از فایل قفل؛ فقط وقتی قفل واقعاً در اختیار یک پروسه است برمی‌گردد.
running_pid() {
    [ -f "$LOCK_FILE" ] || return 1
    local pid
    pid="$(tr -dc '0-9' < "$LOCK_FILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # مطمئن شویم همان برنامه‌ی ماست (نه یک پروسه‌ی بی‌ربط با pid تکراری)
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "main.py" || return 1
    if command -v flock >/dev/null 2>&1; then
        # اگر بتوانیم قفل را بگیریم یعنی کسی آن را نگه نداشته (pid کهنه)
        if flock -n "$LOCK_FILE" true 2>/dev/null; then return 1; fi
    fi
    printf '%s' "$pid"
}

is_running() { running_pid >/dev/null 2>&1; }

ensure_dirs() {
    mkdir -p "$PROJECT_DIR/data/session" "$PROJECT_DIR/data/logs" "$PROJECT_DIR/data/tmp" "$PROJECT_DIR/backups"
    chmod 700 "$PROJECT_DIR/data/session" 2>/dev/null || true
    chmod 700 "$PROJECT_DIR/backups" 2>/dev/null || true
}

# دسترسی فایل‌های حساس را محدود می‌کند (600).
secure_files() {
    [ -f "$PROJECT_DIR/.env" ] && chmod 600 "$PROJECT_DIR/.env"
    find "$PROJECT_DIR/data" -maxdepth 3 \( -name '*.session*' -o -name '*.db' -o -name '*.db-*' -o -name '*.log' \) \
        -type f -exec chmod 600 {} + 2>/dev/null || true
    if [ -d "$PROJECT_DIR/backups" ]; then
        find "$PROJECT_DIR/backups" -type f -exec chmod 600 {} + 2>/dev/null || true
    fi
    if [ -d "$PROJECT_DIR/data/backups" ]; then
        find "$PROJECT_DIR/data/backups" -type f -exec chmod 600 {} + 2>/dev/null || true
    fi
    return 0
}

# آخرین خطوط لاگ برنامه (لاگ‌ها قبل از نوشتن، اطلاعات حساس را پوشانده‌اند).
tail_logs() {
    local n="${1:-15}"
    if [ -f "$APP_LOG" ]; then tail -n "$n" "$APP_LOG"; elif [ -f "$STDOUT_LOG" ]; then tail -n "$n" "$STDOUT_LOG"; fi
}
