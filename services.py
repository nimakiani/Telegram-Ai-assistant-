"""
services.py — اشیا و توابع کمکیِ مشترک بین هسته‌ی دستیار (business.py) و پنل مدیریت (admin.py).

شامل:
  * Services: نگهدارنده‌ی config/db/ai و وضعیت‌های حافظه‌ای (قفل هر چت، Cooldown،
    صف جمع‌کردن پیام‌ها، محدودیت نرخ، شناسه‌ی پیام‌های ارسالیِ خود ربات)
  * توابع کمکی عمومی (تقسیم متن بلند، کوتاه‌سازی، قالب زمان، ...)
  * خواندن تنظیمات زمان‌بندی از دیتابیس
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ai_handler import AIHandler, Part
from config import Config
from database import (
    KEY_SCHEDULE_MODE,
    KEY_TIMEZONE,
    KEY_WORK_DAYS,
    KEY_WORK_END,
    KEY_WORK_START,
    Database,
)
from schedule import (
    MODE_OFF,
    days_from_str,
    is_working_time,
    parse_hours,
    schedule_allows,
)

TELEGRAM_MAX_LEN = 4096
_BOT_MESSAGE_REGISTRY_SIZE = 2000

# کلید هر چت: (شناسه‌ی اتصال Business، شناسه‌ی چت)
Key = tuple[str, int]


@dataclass
class PendingBatch:
    """پیام‌های پشت‌سرهمِ یک چت که برای پاسخ یک‌جا جمع می‌شوند (Debounce)."""

    parts: list[Part] = field(default_factory=list)
    log_texts: list[str] = field(default_factory=list)
    first_ts: float = field(default_factory=time.monotonic)
    last_ts: float = field(default_factory=time.monotonic)
    leader: bool = False
    user: Any = None
    last_message_id: int | None = None


@dataclass
class Services:
    """اشیای مشترک که در application.bot_data نگهداری می‌شوند."""

    config: Config
    db: Database
    ai: AIHandler
    chat_locks: dict[Key, asyncio.Lock] = field(default_factory=dict)
    last_reply_at: dict[Key, float] = field(default_factory=dict)
    pending: dict[Key, PendingBatch] = field(default_factory=dict)
    rate: dict[Key, deque] = field(default_factory=dict)
    bot_msgs: "OrderedDict[tuple[str, int, int], float]" = field(default_factory=OrderedDict)

    def lock_for(self, key: Key) -> asyncio.Lock:
        """قفل جداگانه برای هر چت تا پاسخ‌ها پشت‌سرهم و بدون تداخل ارسال شوند."""
        lock = self.chat_locks.get(key)
        if lock is None:
            lock = self.chat_locks[key] = asyncio.Lock()
        return lock

    def remember_bot_message(self, connection_id: str, chat_id: int, message_id: int | None) -> None:
        """شناسه‌ی پیامی که خود ربات فرستاده را ثبت می‌کند تا با پیام دستیِ صاحب حساب اشتباه نشود."""
        if message_id is None:
            return
        self.bot_msgs[(connection_id, chat_id, message_id)] = time.monotonic()
        while len(self.bot_msgs) > _BOT_MESSAGE_REGISTRY_SIZE:
            self.bot_msgs.popitem(last=False)

    def is_bot_message(self, connection_id: str, chat_id: int, message_id: int) -> bool:
        return (connection_id, chat_id, message_id) in self.bot_msgs

    def rate_allows(self, key: Key, limit: int) -> bool:
        """محدودیت نرخ پنجره‌ی لغزان یک‌ساعته برای هر چت (limit ≤ 0 یعنی نامحدود)."""
        if limit <= 0:
            return True
        now = time.monotonic()
        window = self.rate.setdefault(key, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


def svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def is_admin(update: Update, s: Services) -> bool:
    user = update.effective_user
    return user is not None and user.id in s.config.admin_ids


# ----------------------------------------------------------------------
# توابع کمکی عمومی
# ----------------------------------------------------------------------
def split_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """متن بلند را روی مرز خط/فاصله به تکه‌های کمتر از سقف تلگرام تقسیم می‌کند."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def clip(text: str | None, limit: int) -> str:
    """متن را تک‌خطی و کوتاه می‌کند (برای نمایش در لاگ‌ها)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------------
# زمان‌بندی (ساعت کاری)
# ----------------------------------------------------------------------
async def load_schedule(s: Services) -> dict[str, Any]:
    """تنظیمات زمان‌بندی را از دیتابیس می‌خواند (با پیش‌فرض‌های Config در صورت خرابی)."""
    cfg = s.config
    mode = await s.db.get_setting(KEY_SCHEDULE_MODE, cfg.schedule_mode) or MODE_OFF
    start_raw = await s.db.get_setting(KEY_WORK_START, cfg.work_start) or cfg.work_start
    end_raw = await s.db.get_setting(KEY_WORK_END, cfg.work_end) or cfg.work_end
    hours = parse_hours(f"{start_raw}-{end_raw}") or parse_hours(f"{cfg.work_start}-{cfg.work_end}")
    days = days_from_str(await s.db.get_setting(KEY_WORK_DAYS, cfg.work_days))
    tz_name = await s.db.get_setting(KEY_TIMEZONE, cfg.timezone) or cfg.timezone
    return {"mode": mode, "start": hours[0], "end": hours[1], "days": days, "tz": tz_name}


async def schedule_state(s: Services) -> tuple[str, bool, bool]:
    """برمی‌گرداند: (حالت، «اکنون ساعت کاری است؟»، «ربات اکنون اجازه‌ی پاسخ دارد؟»)."""
    sched = await load_schedule(s)
    working = is_working_time(
        datetime.now(timezone.utc), sched["tz"], sched["start"], sched["end"], sched["days"]
    )
    return sched["mode"], working, schedule_allows(sched["mode"], working)
