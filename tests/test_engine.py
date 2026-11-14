"""تست‌های موتور دستیار (همه با Mock؛ بدون تلگرام و AI واقعی)."""

from __future__ import annotations

import asyncio
import time
import unittest

from tests.support import ME_ID, USER_A, USER_B, Env  # noqa: I001  (باید اول import شود)

from ai_handler import AIError
from database import ACCOUNT, KEY_ENABLED, KEY_LIST_MODE, LIST_BLACK, LIST_WHITE
from tglayer.models import (
    CHAT_GROUP,
    MEDIA_PHOTO,
    MEDIA_VOICE,
    MediaRef,
    RateLimited,
    SendForbidden,
)


class Base(unittest.IsolatedAsyncioTestCase):
    env_kwargs: dict = {}

    async def asyncSetUp(self) -> None:
        self.e = await Env(**self.env_kwargs).setup()

    async def asyncTearDown(self) -> None:
        await self.e.close()


class BasicFlowTests(Base):
    async def test_reply_is_sent_history_and_log_saved(self):
        self.e.ai.replies = ["سلام! چطور کمک کنم؟"]
        await self.e.send(self.e.incoming(text="سلام"))
        await self.e.idle()
        self.assertEqual(self.e.gw.texts_to(USER_A), ["سلام! چطور کمک کنم؟"])
        hist = await self.e.db.get_history(ACCOUNT, USER_A, 10)
        self.assertEqual([h["role"] for h in hist], ["user", "assistant"])
        logs = await self.e.db.get_recent_logs(5)
        self.assertEqual(logs[0]["status"], "ok")
        self.assertIn(USER_A, self.e.gw.typing_calls)

    async def test_own_messages_are_not_mistaken_for_owner_intervention(self):
        """FakeGateway رویداد خروجی را پیش از بازگشت send_text می‌فرستد (بدترین Race)."""
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertEqual(len(self.e.gw.texts_to(USER_A)), 1)
        self.assertIsNone(await self.e.db.get_pause_until(ACCOUNT, USER_A))
        # پیام دوم هم باید جواب بگیرد
        await self.e.send(self.e.incoming(text="دوم"))
        await self.e.idle()
        self.assertEqual(len(self.e.gw.texts_to(USER_A)), 2)

    async def test_non_private_bots_service_and_self_ignored(self):
        e = self.e
        await e.send(e.incoming(chat_type=CHAT_GROUP))
        await e.send(e.incoming(chat_id=2001, is_bot=True))
        await e.send(e.incoming(chat_id=2002, is_service=True))
        await e.send(e.incoming(chat_id=2003, is_self=True))
        await e.idle()
        self.assertEqual(e.gw.sent, [])
        self.assertEqual(e.ai.calls, [])

    async def test_duplicate_event_processed_once(self):
        m = self.e.incoming()
        await self.e.send(m)
        await self.e.send(m)
        await self.e.idle()
        self.assertEqual(len(self.e.ai.calls), 1)

    async def test_burst_messages_are_batched_into_one_reply(self):
        for t in ("یک", "دو", "سه"):
            await self.e.send(self.e.incoming(text=t))
            await asyncio.sleep(0.01)
        await self.e.idle()
        self.assertEqual(len(self.e.ai.calls), 1)
        texts = [p[1] for p in self.e.ai.calls[0]["parts"]]
        self.assertEqual(texts, ["یک", "دو", "سه"])
        self.assertEqual(len(self.e.gw.texts_to(USER_A)), 1)

    async def test_long_reply_is_split(self):
        self.e.ai.replies = ["الف " * 3000]
        await self.e.send(self.e.incoming())
        await self.e.idle()
        parts = self.e.gw.texts_to(USER_A)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 4096 for p in parts))

    async def test_ai_error_status_logged_and_no_reply(self):
        self.e.ai.replies = [AIError("boom")]
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertEqual(self.e.gw.sent, [])
        self.assertTrue((await self.e.db.get_recent_logs(1))[0]["status"].startswith("error"))

    async def test_fallback_message_on_ai_error(self):
        await self.e.close()
        self.e = await Env(fallback_message="فعلاً در دسترس نیستم").setup()
        self.e.ai.replies = [AIError("boom")]
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertEqual(self.e.gw.texts_to(USER_A), ["فعلاً در دسترس نیستم"])

    async def test_per_chat_prompt_reaches_model(self):
        await self.e.db.set_chat_prompt(USER_A, "فقط انگلیسی جواب بده")
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertIn("فقط انگلیسی جواب بده", self.e.ai.calls[0]["system"])


