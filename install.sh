#!/usr/bin/env bash
# install.sh — نصب کامل و خودکار دستیار هوشمند تلگرام روی Ubuntu/Debian.
#
#   ./install.sh                 نصب + راه‌اندازی تعاملی
#   ./install.sh --no-setup      فقط نصب (راه‌اندازی را بعداً با ./start.sh --setup انجام دهید)
#   ./install.sh --yes           بدون پرسش (برای نصب خودکار)
#   ./install.sh --skip-apt      بدون نصب بسته‌های سیستمی (اگر خودتان نصب کرده‌اید)
#
# اجرای دوباره‌ی این اسکریپت امن است: تنظیمات (.env)، دیتابیس و Session هرگز پاک نمی‌شوند.
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"

RUN_SETUP=1; SKIP_APT=0; ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --no-setup) RUN_SETUP=0 ;;
        --skip-apt) SKIP_APT=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "گزینه‌ی ناشناخته: $arg (برای راهنما: ./install.sh --help)" ;;
    esac
done
export ASSUME_YES

trap 'err "نصب در خط $LINENO متوقف شد. پیام بالا را بخوانید؛ پس از رفع مشکل ./install.sh را دوباره اجرا کنید (امن است)."' ERR

echo "══════════════════════════════════════════════"
echo "  نصب دستیار هوشمند تلگرام (حساب شخصی)"
echo "══════════════════════════════════════════════"

# ---------- ۱) تشخیص سیستم‌عامل ----------
OS_ID="unknown"; OS_NAME="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"; OS_NAME="${PRETTY_NAME:-$OS_ID}"
fi
info "سیستم‌عامل: $OS_NAME"
case "$OS_ID" in
    ubuntu|debian|linuxmint|pop|raspbian|zorin|neon|elementary) APT_OK=1 ;;
    *)
        APT_OK=0
        if [ "${ID_LIKE:-}" != "" ] && echo "${ID_LIKE}" | grep -qiE 'debian|ubuntu'; then APT_OK=1; fi
        [ "$APT_OK" = 1 ] || warn "این سیستم Ubuntu/Debian نیست؛ نصب بسته‌های سیستمی خودکار انجام نمی‌شود. پایتون ۳.۱۰+ با ماژول venv را خودتان نصب کنید."
        ;;
esac

# ---------- ۲) پایتون ----------
find_python() {
    local c
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

apt_install() {
    # فقط بسته‌های «نصب‌نشده» را نصب می‌کند و فقط در همان لحظه از root استفاده می‌کند.
    local missing=() pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    [ "$APT_OK" = 1 ] && [ "$SKIP_APT" = 0 ] || die "بسته‌های لازم نصب نیستند: ${missing[*]} — خودتان نصب کنید یا بدون --skip-apt اجرا کنید."
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        die "برای نصب ${missing[*]} دسترسی root لازم است ولی sudo پیدا نشد. با root اجرا کنید: apt-get install -y ${missing[*]}"
    fi
    info "نصب بسته‌های سیستمی: ${missing[*]} (ممکن است رمز sudo پرسیده شود)"
    $SUDO apt-get update -y
    $SUDO apt-get install -y "${missing[@]}"
}

if ! PYTHON="$(find_python)"; then
    info "پایتون ۳.۱۰ یا بالاتر پیدا نشد."
    apt_install python3 python3-venv python3-pip
    PYTHON="$(find_python)" || die "نسخه‌ی پایتون این سیستم قدیمی است (کمتر از ۳.۱۰). روی Ubuntu 20.04 پایتون ۳.۱۰+ را نصب کنید (مثلاً از PPA «deadsnakes») و دوباره اجرا کنید."
fi
ok "پایتون: $("$PYTHON" --version 2>&1) ($PYTHON)"

# ---------- ۳) وابستگی‌های سیستمی ----------
if [ "$APT_OK" = 1 ] && [ "$SKIP_APT" = 0 ]; then
    PYV="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if ! "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
        apt_install python3-venv "python${PYV}-venv" 2>/dev/null || apt_install python3-venv
    fi
    apt_install ca-certificates util-linux
else
    "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "ماژول venv/ensurepip پایتون نصب نیست."
fi

# ---------- ۴) محیط مجازی ----------
if [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    ok "محیط مجازی موجود است؛ دوباره استفاده می‌شود."
else
    if [ -d "$VENV_DIR" ]; then
        warn "محیط مجازی قبلی خراب/قدیمی است؛ بازسازی می‌شود (فقط پوشه‌ی venv؛ داده‌های شما دست‌نخورده می‌ماند)."
        mv -- "$VENV_DIR" "$VENV_DIR.broken.$(date +%s)"
    fi
    info "ساخت محیط مجازی پایتون…"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# ---------- ۵) کتابخانه‌ها ----------
info "نصب کتابخانه‌های پایتون (ممکن است چند دقیقه طول بکشد)…"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt
ok "کتابخانه‌ها نصب شد."

# ---------- ۶) پوشه‌ها ----------
ensure_dirs
ok "پوشه‌های data/، data/session/، backups/ آماده است."

# ---------- ۷) فایل .env ----------
if [ -f .env ]; then
    ok "فایل .env از قبل وجود دارد و دست‌نخورده ماند."
else
    cp .env.example .env
    ok "فایل .env از روی .env.example ساخته شد."
fi

# ---------- ۸) مجوزها ----------
chmod +x install.sh start.sh stop.sh restart.sh status.sh update.sh uninstall.sh service.sh scripts/*.sh 2>/dev/null || true
secure_files
ok "مجوز فایل‌های حساس روی 600 تنظیم شد."

# ---------- ۹) بررسی صحت نصب ----------
info "بررسی صحت نصب…"
"$VENV_PY" - <<'PYCHECK'
import importlib, sys
missing = []
for mod in ("telethon", "openai", "dotenv", "tzdata"):
    try:
        importlib.import_module(mod)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{mod} ({type(exc).__name__})")
if missing:
    print("کتابخانه‌های ناقص:", ", ".join(missing)); sys.exit(1)
PYCHECK
"$VENV_PY" -m compileall -q -x 'venv|tests' . >/dev/null
ok "نصب سالم است."

# ---------- ۱۰) راه‌اندازی اولیه ----------
echo
if [ "$RUN_SETUP" = 1 ] && is_tty; then
    if "$VENV_PY" main.py --check >/dev/null 2>&1; then
        ok "راه‌اندازی قبلاً انجام شده است."
    else
        info "اکنون راهنمای راه‌اندازی (ورود به تلگرام و تنظیم هوش مصنوعی) اجرا می‌شود…"
        "$VENV_PY" main.py --setup || warn "راه‌اندازی کامل نشد؛ هر زمان خواستید: ./start.sh --setup"
    fi
else
    info "راه‌اندازی اولیه را با این دستور انجام دهید:  ./start.sh --setup"
fi
secure_files

echo
ok "نصب تمام شد."
cat <<MSG

قدم‌های بعدی:
  ./start.sh            اجرای دستیار
  ./status.sh           مشاهده‌ی وضعیت
  ./service.sh install  اجرای خودکار با systemd (اختیاری)
  راهنمای دستورات: در تلگرام به «Saved Messages» بروید و بنویسید  /ai help
MSG
