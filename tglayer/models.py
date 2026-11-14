"""
models.py — ساختارهای استاندارد داخلی و رابط Gateway.

رویدادهای خام Telethon هرگز وارد موتور هوش مصنوعی نمی‌شوند؛ ابتدا در
`telethon_gateway.py` به این ساختارها تبدیل می‌شوند.
"""

from __future__ import annotations

import abc
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

# انواع گفتگو
CHAT_PRIVATE = "private"
CHAT_GROUP = "group"
CHAT_CHANNEL = "channel"
CHAT_OTHER = "other"

# انواع رسانه
MEDIA_PHOTO = "photo"
MEDIA_VOICE = "voice"
MEDIA_AUDIO = "audio"
MEDIA_STICKER = "sticker"
MEDIA_VIDEO = "video"
MEDIA_VIDEO_NOTE = "video_note"
MEDIA_ANIMATION = "animation"
MEDIA_DOCUMENT = "document"
MEDIA_LOCATION = "location"
MEDIA_CONTACT = "contact"
MEDIA_POLL = "poll"
MEDIA_DICE = "dice"
MEDIA_OTHER = "other"


# ----------------------------------------------------------------------
# خطاها (مستقل از کتابخانه‌ی تلگرام)
# ----------------------------------------------------------------------
class GatewayError(RuntimeError):
    """خطای عمومی ارتباط با تلگرام."""


class AuthError(GatewayError):
    """خطای ورود/احراز هویت (کد اشتباه، رمز اشتباه، سشن منقضی، API ID نامعتبر ...)."""


class RateLimited(GatewayError):
    """FloodWait: تلگرام خواسته `seconds` ثانیه صبر کنیم."""

    def __init__(self, seconds: int, message: str = "") -> None:
        super().__init__(message or f"FloodWait: {seconds}s")
        self.seconds = int(seconds)


class SendForbidden(GatewayError):
    """ارسال به این گفتگو ممکن نیست (مسدود شده، حریم خصوصی، محدودیت حساب ...). تکرار بی‌فایده است."""


class TemporaryError(GatewayError):
    """خطای موقتی شبکه/سرور تلگرام."""


# ----------------------------------------------------------------------
# مدل‌ها
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class MediaRef:
    """مشخصات رسانه‌ی ضمیمه‌ی پیام. `handle` شیء مبهمی است که فقط Gateway می‌فهمد."""

    kind: str
    size: int | None = None
    duration: int | None = None
    mime: str | None = None
    file_name: str | None = None
    emoji: str | None = None
    handle: Any = field(default=None, compare=False, repr=False)


@dataclass
class IncomingMessage:
    """پیامی که از طرف دیگران در یک گفتگو رسیده است."""

    chat_id: int
    message_id: int
    sender_id: int | None
    sender_name: str = ""
    sender_username: str | None = None
    chat_type: str = CHAT_PRIVATE
    text: str = ""
    media: MediaRef | None = None
    is_bot: bool = False
    is_service: bool = False
    is_self: bool = False
    ts: float = 0.0


@dataclass
class OwnerMessage:
    """پیامی که خودِ صاحب حساب (از هر دستگاه) فرستاده است."""

    chat_id: int
    message_id: int
    chat_type: str = CHAT_PRIVATE
    text: str = ""
    media: MediaRef | None = None
    is_service: bool = False
    ts: float = 0.0


@dataclass
class ChatInfo:
    chat_id: int
    name: str = ""
    username: str | None = None
    chat_type: str = CHAT_PRIVATE
    is_bot: bool = False


@dataclass
class SelfInfo:
    user_id: int
    name: str = ""
    username: str | None = None
    phone: str | None = None


IncomingHandler = Callable[[IncomingMessage], Awaitable[None]]
OwnerHandler = Callable[[OwnerMessage], Awaitable[None]]


class Gateway(abc.ABC):
    """رابط ارتباط با تلگرام؛ هر کتابخانه‌ای که این رابط را پیاده کند قابل جایگزینی است."""

    @abc.abstractmethod
    async def start(self) -> SelfInfo:
        """اتصال با سشن ذخیره‌شده. اگر وارد نشده باشیم AuthError پرتاب می‌شود."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def set_handlers(self, on_incoming: IncomingHandler, on_owner: OwnerHandler) -> None: ...

    @abc.abstractmethod
    async def run_until_disconnected(self) -> None: ...

    @abc.abstractmethod
    async def send_text(self, chat_id: int, text: str) -> int:
        """یک پیام (حداکثر ۴۰۹۶ نویسه) می‌فرستد و شناسه‌ی پیام را برمی‌گرداند."""

    @abc.abstractmethod
    async def send_file(
        self, chat_id: int, data: bytes, filename: str, caption: str | None = None
    ) -> int: ...

    @abc.abstractmethod
    def typing(self, chat_id: int) -> contextlib.AbstractAsyncContextManager[None]:
        """async with gateway.typing(chat_id): ... — نمایش «در حال تایپ»."""

    @abc.abstractmethod
    async def mark_read(self, chat_id: int, message_id: int) -> None: ...

    @abc.abstractmethod
    async def download(self, media: MediaRef, dest_dir: Path) -> Path:
        """رسانه را در dest_dir ذخیره و مسیر فایل را برمی‌گرداند."""

    @abc.abstractmethod
    async def resolve_chat(self, ref: str) -> ChatInfo | None:
        """شناسه‌ی عددی یا @username را به گفتگو تبدیل می‌کند؛ ناموفق → None."""

    @abc.abstractmethod
    async def recent_private_chats(self, limit: int = 30) -> list[ChatInfo]: ...


@contextlib.asynccontextmanager
async def null_typing() -> AsyncIterator[None]:
    yield
