"""
services.py — حالت مشترک و توابع کمکیِ بین موتور دستیار (engine.py) و دستورهای مدیریتی (commands.py).

شامل:
  * Services: نگهدارنده‌ی config/db/ai/gateway و وضعیت‌های حافظه‌ای
  * ChatRuntime: وضعیت زنده‌ی هر گفتگو (قفل‌ها، epoch برای لغو پاسخ‌های در حال تولید، ...)
  * توابع کمکی عمومی (تقسیم متن بلند، کوتاه‌سازی، قالب زمان، ...)

این ماژول هیچ وابستگی‌ای به کتابخانه‌ی تلگرام ندارد.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from ai_handler import Part
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
from tglayer.models import Gateway, SelfInfo

TELEGRAM_MAX_LEN = 4096
_SENT_REGISTRY_SIZE = 4000
_SEEN_REGISTRY_SIZE = 4000


class AIProtocol(Protocol):
    """رابط حداقلی موتور هوش مصنوعی (برای تست‌ها با Mock جایگزین می‌شود)."""

    async def generate_reply(self, system_prompt: str, history: Any, parts: Any) -> Any: ...
    async def transcribe(self, data: bytes, filename: str = "voice.ogg") -> str: ...


@dataclass
class PendingBatch:
    """پیام‌های پشت‌سرهمِ یک گفتگو که برای پاسخ یک‌جا جمع می‌شوند (Debounce)."""

    epoch: int
    parts: list[Part] = field(default_factory=list)
    log_texts: list[str] = field(default_factory=list)
    first_ts: float = field(default_factory=time.monotonic)
    last_ts: float = field(default_factory=time.monotonic)
    sender_id: int | None = None
    sender_name: str = ""
    sender_username: str | None = None
    last_message_id: int | None = None
    recorded: bool = False  # آیا متن کاربر در تاریخچه ثبت شده است؟


@dataclass
class ChatRuntime:
    """وضعیت زنده‌ی یک گفتگو؛ هر گفتگو کاملاً مستقل از بقیه است.

    * send_lock : فقط حین «ارسال» نگه داشته می‌شود (نه حین تولید پاسخ) تا دخالت صاحب حساب
                  هیچ‌وقت پشت یک درخواست طولانی هوش مصنوعی معطل نماند.
    * turn_lock : نوبت‌ها را پشت‌سرهم نگه می‌دارد (پاسخ دوم قبل از اتمام اولی ساخته نمی‌شود).
    * epoch     : با هر دخالت/توقف زیاد می‌شود؛ پاسخ‌هایی که با epoch قدیمی ساخته شده‌اند
                  هرگز ارسال نمی‌شوند.
    """

    chat_id: int
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    epoch: int = 0
    pending: PendingBatch | None = None
    tasks: set = field(default_factory=set)
    last_reply_at: float | None = None
    rate: deque = field(default_factory=deque)


@dataclass
class Services:
    config: Config
    db: Database
    ai: AIProtocol
    gateway: Gateway | None = None
    me: SelfInfo | None = None
    chats: dict[int, ChatRuntime] = field(default_factory=dict)
    sent_ids: "OrderedDict[tuple[int, int], float]" = field(default_factory=OrderedDict)
    seen_ids: "OrderedDict[tuple[int, int], float]" = field(default_factory=OrderedDict)
    # بعد از FloodWait سراسری تا این زمان (monotonic) هیچ پیامی ارسال نمی‌شود
    send_blocked_until: float = 0.0

    def runtime(self, chat_id: int) -> ChatRuntime:
        rt = self.chats.get(chat_id)
        if rt is None:
            rt = self.chats[chat_id] = ChatRuntime(chat_id=chat_id)
        return rt

    # --- شناسایی پیام‌های ارسالیِ خود برنامه (برای تفکیک از پیام دستیِ صاحب حساب)
    def remember_sent(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        self.sent_ids[(chat_id, message_id)] = time.monotonic()
        while len(self.sent_ids) > _SENT_REGISTRY_SIZE:
            self.sent_ids.popitem(last=False)

    def is_own_sent(self, chat_id: int, message_id: int) -> bool:
        return (chat_id, message_id) in self.sent_ids

    def first_time_seen(self, chat_id: int, message_id: int) -> bool:
        """برای جلوگیری از پردازش دوباره‌ی یک پیام (مثلاً بازپخش رویداد پس از قطع/وصل)."""
        key = (chat_id, message_id)
        if key in self.seen_ids:
            return False
        self.seen_ids[key] = time.monotonic()
        while len(self.seen_ids) > _SEEN_REGISTRY_SIZE:
            self.seen_ids.popitem(last=False)
        return True

    def rate_allows(self, rt: ChatRuntime, limit: int) -> bool:
        """محدودیت نرخ پنجره‌ی لغزان یک‌ساعته برای هر گفتگو (limit ≤ 0 یعنی نامحدود)."""
        if limit <= 0:
            return True
        now = time.monotonic()
        window = rt.rate
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


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
    """برمی‌گرداند: (حالت، «اکنون ساعت کاری است؟»، «دستیار اکنون اجازه‌ی پاسخ دارد؟»)."""
    sched = await load_schedule(s)
    working = is_working_time(
        datetime.now(timezone.utc), sched["tz"], sched["start"], sched["end"], sched["days"]
    )
    return sched["mode"], working, schedule_allows(sched["mode"], working)
