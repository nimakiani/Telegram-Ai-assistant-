"""تست دستورهای مدیریتی (Saved Messages)."""

from __future__ import annotations

import csv
import io
import unittest

from tests.support import ME_ID, USER_A, USER_B, Env  # noqa: I001

from database import ACCOUNT, LIST_BLACK, LIST_WHITE
from tglayer.models import MEDIA_DOCUMENT, MediaRef


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.e = await Env().setup()

    async def asyncTearDown(self):
        await self.e.close()

    async def test_help_and_status_and_unknown(self):
        out = await self.e.command("/ai help")
        self.assertIn("/ai prompt", out[0])
        out = await self.e.command("/ai")
        self.assertIn("وضعیت", out[0])
        out = await self.e.command("/ai nonsense")
        self.assertIn("نمی‌شناسم", out[0])

    async def test_commands_ignored_outside_saved_messages(self):
        await self.e.owner_says(self.e.owner(chat_id=USER_A, text="/ai off"))
        self.assertTrue(await self.e.db.get_bool("enabled", True))  # اجرا نشد

    async def test_non_command_text_in_saved_messages_is_ignored(self):
        out = await self.e.command("یادداشت شخصی من")
        self.assertEqual(out, [])

    async def test_command_replies_do_not_loop(self):
        await self.e.command("/ai help")
        await self.e.idle()
        self.assertEqual(len([1 for c, _ in self.e.gw.sent if c == ME_ID]), 1)

    async def test_on_off(self):
        await self.e.command("/ai off")
        self.assertFalse(await self.e.db.get_bool("enabled", True))
        await self.e.command("/ai on")
        self.assertTrue(await self.e.db.get_bool("enabled", False))

    async def test_prompt_set_show_reset_multiline(self):
        await self.e.command("/ai prompt set تو دستیار فروش هستی\nمؤدب باش")
        out = await self.e.command("/ai prompt show")
        self.assertIn("مؤدب باش", out[0])
        await self.e.command("/ai prompt reset")
        out = await self.e.command("/ai prompt show")
        self.assertNotIn("مؤدب باش", out[0])

    async def test_knowledge_set_add_clear_and_limit(self):
        await self.e.command("/ai knowledge set ساعت کاری: ۹ تا ۵")
        await self.e.command("/ai knowledge add آدرس: تهران")
        k = await self.e.db.get_setting("knowledge")
        self.assertIn("ساعت کاری", k)
        self.assertIn("تهران", k)
        out = await self.e.command("/ai knowledge set " + "x" * (self.e.config.knowledge_max_chars + 1))
        self.assertIn("❌", out[0])
        self.assertEqual(await self.e.db.get_setting("knowledge"), k)  # دست‌نخورده
        await self.e.command("/ai knowledge clear")
        self.assertEqual(await self.e.db.get_setting("knowledge"), "")

    async def test_knowledge_from_file(self):
        self.e.gw.download_payload = "فایل دانش".encode()
        media = MediaRef(kind=MEDIA_DOCUMENT, size=20, file_name="info.txt", mime="text/plain")
        await self.e.command("/ai knowledge set", media=media)
        self.assertEqual(await self.e.db.get_setting("knowledge"), "فایل دانش")
        bad = MediaRef(kind=MEDIA_DOCUMENT, size=20, file_name="x.exe", mime="application/x")
        out = await self.e.command("/ai knowledge set", media=bad)
        self.assertIn("txt", out[0])

    async def test_lists(self):
        await self.e.command(f"/ai filter black")
        await self.e.command(f"/ai black add {USER_A} {USER_B}")
        self.assertEqual(sorted(await self.e.db.get_list(LIST_BLACK)), [USER_A, USER_B])
        await self.e.command(f"/ai black remove {USER_A}")
        self.assertEqual(await self.e.db.get_list(LIST_BLACK), [USER_B])
        await self.e.command(f"/ai white add ۱۰۰۱")  # ارقام فارسی
        self.assertEqual(await self.e.db.get_list(LIST_WHITE), [1001])
        out = await self.e.command("/ai white add abc")
        self.assertIn("نامعتبر", out[0])

    async def test_quick_replies(self):
        await self.e.command("/ai quick add سلام|درود => سلام! خوش آمدید")
        rows = await self.e.db.get_quick_replies()
        self.assertEqual(len(rows), 1)
        out = await self.e.command(f"/ai quick del {rows[0]['id']}")
        self.assertIn("حذف", out[0])
        out = await self.e.command("/ai quick add بدون-فلش")
        self.assertIn("قالب", out[0])

    async def test_schedule_commands_validate(self):
        out = await self.e.command("/ai hours 25:00-30:00")
        self.assertIn("نامعتبر", out[0])
        await self.e.command("/ai hours 08:30-17:00")
        self.assertEqual(await self.e.db.get_setting("work_start"), "08:30")
        out = await self.e.command("/ai tz Mars/Base")
        self.assertIn("نامعتبر", out[0])
        await self.e.command("/ai tz Asia/Tehran")
        await self.e.command("/ai schedule inside")
        self.assertEqual(await self.e.db.get_setting("schedule_mode"), "inside")

    async def test_set_numbers_validated(self):
        await self.e.command("/ai set cooldown 4.5")
        self.assertEqual(await self.e.db.get_float("cooldown_seconds", 0), 4.5)
        out = await self.e.command("/ai set history 999")
        self.assertIn("بین", out[0])
        out = await self.e.command("/ai set rate abc")
        self.assertIn("عدد", out[0])
        await self.e.command("/ai toggle vision off")
        self.assertFalse(await self.e.db.get_bool("vision_enabled", True))

    async def test_chat_specific_commands(self):
        e = self.e
        await e.command(f"/ai chat {USER_A} prompt فقط فارسی")
        await e.command(f"/ai chat {USER_A} set history 3")
        await e.command(f"/ai chat {USER_A} off")
        cs = await e.db.get_chat_settings(USER_A)
        self.assertEqual((cs["ai_enabled"], cs["custom_prompt"], cs["overrides"]),
                         (False, "فقط فارسی", {"history_size": 3}))
        out = await e.command(f"/ai chat {USER_A} info")
        self.assertIn("خاموش", out[0])
        await e.command(f"/ai chat {USER_A} unset history")
        await e.command(f"/ai chat {USER_A} default")
        cs = await e.db.get_chat_settings(USER_A)
        self.assertEqual((cs["ai_enabled"], cs["overrides"]), (None, {}))
        out = await e.command(f"/ai chat {USER_A} set history 500")
        self.assertIn("بین", out[0])

    async def test_pause_and_resume_commands(self):
        e = self.e
        await e.command(f"/ai pause {USER_A} 30")
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        out = await e.command("/ai paused")
        self.assertIn(str(USER_A), out[0])
        await e.command("/ai resume all")
        self.assertIsNone(await e.db.get_pause_until(ACCOUNT, USER_A))

    async def test_chats_listing_and_username_resolution(self):
        e = self.e
        await e.db.touch_chat(USER_A, "علی", "ali_x")
        out = await e.command("/ai chats")
        self.assertIn("علی", out[0])
        await e.command("/ai pause @ali_x 5")
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        out = await e.command("/ai pause @nobody 5")
        self.assertIn("پیدا نشد", out[0])

    async def test_stats_logs_csv_and_clear(self):
        e = self.e
        await e.send(e.incoming(text="=HYPERLINK(\"http://evil\")"))
        await e.idle()
        self.assertIn("آمار", (await e.command("/ai stats"))[0])
        self.assertIn("آخرین لاگ", (await e.command("/ai logs"))[0])
        await e.command("/ai csv")
        self.assertEqual(len(e.gw.files), 1)
        rows = list(csv.reader(io.StringIO(e.gw.files[0][2].decode("utf-8-sig"))))
        self.assertEqual(rows[0][0], "time_utc")
        self.assertTrue(rows[1][4].startswith("'="))  # تزریق فرمول خنثی شد
        out = await e.command("/ai clearlogs")
        self.assertIn("1", out[0])

    async def test_forget_removes_user_data(self):
        e = self.e
        await e.send(e.incoming())
        await e.idle()
        await e.command(f"/ai forget {USER_A}")
        self.assertEqual(await e.db.get_history(ACCOUNT, USER_A, 10), [])


if __name__ == "__main__":
    unittest.main()
