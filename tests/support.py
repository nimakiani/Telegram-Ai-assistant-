"""
زیرساخت تست — «شبیه‌سازی» تلگرام و هوش مصنوعی.

⚠️ تمام تست‌های این پوشه با Mock اجرا می‌شوند (FakeGateway / FakeAI / FakeTelethonClient).
هیچ‌کدام جایگزین آزمایش واقعی با یک حساب تلگرام و یک سرویس AI واقعی نیستند.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, AsyncIterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- در محیط‌هایی که openai نصب نیست، یک stub سبک می‌گذاریم (ai_handler فقط import می‌کند).
if "openai" not in sys.modules:
    try:
        import openai  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("openai")

        class _Err(Exception):
            pass

        for name in ("APIConnectionError", "APIStatusError", "InternalServerError", "RateLimitError"):
            setattr(stub, name, type(name, (_Err,), {}))
        stub.AsyncOpenAI = lambda *a, **k: None  # type: ignore[attr-defined]
        sys.modules["openai"] = stub

for k, v in {
    "TG_API_ID": "12345",
    "TG_API_HASH": "0123456789abcdef0123456789abcdef",
    "AI_API_KEY": "test-key",
}.items():
    os.environ.setdefault(k, v)
os.environ["TZ"] = "UTC"

from ai_handler import AIError, AIResult  # noqa: E402
from app import build_services, seed_defaults  # noqa: E402
from config import Config, load_config  # noqa: E402
from database import Database  # noqa: E402
from tglayer.models import (  # noqa: E402
    CHAT_PRIVATE,
    Gateway,
    IncomingMessage,
    MediaRef,
    OwnerMessage,
    SelfInfo,
)

ME_ID = 999
USER_A = 1001
USER_B = 1002


class FakeAI:
    """جایگزین AIHandler: پاسخ‌ها، تأخیر و خطا قابل کنترل‌اند."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies: list[Any] = []  # str | Exception | callable
        self.delay = 0.0
        self.gate: asyncio.Event | None = None  # اگر ست شود، تولید پاسخ تا set شدن منتظر می‌ماند
        self.started = asyncio.Event()
        self.transcript = "متن ویس"
        self.transcribe_calls = 0
        self.transcribe_error: Exception | None = None

    async def generate_reply(self, system_prompt: str, history: Any, parts: Any) -> AIResult:
        self.calls.append({"system": system_prompt, "history": list(history), "parts": list(parts)})
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.replies.pop(0) if self.replies else "پاسخ آزمایشی"
        if callable(item):
            item = item(self.calls[-1])
        if isinstance(item, Exception):
            raise item
        return AIResult(text=item, model="fake-model", latency_ms=5, prompt_tokens=3, completion_tokens=4)

    async def transcribe(self, data: bytes, filename: str = "voice.ogg") -> str:
        self.transcribe_calls += 1
        if self.transcribe_error:
            raise self.transcribe_error
        return self.transcript