class OwnerInterventionTests(Base):
    async def test_manual_message_pauses_chat(self):
        await self.e.owner_says(self.e.owner())
        self.assertIsNotNone(await self.e.db.get_pause_until(ACCOUNT, USER_A))
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertEqual(self.e.ai.calls, [])
        self.assertEqual(self.e.gw.sent, [])
        # پیام کاربر برای حفظ زمینه در تاریخچه ثبت می‌شود
        hist = await self.e.db.get_history(ACCOUNT, USER_A, 10)
        self.assertEqual([h["role"] for h in hist], ["assistant", "user"])  # owner → assistant

    async def test_other_chats_unaffected_by_owner_in_one_chat(self):
        await self.e.owner_says(self.e.owner(chat_id=USER_A))
        await self.e.send(self.e.incoming(chat_id=USER_B))
        await self.e.idle()
        self.assertEqual(len(self.e.gw.texts_to(USER_B)), 1)

    async def test_owner_cancels_reply_while_ai_is_generating(self):
        e = self.e
        e.ai.gate = asyncio.Event()
        await e.send(e.incoming(text="سؤال"))
        await asyncio.wait_for(e.ai.started.wait(), 3)
        await e.owner_says(e.owner(text="خودم جواب می‌دهم"))
        await asyncio.sleep(0.05)
        e.ai.gate.set()
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), [])
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        hist = await e.db.get_history(ACCOUNT, USER_A, 10)
        self.assertIn("user", [h["role"] for h in hist])  # سؤال کاربر گم نشد

    async def test_owner_cancels_while_debouncing(self):
        e = self.e
        await e.send(e.incoming(text="سلام"))
        await asyncio.sleep(0.01)  # هنوز در Debounce
        await e.owner_says(e.owner())
        await e.idle()
        self.assertEqual(e.ai.calls, [])
        self.assertEqual(e.gw.sent, [])

    async def test_owner_message_during_in_flight_send_is_not_lost(self):
        """پیام دستی هم‌زمان با ارسال در حال انجام: باید به‌عنوان دخالت شناخته شود، نه پیام خود دستیار."""
        e = self.e
        e.gw.send_delay = 0.2
        await e.send(e.incoming())
        await asyncio.sleep(0.15)  # ارسال شروع شده
        await e.owner_says(e.owner(text="دستی"))
        await e.idle()
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)  # فقط همان پاسخ در حال ارسال

    async def test_pause_expires_automatically(self):
        e = self.e
        await e.db.set_pause(ACCOUNT, USER_A, int(time.time()) - 5, "owner")
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)

    async def test_manual_resume_command(self):
        e = self.e
        await e.owner_says(e.owner())
        out = await e.command(f"/ai resume {USER_A}")
        self.assertIn("فعال", out[0])
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)

    async def test_pause_disabled_when_minutes_zero_still_cancels_inflight(self):
        e = self.e
        await e.db.set_setting("pause_minutes", "0")
        e.ai.gate = asyncio.Event()
        await e.send(e.incoming())
        await asyncio.wait_for(e.ai.started.wait(), 3)
        await e.owner_says(e.owner())
        e.ai.gate.set()
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), [])
        self.assertIsNone(await e.db.get_pause_until(ACCOUNT, USER_A))

    async def test_many_concurrent_owner_and_incoming_events_stay_consistent(self):
        e = self.e
        for i in range(10):
            await e.send(e.incoming(text=f"m{i}"))
            if i == 4:
                await e.owner_says(e.owner())
        await e.idle()
        # بعد از دخالت، هیچ پاسخی نباید ارسال شود و هیچ تسکی معلق نماند
        self.assertEqual(e.engine._tasks, set())
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))


class ConcurrencyTests(Base):
    async def test_chats_are_isolated(self):
        e = self.e
        e.ai.replies = [lambda c: f"پاسخ به {c['parts'][0][1]}"] * 2
        await asyncio.gather(
            e.send(e.incoming(chat_id=USER_A, text="الف")),
            e.send(e.incoming(chat_id=USER_B, text="ب")),
        )
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), ["پاسخ به الف"])
        self.assertEqual(e.gw.texts_to(USER_B), ["پاسخ به ب"])
        ha = await e.db.get_history(ACCOUNT, USER_A, 10)
        hb = await e.db.get_history(ACCOUNT, USER_B, 10)
        self.assertTrue(all("ب" not in h["content"].replace("پاسخ به", "") for h in ha))
        self.assertTrue(all("الف" not in h["content"] for h in hb))

    async def test_slow_chat_does_not_block_other_chat(self):
        e = self.e
        started = time.monotonic()
        e.ai.delay = 0.0
        await e.send(e.incoming(chat_id=USER_A))
        await e.send(e.incoming(chat_id=USER_B))
        await e.idle()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(e.gw.sent), 2)


