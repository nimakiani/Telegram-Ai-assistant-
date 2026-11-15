"""تست پنل ربات (دکمه‌های شیشه‌ای) — با ربات جعلی؛ بدون اینترنت و بدون تلگرام واقعی."""

from __future__ import annotations

import asyncio
import json
import unittest

from tests.support import ME_ID, USER_A, USER_B, Env  # noqa: I001

from about import CREATOR_FA
from botapi import BotApi, BotApiError, _multipart
from database import ACCOUNT, KEY_ENABLED, KEY_SILENT_UNTIL
from panel import Panel


class FakeBotApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.docs: list[tuple[int, str, bytes]] = []
        self.files: dict[str, bytes] = {}
        self.updates: asyncio.Queue = asyncio.Queue()
        self.fail_get_me: BotApiError | None = None
        self._mid = 100

    async def call(self, method, params=None, timeout=None):
        self.calls.append((method, dict(params or {})))
        if method == "getMe":
            if self.fail_get_me:
                raise self.fail_get_me
            return {"username": "my_panel_bot"}
        if method == "getUpdates":
            item = await self.updates.get()
            if isinstance(item, Exception):
                raise item
            return item
        if method in ("sendMessage", "editMessageText"):
            self._mid += 1
            return {"message_id": self._mid}
        return True

    async def send_document(self, chat_id, data, filename, caption=None):
        self.docs.append((chat_id, filename, data))

    async def download_file(self, file_id, max_bytes):
        return self.files[file_id]

    # --- کمکی‌ها
    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def last_text(self) -> str:
        shown = [p for m, p in self.calls if m in ("sendMessage", "editMessageText")]
        return shown[-1]["text"]

    def last_buttons(self) -> list[str]:
        shown = [p for m, p in self.calls if m in ("sendMessage", "editMessageText")]
        markup = shown[-1].get("reply_markup") or {"inline_keyboard": []}
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def msg(user_id, text="", chat_id=None, **extra):
    return {"update_id": 1, "message": {
        "message_id": 5, "from": {"id": user_id, "is_bot": False},
        "chat": {"id": chat_id or user_id, "type": "private"}, "text": text, **extra}}


def cb(user_id, data, chat_id=None, message_id=77):
    return {"update_id": 2, "callback_query": {
        "id": "cbid", "from": {"id": user_id, "is_bot": False}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id or user_id, "type": "private"}}}}


class PanelBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.e = await Env().setup()
        self.api = FakeBotApi()
        self.panel = Panel(self.e.services, self.e.engine, self.e.commands, self.api, poll_timeout=0)
        self.e.engine.panel_push = self.panel.push
        self.me = ME_ID  # صاحب حساب = مدیر پنل

    async def asyncTearDown(self):
        await self.e.close()

    async def press(self, data):
        await self.panel.handle_update(cb(self.me, data))

    async def say(self, text, **extra):
        await self.panel.handle_update(msg(self.me, text, **extra))


class AuthAndHomeTests(PanelBase):
    async def test_strangers_are_ignored_completely(self):
        await self.panel.handle_update(msg(424242, "/start"))
        await self.panel.handle_update(cb(424242, "tg"))
        self.assertEqual(self.api.calls, [])
        self.assertTrue(await self.e.db.get_bool(KEY_ENABLED, True))

    async def test_group_chat_ignored_even_for_owner(self):
        u = msg(self.me, "/start")
        u["message"]["chat"]["type"] = "group"
        await self.panel.handle_update(u)
        self.assertEqual(self.api.calls, [])

    async def test_start_shows_home_with_creator_and_remembers_chat(self):
        await self.say("/start")
        text = self.api.last_text()
        self.assertIn(CREATOR_FA, text)
        self.assertIn("tg", self.api.last_buttons())
        self.assertEqual(await self.e.db.get_setting("panel_chat_id"), str(self.me))

    async def test_about_shows_creator(self):
        await self.press("ab")
        self.assertIn(CREATOR_FA, self.api.last_text())

    async def test_toggle_enabled_edits_message_in_place(self):
        await self.press("tg")
        self.assertFalse(await self.e.db.get_bool(KEY_ENABLED, True))
        edit = self.api.of("editMessageText")[-1]
        self.assertEqual(edit["message_id"], 77)
        self.assertIn("خاموش", edit["text"])
        await self.press("tg")
        self.assertTrue(await self.e.db.get_bool(KEY_ENABLED, False))

    async def test_all_callback_data_fit_telegram_limit(self):
        e = self.e
        await e.db.touch_chat(USER_A, "یک نام خیلی خیلی طولانی برای تست محدودیت دکمه‌ها", "ali")
        await e.db.add_to_list(USER_A, "blacklist")
        await e.db.add_quick_reply("کلمه|دیگر", "پاسخ")
        screens = ["h", "sl", "cl", f"c:{USER_A}", "pl", "ac", "ac:l:b", "sc", "qr", "pk", "pk:per", "st",
                   "st:n:cooldown", "ss", "lg", "ab", f"c:{USER_A}:fg"]
        for data in screens:
            self.api.calls.clear()
            await self.press(data)
            for b in self.api.last_buttons():
                self.assertLessEqual(len(b.encode()), 64, f"{data} → {b}")
            self.assertLessEqual(len(self.api.last_text()), 4096)


