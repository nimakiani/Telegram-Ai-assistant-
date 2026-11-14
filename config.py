"""
config.py — بارگذاری و اعتبارسنجی تنظیمات برنامه.

تمام اطلاعات حساس (API HASH تلگرام، کلیدهای API) فقط از متغیرهای محیطی (os.environ)
خوانده می‌شوند. برای توسعه‌ی محلی، اگر فایل `.env` وجود داشته باشد، توسط
python-dotenv بارگذاری می‌شود. هیچ مقدار حساسی در کد هاردکد نشده است.

نکته: بیشتر تنظیمات رفتاری (تاخیر، ساعت کاری، فیلترها، ...) فقط «مقدار اولیه» هستند.
پس از اولین اجرا در دیتابیس ذخیره می‌شوند و از پنل مدیریت قابل تغییرند.

Loads and validates application settings from environment variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from schedule import (
    SCHEDULE_MODES,
    days_to_str,
    fmt_time,
    is_valid_tz,
    parse_days,
    parse_hours,
)

load_dotenv()

DEFAULT_SYSTEM_PROMPT = (
    "تو دستیار هوشمند و مؤدب صاحب این حساب تلگرام هستی و به جای او به پیام‌های "
    "خصوصی پاسخ می‌دهی. پاسخ‌ها را کوتاه، دوستانه و مفید بنویس و دقیقاً به همان "
    "زبانی که کاربر نوشته جواب بده. از قالب‌بندی مارک‌داون استفاده نکن. اگر درباره "
    "چیزی مطمئن نیستی، صادقانه بگو و اعلام کن که صاحب حساب بعداً پاسخ خواهد داد. "
    "هرگز اطلاعات خصوصی یا ساختگی از خودت درنیاور."
)

DEFAULT_HANDOFF_MESSAGE = (
    "پیام شما برای صاحب این حساب ارسال شد و در اولین فرصت خودشان پاسخ می‌دهند. 🙏"
)


class ConfigError(RuntimeError):
    """خطای پیکربندی (متغیر محیطی ناقص یا نامعتبر)."""


def _env(name: str, default: str | None = None) -> str | None:
    """مقدار یک متغیر محیطی؛ مقدار خالی معادل «تنظیم نشده» است."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"مقدار {name} باید عدد صحیح باشد (مقدار فعلی: {raw!r}).") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"مقدار {name} باید عدد باشد (مقدار فعلی: {raw!r}).") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """مجموعه‌ی تنظیمات تغییرناپذیر برنامه."""

    # --- تلگرام (حساب شخصی، MTProto)
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str | None
    session_path: str
    # --- هوش مصنوعی (اصلی)
    ai_api_key: str
    ai_base_url: str | None
    ai_model: str
    ai_vision_model: str | None
    ai_temperature: float
    ai_max_tokens: int
    ai_timeout: float
    ai_max_retries: int
    # --- هوش مصنوعی (پشتیبان؛ فقط وقتی AI_FALLBACK_MODEL تنظیم شده باشد)
    ai_fallback_model: str | None
    ai_fallback_api_key: str | None
    ai_fallback_base_url: str | None
    # --- تبدیل صدا به متن
    stt_model: str
    stt_api_key: str
    stt_base_url: str | None
    stt_language: str | None
    # --- ذخیره‌سازی
    database_path: str
    log_max_rows: int
    # --- مقادیر اولیه‌ی تنظیمات پویا
    default_system_prompt: str
    default_cooldown: float
    default_debounce: float
    default_history_size: int
    default_pause_minutes: int
    default_rate_limit: int
    handoff_enabled: bool
    handoff_message: str
    schedule_mode: str
    work_start: str
    work_end: str
    work_days: str
    timezone: str
    vision_enabled: bool
    voice_enabled: bool
    mark_as_read: bool
    default_max_streak: int
    # --- محدودیت‌ها
    max_media_bytes: int
    temp_dir: str
    lock_path: str
    max_flood_wait: int
    max_audio_seconds: int
    knowledge_max_chars: int
    min_typing_seconds: float
    fallback_message: str
    log_level: str


def load_config(require_telegram: bool = True, require_ai: bool = True) -> Config:
    """تنظیمات را از محیط می‌خواند.

    همه‌ی مشکلات یک‌جا گزارش می‌شوند (نه فقط اولین مورد) تا کاربر بداند دقیقاً چه چیزی
    ناقص است. با require_telegram=False می‌توان قبل از ورود به حساب هم تنظیمات را خواند.
    """
    problems: list[str] = []

    raw_api_id = _env("TG_API_ID")
    api_id = 0
    if raw_api_id:
        try:
            api_id = int(raw_api_id)
        except ValueError:
            problems.append(f"TG_API_ID باید فقط عدد باشد (مقدار فعلی: {raw_api_id!r}).")
    elif require_telegram:
        problems.append("TG_API_ID تنظیم نشده است (از https://my.telegram.org بگیرید).")
    api_hash = _env("TG_API_HASH") or ""
    if require_telegram and not api_hash:
        problems.append("TG_API_HASH تنظیم نشده است (از https://my.telegram.org بگیرید).")
    elif api_hash and not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        problems.append("TG_API_HASH باید یک رشته‌ی ۳۲ نویسه‌ای هگزادسیمال باشد.")

    ai_api_key = _env("AI_API_KEY")
    if require_ai and not ai_api_key:
        problems.append(
            "AI_API_KEY تنظیم نشده است. برای سرویس‌های محلی (مثل Ollama) هر مقدار دلخواه "
            "مثل «none» کافی است."
        )

    # --- اعتبارسنجی تنظیمات زمان‌بندی
    schedule_mode = (_env("SCHEDULE_MODE", "off") or "off").lower()
    if schedule_mode not in SCHEDULE_MODES:
        problems.append(f"SCHEDULE_MODE باید یکی از {', '.join(SCHEDULE_MODES)} باشد.")
        schedule_mode = "off"
    hours = parse_hours(f"{_env('WORK_START', '09:00')}-{_env('WORK_END', '18:00')}")
    if hours is None:
        problems.append("WORK_START و WORK_END باید به شکل HH:MM باشند (مثلاً 09:00).")
        hours = parse_hours("09:00-18:00")
    days = parse_days(_env("WORK_DAYS", "mon,tue,wed,thu,fri") or "")
    if days is None:
        problems.append("WORK_DAYS نامعتبر است؛ مثال: mon,tue,wed,thu,fri یا «all».")
        days = parse_days("all")
    tz_name = _env("TIMEZONE", "Asia/Tehran") or "Asia/Tehran"
    if not is_valid_tz(tz_name):
        problems.append(f"TIMEZONE نامعتبر است: {tz_name!r} (مثال: Asia/Tehran یا UTC).")
        tz_name = "UTC"

    try:
        _collect_numbers()
    except ConfigError as exc:
        problems.append(str(exc))

    if problems:
        raise ConfigError("\n".join(f"• {p}" for p in problems))

    return Config(
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        tg_phone=_env("TG_PHONE"),
        session_path=_env("TG_SESSION_PATH", "data/session/assistant") or "data/session/assistant",
        ai_api_key=ai_api_key or "",
        ai_base_url=_env("AI_BASE_URL"),
        ai_model=_env("AI_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        ai_vision_model=_env("AI_VISION_MODEL"),
        ai_temperature=_env_float("AI_TEMPERATURE", 0.7),
        ai_max_tokens=_env_int("AI_MAX_TOKENS", 600),
        ai_timeout=_env_float("AI_TIMEOUT", 60.0),
        ai_max_retries=max(0, _env_int("AI_MAX_RETRIES", 3)),
        ai_fallback_model=_env("AI_FALLBACK_MODEL"),
        ai_fallback_api_key=_env("AI_FALLBACK_API_KEY"),
        ai_fallback_base_url=_env("AI_FALLBACK_BASE_URL"),
        stt_model=_env("TRANSCRIPTION_MODEL", "whisper-1") or "whisper-1",
        stt_api_key=_env("TRANSCRIPTION_API_KEY") or ai_api_key or "",
        stt_base_url=_env("TRANSCRIPTION_BASE_URL") or _env("AI_BASE_URL"),
        stt_language=_env("TRANSCRIPTION_LANGUAGE"),
        database_path=_env("DATABASE_PATH", "data/assistant.db") or "data/assistant.db",
        log_max_rows=max(100, _env_int("LOG_MAX_ROWS", 10000)),
        default_system_prompt=_env("DEFAULT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        or DEFAULT_SYSTEM_PROMPT,
        default_cooldown=max(0.0, _env_float("COOLDOWN_SECONDS", 3.0)),
        default_debounce=max(0.0, _env_float("DEBOUNCE_SECONDS", 2.5)),
        default_history_size=max(0, _env_int("HISTORY_SIZE", 12)),
        default_pause_minutes=max(0, _env_int("PAUSE_MINUTES", 60)),
        default_max_streak=max(0, _env_int("MAX_CONSECUTIVE_REPLIES", 20)),
        default_rate_limit=max(0, _env_int("RATE_LIMIT_PER_HOUR", 30)),
        handoff_enabled=_env_bool("HANDOFF_ENABLED", True),
        handoff_message=_env("HANDOFF_MESSAGE", DEFAULT_HANDOFF_MESSAGE)
        or DEFAULT_HANDOFF_MESSAGE,
        schedule_mode=schedule_mode,
        work_start=fmt_time(hours[0]),
        work_end=fmt_time(hours[1]),
        work_days=days_to_str(days),
        timezone=tz_name,
        vision_enabled=_env_bool("VISION_ENABLED", True),
        voice_enabled=_env_bool("VOICE_ENABLED", True),
        mark_as_read=_env_bool("MARK_AS_READ", False),
        max_media_bytes=max(1, _env_int("MAX_MEDIA_MB", 10)) * 1024 * 1024,
        temp_dir=_env("TEMP_DIR", "data/tmp") or "data/tmp",
        lock_path=_env("LOCK_PATH", "data/assistant.lock") or "data/assistant.lock",
        max_flood_wait=max(0, _env_int("MAX_FLOOD_WAIT_SECONDS", 90)),
        max_audio_seconds=max(1, _env_int("MAX_AUDIO_SECONDS", 300)),
        knowledge_max_chars=max(500, _env_int("KNOWLEDGE_MAX_CHARS", 12000)),
        min_typing_seconds=max(0.0, _env_float("MIN_TYPING_SECONDS", 1.5)),
        fallback_message=_env("FALLBACK_MESSAGE", "") or "",
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
    )


def _collect_numbers() -> None:  # noqa: D401
    """همه‌ی تنظیمات عددی را یک‌بار می‌خواند تا خطای قالب (مثلاً حرف به‌جای عدد) زودتر گزارش شود."""
    for name in ("AI_MAX_TOKENS", "AI_MAX_RETRIES", "HISTORY_SIZE", "PAUSE_MINUTES",
                 "RATE_LIMIT_PER_HOUR", "MAX_CONSECUTIVE_REPLIES", "MAX_MEDIA_MB",
                 "MAX_AUDIO_SECONDS", "KNOWLEDGE_MAX_CHARS", "LOG_MAX_ROWS",
                 "MAX_FLOOD_WAIT_SECONDS"):
        _env_int(name, 0)
    for name in ("AI_TEMPERATURE", "AI_TIMEOUT", "COOLDOWN_SECONDS", "DEBOUNCE_SECONDS",
                 "MIN_TYPING_SECONDS"):
        _env_float(name, 0.0)
