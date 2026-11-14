#!/usr/bin/env bash
# update.sh — به‌روزرسانی امن (تنظیمات، دیتابیس و Session هرگز پاک یا بازنویسی نمی‌شوند).
#
#   ./update.sh                  اگر پروژه با git گرفته شده: git pull
#   ./update.sh نسخه-جدید.zip    اعمال کدهای یک فایل zip
#   ./update.sh /مسیر/پوشه       اعمال کدهای یک پوشه‌ی نسخه‌ی جدید
#
# مراحل: پشتیبان کامل داده‌ها ← به‌روزرسانی کد ← نصب وابستگی‌ها ← بررسی ← اجرای مجدد (اگر در حال اجرا بود)
set -Eeuo pipefail
# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$PROJECT_DIR"
require_venv

case "${1:-}" in -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
SOURCE="${1:-}"
TMP_EXTRACT=""
cleanup() { [ -n "$TMP_EXTRACT" ] && [ -d "$TMP_EXTRACT" ] && rm -rf -- "$TMP_EXTRACT"; return 0; }
trap cleanup EXIT

# مسیرهایی که هیچ‌وقت توسط به‌روزرسانی لمس نمی‌شوند
PROTECTED=(.env data backups venv .git)

fingerprint() {
    { [ -f .env ] && sha256sum .env; find data -type f \( -name '*.session*' -o -name '*.db' \) -exec sha256sum {} + 2>/dev/null; } | sort
}

WAS_RUNNING=0
is_running && WAS_RUNNING=1
BEFORE="$(fingerprint || true)"

# ---------- ۱) پشتیبان ----------
ensure_dirs
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$PROJECT_DIR/backups/pre-update-$STAMP.tar.gz"
info "تهیه‌ی پشتیبان: $BACKUP"
( umask 077
  files=()
  [ -f .env ] && files+=(.env)
  [ -d data ] && files+=(data)
  if [ "${#files[@]}" -gt 0 ]; then
      tar -czf "$BACKUP" --exclude='data/tmp' --exclude='data/logs' "${files[@]}"
  fi )
[ -f "$BACKUP" ] && chmod 600 "$BACKUP" && ok "پشتیبان آماده است."

# ---------- ۲) به‌روزرسانی کد ----------
if [ -z "$SOURCE" ]; then
    [ -d .git ] || die "این پوشه git نیست. نسخه‌ی جدید را به‌صورت zip یا پوشه بدهید:  ./update.sh نسخه-جدید.zip"
    if ! git diff --quiet HEAD 2>/dev/null; then
        die "فایل‌های برنامه تغییر محلی دارند؛ برای جلوگیری از ازدست‌رفتن، ابتدا آن‌ها را commit یا کنار بگذارید."
    fi
    info "دریافت تغییرات از git…"
    git pull --ff-only
else
    if [ -f "$SOURCE" ]; then
        TMP_EXTRACT="$(mktemp -d)"
        "$VENV_PY" - "$SOURCE" "$TMP_EXTRACT" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    z.extractall(sys.argv[2])  # zipfile مسیرهای خطرناک (.. و مطلق) را خنثی می‌کند
PY
        SRC_DIR="$(dirname "$(find "$TMP_EXTRACT" -name main.py -not -path '*/venv/*' | head -n1)")"
    elif [ -d "$SOURCE" ]; then
        SRC_DIR="$(cd "$SOURCE" && pwd)"
    else
        die "منبع پیدا نشد: $SOURCE"
    fi
    [ -f "$SRC_DIR/main.py" ] && [ -f "$SRC_DIR/requirements.txt" ] || die "این پوشه شبیه نسخه‌ی برنامه نیست (main.py یا requirements.txt ندارد)."
    [ "$SRC_DIR" = "$PROJECT_DIR" ] && die "منبع و مقصد یکی است."
    info "کپی کدهای جدید (بدون لمس: ${PROTECTED[*]})…"
    excludes=()
    for p in "${PROTECTED[@]}"; do excludes+=(--exclude="./$p"); done
    excludes+=(--exclude='*.session' --exclude='*.session-journal' --exclude='./.env.*' --exclude='__pycache__')
    tar -C "$SRC_DIR" "${excludes[@]}" -cf - . | tar -C "$PROJECT_DIR" -xf -
    # قالب نمونه همیشه به‌روز می‌شود (ممکن است کلیدهای جدید داشته باشد)؛ .env شما دست‌نخورده می‌ماند.
fi
ok "کد به‌روز شد."

# ---------- ۳) وابستگی‌ها و مجوزها ----------
info "به‌روزرسانی کتابخانه‌ها…"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt
chmod +x install.sh start.sh stop.sh restart.sh status.sh update.sh uninstall.sh service.sh scripts/*.sh 2>/dev/null || true
secure_files

# ---------- ۴) بررسی ----------
"$VENV_PY" -m compileall -q -x 'venv|tests' . >/dev/null
AFTER="$(fingerprint || true)"
if [ "$BEFORE" = "$AFTER" ]; then
    ok "تنظیمات، دیتابیس و Session دست‌نخورده‌اند."
else
    warn "محتوای دیتابیس/Session تغییر کرده است (در صورت اجرای هم‌زمان برنامه طبیعی است). پشتیبان: $BACKUP"
fi
set +e; "$VENV_PY" main.py --check; set -e

# ---------- ۵) اجرای مجدد ----------
if [ "$WAS_RUNNING" = 1 ]; then
    info "برنامه در حال اجرا بود؛ دوباره راه‌اندازی می‌شود (مهاجرت دیتابیس در صورت نیاز، با پشتیبان خودکار)…"
    "$PROJECT_DIR/restart.sh"
fi
if service_installed; then info "اگر قالب سرویس تغییر کرده، اجرا کنید:  ./service.sh install && ./service.sh restart"; fi
ok "به‌روزرسانی تمام شد."