class ChatManagementTests(PanelBase):
    async def test_chat_list_and_detail(self):
        await self.e.db.touch_chat(USER_A, "سارا", "sara")
        await self.press("cl")
        self.assertIn(f"c:{USER_A}", self.api.last_buttons())
        await self.press(f"c:{USER_A}")
        self.assertIn("سارا", self.api.last_text())

    async def test_chat_actions(self):
        e = self.e
        await e.db.touch_chat(USER_A, "سارا", None)
        await self.press(f"c:{USER_A}:off")
        self.assertIs((await e.db.get_chat_settings(USER_A))["ai_enabled"], False)
        await self.press(f"c:{USER_A}:def")
        self.assertIsNone((await e.db.get_chat_settings(USER_A))["ai_enabled"])
        await self.press(f"c:{USER_A}:p30")
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        await self.press("pl")
        self.assertIn(f"pl:r:{USER_A}", self.api.last_buttons())
        await self.press(f"pl:r:{USER_A}")
        self.assertIsNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        await self.press(f"c:{USER_A}:lb")
        self.assertEqual(await e.db.get_list("blacklist"), [USER_A])
        await self.press(f"c:{USER_A}:lx")
        self.assertEqual(await e.db.get_list("blacklist"), [])

    async def test_resume_all(self):
        for cid in (USER_A, USER_B):
            await self.press(f"c:{cid}:p30")
        await self.press("pl:all")
        self.assertEqual(await self.e.db.list_pauses(), [])

    async def test_forget_needs_confirmation(self):
        e = self.e
        await e.db.add_message(ACCOUNT, USER_A, "user", "راز")
        await self.press(f"c:{USER_A}:fg")
        self.assertEqual(len(await e.db.get_history(ACCOUNT, USER_A, 10)), 1)  # هنوز پاک نشده
        await self.press(f"c:{USER_A}:fgy")
        self.assertEqual(await e.db.get_history(ACCOUNT, USER_A, 10), [])

    async def test_chat_prompt_flow_and_cancel(self):
        e = self.e
        await self.press(f"c:{USER_A}:pr")
        self.assertIn("دستورالعمل", self.api.last_text())
        await self.say("فقط انگلیسی")
        self.assertEqual((await e.db.get_chat_settings(USER_A))["custom_prompt"], "فقط انگلیسی")
        await self.press(f"c:{USER_A}:pr")
        await self.say("/cancel")
        await self.say("متن بی‌ربط")  # دیگر در انتظار نیست
        self.assertEqual((await e.db.get_chat_settings(USER_A))["custom_prompt"], "فقط انگلیسی")


