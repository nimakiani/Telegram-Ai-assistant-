"""
تست منطق لایه‌ی Telethon با «کلاینت جعلی».

⚠️ Telethon واقعی در این تست‌ها استفاده نمی‌شود؛ فقط منطق نگاشت خطا، تبدیل پیام، جریان ورود
(کد/رمز دومرحله‌ای) و دسترسی فایل‌ها با یک FakeTelethonClient بررسی می‌شود. رفتار واقعی
سرور تلگرام باید با حساب واقعی آزمایش شود (فهرست در گزارش نهایی).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import tests.support  # noqa: F401
from tglayer import telethon_gateway as tg
from tglayer.models import (
    MEDIA_DOCUMENT,
    MEDIA_PHOTO,
    MEDIA_STICKER,
    MEDIA_VOICE,
    AuthError,
    GatewayError,
    RateLimited,
    SendForbidden,
    TemporaryError,
)


def exc_class(name: str, **attrs):
    return type(name, (Exception,), attrs)


FloodWaitError = exc_class("FloodWaitError")
SessionPasswordNeededError = exc_class("SessionPasswordNeededError")
PhoneCodeInvalidError = exc_class("PhoneCodeInvalidError")
PhoneCodeExpiredError = exc_class("PhoneCodeExpiredError")
PasswordHashInvalidError = exc_class("PasswordHashInvalidError")
PhoneNumberInvalidError = exc_class("PhoneNumberInvalidError")
UserIsBlockedError = exc_class("UserIsBlockedError")
AuthKeyUnregisteredError = exc_class("AuthKeyUnregisteredError")


def flood(seconds: int) -> Exception:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class FakeTelethonClient:
    """شبیه‌ساز حداقلی TelegramClient. وضعیت «وارد شده» با وجود فایل session شبیه‌سازی می‌شود."""

    def __init__(self, session: str, authorized: bool = False):
        self.session_file = Path(session + ".session")
        self.authorized_flag = authorized
        self.code_requests = 0
        self.sign_in_script: list = []  # اقدام‌های sign_in به ترتیب: استثنا یا None (موفق)
        self.send_errors: list = []
        self.sent: list = []
        self.me = NS(id=777, first_name="علی", last_name="رضایی", username="ali", phone="98912")
        self.handlers: list = []
        self.parse_mode = "md"
        self.send_code_error: Exception | None = None

    async def connect(self):
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.touch(exist_ok=True)  # Telethon فایل sqlite را با umask پیش‌فرض می‌سازد
        os.chmod(self.session_file, 0o644)

    async def disconnect(self):
        return None

    async def is_user_authorized(self):
        return self.authorized_flag

    async def send_code_request(self, phone):
        if self.send_code_error:
            raise self.send_code_error
        self.code_requests += 1

    async def sign_in(self, phone=None, code=None, password=None):
        action = self.sign_in_script.pop(0) if self.sign_in_script else None
        if isinstance(action, Exception):
            raise action
        self.authorized_flag = True

    async def get_me(self):
        return self.me

    async def send_message(self, peer, text, link_preview=False):
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((peer, text))
        return NS(id=40 + len(self.sent))

    def add_event_handler(self, cb, ev):
        self.handlers.append((cb, ev))

    async def run_until_disconnected(self):
        return None


def factory_for(client: FakeTelethonClient):
    return lambda api_id, api_hash, session: client


class MapErrorTests(unittest.TestCase):
    def test_mapping(self):
        self.assertIsInstance(tg.map_error(flood(30)), RateLimited)
        self.assertEqual(tg.map_error(flood(30)).seconds, 30)
        self.assertIsInstance(tg.map_error(UserIsBlockedError("x")), SendForbidden)
        self.assertIsInstance(tg.map_error(AuthKeyUnregisteredError("x")), AuthError)
        self.assertIsInstance(tg.map_error(ConnectionResetError("x")), TemporaryError)
        self.assertIsInstance(tg.map_error(asyncio.TimeoutError()), TemporaryError)
        other = tg.map_error(ValueError("weird"))
        self.assertIs(type(other), GatewayError)


class ClassifyMediaTests(unittest.TestCase):
    def msg(self, **kw):
        base = dict(media=object(), file=NS(size=10, duration=3, mime_type="x/y", name="n", emoji=None),
                    photo=None, voice=None, audio=None, sticker=None, video_note=None, gif=None,
                    video=None, document=None, geo=None, contact=None, poll=None, dice=None)
        base.update(kw)
        return NS(**base)

    def test_kinds(self):
        self.assertIsNone(tg.classify_media(NS(media=None)))
        self.assertEqual(tg.classify_media(self.msg(photo=object())).kind, MEDIA_PHOTO)
        # ویس هم document است؛ voice باید قبل از document تشخیص داده شود
        self.assertEqual(tg.classify_media(self.msg(voice=object(), document=object())).kind, MEDIA_VOICE)
        self.assertEqual(tg.classify_media(self.msg(document=object())).kind, MEDIA_DOCUMENT)
        m = self.msg(sticker=object(), file=NS(size=1, duration=None, mime_type=None, name=None, emoji="😀"))
        ref = tg.classify_media(m)
        self.assertEqual((ref.kind, ref.emoji), (MEDIA_STICKER, "😀"))
        self.assertEqual(tg.classify_media(self.msg(voice=object())).size, 10)

    def test_link_preview_is_not_media(self):
        MessageMediaWebPage = type("MessageMediaWebPage", (), {})
        self.assertIsNone(tg.classify_media(self.msg(media=MessageMediaWebPage())))


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = str(Path(self.tmp.name) / "sess" / "assistant")
        self.notes: list[str] = []
        self.codes: list[str] = []
        self.passwords: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    async def ask_code(self):
        return self.codes.pop(0)

    async def ask_password(self):
        return self.passwords.pop(0)

    async def login(self, client, **kw):
        return await tg.login_interactive(
            1, "h" * 32, "+98912", self.session, self.ask_code, self.ask_password,
            notify=self.notes.append, client_factory=factory_for(client), **kw,
        )

    async def test_happy_path_and_session_permissions(self):
        c = FakeTelethonClient(self.session)
        self.codes = ["12345"]
        info = await self.login(c)
        self.assertEqual((info.user_id, info.username), (777, "ali"))
        self.assertEqual(c.code_requests, 1)
        self.assertEqual(oct(c.session_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(c.session_file.parent.stat().st_mode & 0o777), "0o700")

    async def test_already_authorized_skips_code(self):
        c = FakeTelethonClient(self.session, authorized=True)
        await self.login(c)
        self.assertEqual(c.code_requests, 0)

    async def test_wrong_code_then_correct(self):
        c = FakeTelethonClient(self.session)
        c.sign_in_script = [PhoneCodeInvalidError("bad"), None]
        self.codes = ["00000", "12345"]
        await self.login(c)
        self.assertTrue(any("اشتباه" in n for n in self.notes))

    async def test_too_many_wrong_codes(self):
        c = FakeTelethonClient(self.session)
        c.sign_in_script = [PhoneCodeInvalidError("bad")] * 3
        self.codes = ["1", "2", "3"]
        with self.assertRaises(AuthError):
            await self.login(c)

    async def test_expired_code_requests_new_one(self):
        c = FakeTelethonClient(self.session)
        c.sign_in_script = [PhoneCodeExpiredError("old"), None]
        self.codes = ["1", "2"]
        await self.login(c)
        self.assertEqual(c.code_requests, 2)

    async def test_two_step_password_wrong_then_right(self):
        c = FakeTelethonClient(self.session)
        c.sign_in_script = [SessionPasswordNeededError("2fa"), PasswordHashInvalidError("bad"), None]
        self.codes = ["12345"]
        self.passwords = ["wrong", "right"]
        info = await self.login(c)
        self.assertEqual(info.user_id, 777)
        self.assertTrue(any("دومرحله" in n for n in self.notes))

    async def test_two_step_password_exhausted(self):
        c = FakeTelethonClient(self.session)
        c.sign_in_script = [SessionPasswordNeededError("2fa")] + [PasswordHashInvalidError("bad")] * 3
        self.codes = ["12345"]
        self.passwords = ["a", "b", "c"]
        with self.assertRaises(AuthError):
            await self.login(c)

    async def test_invalid_phone_gives_clear_error(self):
        c = FakeTelethonClient(self.session)
        c.send_code_error = PhoneNumberInvalidError("x")
        with self.assertRaises(AuthError) as cm:
            await self.login(c)
        self.assertIn("شماره", str(cm.exception))

    async def test_flood_on_code_request_maps_to_ratelimited(self):
        c = FakeTelethonClient(self.session)
        c.send_code_error = flood(3600)
        with self.assertRaises(RateLimited):
            await self.login(c)

    async def test_session_persists_between_runs(self):
        """اجرای دوم (همان فایل session) بدون پرسیدن کد وارد می‌شود (شبیه‌سازی)."""
        c1 = FakeTelethonClient(self.session)
        self.codes = ["12345"]
        await self.login(c1)
        c2 = FakeTelethonClient(self.session, authorized=c1.authorized_flag and c1.session_file.exists())
        self.codes = []  # اگر کد خواسته شود IndexError می‌شود
        await self.login(c2)
        self.assertEqual(c2.code_requests, 0)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = str(Path(self.tmp.name) / "s" / "a")
        # stub سبک برای `from telethon import events`
        events = types.SimpleNamespace(NewMessage=lambda **kw: ("NewMessage", kw))
        self.patcher = mock.patch.dict(sys.modules, {"telethon": types.SimpleNamespace(events=events)})
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_start_unauthorized_raises_auth_error(self):
        c = FakeTelethonClient(self.session, authorized=False)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        with self.assertRaises(AuthError):
            await gw.start()

    async def test_start_registers_incoming_and_outgoing_handlers(self):
        c = FakeTelethonClient(self.session, authorized=True)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        me = await gw.start()
        self.assertEqual(me.user_id, 777)
        kinds = sorted(tuple(sorted(ev[1].items())) for _, ev in c.handlers)
        self.assertEqual(kinds, [(("incoming", True),), (("outgoing", True),)])

    def event(self, **kw):
        msg = NS(id=11, raw_text="سلام", message="سلام", action=None, media=None, date=datetime.now(timezone.utc))
        sender = NS(first_name="سارا", last_name=None, username="sara", bot=False)

        async def get_sender():
            return sender

        async def get_input_chat():
            return "INPUT_PEER"

        base = dict(message=msg, chat_id=1001, sender_id=1001, is_private=True, is_group=False,
                    is_channel=False, get_sender=get_sender, get_input_chat=get_input_chat)
        base.update(kw)
        return NS(**base)

    async def test_dispatch_converts_raw_events_to_internal_models(self):
        c = FakeTelethonClient(self.session, authorized=True)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        await gw.start()
        got_in, got_out = [], []

        async def on_in(m):
            got_in.append(m)

        async def on_out(m):
            got_out.append(m)

        gw.set_handlers(on_in, on_out)
        await gw._dispatch(self.event(), outgoing=False)
        await gw._dispatch(self.event(chat_id=1002, sender_id=777), outgoing=True)
        m = got_in[0]
        self.assertEqual((m.chat_id, m.sender_name, m.sender_username, m.text, m.chat_type),
                         (1001, "سارا", "sara", "سلام", "private"))
        self.assertFalse(m.is_bot or m.is_service or m.is_self)
        self.assertEqual(got_out[0].chat_id, 1002)
        # پیام‌های بعدی با peer کش‌شده ارسال می‌شوند
        await gw.send_text(1001, "پاسخ")
        self.assertEqual(c.sent[-1][0], "INPUT_PEER")

    async def test_dispatch_flags_service_bot_and_telegram_official(self):
        c = FakeTelethonClient(self.session, authorized=True)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        await gw.start()
        got = []

        async def on_in(m):
            got.append(m)

        gw.set_handlers(on_in, on_in)
        ev = self.event()
        ev.message.action = object()
        await gw._dispatch(ev, outgoing=False)
        await gw._dispatch(self.event(sender_id=777000), outgoing=False)
        bot_sender = NS(first_name="B", last_name=None, username="b_bot", bot=True)

        async def get_bot():
            return bot_sender

        await gw._dispatch(self.event(get_sender=get_bot), outgoing=False)
        await gw._dispatch(self.event(sender_id=777), outgoing=False)
        self.assertEqual([m.is_service for m in got[:2]], [True, True])
        self.assertTrue(got[2].is_bot)
        self.assertTrue(got[3].is_self)

    async def test_dispatch_swallows_handler_exceptions(self):
        c = FakeTelethonClient(self.session, authorized=True)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        await gw.start()

        async def boom(m):
            raise RuntimeError("handler bug")

        gw.set_handlers(boom, boom)
        await gw._dispatch(self.event(), outgoing=False)  # نباید استثنا بدهد

    async def test_send_errors_are_mapped(self):
        c = FakeTelethonClient(self.session, authorized=True)
        gw = tg.TelethonGateway(1, "h", self.session, factory_for(c))
        await gw.start()
        c.send_errors = [flood(42), UserIsBlockedError("blocked"), ConnectionError("down")]
        with self.assertRaises(RateLimited) as cm:
            await gw.send_text(1, "x")
        self.assertEqual(cm.exception.seconds, 42)
        with self.assertRaises(SendForbidden):
            await gw.send_text(1, "x")
        with self.assertRaises(TemporaryError):
            await gw.send_text(1, "x")

    async def test_client_options_for_real_factory(self):
        """_make_client باید FloodWait را به برنامه بسپارد و parse_mode را خاموش کند."""
        captured = {}

        class FakeTelegramClient:
            def __init__(self, session, api_id, api_hash, **kw):
                captured.update(kw, session=session)
                self.parse_mode = "md"

        with mock.patch.dict(sys.modules, {"telethon": types.SimpleNamespace(TelegramClient=FakeTelegramClient)}):
            client = tg._make_client(1, "h", self.session)
        self.assertEqual(captured["flood_sleep_threshold"], 0)
        self.assertTrue(captured["auto_reconnect"])
        self.assertIsNone(client.parse_mode)


if __name__ == "__main__":
    unittest.main()
