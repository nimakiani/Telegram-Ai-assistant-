"""
engine.py — هسته‌ی دستیار (جایگزین business.py): پردازش پیام‌های حساب شخصی.

چرخه‌ی پردازش هر پیام دریافتی:

    پیام → [فقط گفتگوی خصوصی؛ نه ربات/سرویس/خودِ حساب]
         → فیلترها: فعال؟ فعال‌بودن این گفتگو؟ لیست سیاه/سفید؟ ساعت کاری؟ توقف؟ محدودیت نرخ؟
         → پاسخ سریع (کلمه‌ی کلیدی)؟  ← بدون مصرف AI
         → تبدیل رسانه (عکس/صدا/استیکر) به بخش‌های قابل‌فهم
         → جمع‌کردن پیام‌های پشت‌سرهم (Debounce)
         → «در حال تایپ» → AI (با دانش، تاریخچه، تصویر، دستورالعمل اختصاصی گفتگو)
         → [تحویل به انسان؟ → سکوت + اطلاع به صاحب حساب]
         → ارسال «محافظت‌شده» → ثبت در تاریخچه و لاگ

دخالت صاحب حساب (مهم‌ترین بخش):
  وقتی صاحب حساب خودش در یک گفتگو پیام می‌فرستد:
    1. دستیار تشخیص می‌دهد این پیام را خودش نفرستاده (ثبت شناسه‌ی پیام‌های ارسالیِ خود).
    2. «epoch» آن گفتگو بالا می‌رود؛ هر پاسخی که با epoch قدیمی در حال ساخت است لغو می‌شود
       و تسک‌های در حال انتظار/تولید cancel می‌شوند.
    3. گفتگو برای مدتی (قابل تنظیم) متوقف می‌شود؛ پس از آن خودکار فعال می‌شود یا با دستور.
  ضدّ Race Condition:
    * ارسال و دخالت هر دو زیر `send_lock` همان گفتگو انجام می‌شوند و قبل از هر ارسال،
      epoch و وضعیت توقف دوباره (اتمی) بررسی می‌شود.
    * قفل ارسال فقط حین خودِ ارسال نگه داشته می‌شود، نه حین انتظار پاسخ AI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any, Protocol

from ai_handler import AIError
from database import (
    ACCOUNT,
    KEY_COOLDOWN,
    KEY_DEBOUNCE,
    KEY_ENABLED,
    KEY_HANDOFF,
    KEY_HANDOFF_MESSAGE,
    KEY_HISTORY_SIZE,
    KEY_KNOWLEDGE,
    KEY_LIST_MODE,
    KEY_MARK_READ,
    KEY_MAX_STREAK,
    KEY_PAUSE_MINUTES,
    KEY_RATE_LIMIT,
    KEY_SYSTEM_PROMPT,
    KEY_TIMEZONE,
    LIST_BLACK,
    LIST_WHITE,
    MODE_OFF,
)
from media import build_parts
from schedule import get_tz, normalize_text
from services import (
    ChatRuntime,
    PendingBatch,
    Services,
    clip,
    fmt_ts,
    schedule_state,
    split_text,
)
from tglayer.models import (
    CHAT_PRIVATE,
    GatewayError,
    IncomingMessage,
    OwnerMessage,
    RateLimited,
    SendForbidden,
)

logger = logging.getLogger(__name__)

# اگر مدل فقط همین نشانه را بنویسد یعنی «این گفتگو نیاز به انسان دارد»
HANDOFF_TOKEN = "[[HANDOFF]]"
HANDOFF_DEFAULT_PAUSE_MINUTES = 120
LIMIT_DEFAULT_PAUSE_MINUTES = 120
MAX_BATCH_WAIT = 15.0  # سقف انتظار برای جمع‌کردن پیام‌ها (ثانیه)

_HANDOFF_INSTRUCTION = (
    "## تحویل به انسان\n"
    f"اگر کاربر صراحتاً خواست با خود صاحب حساب/یک انسان صحبت کند، یا موضوع حساس، "
    f"فوری یا نیازمند تصمیم‌گیری شخصیِ صاحب حساب است (مثل شکایت، مشکل پرداخت، "
    f"قرارداد یا مذاکره)، فقط و فقط همین متن را بنویس و هیچ چیز دیگری اضافه نکن: {HANDOFF_TOKEN}"
)

# نتیجه‌ی ارسال محافظت‌شده
SENT = "sent"
CANCELLED = "cancelled"
FAILED = "failed"


class CommandHandlerProtocol(Protocol):
    async def handle(self, msg: OwnerMessage) -> bool: ...


# ======================================================================
# ابزارهای کمکی (بدون وابستگی به تلگرام)
# ======================================================================
def build_system_prompt(
    base: str,
    knowledge: str,
    tz_name: str,
    handoff_enabled: bool,
    chat_prompt: str = "",
) -> str:
    """پرامپت نهایی = پایه + دانش + دستورالعمل اختصاصی گفتگو + زمینه‌ی زمانی + تحویل به انسان."""
    sections = [base.strip()]
    if knowledge.strip():
        sections.append(
            "## اطلاعات اختصاصی (منبع حقیقت)\n"
            f"{knowledge.strip()}\n\n"
            "برای سؤال‌های مرتبط فقط بر اساس همین اطلاعات پاسخ بده و اگر پاسخ در آن "
            "نیست، حدس نزن و صادقانه بگو که نمی‌دانی."
        )
    if chat_prompt.strip():
        sections.append("## دستورالعمل اختصاصی این گفتگو\n" + chat_prompt.strip())
    now = datetime.now(get_tz(tz_name))
    sections.append(f"## زمینه\nزمان فعلی: {now:%Y-%m-%d %H:%M} ({tz_name}) — {now:%A}")
    if handoff_enabled:
        sections.append(_HANDOFF_INSTRUCTION)
    return "\n\n".join(s for s in sections if s)


def match_quick_reply(replies: list[Any], text: str) -> str | None:
    """اولین پاسخ سریعی را که یکی از کلمات کلیدی‌اش در متن باشد برمی‌گرداند."""
    haystack = normalize_text(text)
    for row in replies:
        for keyword in row["keywords"].split("|"):
            kw = normalize_text(keyword).strip()
            if kw and kw in haystack:
                return row["response"]
    return None


class Engine:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.commands: CommandHandlerProtocol | None = None
        self._tasks: set[asyncio.Task] = set()
        self.last_owner_task: asyncio.Task | None = None  # (برای تست‌ها)

    # ------------------------------------------------------------------
    # اتصال به Gateway
    # ------------------------------------------------------------------
    async def on_incoming(self, msg: IncomingMessage) -> None:
        """callback گیت‌وی: کار سنگین در تسک جدا انجام می‌شود تا حلقه‌ی دریافت معطل نماند."""
        self._spawn(self._handle_incoming(msg))

    async def on_owner(self, msg: OwnerMessage) -> None:
        self.last_owner_task = self._spawn(self._handle_owner(msg))

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Unhandled error in engine task", exc_info=task.exception())

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """(برای تست‌ها) منتظر می‌ماند تا همه‌ی تسک‌های در حال اجرا تمام شوند."""
        deadline = time.monotonic() + timeout
        while self._tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------------
    # تنظیمات مؤثر (بازنویسی هر گفتگو ← تنظیم کلی ← پیش‌فرض)
    # ------------------------------------------------------------------
    async def _eff_int(self, cs: dict[str, Any], key: str, default: int) -> int:
        if key in cs["overrides"]:
            return int(cs["overrides"][key])
        return await self.s.db.get_int(key, default)

    async def _eff_float(self, cs: dict[str, Any], key: str, default: float) -> float:
        if key in cs["overrides"]:
            return float(cs["overrides"][key])
        return await self.s.db.get_float(key, default)

    # ------------------------------------------------------------------
    # ارسال
    # ------------------------------------------------------------------
    async def send_to_self(self, text: str) -> None:
        """متن را به «Saved Messages» خودِ صاحب حساب می‌فرستد (پاسخ دستورها، اعلان‌ها)."""
        s = self.s
        if s.gateway is None or s.me is None:
            return
        rt = s.runtime(s.me.user_id)
        try:
            async with rt.send_lock:
                for chunk in split_text(text):
                    mid = await s.gateway.send_text(s.me.user_id, chunk)
                    s.remember_sent(s.me.user_id, mid)
        except GatewayError as exc:
            logger.warning("Could not send message to Saved Messages: %s", exc)

    async def send_file_to_self(self, data: bytes, filename: str, caption: str | None = None) -> None:
        s = self.s
        if s.gateway is None or s.me is None:
            return
        rt = s.runtime(s.me.user_id)
        try:
            async with rt.send_lock:
                mid = await s.gateway.send_file(s.me.user_id, data, filename, caption)
                s.remember_sent(s.me.user_id, mid)
        except GatewayError as exc:
            logger.warning("Could not send file to Saved Messages: %s", exc)

    async def _send_guarded(self, rt: ChatRuntime, epoch: int, text: str) -> tuple[str, str]:
        """ارسال محافظت‌شده: درست قبل از ارسال، زیر قفل، epoch و وضعیت توقف را دوباره می‌سنجد.

        برمی‌گرداند: (SENT | CANCELLED | FAILED، توضیح). FloodWait حداکثر یک‌بار و فقط وقتی
        کوتاه باشد منتظر می‌ماند (خارج از قفل)؛ هیچ تلاش بی‌پایانی وجود ندارد.
        """
        s = self.s
        assert s.gateway is not None
        chunks = split_text(text)
        flood_retries = 0
        while True:
            wait = 0.0
            async with rt.send_lock:
                if rt.epoch != epoch:
                    return CANCELLED, "owner/epoch changed"
                if await s.db.get_pause_until(ACCOUNT, rt.chat_id):
                    return CANCELLED, "chat paused"
                if not await s.db.get_bool(KEY_ENABLED, True):
                    return CANCELLED, "assistant disabled"
                blocked = s.send_blocked_until - time.monotonic()
                if blocked > 0:
                    wait = blocked
                else:
                    try:
                        while chunks:
                            mid = await s.gateway.send_text(rt.chat_id, chunks[0])
                            s.remember_sent(rt.chat_id, mid)
                            chunks.pop(0)
                        rt.last_reply_at = time.monotonic()
                        return SENT, ""
                    except RateLimited as exc:
                        wait = float(exc.seconds)
                        s.send_blocked_until = time.monotonic() + wait
                        logger.warning("FloodWait %ss while sending to chat %s", exc.seconds, rt.chat_id)
                    except SendForbidden as exc:
                        return FAILED, f"forbidden: {clip(str(exc), 150)}"
                    except GatewayError as exc:
                        return FAILED, clip(str(exc), 150)
            # --- خارج از قفل: انتظار کوتاه برای FloodWait
            if flood_retries >= 1 or wait > s.config.max_flood_wait:
                return FAILED, f"flood_wait {int(wait)}s"
            flood_retries += 1
            await asyncio.sleep(wait + 1)

    async def _respect_cooldown(self, rt: ChatRuntime, cooldown: float) -> None:
        if rt.last_reply_at is None:
            return
        wait = cooldown - (time.monotonic() - rt.last_reply_at)
        if wait > 0:
            await asyncio.sleep(wait)

    async def _notify_owner(self, text: str) -> None:
        await self.send_to_self(text)

    # ------------------------------------------------------------------
    # پیام صاحب حساب
    # ------------------------------------------------------------------
    async def _handle_owner(self, msg: OwnerMessage) -> None:
        s = self.s
        if s.me is None or msg.is_service:
            return

        # Saved Messages = کانال دستورهای مدیریتی
        if msg.chat_id == s.me.user_id:
            rt_me = s.runtime(s.me.user_id)
            async with rt_me.send_lock:  # تا ثبت شناسه‌ی پیام‌های خودمان تمام شود
                own = s.is_own_sent(msg.chat_id, msg.message_id)
            if not own and self.commands is not None:
                await self.commands.handle(msg)
            return

        if msg.chat_type != CHAT_PRIVATE:
            return

        rt = s.runtime(msg.chat_id)
        async with rt.send_lock:
            # اگر ارسال در حال انجام بود، تا ثبت شناسه‌ها صبر کرده‌ایم؛ حالا تشخیص قطعی است.
            if s.is_own_sent(msg.chat_id, msg.message_id):
                return

            # --- دخالت دستی صاحب حساب ---
            rt.epoch += 1  # هر پاسخِ در حال ساخت برای این گفتگو از این لحظه باطل است
            pending_batch = rt.pending
            rt.pending = None
            for task in list(rt.tasks):
                task.cancel()
            if pending_batch is not None:
                await self._record_unanswered(rt, pending_batch)

            cs = await s.db.get_chat_settings(msg.chat_id)
            await s.db.reset_streak(msg.chat_id)
            text = (msg.text or "").strip() or "[رسانه]"
            await s.db.add_message(ACCOUNT, msg.chat_id, "owner", text)

            minutes = await self._eff_int(cs, KEY_PAUSE_MINUTES, s.config.default_pause_minutes)
            if minutes > 0:
                chat = await s.db.get_chat(msg.chat_id)
                await s.db.set_pause(
                    ACCOUNT,
                    msg.chat_id,
                    int(time.time() + minutes * 60),
                    "owner",
                    chat["name"] if chat else None,
                )
                logger.info("Chat %s paused for %d min (owner replied manually)", msg.chat_id, minutes)

    async def _record_unanswered(self, rt: ChatRuntime, batch: PendingBatch) -> None:
        """متن کاربر را در تاریخچه ثبت می‌کند (حتی اگر پاسخی ارسال نشد) تا زمینه گم نشود."""
        if batch.recorded:
            return
        batch.recorded = True
        text = "\n".join(t for t in batch.log_texts if t)
        if text:
            await self.s.db.add_message(ACCOUNT, rt.chat_id, "user", text)

    # ------------------------------------------------------------------
    # فیلترها
    # ------------------------------------------------------------------
    async def _is_allowed(self, user_id: int) -> bool:
        mode = await self.s.db.get_setting(KEY_LIST_MODE, MODE_OFF)
        if mode == LIST_BLACK:
            return not await self.s.db.in_list(user_id, LIST_BLACK)
        if mode == LIST_WHITE:
            return await self.s.db.in_list(user_id, LIST_WHITE)
        return True

    async def _passes_gates(
        self, rt: ChatRuntime, cs: dict[str, Any], user_id: int
    ) -> tuple[bool, str]:
        s = self.s
        if not await s.db.get_bool(KEY_ENABLED, True):
            return False, "disabled"
        if cs["ai_enabled"] is False:
            return False, "chat_disabled"
        if not await self._is_allowed(user_id):
            return False, "list"
        _mode, _working, allowed = await schedule_state(s)
        if not allowed:
            return False, "schedule"
        if await s.db.get_pause_until(ACCOUNT, rt.chat_id):
            return False, "paused"
        limit = await self._eff_int(cs, KEY_RATE_LIMIT, s.config.default_rate_limit)
        if not s.rate_allows(rt, limit):
            logger.info("Rate limit hit for chat %s", rt.chat_id)
            return False, "rate_limit"
        return True, ""

    # ------------------------------------------------------------------
    # پیام دریافتی
    # ------------------------------------------------------------------
    async def _handle_incoming(self, msg: IncomingMessage) -> None:
        s = self.s
        # فقط گفتگوی خصوصی با یک انسان؛ گروه/کانال/ربات/سرویس/خودِ حساب نادیده گرفته می‌شود
        if msg.chat_type != CHAT_PRIVATE or msg.is_bot or msg.is_service or msg.is_self:
            return
        if msg.sender_id is None or (s.me is not None and msg.chat_id == s.me.user_id):
            return
        if not s.first_time_seen(msg.chat_id, msg.message_id):
            return

        await s.db.touch_chat(msg.chat_id, msg.sender_name, msg.sender_username)
        rt = s.runtime(msg.chat_id)
        epoch0 = rt.epoch
        cs = await s.db.get_chat_settings(msg.chat_id)

        ok, reason = await self._passes_gates(rt, cs, msg.sender_id)
        if not ok:
            if reason == "paused":  # برای حفظ زمینه‌ی مکالمه
                text = (msg.text or "").strip() or ("[رسانه]" if msg.media else "")
                if text:
                    await s.db.add_message(ACCOUNT, msg.chat_id, "user", text)
            return

        # پاسخ سریع بر اساس کلمه‌ی کلیدی (بدون مصرف AI)
        if msg.text and msg.text.strip() and msg.media is None:
            reply = match_quick_reply(await s.db.get_quick_replies(), msg.text)
            if reply:
                await self._send_quick_reply(rt, epoch0, cs, msg, reply)
                return

        parts, log_text = await build_parts(s, msg)
        if not parts:
            return

        if rt.epoch != epoch0:  # صاحب حساب هنگام پردازش رسانه دخالت کرد
            await s.db.add_message(ACCOUNT, msg.chat_id, "user", log_text)
            return
        self._enqueue(rt, msg, parts, log_text)

    async def _send_quick_reply(
        self, rt: ChatRuntime, epoch: int, cs: dict[str, Any], msg: IncomingMessage, reply: str
    ) -> None:
        s = self.s
        text = (msg.text or "").strip()
        async with rt.turn_lock:
            cooldown = await self._eff_float(cs, KEY_COOLDOWN, s.config.default_cooldown)
            await self._respect_cooldown(rt, cooldown)
            outcome, detail = await self._send_guarded(rt, epoch, reply)
        status = {SENT: "quick", CANCELLED: "cancelled"}.get(outcome, f"send_error: {detail}")
        await s.db.add_message(ACCOUNT, msg.chat_id, "user", text)
        if outcome == SENT:
            await s.db.add_message(ACCOUNT, msg.chat_id, "assistant", reply)
        await s.db.add_log(
            ACCOUNT, msg.chat_id, msg.sender_id or 0, msg.sender_username,
            msg.sender_name, text, reply if outcome == SENT else None, status,
        )

    def _enqueue(
        self, rt: ChatRuntime, msg: IncomingMessage, parts: list, log_text: str
    ) -> None:
        """پیام را به صف گفتگو اضافه می‌کند؛ اولین پیام یک تسک «رهبر» می‌سازد."""
        batch = rt.pending
        new_batch = batch is None
        if batch is None:
            batch = rt.pending = PendingBatch(epoch=rt.epoch)
        batch.parts.extend(parts)
        batch.log_texts.append(log_text)
        batch.last_ts = time.monotonic()
        batch.sender_id = msg.sender_id
        batch.sender_name = msg.sender_name
        batch.sender_username = msg.sender_username
        batch.last_message_id = msg.message_id
        if new_batch:
            task = self._spawn(self._run_batch(rt, batch))
            rt.tasks.add(task)
            task.add_done_callback(rt.tasks.discard)

    async def _run_batch(self, rt: ChatRuntime, batch: PendingBatch) -> None:
        s = self.s
        try:
            cs = await s.db.get_chat_settings(rt.chat_id)
            debounce = await self._eff_float(cs, KEY_DEBOUNCE, s.config.default_debounce)
            while True:
                now = time.monotonic()
                if now - batch.first_ts >= MAX_BATCH_WAIT:
                    break
                wait = debounce - (now - batch.last_ts)
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            if rt.pending is batch:  # از اینجا پیام‌های جدید دسته‌ی تازه می‌سازند
                rt.pending = None
            async with rt.turn_lock:
                await self._process_turn(rt, batch)
        except asyncio.CancelledError:
            if rt.pending is batch:
                rt.pending = None
            with contextlib.suppress(Exception):
                await asyncio.shield(self._record_unanswered(rt, batch))
            raise

    # ------------------------------------------------------------------
    # یک «نوبت» گفتگو
    # ------------------------------------------------------------------
    async def _process_turn(self, rt: ChatRuntime, batch: PendingBatch) -> None:
        s = self.s
        cfg = s.config
        assert s.gateway is not None
        chat_id = rt.chat_id
        epoch = batch.epoch
        user_text = "\n".join(t for t in batch.log_texts if t)

        if rt.epoch != epoch or await s.db.get_pause_until(ACCOUNT, chat_id):
            await self._record_unanswered(rt, batch)
            return

        cs = await s.db.get_chat_settings(chat_id)

        # --- سقف پاسخ‌های متوالیِ هوش مصنوعی
        max_streak = await self._eff_int(cs, KEY_MAX_STREAK, cfg.default_max_streak)
        if max_streak > 0 and cs["ai_streak"] >= max_streak:
            minutes = await self._eff_int(cs, KEY_PAUSE_MINUTES, cfg.default_pause_minutes) \
                or LIMIT_DEFAULT_PAUSE_MINUTES
            await s.db.set_pause(
                ACCOUNT, chat_id, int(time.time() + minutes * 60), "limit", batch.sender_name
            )
            await s.db.reset_streak(chat_id)
            await self._record_unanswered(rt, batch)
            await self._notify_owner(
                f"⏸ سقف {max_streak} پاسخ متوالی دستیار در گفتگو با «{batch.sender_name}» "
                f"({chat_id}) رسید؛ دستیار تا {fmt_ts(int(time.time() + minutes * 60))} UTC "
                f"در این گفتگو ساکت می‌ماند.\nبرای ادامه‌ی فوری: /ai resume {chat_id}"
            )
            return

        cooldown = await self._eff_float(cs, KEY_COOLDOWN, cfg.default_cooldown)
        await self._respect_cooldown(rt, cooldown)
        if rt.epoch != epoch:
            await self._record_unanswered(rt, batch)
            return

        if await s.db.get_bool(KEY_MARK_READ, cfg.mark_as_read) and batch.last_message_id:
            with contextlib.suppress(GatewayError):
                await s.gateway.mark_read(chat_id, batch.last_message_id)

        reply_text: str | None = None
        result = None
        status = "ok"
        handoff = False
        handoff_enabled = await s.db.get_bool(KEY_HANDOFF, cfg.handoff_enabled)

        async with s.gateway.typing(chat_id):
            started = time.monotonic()
            system_prompt = build_system_prompt(
                await s.db.get_setting(KEY_SYSTEM_PROMPT, cfg.default_system_prompt) or "",
                await s.db.get_setting(KEY_KNOWLEDGE, "") or "",
                await s.db.get_setting(KEY_TIMEZONE, cfg.timezone) or cfg.timezone,
                handoff_enabled,
                cs["custom_prompt"],
            )
            history_size = await self._eff_int(cs, KEY_HISTORY_SIZE, cfg.default_history_size)
            history = await s.db.get_history(ACCOUNT, chat_id, history_size)
            try:
                result = await s.ai.generate_reply(system_prompt, history, batch.parts)
                reply_text = result.text
            except AIError as exc:
                status = f"error: {clip(str(exc), 200)}"
                logger.error("AI error for chat %s: %s", chat_id, exc)
            elapsed = time.monotonic() - started
            if reply_text and elapsed < cfg.min_typing_seconds:
                await asyncio.sleep(cfg.min_typing_seconds - elapsed)

        # --- تشخیص درخواست تحویل به انسان
        if reply_text and HANDOFF_TOKEN in reply_text:
            reply_text = reply_text.replace(HANDOFF_TOKEN, "").strip()
            if handoff_enabled:
                handoff = True
                status = "handoff"
                if not reply_text:
                    reply_text = (
                        await s.db.get_setting(KEY_HANDOFF_MESSAGE, cfg.handoff_message)
                        or cfg.handoff_message
                    )
            elif not reply_text:
                reply_text = None

        to_send = reply_text or (cfg.fallback_message or None)
        sent_ok = False
        if to_send:
            outcome, detail = await self._send_guarded(rt, epoch, to_send)
            if outcome == SENT:
                sent_ok = True
            elif outcome == CANCELLED:
                status = "cancelled"
                handoff = False
                logger.info("Reply to chat %s cancelled (%s)", chat_id, detail)
            else:
                status = f"send_error: {detail}"
                handoff = False
                logger.error("Failed to send reply to chat %s: %s", chat_id, detail)

        batch.recorded = True
        await s.db.add_message(ACCOUNT, chat_id, "user", user_text)
        if sent_ok and to_send:
            await s.db.add_message(ACCOUNT, chat_id, "assistant", to_send)
            if result is not None and status in ("ok", "handoff"):
                await s.db.bump_streak(chat_id)

        await s.db.add_log(
            connection_id=ACCOUNT,
            chat_id=chat_id,
            user_id=batch.sender_id or 0,
            username=batch.sender_username,
            full_name=batch.sender_name,
            user_text=user_text,
            bot_reply=to_send if sent_ok else None,
            status=status,
            latency_ms=getattr(result, "latency_ms", None),
            prompt_tokens=getattr(result, "prompt_tokens", None),
            completion_tokens=getattr(result, "completion_tokens", None),
            model=getattr(result, "model", None),
        )

        if handoff:
            minutes = (
                await self._eff_int(cs, KEY_PAUSE_MINUTES, cfg.default_pause_minutes)
                or HANDOFF_DEFAULT_PAUSE_MINUTES
            )
            until = int(time.time() + minutes * 60)
            await s.db.set_pause(ACCOUNT, chat_id, until, "handoff", batch.sender_name)
            who = batch.sender_name or str(chat_id)
            handle = f" (@{batch.sender_username})" if batch.sender_username else ""
            await self._notify_owner(
                "🙋 درخواست دخالت انسانی\n"
                f"کاربر: {who}{handle} — شناسه: {chat_id}\n"
                f"آخرین پیام: {clip(user_text, 200)}\n"
                f"دستیار تا {fmt_ts(until)} UTC در این گفتگو ساکت می‌ماند.\n"
                f"برای فعال‌سازی زودتر: /ai resume {chat_id}"
            )