class SettingsFlowTests(PanelBase):
    async def test_access_mode_and_list_add(self):
        await self.press("ac:m:white")
        self.assertEqual(await self.e.db.get_setting("list_mode"), "whitelist")
        await self.press("ac:a:w")
        await self.say(f"{USER_A} {USER_B}")
        self.assertEqual(sorted(await self.e.db.get_list("whitelist")), [USER_A, USER_B])
        await self.press(f"ac:r:w:{USER_A}")
        self.assertEqual(await self.e.db.get_list("whitelist"), [USER_B])

    async def test_schedule_buttons(self):
        await self.press("sc:m:inside")
        self.assertEqual(await self.e.db.get_setting("schedule_mode"), "inside")
        await self.press("sc:d:0")  # روز ۰ را خاموش می‌کند
        days = await self.e.db.get_setting("work_days")
        self.assertNotIn("0", days.split(","))
        await self.press("sc:h")
        await self.say("08:30-17:00")
        self.assertEqual(await self.e.db.get_setting("work_start"), "08:30")
        await self.press("sc:z")
        await self.say("Mars/Base")
        self.assertIn("نامعتبر", self.api.last_text())

    async def test_last_workday_cannot_be_disabled(self):
        await self.e.db.set_setting("work_days", "3")
        await self.press("sc:d:3")
        self.assertEqual(await self.e.db.get_setting("work_days"), "3")
        self.assertIn("دست‌کم", self.api.last_text())

    async def test_quick_replies_add_delete(self):
        await self.press("qr:a")
        await self.say("سلام|درود => سلام! خوش آمدید")
        rows = await self.e.db.get_quick_replies()
        self.assertEqual(len(rows), 1)
        await self.press(f"qr:d:{rows[0]['id']}")
        self.assertEqual(await self.e.db.get_quick_replies(), [])

    async def test_prompt_persona_and_knowledge(self):
        e = self.e
        await self.press("pk:pe")
        await self.say("پرامپت تازه")
        self.assertEqual(await e.db.get_setting("system_prompt"), "پرامپت تازه")
        await self.press("pk:per:sales")
        from personas import PERSONAS

        self.assertEqual(await e.db.get_setting("system_prompt"), PERSONAS["sales"][1])
        await self.press("pk:ke")
        await self.say("ساعت کاری ۹ تا ۵")
        await self.press("pk:ka")
        await self.say("آدرس تهران")
        k = await e.db.get_setting("knowledge")
        self.assertIn("ساعت کاری", k)
        self.assertIn("تهران", k)
        await self.press("pk:kc")  # فقط تأیید می‌خواهد
        self.assertIn("ساعت کاری", await e.db.get_setting("knowledge"))
        await self.press("pk:kc:y")
        self.assertEqual(await e.db.get_setting("knowledge"), "")

    async def test_knowledge_file_upload(self):
        await self.press("pk:ke")
        self.api.files["f1"] = "دانش از فایل".encode()
        await self.say("", document={"file_id": "f1", "file_name": "k.txt", "mime_type": "text/plain"})
        self.assertEqual(await self.e.db.get_setting("knowledge"), "دانش از فایل")
        await self.press("pk:ke")
        await self.say("", document={"file_id": "f2", "file_name": "evil.exe", "mime_type": "application/x"})
        self.assertIn("txt", self.api.last_text())

    async def test_numeric_presets_toggles_and_custom_input(self):
        e = self.e
        await self.press("st:v:cooldown:10")
        self.assertEqual(await e.db.get_float("cooldown_seconds", 0), 10)
        await self.press("st:c:history")
        await self.say("6")
        self.assertEqual(await e.db.get_int("history_size", 0), 6)
        await self.press("st:c:history")
        await self.say("خیلی")
        self.assertIn("عدد", self.api.last_text())
        await self.press("st:t:vision")
        self.assertFalse(await e.db.get_bool("vision_enabled", True))
        await self.press("st:t:vision")
        self.assertTrue(await e.db.get_bool("vision_enabled", False))
        await self.press("st:nt")
        self.assertFalse(await e.db.get_bool("panel_notify", True))

    async def test_pending_expires(self):
        await self.press("pk:pe")
        self.panel.pending[self.me].expires = 0
        await self.say("متنی که نباید ذخیره شود")
        self.assertNotEqual(await self.e.db.get_setting("system_prompt"), "متنی که نباید ذخیره شود")


class ReportsAndToolsTests(PanelBase):
    async def test_stats_logs_csv_and_ai_test(self):
        e = self.e
        await e.send(e.incoming())
        await e.idle()
        await self.press("ss")
        self.assertIn("امروز", self.api.last_text())
        await self.press("lg")
        await self.press("csv")
        self.assertEqual(self.api.docs[0][1], "logs.csv")
        self.assertIn(b"time_utc", self.api.docs[0][2])
        await self.press("ts")
        self.assertIn("fake-model", self.api.last_text())

    async def test_silent_mode_blocks_replies_and_cancels_inflight(self):
        e = self.e
        await self.press("sl:60")
        self.assertGreater(await e.db.get_int(KEY_SILENT_UNTIL, 0), 0)
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), [])
        await self.press("sl:0")
        await e.send(e.incoming(text="دوباره"))
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)

    async def test_silent_during_generation_cancels_send(self):
        e = self.e
        e.ai.gate = asyncio.Event()
        await e.send(e.incoming())
        await asyncio.wait_for(e.ai.started.wait(), 3)
        await self.press("sl:60")
        e.ai.gate.set()
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), [])


