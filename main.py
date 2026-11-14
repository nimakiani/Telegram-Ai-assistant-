"""
main.py — نقطه‌ی ورود دستیار هوشمند تلگرام (حساب شخصی، بدون Premium و بدون BotFather).

  python main.py                 اجرای دستیار (اگر راه‌اندازی انجام نشده باشد، راهنمای فارسی باز می‌شود)
  python main.py --setup         راهنمای مرحله‌به‌مرحله‌ی راه‌اندازی/ورود دوباره
  python main.py --check         بررسی تنظیمات و وجود نشست (بدون اتصال)
  python main.py --show-config   نمایش تنظیمات (کلیدها مخفی)
  python main.py --set KEY=VALUE تغییر یک تنظیم در .env (بدون ویرایش دستی فایل)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
os.chdir(PROJECT_DIR)  # مسیرهای نسبی (data/...) همیشه نسبت به پوشه‌ی پروژه‌اند

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR / ".env")

import app  # noqa: E402
from config import ConfigError, load_config  # noqa: E402
from envfile import SENSITIVE_KEYS, mask, read_env, update_env  # noqa: E402

ENV_PATH = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"


class RedactFilter(logging.Filter):
    """هر مقدار حساس (API HASH، کلید API) اگر به لاگ برسد با *** جایگزین می‌شود."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for secret in self._secrets:
                msg = msg.replace(secret, "***")
            record.msg, record.args = msg, ()
        return True


def setup_logging(level_name: str, secrets: list[str]) -> None:
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if "INVOCATION_ID" not in os.environ:  # زیر systemd لاگ‌ها در journalctl هستند
        log_dir = PROJECT_DIR / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "assistant.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handlers.append(file_handler)
    redact = RedactFilter(secrets)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redact)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    try:
        os.chmod(PROJECT_DIR / "data" / "logs" / "assistant.log", 0o600)
    except OSError:
        pass
    # کتابخانه‌های شبکه‌ای جزئیات درخواست‌ها را چاپ نکنند
    for name in ("httpx", "httpcore", "telethon", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _session_file(session_path: str) -> Path:
    return Path(str(Path(session_path).expanduser()) + ".session")


def cmd_check() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"❌ تنظیمات ناقص یا نادرست است:\n{exc}")
        print("\nبرای رفع خودکار: ./start.sh --setup")
        return 1
    if not _session_file(cfg.session_path).exists():
        print("❌ هنوز وارد حساب تلگرام نشده‌اید (فایل نشست وجود ندارد).")
        print("برای ورود: ./start.sh --setup")
        return 3
    print("✅ تنظیمات کامل است و نشست ورود وجود دارد.")
    return 0


def cmd_show_config() -> int:
    values = read_env(ENV_PATH)
    if not values:
        print("فایل .env هنوز ساخته نشده است. ابتدا ./install.sh یا ./start.sh --setup را اجرا کنید.")
        return 1
    for key, value in values.items():
        if value:
            print(f"{key}={mask(key, value)}")
    return 0


def cmd_set(assignment: str) -> int:
    key, sep, value = assignment.partition("=")
    key = key.strip().upper()
    if not sep or not key.replace("_", "").isalnum():
        print("قالب درست: python main.py --set KEY=VALUE   (مثلاً AI_MODEL=gpt-4o-mini)")
        return 1
    update_env(ENV_PATH, {key: value.strip()}, template=ENV_EXAMPLE)
    shown = mask(key, value.strip()) if key in SENSITIVE_KEYS else value.strip()
    print(f"✅ {key}={shown} ذخیره شد. برای اعمال: ./restart.sh")
    return 0


def needs_setup() -> bool:
    try:
        cfg = load_config()
    except ConfigError:
        return True
    return not _session_file(cfg.session_path).exists()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="دستیار هوشمند تلگرام (حساب شخصی)")
    parser.add_argument("--setup", action="store_true", help="راه‌اندازی/ورود تعاملی")
    parser.add_argument("--check", action="store_true", help="بررسی تنظیمات")
    parser.add_argument("--show-config", action="store_true", help="نمایش تنظیمات (با کلیدهای مخفی)")
    parser.add_argument("--set", metavar="KEY=VALUE", help="تغییر یک تنظیم در .env")
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check()
    if args.show_config:
        return cmd_show_config()
    if args.set:
        return cmd_set(args.set)

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if args.setup or (needs_setup() and interactive):
        import setup_wizard

        code = setup_wizard.run_wizard(PROJECT_DIR)
        if args.setup or code != 0:
            return code
        load_dotenv(ENV_PATH, override=True)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[خطای پیکربندی]\n{exc}\n\nبرای رفع خودکار اجرا کنید: ./start.sh --setup", file=sys.stderr)
        return app.EXIT_CONFIG
    if not _session_file(cfg.session_path).exists():
        print("[خطا] هنوز وارد حساب تلگرام نشده‌اید. اجرا کنید: ./start.sh --setup", file=sys.stderr)
        return app.EXIT_AUTH

    setup_logging(cfg.log_level, [cfg.tg_api_hash, cfg.ai_api_key, cfg.stt_api_key,
                                  cfg.ai_fallback_api_key or ""])
    try:
        return asyncio.run(app.run(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
