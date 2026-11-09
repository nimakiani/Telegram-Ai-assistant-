"""
main.py — نقطه‌ی ورود برنامه.

اجرا:  python main.py

ربات با Long Polling کار می‌کند؛ بنابراین به دامنه، گواهی SSL یا باز کردن پورت
نیاز ندارد و روی هر سرور یا سرویس ابری (VPS، AWS EC2، Railway، Render و ...) اجرا می‌شود.
"""

from __future__ import annotations

import logging
import sys

from telegram import Update

from ai_handler import AIHandler
from bot import build_application
from config import ConfigError, load_config
from database import Database


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"[خطای پیکربندی] {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        level=getattr(logging, config.log_level, logging.INFO),
    )
    # لاگ‌های httpx آدرس کامل درخواست‌ها (شامل توکن ربات) را چاپ می‌کنند؛ ساکت‌شان می‌کنیم.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    db = Database(config.database_path, log_max_rows=config.log_max_rows)
    ai = AIHandler(config)
    application = build_application(config, db, ai)

    # allowed_updates=ALL_TYPES تضمین می‌کند به‌روزرسانی‌های Business هم دریافت شوند.
    # run_polling سیگنال‌های SIGINT/SIGTERM را مدیریت می‌کند (خاموشی تمیز در Docker).
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=config.drop_pending_updates,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