class PushTests(PanelBase):
    async def test_handoff_notification_reaches_panel_with_action_buttons(self):
        e = self.e
        await self.say("/start")  # ربات پنل chat را یاد می‌گیرد
        self.api.calls.clear()
        e.ai.replies = ["[[HANDOFF]]"]
        await e.send(e.incoming(text="می‌خواهم با خود شما صحبت کنم"))
        await e.idle()
        self.assertIn(f"c:{USER_A}:rs", self.api.last_buttons())
        self.assertIn("دخالت انسانی", self.api.last_text())
        self.assertTrue(any("دخالت انسانی" in t for t in e.gw.texts_to(ME_ID)))  # Saved Messages هم
        await self.press(f"c:{USER_A}:rs")  # دکمه‌ی اعلان کار می‌کند
        self.assertIsNone(await e.db.get_pause_until(ACCOUNT, USER_A))

    async def test_notifications_can_be_disabled(self):
        e = self.e
        await self.say("/start")
        await self.press("st:nt")
        self.api.calls.clear()
        e.ai.replies = ["[[HANDOFF]]"]
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(self.api.of("sendMessage"), [])

    async def test_panel_failure_never_breaks_assistant(self):
        e = self.e

        async def boom(text, actions):
            raise RuntimeError("panel down")

        e.engine.panel_push = boom
        e.ai.replies = ["[[HANDOFF]]"]
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)


class RunLoopTests(PanelBase):
    async def test_run_processes_updates_then_stops_on_cancel(self):
        self.api.updates.put_nowait([{**msg(self.me, "/start"), "update_id": 10}])
        task = asyncio.create_task(self.panel.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self.api.of("sendMessage"):
                break
        self.assertIn(CREATOR_FA, self.api.last_text())
        # offset بعد از پردازش جلو می‌رود
        await asyncio.sleep(0.05)
        self.assertEqual(self.api.of("getUpdates")[-1].get("offset"), 11)
        self.assertTrue(self.api.of("setMyDescription"))
        self.assertIn(CREATOR_FA, self.api.of("setMyDescription")[0]["description"])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_invalid_token_disables_panel(self):
        self.api.fail_get_me = BotApiError("getMe", "Unauthorized", 401)
        await asyncio.wait_for(self.panel.run(), 3)  # بدون استثنا و بدون حلقه برمی‌گردد

    async def test_401_during_polling_stops(self):
        self.api.updates.put_nowait(BotApiError("getUpdates", "Unauthorized", 401))
        await asyncio.wait_for(self.panel.run(), 3)

    async def test_bad_update_does_not_kill_loop(self):
        self.api.updates.put_nowait([{"update_id": 1, "message": {"from": {"id": self.me}, "chat": "garbage"}},
                                     {**msg(self.me, "/start"), "update_id": 2}])
        task = asyncio.create_task(self.panel.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if self.api.of("sendMessage"):
                break
        self.assertTrue(self.api.of("sendMessage"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class BotApiClientTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "123456:ABCDEF-secret-token-value-0123456789"

    async def test_success_and_json_body(self):
        seen = {}

        def transport(method, body, ctype, timeout):
            seen.update(method=method, body=json.loads(body), ctype=ctype)
            return 200, json.dumps({"ok": True, "result": {"id": 1}}).encode()

        api = BotApi(self.TOKEN, transport)
        self.assertEqual(await api.call("getMe", {"a": "ب"}), {"id": 1})
        self.assertEqual((seen["method"], seen["body"], seen["ctype"]), ("getMe", {"a": "ب"}, "application/json"))

    async def test_api_error_includes_retry_after(self):
        def transport(*a):
            return 429, json.dumps({"ok": False, "error_code": 429, "description": "Too Many Requests",
                                    "parameters": {"retry_after": 7}}).encode()

        with self.assertRaises(BotApiError) as cm:
            await BotApi(self.TOKEN, transport).call("sendMessage")
        self.assertEqual((cm.exception.code, cm.exception.retry_after), (429, 7))

    async def test_token_never_leaks_in_errors(self):
        def transport(*a):
            raise OSError(f"cannot reach https://api.telegram.org/bot{self.TOKEN}/getMe")

        with self.assertRaises(BotApiError) as cm:
            await BotApi(self.TOKEN, transport).call("getMe")
        self.assertNotIn("secret-token", str(cm.exception))
        self.assertNotIn("secret-token", repr(cm.exception.__cause__))

    async def test_invalid_json_response(self):
        with self.assertRaises(BotApiError):
            await BotApi(self.TOKEN, lambda *a: (502, b"<html>bad gateway</html>")).call("getMe")

    def test_multipart_contains_file_and_safe_filename(self):
        body, ctype = _multipart({"chat_id": "5"}, "document", 'a"b\r\n.csv', b"DATA")
        self.assertIn(b"DATA", body)
        self.assertIn("boundary=", ctype)
        self.assertNotIn(b'a"b', body)


if __name__ == "__main__":
    unittest.main()
