"""
media.py — تبدیل انواع پیام (متن، عکس، صدا، استیکر، ...) به «بخش‌های» قابل‌فهم برای مدل.

خروجی `build_parts` دو چیز است:
  * parts    : فهرست ("text", متن) و ("image", data-URL) برای ارسال به مدل
  * log_text : نسخه‌ی متنیِ کوتاه پیام برای لاگ و تاریخچه (مثلاً «[تصویر] کپشن»)

قواعد دانلود:
  * حجم فایل (در صورت اعلام‌شدن توسط تلگرام) «قبل از دانلود» با سقف تنظیم‌شده سنجیده می‌شود.
  * فایل‌ها در یک پوشه‌ی موقتِ اختصاصیِ همان پیام ذخیره و بلافاصله پس از پردازش پاک می‌شوند.
  * هر شکستی فقط به یک متن جایگزین تبدیل می‌شود؛ هرگز استثنا به بیرون نمی‌رود.
"""

from __future__ import annotations

import base64
import logging
import shutil
import uuid
from pathlib import Path

from ai_handler import AIError, Part
from database import KEY_MAX_MEDIA_MB, KEY_VISION, KEY_VOICE
from services import Services
from tglayer.models import (
    MEDIA_ANIMATION,
    MEDIA_AUDIO,
    MEDIA_CONTACT,
    MEDIA_DICE,
    MEDIA_DOCUMENT,
    MEDIA_LOCATION,
    MEDIA_PHOTO,
    MEDIA_POLL,
    MEDIA_STICKER,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    GatewayError,
    IncomingMessage,
    MediaRef,
)

logger = logging.getLogger(__name__)

# انواع رسانه‌ای که پردازش نمی‌کنیم؛ فقط به مدل خبر می‌دهیم کاربر چه چیزی فرستاده
_UNSUPPORTED_TITLES = {
    MEDIA_VIDEO: "ویدیو",
    MEDIA_VIDEO_NOTE: "ویدیو پیام",
    MEDIA_ANIMATION: "گیف",
    MEDIA_DOCUMENT: "فایل",
    MEDIA_LOCATION: "موقعیت مکانی",
    MEDIA_CONTACT: "مخاطب",
    MEDIA_POLL: "نظرسنجی",
    MEDIA_DICE: "تاس",
}


class MediaTooLarge(ValueError):
    pass


async def _download_bytes(s: Services, media: MediaRef, max_bytes: int) -> bytes:
    """رسانه را در پوشه‌ی موقت دانلود، می‌خواند و پوشه را (حتی در خطا) پاک می‌کند."""
    if media.size is not None and media.size > max_bytes:
        raise MediaTooLarge(f"{media.size} > {max_bytes}")
    assert s.gateway is not None
    work_dir = Path(s.config.temp_dir).expanduser() / uuid.uuid4().hex
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        path = await s.gateway.download(media, work_dir)
        if path.stat().st_size > max_bytes:  # حجم اعلام‌نشده بود؛ بعد از دانلود هم کنترل می‌کنیم
            raise MediaTooLarge(f"{path.stat().st_size} > {max_bytes}")
        return path.read_bytes()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def build_parts(s: Services, msg: IncomingMessage) -> tuple[list[Part], str]:
    """پیام دریافتی را به (parts, log_text) تبدیل می‌کند؛ پیام بی‌محتوا → ([], "")."""
    cfg = s.config
    caption = (msg.text or "").strip()
    parts: list[Part] = []
    label = ""
    media = msg.media
    max_mb = await s.db.get_int(KEY_MAX_MEDIA_MB, cfg.max_media_bytes // (1024 * 1024))
    max_bytes = max(1, max_mb) * 1024 * 1024

    if media is not None and media.kind == MEDIA_PHOTO:
        label = "[تصویر]"
        if await s.db.get_bool(KEY_VISION, cfg.vision_enabled):
            try:
                data = await _download_bytes(s, media, max_bytes)
                url = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                parts.append(("image", url))
            except MediaTooLarge:
                label = "[تصویر (حجم بیش‌ازحد)]"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Photo download failed: %s", exc)
                label = "[تصویر (بارگیری نشد)]"
        else:
            label = "[تصویر (تحلیل تصویر غیرفعال است)]"

    elif media is not None and media.kind in (MEDIA_VOICE, MEDIA_AUDIO):
        label = "[پیام صوتی]"
        too_long = media.duration is not None and media.duration > cfg.max_audio_seconds
        if not await s.db.get_bool(KEY_VOICE, cfg.voice_enabled):
            label = "[پیام صوتی (تبدیل صدا به متن غیرفعال است)]"
        elif too_long:
            label = "[پیام صوتی (بیش‌ازحد طولانی)]"
        else:
            try:
                data = await _download_bytes(s, media, max_bytes)
                filename = media.file_name or "voice.ogg"
                transcript = await s.ai.transcribe(data, filename)
                label = f"[پیام صوتی]: {transcript}"
            except MediaTooLarge:
                label = "[پیام صوتی (حجم بیش‌ازحد)]"
            except (AIError, GatewayError) as exc:
                logger.warning("Voice transcription failed: %s", exc)
                label = "[پیام صوتی (قابل تبدیل به متن نبود)]"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Voice processing failed: %s", exc)
                label = "[پیام صوتی (پردازش نشد)]"

    elif media is not None and media.kind == MEDIA_STICKER:
        label = f"[استیکر {media.emoji or ''}]".replace(" ]", "]")

    elif media is not None and not caption:
        title = _UNSUPPORTED_TITLES.get(media.kind)
        if title:
            label = f"[کاربر یک «{title}» فرستاد که قابل پردازش نیست]"

    if not label and not caption:
        return [], ""

    full_text = "\n".join(t for t in (label, caption) if t)
    parts.insert(0, ("text", full_text))
    return parts, full_text
