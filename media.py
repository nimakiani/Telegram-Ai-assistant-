"""
media.py — تبدیل انواع پیام تلگرام (متن، عکس، صدا، استیکر، ...) به «بخش‌های» قابل‌فهم برای مدل.

خروجی `build_parts` دو چیز است:
  * parts    : فهرست ("text", متن) و ("image", data-URL) برای ارسال به مدل
  * log_text : نسخه‌ی متنیِ کوتاه پیام برای لاگ و تاریخچه (مثلاً «[تصویر] کپشن»)

اصل مهم: هر شکستی در دانلود/تبدیل، فقط به یک متن جایگزین تبدیل می‌شود؛ هرگز استثنا
به بیرون نمی‌رود تا ربات کرش نکند.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from telegram.ext import ContextTypes

from ai_handler import AIError, Part
from database import KEY_VISION, KEY_VOICE
from services import Services

logger = logging.getLogger(__name__)

# انواع رسانه‌ای که پردازش نمی‌کنیم؛ فقط به مدل خبر می‌دهیم کاربر چه چیزی فرستاده
_UNSUPPORTED = (
    ("video", "ویدیو"),
    ("video_note", "ویدیو پیام"),
    ("animation", "گیف"),
    ("document", "فایل"),
    ("location", "موقعیت مکانی"),
    ("venue", "مکان"),
    ("contact", "مخاطب"),
    ("poll", "نظرسنجی"),
    ("dice", "تاس"),
)


async def _download(bot: Any, file_id: str, max_bytes: int) -> bytes:
    """فایل را از تلگرام دانلود می‌کند و حجم آن را کنترل می‌کند."""
    tg_file = await bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    if len(data) > max_bytes:
        raise ValueError("file too large")
    return data


def _pick_photo(sizes: list[Any], max_bytes: int) -> Any | None:
    """بزرگ‌ترین نسخه‌ی عکس را که از سقف حجم کمتر باشد برمی‌گرداند."""
    for photo in reversed(sizes):  # تلگرام از کوچک به بزرگ مرتب می‌کند
        size = getattr(photo, "file_size", None)
        if size is None or size <= max_bytes:
            return photo
    return None


async def build_parts(
    context: ContextTypes.DEFAULT_TYPE, s: Services, msg: Any
) -> tuple[list[Part], str]:
    """پیام Business را به (parts, log_text) تبدیل می‌کند؛ پیام بی‌محتوا → ([], "")."""
    cfg = s.config
    caption = (msg.text or msg.caption or "").strip()
    parts: list[Part] = []
    label = ""

    if getattr(msg, "photo", None):
        label = "[تصویر]"
        if await s.db.get_bool(KEY_VISION, cfg.vision_enabled):
            photo = _pick_photo(list(msg.photo), cfg.max_media_bytes)
            if photo is None:
                label = "[تصویر (حجم بیش‌ازحد)]"
            else:
                try:
                    data = await _download(context.bot, photo.file_id, cfg.max_media_bytes)
                    url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                    parts.append(("image", url))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Photo download failed: %s", exc)
                    label = "[تصویر (بارگیری نشد)]"
        else:
            label = "[تصویر (تحلیل تصویر غیرفعال است)]"

    elif getattr(msg, "voice", None) or getattr(msg, "audio", None):
        media = msg.voice or msg.audio
        label = "[پیام صوتی]"
        duration = getattr(media, "duration", None)
        too_long = duration is not None and duration > cfg.max_audio_seconds
        if not await s.db.get_bool(KEY_VOICE, cfg.voice_enabled):
            label = "[پیام صوتی (تبدیل صدا به متن غیرفعال است)]"
        elif too_long:
            label = "[پیام صوتی (بیش‌ازحد طولانی)]"
        else:
            try:
                data = await _download(context.bot, media.file_id, cfg.max_media_bytes)
                filename = getattr(media, "file_name", None) or "voice.ogg"
                transcript = await s.ai.transcribe(data, filename)
                label = f"[پیام صوتی]: {transcript}"
            except (AIError, ValueError) as exc:
                logger.warning("Voice transcription failed: %s", exc)
                label = "[پیام صوتی (قابل تبدیل به متن نبود)]"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Voice processing failed: %s", exc)
                label = "[پیام صوتی (پردازش نشد)]"

    elif getattr(msg, "sticker", None):
        emoji = getattr(msg.sticker, "emoji", None) or ""
        label = f"[استیکر {emoji}]".replace(" ]", "]")

    elif not caption:
        for attr, title in _UNSUPPORTED:
            if getattr(msg, attr, None):
                label = f"[کاربر یک «{title}» فرستاد که قابل پردازش نیست]"
                break

    if not label and not caption:
        return [], ""

    texts = [t for t in (label, caption) if t]
    # برای پیام صوتی، متنِ تبدیل‌شده خودش کافی است؛ کپشن (اگر باشد) پس از آن می‌آید.
    full_text = "\n".join(texts)
    parts.insert(0, ("text", full_text))
    return parts, full_text