class FakeGateway(Gateway):
    """شبیه‌ساز تلگرام. با echo_outgoing=True هر پیامی که می‌فرستیم، مثل تلگرام واقعی،
    به‌صورت «رویداد پیام خروجی» هم برمی‌گردد (حتی قبل از بازگشت send_text)."""

    def __init__(self, me_id: int = ME_ID, echo_outgoing: bool = True) -> None:
        self.me = SelfInfo(user_id=me_id, name="Owner")
        self.echo_outgoing = echo_outgoing
        self.sent: list[tuple[int, str]] = []
        self.files: list[tuple[int, str, bytes]] = []
        self.send_delay = 0.0
        self.send_errors: list[Exception] = []  # در هر ارسال یکی برداشته و پرتاب می‌شود
        self.read: list[tuple[int, int]] = []
        self.typing_calls: list[int] = []
        self.downloads: list[Path] = []
        self.download_payload = b"fake-bytes"
        self.download_error: Exception | None = None
        self._next_id = 5000
        self._on_incoming = None
        self._on_owner = None
        self.chats: dict[str, Any] = {}

    async def start(self) -> SelfInfo:
        return self.me

    async def stop(self) -> None:
        return None

    def set_handlers(self, on_incoming, on_owner) -> None:
        self._on_incoming, self._on_owner = on_incoming, on_owner

    async def run_until_disconnected(self) -> None:
        await asyncio.Event().wait()

    async def send_text(self, chat_id: int, text: str) -> int:
        if self.send_errors:
            raise self.send_errors.pop(0)
        self._next_id += 1
        mid = self._next_id
        if self.echo_outgoing and self._on_owner is not None:
            # رویداد خروجی پیش از بازگشت send_text می‌رسد (بدترین حالت Race)
            asyncio.get_running_loop().create_task(
                self._on_owner(OwnerMessage(chat_id=chat_id, message_id=mid, text=text))
            )
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        else:
            await asyncio.sleep(0)
        self.sent.append((chat_id, text))
        return mid

    async def send_file(self, chat_id: int, data: bytes, filename: str, caption=None) -> int:
        self._next_id += 1
        self.files.append((chat_id, filename, data))
        return self._next_id

    def typing(self, chat_id: int):
        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[None]:
            self.typing_calls.append(chat_id)
            yield

        return cm()

    async def mark_read(self, chat_id: int, message_id: int) -> None:
        self.read.append((chat_id, message_id))

    async def download(self, media: MediaRef, dest_dir: Path) -> Path:
        if self.download_error:
            raise self.download_error
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / (media.file_name or "file.bin")
        path.write_bytes(self.download_payload)
        self.downloads.append(path)
        return path

    async def resolve_chat(self, ref: str):
        return self.chats.get(ref)

    async def recent_private_chats(self, limit: int = 30):
        return list(self.chats.values())[:limit]

    # --- کمکی‌های تست
    def texts_to(self, chat_id: int) -> list[str]:
        return [t for c, t in self.sent if c == chat_id]


class Env:
    """یک محیط کامل تست: دیتابیس (حافظه‌ای)، AI و Gateway جعلی، موتور و دستورها."""

    def __init__(self, **cfg_overrides: Any) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = load_config()
        defaults = dict(
            default_debounce=0.05, default_cooldown=0.0, min_typing_seconds=0.0,
            default_rate_limit=0, default_pause_minutes=60, temp_dir=str(Path(self.tmp.name) / "tmp"),
            database_path=":memory:", max_flood_wait=5, fallback_message="", mark_as_read=False,
        )
        defaults.update(cfg_overrides)
        self.config: Config = dataclasses.replace(base, **defaults)
        self.db = Database(":memory:")
        self.ai = FakeAI()
        self.gw = FakeGateway()
        self.services, self.engine, self.commands = build_services(
            self.config, self.db, self.ai, self.gw
        )
        self.services.me = self.gw.me
        self.gw.set_handlers(self.engine.on_incoming, self.engine.on_owner)
        self._mid = 0

    async def setup(self) -> "Env":
        await seed_defaults(self.db, self.config)
        return self

    async def close(self) -> None:
        await self.engine.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def incoming(self, chat_id: int = USER_A, text: str = "سلام", **kw: Any) -> IncomingMessage:
        self._mid += 1
        return IncomingMessage(
            chat_id=chat_id, message_id=self._mid, sender_id=kw.pop("sender_id", chat_id),
            sender_name=kw.pop("sender_name", f"user{chat_id}"), text=text, **kw,
        )

    def owner(self, chat_id: int = USER_A, text: str = "من خودم هستم", **kw: Any) -> OwnerMessage:
        self._mid += 1
        return OwnerMessage(chat_id=chat_id, message_id=100000 + self._mid, text=text, **kw)

    async def send(self, msg: IncomingMessage) -> None:
        await self.engine.on_incoming(msg)

    async def owner_says(self, msg: OwnerMessage) -> None:
        """پیام صاحب حساب را می‌دهد و صبر می‌کند تا همان پیام کامل پردازش شود."""
        await self.engine.on_owner(msg)
        task = self.engine.last_owner_task
        if task is not None:
            await asyncio.wait_for(task, 10)

    async def idle(self) -> None:
        await self.engine.wait_idle(10)
        await asyncio.sleep(0.02)
        await self.engine.wait_idle(10)

    async def command(self, text: str, **kw: Any) -> list[str]:
        """یک دستور را در Saved Messages شبیه‌سازی و پاسخ‌های ارسالی را برمی‌گرداند."""
        before = len(self.gw.sent)
        await self.owner_says(self.owner(chat_id=ME_ID, text=text, **kw))
        await self.idle()
        return [t for c, t in self.gw.sent[before:] if c == ME_ID]
