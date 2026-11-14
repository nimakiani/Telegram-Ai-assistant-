"""
telethon_gateway.py — پیاده‌سازی Gateway با Telethon (پروتکل MTProto، حساب شخصی).

* تنها فایلی که Telethon را import می‌کند (به‌صورت tel‌بل، داخل توابع).
* ورود با روش رسمی تلگرام: شماره تلفن ← کد تأیید ← (در صورت فعال‌بودن) رمز دومرحله‌ای.
* هیچ مکانیزمی برای دور زدن تأیید هویت یا محدودیت‌های تلگرام در اینجا وجود ندارد.
* خطاهای Telethon بر اساس «نام کلاس» به خطاهای داخلی (models.py) نگاشت می‌شوند تا
  موتور به کتابخانه وابسته نباشد و تفاوت نسخه‌ها مشکل‌ساز نشود.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import stat
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from .models import (
    CHAT_CHANNEL,
    CHAT_GROUP,
    CHAT_OTHER,
    CHAT_PRIVATE,
    MEDIA_ANIMATION,
    MEDIA_AUDIO,
    MEDIA_CONTACT,
    MEDIA_DICE,
    MEDIA_DOCUMENT,
    MEDIA_LOCATION,
    MEDIA_OTHER,
    MEDIA_PHOTO,
    MEDIA_POLL,
    MEDIA_STICKER,
    MEDIA_VIDEO,
    MEDIA_VIDEO_NOTE,
    MEDIA_VOICE,
    AuthError,
    ChatInfo,
    Gateway,
    GatewayError,
    IncomingHandler,
    IncomingMessage,
    MediaRef,
    OwnerHandler,
    OwnerMessage,
    RateLimited,
    SelfInfo,
    SendForbidden,
    TemporaryError,
)

logger = logging.getLogger(__name__)

TELEGRAM_SERVICE_USER_ID = 777000  # حساب رسمی «Telegram» (کدهای ورود و اعلان‌ها)

# --- نگاشت خطاها بر اساس نام کلاس
_FLOOD_NAMES = {"FloodWaitError", "SlowModeWaitError", "FloodPremiumWaitError"}
_FORBIDDEN_NAMES = {
    "UserIsBlockedError", "UserPrivacyRestrictedError", "ChatWriteForbiddenError",
    "ChatAdminRequiredError", "InputUserDeactivatedError", "UserDeactivatedError",
    "PeerFloodError", "YouBlockedUserError", "UserBannedInChannelError", "ForbiddenError",
    "MessageEmptyError", "MessageTooLongError",
}
_AUTH_NAMES = {
    "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyInvalidError", "AuthKeyDuplicatedError",
    "SessionRevokedError", "SessionExpiredError", "UserDeactivatedBanError",
    "ApiIdInvalidError", "PhoneNumberBannedError", "SessionPasswordNeededError",
}
_TEMPORARY_NAMES = {
    "RpcCallFailError", "RpcMcgetFailError", "ServerError", "TimedOutError",
    "InternalServerError", "NetworkMigrateError", "ConnectionError",
}


def map_error(exc: BaseException) -> GatewayError:
    """خطای Telethon (یا شبکه) را به خطای داخلی تبدیل می‌کند."""
    if isinstance(exc, GatewayError):
        return exc
    name = type(exc).__name__
    seconds = getattr(exc, "seconds", None)
    if name in _FLOOD_NAMES and isinstance(seconds, int):
        return RateLimited(seconds, f"{name}: باید {seconds} ثانیه صبر کنیم")
    if name in _AUTH_NAMES:
        return AuthError(f"{name}: {exc}")
    if name in _FORBIDDEN_NAMES:
        return SendForbidden(f"{name}: {exc}")
    if name in _TEMPORARY_NAMES or isinstance(
        exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)
    ):
        return TemporaryError(f"{name}: {exc}")
    return GatewayError(f"{name}: {exc}")


# ----------------------------------------------------------------------
# تبدیل پیام‌های خام به مدل‌های داخلی (با duck-typing؛ بدون وابستگی به import)
# ----------------------------------------------------------------------
def classify_media(message: Any) -> MediaRef | None:
    """نوع رسانه‌ی پیام را تشخیص می‌دهد؛ پیام بدون رسانه → None."""
    media_obj = getattr(message, "media", None)
    if media_obj is None:
        return None
    # پیش‌نمایش لینک (WebPage) «رسانه» نیست؛ متن پیام همچنان عادی پردازش می‌شود
    if type(media_obj).__name__ in ("MessageMediaWebPage", "MessageMediaUnsupported"):
        return None
    f = getattr(message, "file", None)
    size = getattr(f, "size", None)
    duration = getattr(f, "duration", None)
    mime = getattr(f, "mime_type", None)
    name = getattr(f, "name", None)
    emoji = getattr(f, "emoji", None)

    def make(kind: str) -> MediaRef:
        return MediaRef(kind=kind, size=size, duration=duration, mime=mime,
                        file_name=name, emoji=emoji, handle=message)

    # ترتیب مهم است: document برای همه‌ی فایل‌ها truthy است
    for attr, kind in (
        ("photo", MEDIA_PHOTO),
        ("voice", MEDIA_VOICE),
        ("audio", MEDIA_AUDIO),
        ("sticker", MEDIA_STICKER),
        ("video_note", MEDIA_VIDEO_NOTE),
        ("gif", MEDIA_ANIMATION),
        ("video", MEDIA_VIDEO),
        ("document", MEDIA_DOCUMENT),
        ("geo", MEDIA_LOCATION),
        ("contact", MEDIA_CONTACT),
        ("poll", MEDIA_POLL),
        ("dice", MEDIA_DICE),
    ):
        if getattr(message, attr, None):
            return make(kind)
    return make(MEDIA_OTHER)


def _chat_type(event: Any) -> str:
    if getattr(event, "is_private", False):
        return CHAT_PRIVATE
    if getattr(event, "is_group", False):
        return CHAT_GROUP
    if getattr(event, "is_channel", False):
        return CHAT_CHANNEL
    return CHAT_OTHER


def _display_name(entity: Any) -> str:
    if entity is None:
        return ""
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    full = f"{first} {last}".strip()
    return full or getattr(entity, "title", None) or getattr(entity, "username", None) or ""


def _msg_text(message: Any) -> str:
    return (getattr(message, "raw_text", None) or getattr(message, "message", None) or "").strip()


def _msg_ts(message: Any) -> float:
    date = getattr(message, "date", None)
    try:
        return float(date.timestamp()) if date else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def secure_session_files(session_path: str) -> None:
    """دسترسی فایل‌های Session را 600 (و پوشه را 700) می‌کند."""
    base = Path(session_path).expanduser()
    with contextlib.suppress(OSError):
        os.chmod(base.parent, 0o700)
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        p = Path(str(base) + suffix)
        if p.exists():
            with contextlib.suppress(OSError):
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)

def _resolve_proxy() -> tuple | None:
    """پروکسی تلگرام را از TG_PROXY می‌خواند (نمونه: socks5://127.0.0.1:10808).

    * اگر TG_PROXY خالی باشد، از ALL_PROXY / HTTPS_PROXY محیط استفاده می‌شود.
    * TG_PROXY=none یعنی بدون پروکسی.
    """
    from urllib.parse import unquote, urlsplit

    raw = (os.environ.get("TG_PROXY") or "").strip()
    if raw.lower() in ("none", "off", "false", "0"):
        return None
    if not raw:
        for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
            raw = (os.environ.get(name) or "").strip()
            if raw:
                break
    if not raw:
        return None
    if "://" not in raw:
        raw = "socks5://" + raw
    parts = urlsplit(raw)
    kinds = {"socks": "socks5", "socks5": "socks5", "socks5h": "socks5",
             "socks4": "socks4", "http": "http", "https": "http"}
    kind = kinds.get(parts.scheme.lower())
    if kind is None or not parts.hostname or not parts.port:
        raise ValueError(f"TG_PROXY نامعتبر است: {raw!r} (نمونه: socks5://127.0.0.1:10808)")
    user = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    return (kind, parts.hostname, parts.port, True, user, password)
def _make_client(api_id: int, api_hash: str, session_path: str) -> Any:
    from telethon import TelegramClient  # import تنبل: فقط وقتی واقعاً لازم است

    Path(session_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(Path(session_path).expanduser()),
        api_id,
        api_hash,
        # FloodWait را خودمان مدیریت می‌کنیم (نه خواب پنهان داخل کتابخانه)
        flood_sleep_threshold=0,
        connection_retries=10,
        retry_delay=2,
        auto_reconnect=True,
        device_model="Telegram AI Assistant",
        app_version="2.0",
        proxy=_resolve_proxy(),
    )
    client.parse_mode = None  # متن ساده: «*» و «_» در پاسخ مدل نباید قالب‌بندی شوند
    return client


# ----------------------------------------------------------------------
# ورود تعاملی
# ----------------------------------------------------------------------
AskFn = Callable[[], Awaitable[str]]
NotifyFn = Callable[[str], None]


async def login_interactive(
    api_id: int,
    api_hash: str,
    phone: str,
    session_path: str,
    ask_code: AskFn,
    ask_password: AskFn,
    notify: NotifyFn = print,
    max_attempts: int = 3,
    client_factory: Callable[[int, str, str], Any] | None = None,
) -> SelfInfo:
    """ورود رسمی با شماره تلفن؛ در پایان Session روی دیسک ذخیره می‌شود."""
    client = (client_factory or _make_client)(api_id, api_hash, session_path)
    try:
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            raise TemporaryError(
                f"اتصال به تلگرام برقرار نشد ({type(exc).__name__}). اینترنت/فیلترشکن را بررسی کنید."
            ) from exc

        if await client.is_user_authorized():
            notify("✅ قبلاً وارد شده‌اید؛ از نشست ذخیره‌شده استفاده می‌شود.")
            return await _self_info(client)

        await _request_code(client, phone, notify)
        signed_in = False
        for attempt in range(1, max_attempts + 1):
            code = (await ask_code()).strip().replace(" ", "").replace("-", "")
            try:
                await client.sign_in(phone=phone, code=code)
                signed_in = True
                break
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if name == "SessionPasswordNeededError":
                    await _sign_in_with_password(client, ask_password, notify, max_attempts)
                    signed_in = True
                    break
                if name == "PhoneCodeInvalidError":
                    notify(f"❌ کد اشتباه است ({attempt}/{max_attempts}). دوباره وارد کنید.")
                    continue
                if name in ("PhoneCodeExpiredError", "PhoneCodeEmptyError"):
                    notify("⌛ کد منقضی شد؛ کد جدید درخواست می‌شود.")
                    await _request_code(client, phone, notify)
                    continue
                raise map_error(exc) from exc
        if not signed_in:
            raise AuthError("کد تأیید چند بار اشتباه وارد شد. چند دقیقه بعد دوباره تلاش کنید.")
        return await _self_info(client)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()
        secure_session_files(session_path)


async def _request_code(client: Any, phone: str, notify: NotifyFn) -> None:
    try:
        await client.send_code_request(phone)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name == "PhoneNumberInvalidError":
            raise AuthError("شماره تلفن نامعتبر است. با کد کشور وارد کنید؛ مثل +989121234567") from exc
        if name == "ApiIdInvalidError":
            raise AuthError("API ID یا API HASH نامعتبر است. از my.telegram.org دوباره بررسی کنید.") from exc
        raise map_error(exc) from exc
    notify("📨 کد تأیید از طرف تلگرام ارسال شد (داخل خود برنامه‌ی تلگرام، از حساب «Telegram»).")


async def _sign_in_with_password(
    client: Any, ask_password: AskFn, notify: NotifyFn, max_attempts: int
) -> None:
    notify("🔐 تأیید دومرحله‌ای (Two-Step Verification) روی حساب شما فعال است.")
    for attempt in range(1, max_attempts + 1):
        password = await ask_password()
        try:
            await client.sign_in(password=password)
            return
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "PasswordHashInvalidError":
                notify(f"❌ رمز اشتباه است ({attempt}/{max_attempts}).")
                continue
            raise map_error(exc) from exc
    raise AuthError("رمز دومرحله‌ای چند بار اشتباه وارد شد.")


async def _self_info(client: Any) -> SelfInfo:
    me = await client.get_me()
    return SelfInfo(
        user_id=int(me.id),
        name=_display_name(me),
        username=getattr(me, "username", None),
        phone=getattr(me, "phone", None),
    )


# ----------------------------------------------------------------------
# Gateway
# ----------------------------------------------------------------------
class TelethonGateway(Gateway):
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: str,
        client_factory: Callable[[int, str, str], Any] | None = None,
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._client_factory = client_factory or _make_client
        self._client: Any = None
        self._me: SelfInfo | None = None
        self._peers: dict[int, Any] = {}
        self._on_incoming: IncomingHandler | None = None
        self._on_owner: OwnerHandler | None = None

    # -- چرخه‌ی حیات
    async def start(self) -> SelfInfo:
        self._client = self._client_factory(self._api_id, self._api_hash, self._session_path)
        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise AuthError(
                    "نشست ورود وجود ندارد یا منقضی شده است. ابتدا ورود را انجام دهید: ./start.sh --setup"
                )
            self._me = await _self_info(self._client)
        except GatewayError:
            await self.stop()
            raise
        except Exception as exc:  # noqa: BLE001
            await self.stop()
            raise map_error(exc) from exc
        secure_session_files(self._session_path)
        self._register_events()
        return self._me

    def _register_events(self) -> None:
        from telethon import events

        async def incoming(event: Any) -> None:
            await self._dispatch(event, outgoing=False)

        async def outgoing(event: Any) -> None:
            await self._dispatch(event, outgoing=True)

        self._client.add_event_handler(incoming, events.NewMessage(incoming=True))
        self._client.add_event_handler(outgoing, events.NewMessage(outgoing=True))

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        secure_session_files(self._session_path)

    def set_handlers(self, on_incoming: IncomingHandler, on_owner: OwnerHandler) -> None:
        self._on_incoming = on_incoming
        self._on_owner = on_owner

    async def run_until_disconnected(self) -> None:
        try:
            await self._client.run_until_disconnected()
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc

    # -- دریافت
    async def _dispatch(self, event: Any, outgoing: bool) -> None:
        try:
            message = event.message
            chat_id = int(event.chat_id)
            with contextlib.suppress(Exception):
                peer = await event.get_input_chat()
                if peer is not None:
                    self._peers[chat_id] = peer
            ctype = _chat_type(event)
            service = getattr(message, "action", None) is not None
            if outgoing:
                if self._on_owner is None:
                    return
                await self._on_owner(
                    OwnerMessage(
                        chat_id=chat_id,
                        message_id=int(message.id),
                        chat_type=ctype,
                        text=_msg_text(message),
                        media=classify_media(message),
                        is_service=service,
                        ts=_msg_ts(message),
                    )
                )
                return
            if self._on_incoming is None:
                return
            sender = None
            with contextlib.suppress(Exception):
                sender = await event.get_sender()
            sender_id = getattr(event, "sender_id", None)
            await self._on_incoming(
                IncomingMessage(
                    chat_id=chat_id,
                    message_id=int(message.id),
                    sender_id=int(sender_id) if sender_id is not None else None,
                    sender_name=_display_name(sender),
                    sender_username=getattr(sender, "username", None),
                    chat_type=ctype,
                    text=_msg_text(message),
                    media=classify_media(message),
                    is_bot=bool(getattr(sender, "bot", False)),
                    is_service=service or sender_id == TELEGRAM_SERVICE_USER_ID,
                    is_self=self._me is not None and sender_id == self._me.user_id,
                    ts=_msg_ts(message),
                )
            )
        except Exception:  # noqa: BLE001 — خطای یک پیام نباید حلقه‌ی دریافت را بکشد
            logger.exception("Error while handling a Telegram event")

    def _peer(self, chat_id: int) -> Any:
        return self._peers.get(chat_id, chat_id)

    # -- ارسال
    async def send_text(self, chat_id: int, text: str) -> int:
        try:
            sent = await self._client.send_message(self._peer(chat_id), text, link_preview=False)
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
        return int(sent.id)

    async def send_file(
        self, chat_id: int, data: bytes, filename: str, caption: str | None = None
    ) -> int:
        buf = io.BytesIO(data)
        buf.name = filename
        try:
            sent = await self._client.send_file(
                self._peer(chat_id), buf, caption=caption, force_document=True
            )
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
        return int(sent.id)

    @contextlib.asynccontextmanager
    async def typing(self, chat_id: int) -> AsyncIterator[None]:
        cm: Any = None
        try:
            cm = self._client.action(self._peer(chat_id), "typing")
            await cm.__aenter__()
        except Exception as exc:  # noqa: BLE001
            logger.debug("typing action unavailable: %s", exc)
            cm = None
        try:
            yield
        finally:
            if cm is not None:
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)

    async def mark_read(self, chat_id: int, message_id: int) -> None:
        try:
            await self._client.send_read_acknowledge(self._peer(chat_id), max_id=message_id)
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc

    # -- فایل
    async def download(self, media: MediaRef, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            path = await self._client.download_media(media.handle, file=str(dest_dir) + os.sep)
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
        if not path:
            raise GatewayError("دانلود فایل ناموفق بود (مسیری برنگشت).")
        return Path(path)

    # -- جست‌وجو
    async def resolve_chat(self, ref: str) -> ChatInfo | None:
        ref = ref.strip()
        key: Any = ref
        if ref.lstrip("-").isdigit():
            key = int(ref)
        try:
            entity = await self._client.get_entity(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("resolve_chat(%r) failed: %s", ref, exc)
            return None
        return self._entity_to_chat(entity)

    @staticmethod
    def _entity_to_chat(entity: Any) -> ChatInfo:
        cname = type(entity).__name__
        ctype = CHAT_PRIVATE if cname == "User" else (CHAT_CHANNEL if cname == "Channel" else CHAT_GROUP)
        return ChatInfo(
            chat_id=int(entity.id),
            name=_display_name(entity),
            username=getattr(entity, "username", None),
            chat_type=ctype,
            is_bot=bool(getattr(entity, "bot", False)),
        )

    async def recent_private_chats(self, limit: int = 30) -> list[ChatInfo]:
        chats: list[ChatInfo] = []
        try:
            async for dialog in self._client.iter_dialogs(limit=limit * 2):
                if not getattr(dialog, "is_user", False):
                    continue
                entity = dialog.entity
                if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
                    continue
                if int(entity.id) == TELEGRAM_SERVICE_USER_ID:
                    continue
                chats.append(self._entity_to_chat(entity))
                if len(chats) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
        return chats
