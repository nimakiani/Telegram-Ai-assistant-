"""
bot.py — اتصال اجزا به هم: ساخت Application، ثبت هندلرها و چرخه‌ی حیات برنامه.

ماژول‌ها:
  * business.py : هسته‌ی دستیار (پیام‌ها و اتصال‌های Business)
  * admin.py    : پنل مدیریت داخل تلگرام
  * media.py    : تبدیل عکس/صدا/استیکر به ورودی مدل
  * services.py : حالت مشترک و توابع کمکی
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from admin import (
    cmd_cancel,
    cmd_forget,
    cmd_start,
    on_admin_document,
    on_admin_text,
    on_callback,
)
from ai_handler import AIHandler
from business import on_business_update
from config import Config
from database import (
    KEY_COOLDOWN,
    KEY_DEBOUNCE,
    KEY_ENABLED,
    KEY_HANDOFF,
    KEY_HANDOFF_MESSAGE,
    KEY_HISTORY_SIZE,
    KEY_KNOWLEDGE,
    KEY_LIST_MODE,
    KEY_MARK_READ,
    KEY_PAUSE_MINUTES,
    KEY_RATE_LIMIT,
    KEY_SCHEDULE_MODE,
    KEY_SYSTEM_PROMPT,
    KEY_TIMEZONE,
    KEY_VISION,
    KEY_VOICE,
    KEY_WORK_DAYS,
    KEY_WORK_END,
    KEY_WORK_START,
    MODE_OFF,
    Database,
)
from services import Services

logger = logging.getLogger(__name__)


def _flag(value: bool) -> str:
    return "1" if value else "0"


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هر خطای مدیریت‌نشده را فقط لاگ می‌کند تا ربات کرش نکند."""
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


async def _post_init(app: Application) -> None:
    """مقداردهی اولیه‌ی تنظیمات پیش‌فرض و بررسی فعال‌بودن Business Mode برای ربات."""
    s: Services = app.bot_data["services"]
    cfg = s.config
    await s.db.init_defaults(
        {
            KEY_ENABLED: "1",
            KEY_SYSTEM_PROMPT: cfg.default_system_prompt,
            KEY_KNOWLEDGE: "",
            KEY_LIST_MODE: MODE_OFF,
            KEY_COOLDOWN: str(cfg.default_cooldown),
            KEY_DEBOUNCE: str(cfg.default_debounce),
            KEY_HISTORY_SIZE: str(cfg.default_history_size),
            KEY_PAUSE_MINUTES: str(cfg.default_pause_minutes),
            KEY_RATE_LIMIT: str(cfg.default_rate_limit),
            KEY_HANDOFF: _flag(cfg.handoff_enabled),
            KEY_HANDOFF_MESSAGE: cfg.handoff_message,
            KEY_SCHEDULE_MODE: cfg.schedule_mode,
            KEY_WORK_START: cfg.work_start,
            KEY_WORK_END: cfg.work_end,
            KEY_WORK_DAYS: cfg.work_days,
            KEY_TIMEZONE: cfg.timezone,
            KEY_VISION: _flag(cfg.vision_enabled),
            KEY_VOICE: _flag(cfg.voice_enabled),
            KEY_MARK_READ: _flag(cfg.mark_as_read),
        }
    )
    me = await app.bot.get_me()
    logger.info("Bot started as @%s (id=%s)", me.username, me.id)
    if not getattr(me, "can_connect_to_business", False):
        logger.warning(
            "Business Mode برای این ربات فعال نیست! در @BotFather: "
            "Bot Settings → Business Mode را روشن کنید."
        )


async def _post_shutdown(app: Application) -> None:
    app.bot_data["services"].db.close()


def build_application(config: Config, db: Database, ai: AIHandler) -> Application:
    """Application را می‌سازد و همه‌ی هندلرها را ثبت می‌کند."""
    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .concurrent_updates(True)  # انتظار/تاخیر یک چت نباید بقیه‌ی چت‌ها و پنل را مسدود کند
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["services"] = Services(config=config, db=db, ai=ai)

    # فقط پیام‌های عادی در چت خصوصی با ربات (نه پیام‌های Business)
    private_msg = filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE

    # گروه ۰: پنل مدیریت
    app.add_handler(CommandHandler("start", cmd_start, filters=private_msg), group=0)
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=private_msg), group=0)
    app.add_handler(CommandHandler("forget", cmd_forget, filters=private_msg), group=0)
    app.add_handler(CallbackQueryHandler(on_callback), group=0)
    app.add_handler(MessageHandler(private_msg & filters.Document.ALL, on_admin_document), group=0)
    app.add_handler(
        MessageHandler(private_msg & filters.TEXT & ~filters.COMMAND, on_admin_text), group=0
    )

    # گروه ۱: به‌روزرسانی‌های Business (اتصال و پیام)
    app.add_handler(TypeHandler(Update, on_business_update), group=1)

    app.add_error_handler(on_error)
    return app