class GateTests(Base):
    async def test_global_off(self):
        await self.e.db.set_setting(KEY_ENABLED, "0")
        await self.e.send(self.e.incoming())
        await self.e.idle()
        self.assertEqual(self.e.gw.sent, [])

    async def test_global_off_mid_generation_cancels_send(self):
        e = self.e
        e.ai.gate = asyncio.Event()
        await e.send(e.incoming())
        await asyncio.wait_for(e.ai.started.wait(), 3)
        await e.db.set_setting(KEY_ENABLED, "0")
        e.ai.gate.set()
        await e.idle()
        self.assertEqual(e.gw.texts_to(USER_A), [])

    async def test_per_chat_disable(self):
        await self.e.db.set_chat_ai_enabled(USER_A, False)
        await self.e.send(self.e.incoming(chat_id=USER_A))
        await self.e.send(self.e.incoming(chat_id=USER_B))
        await self.e.idle()
        self.assertEqual(self.e.gw.texts_to(USER_A), [])
        self.assertEqual(len(self.e.gw.texts_to(USER_B)), 1)

    async def test_blacklist_and_whitelist(self):
        e = self.e
        await e.db.set_setting(KEY_LIST_MODE, LIST_BLACK)
        await e.db.add_to_list(USER_A, LIST_BLACK)
        await e.send(e.incoming(chat_id=USER_A))
        await e.send(e.incoming(chat_id=USER_B))
        await e.idle()
        self.assertEqual((e.gw.texts_to(USER_A), len(e.gw.texts_to(USER_B))), ([], 1))
        e.gw.sent.clear()
        await e.db.set_setting(KEY_LIST_MODE, LIST_WHITE)
        await e.db.add_to_list(USER_A, LIST_WHITE)
        await e.send(e.incoming(chat_id=USER_A))
        await e.send(e.incoming(chat_id=USER_B))
        await e.idle()
        self.assertEqual((len(e.gw.texts_to(USER_A)), e.gw.texts_to(USER_B)), (1, []))

    async def test_schedule_outside_hours_blocks(self):
        e = self.e
        from datetime import datetime, timedelta, timezone
        far = datetime.now(timezone.utc) + timedelta(hours=12)
        await e.db.set_setting("schedule_mode", "inside")
        await e.db.set_setting("timezone", "UTC")
        await e.db.set_setting("work_days", "all")
        await e.db.set_setting("work_start", f"{far:%H}:00")
        await e.db.set_setting("work_end", f"{far:%H}:30")
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(e.gw.sent, [])

    async def test_quick_reply_skips_ai(self):
        await self.e.db.add_quick_reply("قیمت|price", "قیمت‌ها در سایت است")
        await self.e.send(self.e.incoming(text="لطفاً قیمت را بگویید"))
        await self.e.idle()
        self.assertEqual(self.e.ai.calls, [])
        self.assertEqual(self.e.gw.texts_to(USER_A), ["قیمت‌ها در سایت است"])
        self.assertEqual((await self.e.db.get_recent_logs(1))[0]["status"], "quick")


class RateAndStreakTests(Base):
    env_kwargs = {"default_rate_limit": 2}

    async def test_rate_limit_per_chat(self):
        e = self.e
        for i in range(4):
            await e.send(e.incoming(text=f"x{i}"))
            await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 2)
        # گفتگوی دیگر محدودیت جدا دارد
        await e.send(e.incoming(chat_id=USER_B))
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_B)), 1)


class StreakTests(Base):
    env_kwargs = {"default_max_streak": 2}

    async def test_streak_limit_pauses_and_notifies_owner(self):
        e = self.e
        for i in range(3):
            await e.send(e.incoming(text=f"x{i}"))
            await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 2)
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        self.assertTrue(any("سقف" in t for t in e.gw.texts_to(ME_ID)))

    async def test_owner_message_resets_streak(self):
        e = self.e
        for i in range(2):
            await e.send(e.incoming(text=f"x{i}"))
            await e.idle()
        await e.owner_says(e.owner())
        await e.command(f"/ai resume {USER_A}")
        self.assertEqual((await e.db.get_chat_settings(USER_A))["ai_streak"], 0)


