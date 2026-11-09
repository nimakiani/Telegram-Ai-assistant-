"""
schedule.py — منطق ساعت کاری، منطقه‌ی زمانی و ابزارهای تحلیل متن ورودی مدیر.

این ماژول هیچ وابستگی به تلگرام یا دیتابیس ندارد و به‌سادگی قابل تست است.

حالت‌های زمان‌بندی:
  * off     : همیشه پاسخ بده
  * inside  : فقط در ساعت کاری پاسخ بده
  * outside : فقط «خارج از ساعت کاری» پاسخ بده (یعنی وقتی شما دور هستید)

Pure helpers for working-hours logic. Weekday numbering follows Python: Monday=0 … Sunday=6.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODE_OFF = "off"
MODE_INSIDE = "inside"
MODE_OUTSIDE = "outside"
SCHEDULE_MODES = (MODE_OFF, MODE_INSIDE, MODE_OUTSIDE)

# ارقام فارسی و عربی → لاتین
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "دوشنبه": 0, "سهشنبه": 1, "چهارشنبه": 2, "پنجشنبه": 3, "جمعه": 4,
    "شنبه": 5, "یکشنبه": 6,
}
_DAY_RE = re.compile("|".join(sorted(_DAY_MAP, key=len, reverse=True)))
_ALL_WORDS = ("all", "daily", "everyday", "همه", "هرروز", "روزانه")
_DAY_LABELS_FA = {
    0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه", 3: "پنج‌شنبه",
    4: "جمعه", 5: "شنبه", 6: "یکشنبه",
}
_WEEK_ORDER_FA = (5, 6, 0, 1, 2, 3, 4)  # هفته‌ی ایرانی: از شنبه شروع می‌شود

_HOURS_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*(?:-|–|—|to|تا)\s*(\d{1,2}):(\d{2})\s*")


def normalize_digits(text: str) -> str:
    """ارقام فارسی/عربی را به ارقام لاتین تبدیل می‌کند."""
    return text.translate(_DIGITS)


def normalize_text(text: str) -> str:
    """نرمال‌سازی برای مقایسه‌ی متن: حروف کوچک، یکسان‌سازی ی/ک، حذف نیم‌فاصله، ارقام لاتین."""
    t = normalize_digits(text).lower()
    return t.replace("\u200c", "").replace("ي", "ی").replace("ك", "ک")


def parse_hours(text: str) -> tuple[time, time] | None:
    """«09:00-18:00» را به (شروع، پایان) تبدیل می‌کند؛ در صورت نامعتبربودن None."""
    m = _HOURS_RE.fullmatch(normalize_digits(text))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        return None
    return time(h1, m1), time(h2, m2)


def fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def parse_days(text: str) -> frozenset[int] | None:
    """نام روزها (فارسی یا انگلیسی) یا «همه» را به مجموعه‌ی شماره‌ی روز تبدیل می‌کند."""
    t = normalize_text(text).replace(" ", "")
    if any(word in t for word in _ALL_WORDS):
        return frozenset(range(7))
    days = {_DAY_MAP[m] for m in _DAY_RE.findall(t)}
    return frozenset(days) if days else None


def days_to_str(days: frozenset[int] | set[int]) -> str:
    """ذخیره‌ی روزها در دیتابیس: «0,1,2»."""
    return ",".join(str(d) for d in sorted(days))


def days_from_str(raw: str | None) -> frozenset[int]:
    """بازیابی روزها از رشته‌ی دیتابیس؛ در صورت خرابی همه‌ی روزها."""
    try:
        days = {int(x) for x in (raw or "").split(",") if x.strip() != ""}
    except ValueError:
        days = set()
    days = {d for d in days if 0 <= d <= 6}
    return frozenset(days) if days else frozenset(range(7))


def format_days(days: frozenset[int]) -> str:
    """نمایش فارسی روزها به ترتیب هفته‌ی ایرانی."""
    if len(days) == 7:
        return "همه‌ی روزها"
    return "، ".join(_DAY_LABELS_FA[d] for d in _WEEK_ORDER_FA if d in days)


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return False


def get_tz(name: str) -> tzinfo:
    """منطقه‌ی زمانی را برمی‌گرداند؛ اگر نامعتبر بود UTC."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return timezone.utc


def is_working_time(
    now_utc: datetime, tz_name: str, start: time, end: time, days: frozenset[int]
) -> bool:
    """آیا اکنون (در منطقه‌ی زمانی داده‌شده) داخل ساعت کاری است؟

    * اگر start == end باشد، کل روزِ انتخاب‌شده کاری است.
    * بازه‌ی شبانه (مثلاً 22:00-06:00) پشتیبانی می‌شود؛ بخش بعد از نیمه‌شب
      به روزِ شروع بازه تعلق دارد.
    """
    local = now_utc.astimezone(get_tz(tz_name))
    t = local.time().replace(tzinfo=None)
    weekday = local.weekday()
    if start == end:
        return weekday in days
    if start < end:
        return weekday in days and start <= t < end
    if t >= start:
        return weekday in days
    if t < end:
        return (weekday - 1) % 7 in days
    return False


def schedule_allows(mode: str, working: bool) -> bool:
    """بر اساس حالت زمان‌بندی تعیین می‌کند ربات اکنون اجازه‌ی پاسخ دارد یا نه."""
    if mode == MODE_INSIDE:
        return working
    if mode == MODE_OUTSIDE:
        return not working
    return True


def local_day_start_ts(tz_name: str, now_utc: datetime | None = None) -> int:
    """زمان یونیکس شروع «امروز» در منطقه‌ی زمانی داده‌شده."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(get_tz(tz_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def format_local_now(tz_name: str, now_utc: datetime | None = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(get_tz(tz_name)).strftime("%Y-%m-%d %H:%M")

