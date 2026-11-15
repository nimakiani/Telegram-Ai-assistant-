"""
panel.py — پنل کنترل با ربات تلگرام و «دکمه‌های شیشه‌ای» (اختیاری).

* فقط وقتی فعال است که PANEL_BOT_TOKEN تنظیم شده باشد. دستیار همچنان با حساب شخصی شما کار می‌کند؛
  این ربات فقط «کنترل‌پنل» است.
* فقط خودِ صاحب حساب (شناسه‌ی کاربری همین حساب) اجازه‌ی کار با ربات را دارد؛ بقیه نادیده گرفته می‌شوند.
* این ماژول یک لایه‌ی نازک روی `CommandHandler` است؛ هر دکمه همان دستور `/ai …` را اجرا می‌کند
  تا رفتار پنل دکمه‌ای و دستورهای متنی همیشه یکی باشد.
* ورودی‌های متنی (مثل پرامپت جدید) با حالت «در انتظار پیام» گرفته می‌شود؛ /cancel لغو می‌کند.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from about import APP_NAME, CREATOR_FA, CREATOR_LINE, VERSION
from botapi import BotApi, BotApiError
from commands import CommandHandler
from database import (
    ACCOUNT,
    KEY_COOLDOWN,
    KEY_DEBOUNCE,
    KEY_ENABLED,
    KEY_HANDOFF,
    KEY_HISTORY_SIZE,
    KEY_LIST_MODE,
    KEY_MARK_READ,
    KEY_MAX_MEDIA_MB,
    KEY_MAX_STREAK,
    KEY_PANEL_CHAT,
    KEY_PANEL_NOTIFY,
    KEY_PAUSE_MINUTES,
    KEY_RATE_LIMIT,
    KEY_SILENT_UNTIL,
    KEY_SYSTEM_PROMPT,
    KEY_VISION,
    KEY_VOICE,
    KEY_WORK_DAYS,
    KEY_KNOWLEDGE,
    KEY_TIMEZONE,
    LIST_BLACK,
    LIST_WHITE,
    MODE_OFF,
)
from engine import Engine
from personas import PERSONAS
from schedule import _DAY_LABELS_FA, _WEEK_ORDER_FA, days_from_str, days_to_str, format_days, local_day_start_ts
from services import Services, clip, fmt_ts, load_schedule, schedule_state

logger = logging.getLogger(__name__)

Button = tuple[str, str]
Rows = list[list[Button]]

PENDING_TTL = 300  # ثانیه
MAX_TEXT = 4000

# مقدارهای آماده برای تنظیمات عددی: نام ← (کلید دیتابیس، توضیح، پیشنهادها)
SETTINGS: dict[str, tuple[str, str, list[str]]] = {
    "cooldown": (KEY_COOLDOWN, "فاصله‌ی حداقلی بین پاسخ‌ها (ثانیه)", ["0", "3", "10", "30"]),
    "debounce": (KEY_DEBOUNCE, "انتظار برای جمع‌کردن پیام‌های پشت‌سرهم (ثانیه)", ["1", "2.5", "5", "10"]),
    "history": (KEY_HISTORY_SIZE, "تعداد پیام‌های تاریخچه برای مدل", ["4", "8", "12", "20"]),
    "pause": (KEY_PAUSE_MINUTES, "توقف بعد از دخالت شما (دقیقه؛ ۰ = بدون توقف)", ["15", "60", "180", "720"]),
    "rate": (KEY_RATE_LIMIT, "حداکثر پاسخ در ساعت برای هر گفتگو (۰ = نامحدود)", ["0", "10", "30", "100"]),
    "streak": (KEY_MAX_STREAK, "حداکثر پاسخ متوالی بدون دخالت شما (۰ = نامحدود)", ["0", "10", "20", "50"]),
    "media": (KEY_MAX_MEDIA_MB, "سقف حجم رسانه (مگابایت)", ["5", "10", "20", "50"]),
}
TOGGLES: dict[str, tuple[str, str]] = {
    "vision": (KEY_VISION, "🖼 تحلیل تصویر"),
    "voice": (KEY_VOICE, "🎙 تبدیل ویس به متن"),
    "handoff": (KEY_HANDOFF, "🙋 تحویل به انسان"),
    "markread": (KEY_MARK_READ, "👁 علامت «خوانده‌شده»"),
}


@dataclass
class Pending:
    kind: str
    arg: str
    expires: float


def kb(rows: Rows) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows if row
        ]
    }


def _flag(value: bool) -> str:
    return "🟢" if value else "🔴"


class Panel:
    def __init__(
        self,
        services: Services,
        engine: Engine,
        commands: CommandHandler,
        api: BotApi,
        poll_timeout: int = 8,
    ) -> None:
        self.s = services
        self.engine = engine
        self.cmd = commands
        self.api = api
        self.poll_timeout = poll_timeout
        self.pending: dict[int, Pending] = {}
        self.chat_id: int | None = None
        self.username: str | None = None
        self._notice_sent_to: set[int] = set()

    @property
    def owner_id(self) -> int:
        assert self.s.me is not None
        return self.s.me.user_id

    # ==================================================================
    # چرخه‌ی اجرا
    # ==================================================================
    async def run(self) -> None:
        try:
            me = await self.api.call("getMe")
            self.username = me.get("username")
            await self._quiet(self.api.call("deleteWebhook", {"drop_pending_updates": False}))
            await self._quiet(self.api.call("setMyCommands", {"commands": [
                {"command": "panel", "description": "پنل مدیریت دستیار"},
                {"command": "cancel", "description": "لغو ورودی در حال انتظار"},
            ]}))
            await self._quiet(self.api.call("setMyDescription", {
                "description": f"{APP_NAME} — پنل کنترل خصوصی\nسازنده: {CREATOR_FA}"}))
            await self._quiet(self.api.call("setMyShortDescription", {
                "short_description": f"{APP_NAME} | سازنده: {CREATOR_FA}"}))
        except BotApiError as exc:
            logger.error("پنل ربات فعال نشد: %s (توکن PANEL_BOT_TOKEN را بررسی کنید)", exc)
            return
        saved = await self.s.db.get_setting(KEY_PANEL_CHAT, "")
        if saved and saved.lstrip("-").isdigit():
            self.chat_id = int(saved)
        logger.info("پنل ربات فعال شد: @%s — در تلگرام آن را باز کنید و /start بزنید.", self.username)

        offset: int | None = None
        backoff = 1.0
        while True:
            params: dict[str, Any] = {"timeout": self.poll_timeout, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            try:
                updates = await self.api.call("getUpdates", params, timeout=self.poll_timeout + 10)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except BotApiError as exc:
                if exc.code == 401:
                    logger.error("توکن پنل ربات نامعتبر شد؛ پنل متوقف شد.")
                    return
                wait = 30.0 if exc.code == 409 else max(backoff, float(exc.retry_after))
                if exc.code == 409:
                    logger.warning("نمونه‌ی دیگری با همین توکن در حال دریافت پیام است (۴۰۹).")
                else:
                    logger.warning("خطای دریافت از ربات پنل: %s", exc)
                await asyncio.sleep(min(wait, 60.0))
                backoff = min(backoff * 2, 60.0)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Panel update failed")

    @staticmethod
    async def _quiet(coro: Any) -> None:
        try:
            await coro
        except BotApiError as exc:
            logger.debug("Optional bot call failed: %s", exc)

    # ==================================================================
    # ارسال و ویرایش
    # ==================================================================
    async def _send(self, chat_id: int, text: str, rows: Rows | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": clip_text(text), "disable_web_page_preview": True}
        if rows:
            params["reply_markup"] = kb(rows)
        await self.api.call("sendMessage", params)

    async def _show(self, chat_id: int, message_id: int | None, text: str, rows: Rows) -> None:
        if message_id is None:
            await self._send(chat_id, text, rows)
            return
        try:
            await self.api.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": clip_text(text),
                "reply_markup": kb(rows), "disable_web_page_preview": True,
            })
        except BotApiError as exc:
            if "not modified" in exc.description.lower():
                return
            await self._send(chat_id, text, rows)

    async def push(self, text: str, actions: list[Button] | None = None) -> None:
        """اعلان از موتور (تحویل به انسان، سقف پاسخ …) به ربات پنل، همراه دکمه‌های اقدام."""
        if self.chat_id is None or not await self.s.db.get_bool(KEY_PANEL_NOTIFY, True):
            return
        rows: Rows = [[b] for b in (actions or [])] + [[("🏠 پنل", "h")]]
        try:
            await self._send(self.chat_id, text, rows)
        except BotApiError as exc:
            logger.debug("Panel push failed: %s", exc)

    # ==================================================================
    # دریافت
    # ==================================================================
    def _authorized(self, user: dict[str, Any] | None) -> bool:
        return bool(user) and not user.get("is_bot") and int(user["id"]) == self.owner_id  # type: ignore[index]

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])

    async def _remember_chat(self, chat_id: int) -> None:
        if self.chat_id != chat_id:
            self.chat_id = chat_id
            await self.s.db.set_setting(KEY_PANEL_CHAT, str(chat_id))

    async def _on_message(self, msg: dict[str, Any]) -> None:
        user = msg.get("from")
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or not self._authorized(user):
            logger.debug("Ignoring panel message from unauthorized user")
            return
        chat_id = int(chat["id"])
        await self._remember_chat(chat_id)
        text = (msg.get("text") or "").strip()
        pending = self.pending.get(chat_id)
        if pending and pending.expires < time.time():
            self.pending.pop(chat_id, None)
            pending = None

        if text.startswith("/cancel"):
            self.pending.pop(chat_id, None)
            await self._send(chat_id, "✅ لغو شد.", [[("🏠 پنل", "h")]])
            return
        if text.startswith(("/start", "/panel", "/menu")):
            self.pending.pop(chat_id, None)
            t, rows = await self.screen_home()
            await self._send(chat_id, t, rows)
            return

        doc = msg.get("document")
        if doc and pending and pending.kind in ("kn_set", "kn_add"):
            await self._apply_knowledge_file(chat_id, pending, doc)
            return
        if pending and text:
            self.pending.pop(chat_id, None)
            t, rows = await self._apply_pending(pending, text)
            await self._send(chat_id, t, rows)
            return
        await self._send(chat_id, "از دکمه‌های پنل استفاده کنید 👇", [[("🏠 پنل", "h")]])

    async def _on_callback(self, cq: dict[str, Any]) -> None:
        if not self._authorized(cq.get("from")):
            return
        await self._quiet(self.api.call("answerCallbackQuery", {"callback_query_id": cq["id"]}))
        message = cq.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or cq["from"]["id"])
        message_id = message.get("message_id")
        await self._remember_chat(chat_id)
        data = str(cq.get("data") or "h")
        try:
            result = await self.route(data, chat_id)
        except BotApiError as exc:
            logger.warning("Panel route %r failed: %s", data, exc)
            return
        if result is not None:
            text, rows = result
            await self._show(chat_id, message_id, text, rows)

    # ==================================================================
    # مسیریابی دکمه‌ها
    # ==================================================================
    async def route(self, data: str, chat_id: int) -> tuple[str, Rows] | None:
        p = data.split(":")
        head = p[0]
        ex = self.cmd.execute

        if head == "h":
            self.pending.pop(chat_id, None)
            return await self.screen_home()
        if head == "tg":
            on = await self.s.db.get_bool(KEY_ENABLED, True)
            await ex("off" if on else "on")
            return await self.screen_home()
        if head == "ab":
            return self.screen_about()
        if head == "cx":
            self.pending.pop(chat_id, None)
            return await self.screen_home()

        # --- سکوت موقت
        if head == "sl":
            if len(p) > 1:
                await ex(f"silent {p[1]}")
            return await self.screen_silent()

        # --- گفتگوها
        if head == "cl":
            return await self.screen_chats()
        if head == "c" and len(p) >= 2 and p[1].lstrip("-").isdigit():
            return await self._chat_action(chat_id, int(p[1]), p[2] if len(p) > 2 else "")
        if head == "pl":
            if len(p) == 3 and p[1] == "r":
                await ex(f"resume {p[2]}")
            if len(p) == 2 and p[1] == "all":
                await ex("resume all")
            return await self.screen_paused()

        # --- دسترسی
        if head == "ac":
            return await self._access_action(chat_id, p)
        # --- ساعت کاری
        if head == "sc":
            return await self._schedule_action(chat_id, p)
        # --- پاسخ سریع
        if head == "qr":
            if len(p) == 3 and p[1] == "d":
                await ex(f"quick del {p[2]}")
            if len(p) == 2 and p[1] == "a":
                return self._ask(chat_id, "qr_add", "", "پاسخ سریع را بنویسید:\nکلمه۱|کلمه۲ => متن پاسخ\nمثال: سلام|درود => سلام! چطور کمک کنم؟")
            return await self.screen_quick()
        # --- پرامپت و دانش
        if head == "pk":
            return await self._prompt_action(chat_id, p)
        # --- تنظیمات
        if head == "st":
            return await self._settings_action(chat_id, p)
        # --- گزارش‌ها
        if head == "ss":
            return await self.screen_stats()
        if head == "lg":
            return await self.screen_logs()
        if head == "csv":
            data_bytes, count = await self.cmd.csv_bytes()
            await self.api.send_document(chat_id, data_bytes, "logs.csv", f"{count} ردیف لاگ")
            return None
        if head == "ts":
            await self._quiet(self._send(chat_id, "⏳ در حال آزمایش هوش مصنوعی…"))
            result = await ex("test")
            return (result or "—", [[("🔁 دوباره", "ts"), ("🏠 پنل", "h")]])
        return await self.screen_home()

    def _ask(self, chat_id: int, kind: str, arg: str, prompt: str) -> tuple[str, Rows]:
        self.pending[chat_id] = Pending(kind, arg, time.time() + PENDING_TTL)
        return (f"✍️ {prompt}\n\n(برای لغو: /cancel — اعتبار ۵ دقیقه)", [[("❌ لغو", "cx")]])

    # ==================================================================
    # ورودی‌های متنی
    # ==================================================================
    async def _apply_pending(self, pending: Pending, text: str) -> tuple[str, Rows]:
        ex = self.cmd.execute
        kind, arg = pending.kind, pending.arg
        notice = ""
        back: tuple[str, Rows]
        if kind == "prompt_chat":
            notice = await ex(f"chat {arg} prompt {text}") or ""
            back = await self.screen_chat(int(arg), notice)
        elif kind == "prompt_global":
            notice = await ex(f"prompt set {text}") or ""
            back = await self.screen_prompt(notice)
        elif kind in ("kn_set", "kn_add"):
            notice = await ex(f"knowledge {kind[3:]} {text}") or ""
            back = await self.screen_prompt(notice)
        elif kind in ("list_b", "list_w"):
            notice = await ex(f"{'black' if kind == 'list_b' else 'white'} add {text}") or ""
            back = await self.screen_access(notice)
        elif kind == "qr_add":
            notice = await ex(f"quick add {text}") or ""
            back = await self.screen_quick(notice)
        elif kind == "hours":
            notice = await ex(f"hours {text}") or ""
            back = await self.screen_schedule(notice)
        elif kind == "tz":
            notice = await ex(f"tz {text}") or ""
            back = await self.screen_schedule(notice)
        elif kind == "num":
            notice = await ex(f"set {arg} {text}") or ""
            back = await self.screen_settings(notice)
        elif kind == "handoffmsg":
            notice = await ex(f"handoffmsg {text}") or ""
            back = await self.screen_settings(notice)
        else:
            back = await self.screen_home()
        return back

    async def _apply_knowledge_file(self, chat_id: int, pending: Pending, doc: dict[str, Any]) -> None:
        self.pending.pop(chat_id, None)
        name = (doc.get("file_name") or "").lower()
        if not (name.endswith((".txt", ".md")) or str(doc.get("mime_type", "")).startswith("text/")):
            await self._send(chat_id, "فقط فایل متنی (txt یا md) پذیرفته می‌شود.", [[("🧠 پرامپت و دانش", "pk")]])
            return
        limit = self.s.config.knowledge_max_chars * 4 + 1024
        try:
            raw = await self.api.download_file(doc["file_id"], limit)
        except BotApiError as exc:
            await self._send(chat_id, f"❌ دریافت فایل ناموفق بود: {clip(exc.description, 100)}", [[("🧠 پرامپت و دانش", "pk")]])
            return
        notice = await self.cmd._apply_knowledge(pending.kind[3:], raw.decode("utf-8", errors="replace"))
        t, rows = await self.screen_prompt(notice)
        await self._send(chat_id, t, rows)

    # ==================================================================
    # صفحه‌ها
    # ==================================================================
    async def screen_home(self) -> tuple[str, Rows]:
        s, cfg = self.s, self.s.config
        enabled = await s.db.get_bool(KEY_ENABLED, True)
        mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF)
        mode_fa = {MODE_OFF: "همه‌ی گفتگوهای خصوصی", LIST_BLACK: "همه به‌جز لیست سیاه", LIST_WHITE: "فقط لیست سفید"}.get(mode or "", mode)
        sched_mode, working, allowed = await schedule_state(s)
        pauses = await s.db.list_pauses()
        today = await s.db.get_stats(local_day_start_ts(await s.db.get_setting(KEY_TIMEZONE, cfg.timezone) or cfg.timezone))
        silent_until = await s.db.get_int(KEY_SILENT_UNTIL, 0)
        silent = f"\n🌙 سکوت موقت: {max(1, int((silent_until - time.time()) / 60))} دقیقه‌ی دیگر" if silent_until > time.time() else ""
        text = (
            f"🤖 {APP_NAME}\n\n"
            f"وضعیت: {'🟢 روشن' if enabled else '🔴 خاموش'}{silent}\n"
            f"حساب: {s.me.name if s.me else '—'}\n"
            f"مدل: {cfg.ai_model}\n"
            f"دسترسی: {mode_fa}\n"
            f"ساعت کاری: {sched_mode} — الان {'پاسخ می‌دهد' if allowed else 'پاسخ نمی‌دهد'}\n"
            f"گفتگوهای ساکت: {len(pauses)}\n"
            f"امروز: {today['ok']} پاسخ AI، {today['quick']} سریع، {today['handoff']} تحویل به انسان، {today['errors']} خطا\n\n"
            f"{CREATOR_LINE}"
        )
        rows: Rows = [
            [(("🔴 خاموش‌کردن" if enabled else "🟢 روشن‌کردن"), "tg"), ("🌙 سکوت موقت", "sl")],
            [("💬 گفتگوها", "cl"), (f"⏸ ساکت‌شده‌ها ({len(pauses)})", "pl")],
            [("🛡 دسترسی", "ac"), ("🕒 ساعت کاری", "sc")],
            [("⚡ پاسخ سریع", "qr"), ("🧠 پرامپت و دانش", "pk")],
            [("⚙️ تنظیمات", "st"), ("📊 آمار", "ss")],
            [("📜 لاگ‌ها", "lg"), ("📤 CSV", "csv")],
            [("🧪 تست هوش مصنوعی", "ts"), ("ℹ️ درباره", "ab")],
        ]
        return text, rows

    def screen_about(self) -> tuple[str, Rows]:
        return (
            f"ℹ️ {APP_NAME}\nنسخه‌ی {VERSION}\n\n"
            f"{CREATOR_LINE}\n\n"
            "با حساب شخصی تلگرام شما کار می‌کند (بدون Premium و بدون ربات واسط). "
            "این ربات فقط پنل کنترل خصوصی شماست و به هیچ‌کس دیگری پاسخ نمی‌دهد.\n\n"
            "قابلیت‌ها: پاسخ هوشمند، تشخیص دخالت دستی شما، تحویل به انسان، لیست سیاه/سفید، "
            "ساعت کاری، پاسخ سریع، تحلیل تصویر، ویس‌به‌متن، سکوت موقت، قالب‌های آماده‌ی پرامپت، آمار و CSV.",
            [[("🏠 پنل", "h")]],
        )

    async def screen_silent(self) -> tuple[str, Rows]:
        until = await self.s.db.get_int(KEY_SILENT_UNTIL, 0)
        state = f"الان: ساکت تا {max(1, int((until - time.time()) / 60))} دقیقه‌ی دیگر" if until > time.time() else "الان: دستیار فعال است"
        return (
            f"🌙 سکوت موقت\n\n{state}\n\nدر این مدت هیچ پاسخی ارسال نمی‌شود (پاسخ‌های در حال ساخت هم لغو می‌شوند) "
            "و بعد از پایان مدت خودکار فعال می‌شود.",
            [[("۱ ساعت", "sl:60"), ("۴ ساعت", "sl:240"), ("۲۴ ساعت", "sl:1440")],
             [("🔔 لغو سکوت", "sl:0")], [("🏠 پنل", "h")]],
        )

    async def _label(self, cid: int) -> str:
        return await self.cmd._chat_label(cid)

    async def screen_chats(self) -> tuple[str, Rows]:
        rows_db = await self.s.db.list_chats(8)
        if not rows_db:
            return "هنوز گفتگویی دیده نشده است. وقتی کسی پیام بدهد اینجا می‌آید.", [[("🏠 پنل", "h")]]
        rows: Rows = []
        for r in rows_db:
            cid = int(r["chat_id"])
            cs = await self.s.db.get_chat_settings(cid)
            flags = ("🚫" if cs["ai_enabled"] is False else "") + ("⏸" if await self.s.db.get_pause_until(ACCOUNT, cid) else "")
            rows.append([(f"{flags}{(r['name'] or str(cid))[:22]}", f"c:{cid}")])
        rows.append([("🏠 پنل", "h")])
        return "💬 گفتگوهای اخیر\n(🚫 = هوش مصنوعی خاموش، ⏸ = ساکت‌شده)", rows

    async def screen_chat(self, cid: int, notice: str = "") -> tuple[str, Rows]:
        db = self.s.db
        cs = await db.get_chat_settings(cid)
        pause = await db.get_pause_until(ACCOUNT, cid)
        state = {None: "تابع تنظیم کلی", True: "همیشه روشن", False: "خاموش"}[cs["ai_enabled"]]
        in_b = cid in await db.get_list(LIST_BLACK)
        in_w = cid in await db.get_list(LIST_WHITE)
        hist = await db.get_history(ACCOUNT, cid, 4)
        recent = "\n".join(f"{'👤' if h['role'] == 'user' else '🤖'} {clip(h['content'], 70)}" for h in hist) or "—"
        ov = "، ".join(f"{k}={v:g}" for k, v in cs["overrides"].items()) or "—"
        head = f"{notice}\n\n" if notice else ""
        text = (
            f"{head}💬 {await self._label(cid)}\n\n"
            f"هوش مصنوعی: {state}\n"
            f"توقف: {('تا ' + str(max(1, int((pause - time.time()) / 60))) + ' دقیقه‌ی دیگر') if pause else 'ندارد'}\n"
            f"پاسخ‌های متوالی: {cs['ai_streak']}\n"
            f"لیست‌ها: {'سیاه ' if in_b else ''}{'سفید' if in_w else ''}{'—' if not (in_b or in_w) else ''}\n"
            f"تنظیمات اختصاصی: {ov}\n"
            f"دستورالعمل اختصاصی: {clip(cs['custom_prompt'], 100) or '—'}\n\n"
            f"آخرین پیام‌ها:\n{recent}"
        )
        rows: Rows = [
            [("🤖 روشن", f"c:{cid}:on"), ("🚫 خاموش", f"c:{cid}:off"), ("↩️ پیش‌فرض", f"c:{cid}:def")],
            [("⏸ ۳۰ دقیقه", f"c:{cid}:p30"), ("⏸ ۲ ساعت", f"c:{cid}:p120"), ("⏸ ۲۴ ساعت", f"c:{cid}:p1440")],
            [("▶️ فعال‌سازی دوباره", f"c:{cid}:rs")],
            [("📝 دستورالعمل اختصاصی", f"c:{cid}:pr"), ("🧹 پاک‌کردن", f"c:{cid}:pc")],
            [("⛔ لیست سیاه", f"c:{cid}:lb"), ("✅ لیست سفید", f"c:{cid}:lw"), ("➖ از لیست‌ها", f"c:{cid}:lx")],
            [("🗑 حذف تاریخچه", f"c:{cid}:fg")],
            [("🔙 گفتگوها", "cl"), ("🏠 پنل", "h")],
        ]
        return text, rows

    async def _chat_action(self, chat_id: int, cid: int, act: str) -> tuple[str, Rows]:
        ex = self.cmd.execute
        notice = ""
        if act in ("on", "off"):
            notice = await ex(f"chat {cid} {act}") or ""
        elif act == "def":
            notice = await ex(f"chat {cid} default") or ""
        elif act in ("p30", "p120", "p1440"):
            notice = await ex(f"pause {cid} {act[1:]}") or ""
        elif act == "rs":
            notice = await ex(f"resume {cid}") or ""
        elif act == "pr":
            return self._ask(chat_id, "prompt_chat", str(cid), "دستورالعمل اختصاصی این گفتگو را بنویسید (مثلاً: «فقط انگلیسی جواب بده»):")
        elif act == "pc":
            notice = await ex(f"chat {cid} prompt clear") or ""
        elif act == "lb":
            notice = await ex(f"black add {cid}") or ""
        elif act == "lw":
            notice = await ex(f"white add {cid}") or ""
        elif act == "lx":
            await ex(f"black remove {cid}")
            await ex(f"white remove {cid}")
            notice = "➖ از هر دو لیست حذف شد."
        elif act == "fg":
            return (
                f"⚠️ تاریخچه، لاگ‌ها و تنظیمات اختصاصی {await self._label(cid)} برای همیشه پاک شود؟",
                [[("✅ بله، پاک کن", f"c:{cid}:fgy"), ("❌ نه", f"c:{cid}")]],
            )
        elif act == "fgy":
            notice = await ex(f"forget {cid}") or ""
        return await self.screen_chat(cid, notice)

    async def screen_paused(self) -> tuple[str, Rows]:
        rows_db = await self.s.db.list_pauses()
        if not rows_db:
            return "⏸ هیچ گفتگوی ساکت‌شده‌ای نیست.", [[("🏠 پنل", "h")]]
        reasons = {"owner": "دخالت شما", "handoff": "تحویل به انسان", "limit": "سقف پاسخ متوالی", "manual": "دستی"}
        lines, rows = [], []
        for r in rows_db[:10]:
            cid = int(r["chat_id"])
            mins = max(1, int((r["paused_until"] - time.time()) / 60))
            label = await self._label(cid)
            lines.append(f"• {label} — {reasons.get(r['reason'], r['reason'])} — {mins} دقیقه‌ی دیگر")
            rows.append([(f"▶️ {clip(label, 28)}", f"pl:r:{cid}")])
        rows.append([("▶️ فعال‌سازی همه", "pl:all")])
        rows.append([("🏠 پنل", "h")])
        return "⏸ گفتگوهای ساکت‌شده:\n" + "\n".join(lines), rows

    # --- دسترسی
    async def screen_access(self, notice: str = "") -> tuple[str, Rows]:
        db = self.s.db
        mode = await db.get_setting(KEY_LIST_MODE, MODE_OFF)
        nb, nw = len(await db.get_list(LIST_BLACK)), len(await db.get_list(LIST_WHITE))
        mark = lambda m: "✅ " if mode == m else ""  # noqa: E731
        head = f"{notice}\n\n" if notice else ""
        return (
            f"{head}🛡 کنترل دسترسی\n\nحالت فعلی تعیین می‌کند دستیار به چه کسانی جواب دهد:\n"
            f"• همه: همه‌ی گفتگوهای خصوصی\n• لیست سیاه: همه به‌جز افراد لیست ({nb} نفر)\n• لیست سفید: فقط افراد لیست ({nw} نفر)",
            [[(f"{mark(MODE_OFF)}همه", "ac:m:off"), (f"{mark(LIST_BLACK)}لیست سیاه", "ac:m:black"), (f"{mark(LIST_WHITE)}لیست سفید", "ac:m:white")],
             [(f"⛔ مشاهده‌ی سیاه ({nb})", "ac:l:b"), (f"✅ مشاهده‌ی سفید ({nw})", "ac:l:w")],
             [("➕ افزودن به سیاه", "ac:a:b"), ("➕ افزودن به سفید", "ac:a:w")],
             [("🏠 پنل", "h")]],
        )

    async def _access_action(self, chat_id: int, p: list[str]) -> tuple[str, Rows]:
        if len(p) == 3 and p[1] == "m":
            await self.cmd.execute(f"filter {p[2]}")
        elif len(p) == 3 and p[1] == "a":
            who = "سیاه" if p[2] == "b" else "سفید"
            return self._ask(chat_id, f"list_{p[2]}", "", f"شناسه‌ها یا @username افراد را برای لیست {who} بنویسید (با فاصله):")
        elif len(p) >= 3 and p[1] in ("l", "r"):
            kind = LIST_BLACK if p[2] == "b" else LIST_WHITE
            if p[1] == "r" and len(p) == 4:
                await self.cmd.execute(f"{'black' if p[2] == 'b' else 'white'} remove {p[3]}")
            ids = await self.s.db.get_list(kind)
            title = "⛔ لیست سیاه" if p[2] == "b" else "✅ لیست سفید"
            rows: Rows = []
            for cid in ids[:12]:
                rows.append([(f"➖ {clip(await self._label(cid), 30)}", f"ac:r:{p[2]}:{cid}")])
            rows.append([("🔙 دسترسی", "ac"), ("🏠 پنل", "h")])
            return (f"{title} ({len(ids)} نفر)\nبرای حذف، روی نام بزنید." if ids else f"{title}: خالی است."), rows
        return await self.screen_access()

    # --- ساعت کاری
    async def screen_schedule(self, notice: str = "") -> tuple[str, Rows]:
        sched = await load_schedule(self.s)
        mark = lambda m: "✅ " if sched["mode"] == m else ""  # noqa: E731
        head = f"{notice}\n\n" if notice else ""
        day_buttons = [
            (f"{'✅' if d in sched['days'] else '⬜'} {_DAY_LABELS_FA[d]}", f"sc:d:{d}") for d in _WEEK_ORDER_FA
        ]
        return (
            f"{head}🕒 ساعت کاری\n\nحالت: {sched['mode']}\nساعت: {sched['start']:%H:%M} تا {sched['end']:%H:%M}\n"
            f"روزها: {format_days(sched['days'])}\nمنطقه‌ی زمانی: {sched['tz']}\n\n"
            "• خاموش: همیشه پاسخ بده\n• داخل: فقط در ساعت کاری\n• خارج: فقط بیرون از ساعت کاری (مثل پیام غیبت)",
            [[(f"{mark('off')}همیشه", "sc:m:off"), (f"{mark('inside')}داخل ساعت کاری", "sc:m:inside"), (f"{mark('outside')}خارج از ساعت", "sc:m:outside")],
             day_buttons[:4], day_buttons[4:],
             [("✏️ تغییر ساعت", "sc:h"), ("🌍 منطقه‌ی زمانی", "sc:z")],
             [("🏠 پنل", "h")]],
        )

    async def _schedule_action(self, chat_id: int, p: list[str]) -> tuple[str, Rows]:
        if len(p) == 3 and p[1] == "m":
            await self.cmd.execute(f"schedule {p[2]}")
        elif len(p) == 3 and p[1] == "d" and p[2].isdigit() and int(p[2]) in range(7):
            days = set(days_from_str(await self.s.db.get_setting(KEY_WORK_DAYS, self.s.config.work_days)))
            days ^= {int(p[2])}
            if not days:
                return await self.screen_schedule("⚠️ دست‌کم یک روز باید فعال بماند.")
            await self.s.db.set_setting(KEY_WORK_DAYS, days_to_str(frozenset(days)))
        elif len(p) == 2 and p[1] == "h":
            return self._ask(chat_id, "hours", "", "ساعت کاری را بنویسید، مثل: 09:00-18:00")
        elif len(p) == 2 and p[1] == "z":
            return self._ask(chat_id, "tz", "", "منطقه‌ی زمانی را بنویسید، مثل: Asia/Tehran")
        return await self.screen_schedule()

    # --- پاسخ سریع
    async def screen_quick(self, notice: str = "") -> tuple[str, Rows]:
        replies = await self.s.db.get_quick_replies()
        head = f"{notice}\n\n" if notice else ""
        rows: Rows = [[(f"🗑 {r['id']}. {clip(r['keywords'], 24)}", f"qr:d:{r['id']}")] for r in replies[:12]]
        rows.append([("➕ افزودن", "qr:a")])
        rows.append([("🏠 پنل", "h")])
        body = "\n".join(f"{r['id']}. [{r['keywords']}] ⟵ {clip(r['response'], 60)}" for r in replies[:12]) or "هنوز پاسخ سریعی تعریف نشده."
        return f"{head}⚡ پاسخ‌های سریع\nاگر پیام شامل کلمه‌ی کلیدی باشد، بدون مصرف هوش مصنوعی جواب داده می‌شود.\n\n{body}", rows

    # --- پرامپت و دانش
    async def screen_prompt(self, notice: str = "") -> tuple[str, Rows]:
        k = await self.s.db.get_setting(KEY_KNOWLEDGE, "") or ""
        p = await self.s.db.get_setting(KEY_SYSTEM_PROMPT, self.s.config.default_system_prompt) or ""
        head = f"{notice}\n\n" if notice else ""
        return (
            f"{head}🧠 پرامپت و دانش\n\nپرامپت: {len(p)} نویسه\nدانش اختصاصی: {len(k)} از {self.s.config.knowledge_max_chars} نویسه",
            [[("👁 دیدن پرامپت", "pk:ps"), ("✏️ ویرایش پرامپت", "pk:pe")],
             [("🎭 قالب‌های آماده", "pk:per"), ("↩️ پیش‌فرض", "pk:pr")],
             [("👁 دیدن دانش", "pk:ks")],
             [("✏️ جایگزینی دانش", "pk:ke"), ("➕ افزودن به دانش", "pk:ka")],
             [("🗑 پاک‌کردن دانش", "pk:kc")],
             [("🏠 پنل", "h")]],
        )

    async def _prompt_action(self, chat_id: int, p: list[str]) -> tuple[str, Rows]:
        act = p[1] if len(p) > 1 else ""
        ex = self.cmd.execute
        back: Rows = [[("🔙 بازگشت", "pk"), ("🏠 پنل", "h")]]
        if act == "ps":
            return (await ex("prompt show") or "—", back)
        if act == "ks":
            return (await ex("knowledge show") or "—", back)
        if act == "pe":
            return self._ask(chat_id, "prompt_global", "", "پرامپت جدید را بنویسید:")
        if act == "ke":
            return self._ask(chat_id, "kn_set", "", "متن دانش جدید را بنویسید یا یک فایل txt/md بفرستید (جایگزین دانش فعلی می‌شود):")
        if act == "ka":
            return self._ask(chat_id, "kn_add", "", "متنی که باید به دانش اضافه شود را بنویسید یا فایل txt/md بفرستید:")
        if act == "pr":
            return await self.screen_prompt(await ex("prompt reset") or "")
        if act == "kc":
            if len(p) == 3 and p[2] == "y":
                return await self.screen_prompt(await ex("knowledge clear") or "")
            return ("⚠️ همه‌ی دانش اختصاصی پاک شود؟", [[("✅ بله", "pk:kc:y"), ("❌ نه", "pk")]])
        if act == "per":
            if len(p) == 3 and p[2] in PERSONAS:
                return await self.screen_prompt(await ex(f"persona {p[2]}") or "")
            rows: Rows = [[(title, f"pk:per:{key}")] for key, (title, _) in PERSONAS.items()]
            rows.append([("🔙 بازگشت", "pk")])
            return "🎭 یک قالب را انتخاب کنید (جایگزین پرامپت فعلی می‌شود؛ بعداً قابل ویرایش است):", rows
        return await self.screen_prompt()

    # --- تنظیمات
    async def screen_settings(self, notice: str = "") -> tuple[str, Rows]:
        db, cfg = self.s.db, self.s.config
        head = f"{notice}\n\n" if notice else ""
        lines, rows = [], []
        defaults = {"cooldown": cfg.default_cooldown, "debounce": cfg.default_debounce, "history": cfg.default_history_size,
                    "pause": cfg.default_pause_minutes, "rate": cfg.default_rate_limit, "streak": cfg.default_max_streak,
                    "media": cfg.max_media_bytes // (1024 * 1024)}
        for name, (key, desc, _) in SETTINGS.items():
            value = await db.get_float(key, float(defaults[name]))
            lines.append(f"• {desc}: {value:g}")
            rows.append([(f"✏️ {clip(desc, 34)}", f"st:n:{name}")])
        toggles: list[Button] = []
        for name, (key, label) in TOGGLES.items():
            default = {"vision": cfg.vision_enabled, "voice": cfg.voice_enabled, "handoff": cfg.handoff_enabled, "markread": cfg.mark_as_read}[name]
            on = await db.get_bool(key, default)
            toggles.append((f"{_flag(on)} {label}", f"st:t:{name}"))
        notify = await db.get_bool(KEY_PANEL_NOTIFY, True)
        rows += [toggles[:2], toggles[2:], [(f"{_flag(notify)} 🔔 اعلان‌ها در این ربات", "st:nt"), ("💬 پیام تحویل به انسان", "st:hm")], [("🏠 پنل", "h")]]
        return f"{head}⚙️ تنظیمات\n\n" + "\n".join(lines), rows

    async def _settings_action(self, chat_id: int, p: list[str]) -> tuple[str, Rows]:
        act = p[1] if len(p) > 1 else ""
        ex = self.cmd.execute
        if act == "n" and len(p) == 3 and p[2] in SETTINGS:
            key, desc, presets = SETTINGS[p[2]]
            cur = await self.s.db.get_float(key, 0.0)
            rows: Rows = [[(v, f"st:v:{p[2]}:{v}") for v in presets], [("✏️ مقدار دلخواه", f"st:c:{p[2]}")], [("🔙 تنظیمات", "st")]]
            return f"{desc}\nمقدار فعلی: {cur:g}\nیک مقدار انتخاب کنید:", rows
        if act == "v" and len(p) == 4 and p[2] in SETTINGS:
            return await self.screen_settings(await ex(f"set {p[2]} {p[3]}") or "")
        if act == "c" and len(p) == 3 and p[2] in SETTINGS:
            return self._ask(chat_id, "num", p[2], f"مقدار جدید «{SETTINGS[p[2]][1]}» را بنویسید (فقط عدد):")
        if act == "t" and len(p) == 3 and p[2] in TOGGLES:
            key, label = TOGGLES[p[2]]
            cfg = self.s.config
            default = {"vision": cfg.vision_enabled, "voice": cfg.voice_enabled, "handoff": cfg.handoff_enabled, "markread": cfg.mark_as_read}[p[2]]
            on = await self.s.db.get_bool(key, default)
            return await self.screen_settings(await ex(f"toggle {p[2]} {'off' if on else 'on'}") or "")
        if act == "nt":
            on = await self.s.db.get_bool(KEY_PANEL_NOTIFY, True)
            await self.s.db.set_setting(KEY_PANEL_NOTIFY, "0" if on else "1")
            return await self.screen_settings("🔔 اعلان‌ها " + ("خاموش شد." if on else "روشن شد."))
        if act == "hm":
            return self._ask(chat_id, "handoffmsg", "", "متنی که هنگام تحویل به انسان برای مخاطب فرستاده می‌شود را بنویسید:")
        return await self.screen_settings()

    # --- گزارش‌ها
    async def screen_stats(self) -> tuple[str, Rows]:
        db = self.s.db
        tz = await db.get_setting(KEY_TIMEZONE, self.s.config.timezone) or self.s.config.timezone
        blocks = []
        for title, since in (("امروز", local_day_start_ts(tz)), ("۷ روز اخیر", int(time.time()) - 7 * 86400), ("کل لاگ‌ها", 0)):
            st = await db.get_stats(since)
            avg = st["avg_latency"]
            blocks.append(
                f"▪️ {title}\nپاسخ AI: {st['ok']} | سریع: {st['quick']} | انسان: {st['handoff']} | خطا: {st['errors']}\n"
                f"گفتگوهای یکتا: {st['chats']} | تاخیر: {f'{avg / 1000:.1f}s' if avg else '—'}\n"
                f"توکن: {st['prompt_tokens']:,} ورودی / {st['completion_tokens']:,} خروجی"
            )
        return "📊 آمار\n\n" + "\n\n".join(blocks), [[("🔄 تازه‌سازی", "ss"), ("🏠 پنل", "h")]]

    async def screen_logs(self) -> tuple[str, Rows]:
        text = await self.cmd.execute("logs 5") or "—"
        return text, [[("🔄 تازه‌سازی", "lg"), ("📤 CSV", "csv")], [("🏠 پنل", "h")]]


def clip_text(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