class HandoffTests(Base):
    async def test_handoff_pauses_and_notifies(self):
        e = self.e
        e.ai.replies = ["[[HANDOFF]]"]
        await e.send(e.incoming(text="می‌خواهم با خود شما صحبت کنم"))
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)  # پیام تحویل به انسان
        self.assertIsNotNone(await e.db.get_pause_until(ACCOUNT, USER_A))
        self.assertTrue(any("دخالت انسانی" in t for t in e.gw.texts_to(ME_ID)))
        self.assertEqual((await e.db.get_recent_logs(1))[0]["status"], "handoff")


class FloodAndErrorTests(Base):
    async def test_short_floodwait_retries_once_and_succeeds(self):
        e = self.e
        e.gw.send_errors = [RateLimited(0)]
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)

    async def test_long_floodwait_gives_up_without_retry_loop(self):
        e = self.e
        e.gw.send_errors = [RateLimited(500)]
        await e.send(e.incoming(text="اول"))
        await e.idle()
        self.assertEqual(e.gw.sent, [])
        self.assertTrue((await e.db.get_recent_logs(1))[0]["status"].startswith("send_error"))
        # بلوک سراسری: پیام بعدی بلافاصله و بدون فشار به تلگرام رد می‌شود
        await e.send(e.incoming(text="دوم"))
        await e.idle()
        self.assertEqual(e.gw.sent, [])
        self.assertEqual(e.gw.send_errors, [])  # هیچ تلاش اضافه‌ای نشد

    async def test_send_forbidden_not_retried(self):
        e = self.e
        e.gw.send_errors = [SendForbidden("blocked"), SendForbidden("blocked")]
        await e.send(e.incoming())
        await e.idle()
        self.assertEqual(len(e.gw.send_errors), 1)  # فقط یک تلاش
        self.assertIn("forbidden", (await e.db.get_recent_logs(1))[0]["status"])


class MediaTests(Base):
    async def test_photo_becomes_image_part_and_temp_is_cleaned(self):
        e = self.e
        media = MediaRef(kind=MEDIA_PHOTO, size=1000, file_name="p.jpg")
        await e.send(e.incoming(text="این چیست؟", media=media))
        await e.idle()
        kinds = [k for k, _ in e.ai.calls[0]["parts"]]
        self.assertIn("image", kinds)
        self.assertTrue(e.gw.downloads)
        self.assertFalse(any(p.exists() for p in e.gw.downloads))
        self.assertFalse(any(p.parent.exists() for p in e.gw.downloads))

    async def test_voice_is_transcribed(self):
        e = self.e
        media = MediaRef(kind=MEDIA_VOICE, size=1000, duration=5, file_name="v.ogg")
        await e.send(e.incoming(text="", media=media))
        await e.idle()
        self.assertEqual(e.ai.transcribe_calls, 1)
        self.assertIn("متن ویس", e.ai.calls[0]["parts"][0][1])

    async def test_oversized_media_not_downloaded(self):
        e = self.e
        media = MediaRef(kind=MEDIA_PHOTO, size=50 * 1024 * 1024)
        await e.send(e.incoming(text="x", media=media))
        await e.idle()
        self.assertEqual(e.gw.downloads, [])
        self.assertIn("حجم", e.ai.calls[0]["parts"][0][1])

    async def test_too_long_voice_skipped(self):
        e = self.e
        media = MediaRef(kind=MEDIA_VOICE, size=10, duration=99999)
        await e.send(e.incoming(text="", media=media))
        await e.idle()
        self.assertEqual(e.ai.transcribe_calls, 0)

    async def test_download_failure_is_handled(self):
        e = self.e
        e.gw.download_error = RuntimeError("net down")
        media = MediaRef(kind=MEDIA_PHOTO, size=10)
        await e.send(e.incoming(text="x", media=media))
        await e.idle()
        self.assertEqual(len(e.gw.texts_to(USER_A)), 1)  # پاسخ متنی همچنان ارسال می‌شود
        self.assertIn("بارگیری", e.ai.calls[0]["parts"][0][1])

    async def test_transcription_failure_is_handled(self):
        e = self.e
        e.ai.transcribe_error = AIError("stt down")
        media = MediaRef(kind=MEDIA_VOICE, size=10, duration=3)
        await e.send(e.incoming(text="", media=media))
        await e.idle()
        self.assertIn("قابل تبدیل", e.ai.calls[0]["parts"][0][1])

    async def test_unsupported_media_is_labeled(self):
        e = self.e
        media = MediaRef(kind="video", size=10)
        await e.send(e.incoming(text="", media=media))
        await e.idle()
        self.assertIn("ویدیو", e.ai.calls[0]["parts"][0][1])


if __name__ == "__main__":
    unittest.main()
