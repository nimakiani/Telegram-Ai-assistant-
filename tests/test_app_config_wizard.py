"""تست config، فایل .env، wizard راه‌اندازی و چرخه‌ی اجرای برنامه (همه با Mock)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support as support
from tests.support import FakeGateway
from tests.test_gateway import (
    FakeTelethonClient,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    factory_for,
)

import app
import config as config_mod
import envfile
import setup_wizard
from database import KEY_LIST_MODE, LIST_WHITE, Database
from tglayer.models import AuthError, ChatInfo, TemporaryError


class ConfigTests(unittest.TestCase):
    def test_all_problems_reported_together(self):
        env = {"TG_API_ID": "abc", "TG_API_HASH": "short", "AI_API_KEY": "", "TIMEZONE": "Mars/X",
               "SCHEDULE_MODE": "weird", "HISTORY_SIZE": "many"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(config_mod.ConfigError) as cm:
                config_mod.load_config()
        msg = str(cm.exception)
        for needle in ("TG_API_ID", "TG_API_HASH", "AI_API_KEY", "TIMEZONE", "SCHEDULE_MODE", "HISTORY_SIZE"):
            self.assertIn(needle, msg)

    def test_valid_config(self):
        cfg = config_mod.load_config()
        self.assertEqual(cfg.tg_api_id, 12345)
        self.assertEqual(cfg.default_max_streak, 20)

    def test_bot_token_no_longer_required(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_TOKEN", None)
            os.environ.pop("ADMIN_IDS", None)
            config_mod.load_config()


class EnvFileTests(unittest.TestCase):
    def test_update_preserves_comments_and_sets_600(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# توضیح\nAI_MODEL=old\nUNKNOWN=keep\n", encoding="utf-8")
            os.chmod(p, 0o644)
            envfile.update_env(p, {"AI_MODEL": "new", "TG_PHONE": "+98912"})
            text = p.read_text(encoding="utf-8")
            self.assertIn("# توضیح", text)
            self.assertIn("UNKNOWN=keep", text)
            self.assertIn("AI_MODEL=new", text)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(envfile.read_env(p)["TG_PHONE"], "+98912")

    def test_roundtrip_special_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            tricky = 'p@ss "w#rd" با فاصله\\x'
            envfile.update_env(p, {"AI_API_KEY": tricky})
            self.assertEqual(envfile.read_env(p)["AI_API_KEY"], tricky)

    def test_mask_hides_secrets(self):
        self.assertNotIn("abcdef", envfile.mask("AI_API_KEY", "sk-abcdef123456789"))
        self.assertEqual(envfile.mask("AI_MODEL", "gpt"), "gpt")


class WizardTests(unittest.IsolatedAsyncioTestCase):
    HASH = "ab" * 16

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / ".env.example").write_text(
            (support.ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
        )
        # مسیرها مطلق تا به cwd وابسته نباشد
        envfile.update_env(
            self.dir / ".env",
            {"TG_SESSION_PATH": str(self.dir / "data/session/assistant"),
             "DATABASE_PATH": str(self.dir / "data/assistant.db")},
        )
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for k in ("TG_API_ID", "TG_API_HASH", "AI_API_KEY"):
            os.environ.pop(k, None)
        self.out: list[str] = []

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def make(self, answers, secrets, client, chats=()):
        answers, secrets = list(answers), list(secrets)
        gw = FakeGateway()
        gw.chats = {str(c.chat_id): c for c in chats}

        class FakeAIOut:
            model = "gpt-test"

        class FakeAI:
            async def generate_reply(self, *a):
                return FakeAIOut()

        return setup_wizard.Wizard(
            self.dir, ask=lambda prompt: answers.pop(0), ask_secret=lambda prompt: secrets.pop(0),
            out=self.out.append, client_factory=factory_for(client),
            gateway_factory=lambda *a: gw, ai_factory=lambda cfg: FakeAI(),
        ), answers, secrets

    async def test_full_run_whitelist_openai_with_2fa(self):
        client = FakeTelethonClient(str(self.dir / "data/session/assistant"))
        client.sign_in_script = [PhoneCodeInvalidError("x"), SessionPasswordNeededError("2fa"), None]
        chats = [ChatInfo(111, "سارا", "sara"), ChatInfo(222, "رضا", None)]
        wiz, answers, secrets = self.make(
            answers=["۱۲۳۴۵۶۷", "+989121234567", "00000", "12345", "3", "2", "1", "", ],
            #        api id      phone            bad    good   white  pick#2 openai model(Enter)
            secrets=[self.HASH, "my-2fa-password", "sk-SECRET-KEY-123456"],
            client=client, chats=chats,
        )
        # ترتیب secrets: api_hash، رمز دومرحله‌ای (داخل login)، کلید AI
        code = await wiz.run_async()
        self.assertEqual(code, 0, "\n".join(self.out))
        env = envfile.read_env(self.dir / ".env")
        self.assertEqual((env["TG_API_ID"], env["TG_API_HASH"], env["TG_PHONE"]), ("1234567", self.HASH, "+989121234567"))
        self.assertEqual((env["AI_API_KEY"], env["AI_MODEL"]), ("sk-SECRET-KEY-123456", "gpt-4o-mini"))
        self.assertEqual(stat.S_IMODE((self.dir / ".env").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(client.session_file.stat().st_mode), 0o600)
        # اطلاعات حساس هرگز چاپ نشده‌اند
        printed = "\n".join(self.out)
        for secret in (self.HASH, "my-2fa-password", "sk-SECRET-KEY-123456"):
            self.assertNotIn(secret, printed)
        # لیست سفید در دیتابیس
        db = Database(str(self.dir / "data/assistant.db"))
        self.assertEqual(await db.get_setting(KEY_LIST_MODE), LIST_WHITE)
        self.assertEqual(await db.get_list(LIST_WHITE), [222])
        db.close()
        for step in range(1, 13):
            self.assertIn(f"مرحله {step} از 12", printed)
        self.assertEqual((answers, secrets), ([], []))

    async def test_validation_loops_reprompt(self):
        client = FakeTelethonClient(str(self.dir / "data/session/assistant"), authorized=True)
        wiz, _, _ = self.make(
            answers=["abc", "42", "09121234567", "+989121234567", "1", "4", "llama3.1"],
            secrets=["short", self.HASH],
            client=client,
        )
        wiz.step_api_id()
        wiz.step_api_hash()
        wiz.step_phone()
        printed = "\n".join(self.out)
        self.assertIn("فقط یک عدد", printed)
        self.assertIn("۳۲ نویسه", printed)
        self.assertIn("نامعتبر", printed)
        self.assertEqual(wiz.env["TG_PHONE"], "+989121234567")

    async def test_ollama_needs_no_key_and_disables_voice_vision(self):
        client = FakeTelethonClient(str(self.dir / "data/session/assistant"))
        wiz, _, _ = self.make(["4", "mistral"], [], client)
        provider = wiz.step_ai()
        wiz.step_api_key(provider)
        wiz.step_model(provider)
        env = envfile.read_env(self.dir / ".env")
        self.assertEqual((env["AI_API_KEY"], env["AI_MODEL"], env["VOICE_ENABLED"], env["VISION_ENABLED"]),
                         ("none", "mistral", "false", "false"))

    async def test_login_failure_aborts_with_clear_message(self):
        client = FakeTelethonClient(str(self.dir / "data/session/assistant"))
        client.sign_in_script = [PhoneCodeInvalidError("x")] * 3
        wiz, _, _ = self.make(["1234567", "+989121234567", "1", "2", "3"], [self.HASH], client)
        self.assertEqual(await wiz.run_async(), 3)
        self.assertIn("ورود ناموفق", "\n".join(self.out))


class AppLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = config_mod.load_config()
        self.cfg = dataclasses.replace(
            base, database_path=str(Path(self.tmp.name) / "data" / "a.db"), log_max_rows=100,
            lock_path=str(Path(self.tmp.name) / "data" / "a.lock")
        )
        patcher = mock.patch.multiple(app, INITIAL_BACKOFF=0.01, MAX_BACKOFF=0.02, MAX_CONSECUTIVE_FAILURES=3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    async def test_clean_start_and_stop_releases_lock(self):
        stop = asyncio.Event()
        gw = FakeGateway()
        task = asyncio.create_task(app.run(self.cfg, lambda c: gw, False, stop))
        await asyncio.sleep(0.2)
        # نمونه‌ی دوم باید رد شود
        self.assertEqual(await app.run(self.cfg, lambda c: FakeGateway(), False, asyncio.Event()), app.EXIT_LOCKED)
        stop.set()
        self.assertEqual(await asyncio.wait_for(task, 5), app.EXIT_OK)
        # قفل آزاد شد؛ اجرای بعدی ممکن است
        stop2 = asyncio.Event()
        t2 = asyncio.create_task(app.run(self.cfg, lambda c: FakeGateway(), False, stop2))
        await asyncio.sleep(0.1)
        stop2.set()
        self.assertEqual(await asyncio.wait_for(t2, 5), app.EXIT_OK)

    async def test_auth_failure_at_start(self):
        class Bad(FakeGateway):
            async def start(self):
                raise AuthError("no session")

        self.assertEqual(await app.run(self.cfg, lambda c: Bad(), False, asyncio.Event()), app.EXIT_AUTH)

    async def test_repeated_disconnects_give_up_with_exit_code(self):
        calls = {"n": 0}

        class Flaky(FakeGateway):
            async def run_until_disconnected(self):
                calls["n"] += 1
                raise TemporaryError("network down")

        code = await asyncio.wait_for(app.run(self.cfg, lambda c: Flaky(), False, asyncio.Event()), 10)
        self.assertEqual(code, app.EXIT_CONNECTION)
        self.assertEqual(calls["n"], 3)

    async def test_revoked_session_during_run_exits_with_auth_code(self):
        class Revoked(FakeGateway):
            async def run_until_disconnected(self):
                raise AuthError("revoked")

        code = await asyncio.wait_for(app.run(self.cfg, lambda c: Revoked(), False, asyncio.Event()), 5)
        self.assertEqual(code, app.EXIT_AUTH)

    async def test_reconnect_recovers(self):
        state = {"runs": 0}
        stop = asyncio.Event()

        class Recovering(FakeGateway):
            async def run_until_disconnected(self):
                state["runs"] += 1
                if state["runs"] == 1:
                    raise TemporaryError("blip")
                await asyncio.Event().wait()

        task = asyncio.create_task(app.run(self.cfg, lambda c: Recovering(), False, stop))
        await asyncio.sleep(0.3)
        self.assertEqual(state["runs"], 2)
        stop.set()
        self.assertEqual(await asyncio.wait_for(task, 5), app.EXIT_OK)


class LogRedactionTests(unittest.TestCase):
    def test_secrets_are_redacted(self):
        import logging

        import main

        f = main.RedactFilter(["sk-SECRET-KEY-123456", "0123456789abcdef0123456789abcdef"])
        rec = logging.LogRecord("x", logging.INFO, "f", 1, "key=%s hash=%s", ("sk-SECRET-KEY-123456", "0123456789abcdef0123456789abcdef"), None)
        f.filter(rec)
        self.assertEqual(rec.getMessage(), "key=*** hash=***")


if __name__ == "__main__":
    unittest.main()
