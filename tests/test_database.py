"""تست دیتابیس و مهاجرت از نسخه‌ی Business (v1) به نسخه‌ی شخصی (v2)."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

import tests.support  # noqa: F401
from database import ACCOUNT, SCHEMA_VERSION, Database
from tests.fixtures import legacy_database_v1 as legacy


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def _make_legacy(self, path: str) -> None:
        old = legacy.Database(path)
        await old.set_setting("system_prompt", "پرامپت قدیمی")
        await old.set_setting("knowledge", "دانش قدیمی")
        await old.add_message("biz-conn-1", 1001, "user", "سلام قدیمی")
        await old.add_message("biz-conn-1", 1001, "assistant", "پاسخ قدیمی")
        await old.add_quick_reply("قیمت", "۱۰۰ تومان")
        await old.add_to_list(555, "blacklist")
        await old.add_log("biz-conn-1", 1001, 1001, "u", "کاربر", "سلام", "درود", "ok")
        await old.set_pause("biz-conn-1", 1001, 4102444800, "owner", "کاربر")
        old.close()

    async def test_legacy_db_is_migrated_without_data_loss_and_backed_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "assistant.db")
            await self._make_legacy(path)
            db = Database(path)
            # داده‌های قبلی سالم‌اند و با شناسه‌ی حساب شخصی در دسترس
            hist = await db.get_history(ACCOUNT, 1001, 10)
            self.assertEqual([h["content"] for h in hist], ["سلام قدیمی", "پاسخ قدیمی"])
            self.assertEqual(await db.get_setting("knowledge"), "دانش قدیمی")
            self.assertEqual(len(await db.get_quick_replies()), 1)
            self.assertEqual(await db.get_list("blacklist"), [555])
            self.assertEqual(len(await db.export_logs()), 1)
            self.assertIsNotNone(await db.get_pause_until(ACCOUNT, 1001))
            # جدول‌های جدید ساخته شده‌اند
            self.assertEqual(await db.list_chats(), [])
            # پشتیبان قبل از مهاجرت ساخته شده و محتوای قدیمی را دارد
            backups = list((Path(d) / "backups").glob("assistant.db.pre-v2-*"))
            self.assertEqual(len(backups), 1)
            con = sqlite3.connect(backups[0])
            self.assertEqual(con.execute("SELECT connection_id FROM messages").fetchone()[0], "biz-conn-1")
            con.close()
            self.assertEqual(oct(backups[0].stat().st_mode & 0o777), "0o600")
            db.close()

    async def test_migration_is_idempotent_and_no_second_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "assistant.db")
            await self._make_legacy(path)
            Database(path).close()
            db = Database(path)
            db.close()
            self.assertEqual(len(list((Path(d) / "backups").glob("*"))), 1)
            con = sqlite3.connect(path)
            self.assertEqual(con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            con.close()

    async def test_fresh_db_has_no_backup(self):
        with tempfile.TemporaryDirectory() as d:
            Database(str(Path(d) / "x.db")).close()
            self.assertFalse((Path(d) / "backups").exists())


class ChatTablesTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_settings_roundtrip_and_validation(self):
        db = Database(":memory:")
        await db.touch_chat(1, "علی", "ali")
        await db.touch_chat(1, "", None)  # نباید نام را پاک کند
        row = await db.get_chat(1)
        self.assertEqual((row["name"], row["username"]), ("علی", "ali"))
        await db.set_chat_override(1, "history_size", 5)
        with self.assertRaises(KeyError):
            await db.set_chat_override(1, "evil_key", 1)
        self.assertEqual((await db.get_chat_settings(1))["overrides"], {"history_size": 5})
        self.assertEqual(await db.bump_streak(1), 1)
        self.assertEqual(await db.bump_streak(1), 2)
        await db.reset_streak(1)
        self.assertEqual((await db.get_chat_settings(1))["ai_streak"], 0)
        db.close()

    async def test_concurrent_writes_do_not_corrupt(self):
        db = Database(":memory:")
        await asyncio.gather(*(db.add_message(ACCOUNT, i % 3, "user", f"m{i}") for i in range(60)))
        total = 0
        for c in range(3):
            total += len(await db.get_history(ACCOUNT, c, 100))
        self.assertEqual(total, 60)
        db.close()


if __name__ == "__main__":
    unittest.main()
