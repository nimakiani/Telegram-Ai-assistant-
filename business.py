"""
business.py — هسته‌ی دستیار: پردازش اتصال‌ها و پیام‌های Business (Chat Automation).

چرخه‌ی پردازش هر پیام مشتری:

    پیام → [پیام صاحب حساب؟ → ثبت + توقف موقت ربات]
         → فیلترها: فعال؟ لیست سیاه/سفید؟ ساعت کاری؟ چت ساکت‌شده؟ محدودیت نرخ؟
         → پاسخ سریع (کلمه‌ی کلیدی)؟  ← بدون مصرف AI
         → تبدیل رسانه (عکس/صدا/استیکر) به بخش‌های قابل‌فهم
         → جمع‌کردن پیام‌های پشت‌سرهم (Debounce)
         → قفل چت + Cooldown → «در حال تایپ» → AI (با دانش، تاریخچه، تصویر)
         → [تحویل به انسان؟ → سکوت + اطلاع به مدیر]
         → ارسال پاسخ → لاگ و تاریخچه

برای ایمنی در برابر تفاوت نسخه‌ها، به‌روزرسانی‌های Business با یک TypeHandler عمومی و
بررسی فیلدهای `update.business_connection` و `update.business_message` پردازش می‌شوند.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from datetime import datetime
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ai_handler import AIError
from database import (
    KEY_COOLDOWN,
    KEY_DEBOUNCE,
    KEY_ENABLED,
    KEY_HANDOFF,
    KEY_HANDOFF_MESSAGE,
    KEY_HISTORY_SIZE,
    KEY_KNOWLEDGE,
    KEY_LIST_MODE,
    KEY_MARK_READ,
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
from services import Key, PendingBatch, Services, clip, fmt_ts, schedule_state, split_text, svc

logger = logging.getLogger(__name__)

# اگر مدل فقط همین نشانه را بنویسد یعنی «این گفتگو نیاز به انسان دارد»
HANDOFF_TOKEN = "[[HANDOFF]]"
HANDOFF_DEFAULT_PAUSE_MINUTES = 120
MAX_BATCH_WAIT = 15.0  # سقف انتظار برای جمع‌کردن پیام‌ها (ثانیه)
TYPING_REFRESH_SECONDS = 4.0  # اکشن «در حال تایپ» حدود ۵ ثانیه دوام دارد

_HANDOFF_INSTRUCTION = (
    "## تحویل به انسان\n"
    f"اگر کاربر صراحتاً خواست با خود صاحب حساب/یک انسان صحبت کند، یا موضوع حساس، "
    f"فوری یا نیازمند تصمیم‌گیری شخصیِ صاحب حساب است (مثل شکایت، مشکل پرداخت، "
    f"قرارداد یا مذاکره)، فقط و فقط همین متن را بنویس و هیچ چیز دیگری اضافه نکن: {HANDOFF_TOKEN}"
)


# ======================================================================
# ابزارهای کمکی
# ======================================================================
def _can_reply(conn: Any) -> bool:
    """سازگار با هر دو نسخه‌ی API: فیلد قدیمی can_reply یا شیء جدید rights."""
    rights = getattr(conn, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", True))
    return bool(getattr(conn, "can_reply", True))


def build_system_prompt(
    base: str, knowledge: str, tz_name: str, handoff_enabled: bool
) -> str:
    """پرامپت نهایی = پرامپت پایه + دانش کسب‌وکار + زمینه‌ی زمانی + دستور تحویل به انسان."""
    sections = [base.strip()]
    if knowledge.strip():
        sections.append(
            "## اطلاعات کسب‌وکار (منبع حقیقت)\n"
            f"{knowledge.strip()}\n\n"
            "برای سؤال‌های مرتبط فقط بر اساس همین اطلاعات پاسخ بده و اگر پاسخ در آن "
            "نیست، حدس نزن و صادقانه بگو که نمی‌دانی."
        )
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


# ======================================================================
# اتصال‌های Business
# ======================================================================
async def _store_connection(s: Services, conn: Any) -> None:
    await s.db.upsert_connection(
        connection_id=conn.id,
        user_id=conn.user.id,
        user_chat_id=getattr(conn, "user_chat_id", None),
        is_enabled=bool(conn.is_enabled),
        can_reply=_can_reply(conn),
    )


async def ensure_connection(
    context: ContextTypes.DEFAULT_TYPE, s: Services, connection_id: str
) -> Any:
    """اتصال را از دیتابیس می‌خواند؛ اگر نبود از تلگرام می‌گیرد و ذخیره می‌کند."""
    row = await s.db.get_connection(connection_id)
    if row is not None:
        return row
    try:
        conn = await context.bot.get_business_connection(business_connection_id=connection_id)
    except TelegramError as exc:
        logger.warning("Could not fetch business connection: %s", exc)
        return None
    await _store_connection(s, conn)
    return await s.db.get_connection(connection_id)


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, s: Services, text: str) -> None:
    for admin_id in s.config.admin_ids:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as exc:  # مثلاً مدیر هنوز /start نزده است
            logger.debug("Could not notify admin %s: %s", admin_id, exc)


async def handle_business_connection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """اتصال/قطع اتصال/تغییر دسترسی ربات در Chat Automation را ثبت و به مدیران اطلاع می‌دهد."""
    s = svc(context)
    conn = update.business_connection
    await _store_connection(s, conn)
    owner = conn.user
    name = html.escape(owner.full_name or str(owner.id))
    if conn.is_enabled:
        note = "می‌تواند پاسخ دهد ✅" if _can_reply(conn) else "دسترسی پاسخ‌دادن ندارد ⚠️"
        text = f"🔗 اتصال Business فعال شد\nحساب: {name} (<code>{owner.id}</code>)\nوضعیت: {note}"
    else:
        text = f"⛔ اتصال Business قطع/غیرفعال شد\nحساب: {name} (<code>{owner.id}</code>)"
    await notify_admins(context, s, text)


# ======================================================================
# فیلترها
# ======================================================================
async def _is_allowed(s: Services, user_id: int) -> bool:
    """بر اساس حالت لیست (خاموش/سیاه/سفید) تعیین می‌کند به این کاربر پاسخ داده شود یا نه."""
    mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF)
    if mode == LIST_BLACK:
        return not await s.db.in_list(user_id, LIST_BLACK)
    if mode == LIST_WHITE:
        return await s.db.in_list(user_id, LIST_WHITE)
    return True


async def _passes_gates(s: Services, key: Key, user_id: int) -> bool:
    """همه‌ی فیلترهای ورودی: فعال بودن، لیست‌ها، ساعت کاری، توقف چت، محدودیت نرخ."""
    if not await s.db.get_bool(KEY_ENABLED, True):
        return False
    if not await _is_allowed(s, user_id):
        return False
    _mode, _working, allowed = await schedule_state(s)
    if not allowed:
        return False
    if await s.db.get_pause_until(*key):
        return False
    limit = await s.db.get_int(KEY_RATE_LIMIT, s.config.default_rate_limit)
    if not s.rate_allows(key, limit):
        logger.info("Rate limit hit for chat %s", key[1])
        return False
    return True


# ======================================================================
# ارسال
# ======================================================================
async def _typing_loop(bot: Any, chat_id: int, connection_id: str) -> None:
    """تا زمانی که لغو نشود، هر چند ثانیه «در حال تایپ...» را تازه می‌کند."""
    while True:
        try:
            await bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=connection_id
            )
        except TelegramError as exc:
            logger.debug("send_chat_action failed: %s", exc)
        await asyncio.sleep(TYPING_REFRESH_SECONDS)


async def _send_text(context: ContextTypes.DEFAULT_TYPE, s: Services, key: Key, text: str) -> None:
    """متن را (در صورت نیاز تکه‌تکه) ارسال و شناسه‌ی پیام‌ها را برای تشخیص «پیام خود ربات» ثبت می‌کند."""
    connection_id, chat_id = key
    for chunk in split_text(text):
        sent = await context.bot.send_message(
            chat_id=chat_id, text=chunk, business_connection_id=connection_id
        )
        s.remember_bot_message(connection_id, chat_id, getattr(sent, "message_id", None))
    s.last_reply_at[key] = time.monotonic()


async def _respect_cooldown(s: Services, key: Key) -> None:
    """اگر از آخرین پاسخ در این چت کمتر از Cooldown گذشته باشد، صبر می‌کند (پیام حذف نمی‌شود)."""
    cooldown = await s.db.get_float(KEY_COOLDOWN, s.config.default_cooldown)
    last = s.last_reply_at.get(key)
    if last is None:
        return
    wait = cooldown - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)


# ======================================================================
# پیام صاحب حساب (تحویل به انسان)
# ======================================================================
async def _handle_owner_message(s: Services, msg: Any, key: Key) -> None:
    """پیامی که خود صاحب حساب در چت فرستاده: ثبت در تاریخچه و سکوت موقت ربات."""
    connection_id, chat_id = key
    # صبر می‌کنیم اگر ربات همین حالا در حال ارسال است؛ تا شناسه‌ی پیام‌هایش ثبت شده باشد
    # و پیام خودِ ربات با پیام دستیِ صاحب حساب اشتباه گرفته نشود.
    async with s.lock_for(key):
        if s.is_bot_message(connection_id, chat_id, msg.message_id):
            return

    text = (msg.text or msg.caption or "").strip() or "[رسانه]"
    await s.db.add_message(connection_id, chat_id, "owner", text)

    minutes = await s.db.get_int(KEY_PAUSE_MINUTES, s.config.default_pause_minutes)
    if minutes > 0:
        await s.db.set_pause(
            connection_id,
            chat_id,
            int(time.time() + minutes * 60),
            "owner",
            getattr(msg.chat, "full_name", None),
        )
        logger.info("Chat %s paused for %d min (owner replied manually)", chat_id, minutes)


# ======================================================================
# پردازش پیام مشتری
# ======================================================================
async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نقطه‌ی ورود پردازش `business_message` (به شرح بالای فایل)."""
    s = svc(context)
    msg = update.business_message
    user = msg.from_user
    if user is None:
        return
    connection_id = msg.business_connection_id
    chat_id = msg.chat.id
    key: Key = (connection_id, chat_id)

    conn = await ensure_connection(context, s, connection_id)

    # در چت خصوصی، پیام مشتری همیشه از همان chat.id می‌آید؛ هر پیام دیگر از صاحب حساب است.
    if user.id != chat_id or (conn is not None and user.id == conn["user_id"]):
        await _handle_owner_message(s, msg, key)
        return
    if user.is_bot:
        return
    if conn is not None and (not conn["is_enabled"] or not conn["can_reply"]):
        return
    if not await _passes_gates(s, key, user.id):
        return

    # پاسخ سریع بر اساس کلمه‌ی کلیدی (بدون مصرف AI)
    if msg.text and msg.text.strip():
        reply = match_quick_reply(await s.db.get_quick_replies(), msg.text)
        if reply:
            await _send_quick_reply(context, s, key, user, msg.text.strip(), reply)
            return

    parts, log_text = await build_parts(context, s, msg)
    if not parts:
        return
    await _enqueue(context, s, key, parts, log_text, user, msg.message_id)


async def _send_quick_reply(
    context: ContextTypes.DEFAULT_TYPE, s: Services, key: Key, user: Any, text: str, reply: str
) -> None:
    connection_id, chat_id = key
    status = "quick"
    async with s.lock_for(key):
        await _respect_cooldown(s, key)
        try:
            await _send_text(context, s, key, reply)
        except TelegramError as exc:
            status = f"send_error: {clip(str(exc), 200)}"
            logger.error("Failed to send quick reply to chat %s: %s", chat_id, exc)
    await s.db.add_message(connection_id, chat_id, "user", text)
    if status == "quick":
        await s.db.add_message(connection_id, chat_id, "assistant", reply)
    await s.db.add_log(
        connection_id, chat_id, user.id, user.username, user.full_name, text, reply, status
    )


async def _enqueue(
    context: ContextTypes.DEFAULT_TYPE,
    s: Services,
    key: Key,
    parts: list[tuple[str, str]],
    log_text: str,
    user: Any,
    message_id: int | None,
) -> None:
    """پیام را به صف چت اضافه می‌کند؛ اولین پیام «رهبر» می‌شود و پس از آرام‌شدن چت، همه را یک‌جا پردازش می‌کند."""
    batch = s.pending.get(key)
    if batch is None:
        batch = s.pending[key] = PendingBatch()
    batch.parts.extend(parts)
    batch.log_texts.append(log_text)
    batch.last_ts = time.monotonic()
    batch.user = user
    batch.last_message_id = message_id
    if batch.leader:
        return  # رهبر قبلاً در حال انتظار است و این پیام را هم برمی‌دارد
    batch.leader = True

    try:
        debounce = await s.db.get_float(KEY_DEBOUNCE, s.config.default_debounce)
        while True:
            now = time.monotonic()
            if now - batch.first_ts >= MAX_BATCH_WAIT:
                break
            wait = debounce - (now - batch.last_ts)
            if wait <= 0:
                break
            await asyncio.sleep(wait)
    finally:
        # از این لحظه پیام‌های جدید یک دسته‌ی تازه می‌سازند
        if s.pending.get(key) is batch:
            del s.pending[key]

    await _process_turn(context, s, key, batch)


async def _process_turn(
    context: ContextTypes.DEFAULT_TYPE, s: Services, key: Key, batch: PendingBatch
) -> None:
    """یک «نوبت» گفتگو را پردازش می‌کند: AI → (تحویل به انسان؟) → ارسال → لاگ."""
    connection_id, chat_id = key
    user = batch.user
    user_text = "\n".join(t for t in batch.log_texts if t)
    cfg = s.config

    reply_text: str | None = None
    result = None
    status = "ok"
    handoff = False

    async with s.lock_for(key):
        # ممکن است در فاصله‌ی انتظار، صاحب حساب خودش جواب داده باشد
        if await s.db.get_pause_until(connection_id, chat_id):
            await s.db.add_message(connection_id, chat_id, "user", user_text)
            return
        await _respect_cooldown(s, key)

        if await s.db.get_bool(KEY_MARK_READ, cfg.mark_as_read) and batch.last_message_id:
            with contextlib.suppress(TelegramError, AttributeError):
                await context.bot.read_business_message(
                    business_connection_id=connection_id,
                    chat_id=chat_id,
                    message_id=batch.last_message_id,
                )

        typing_task = asyncio.create_task(_typing_loop(context.bot, chat_id, connection_id))
        try:
            started = time.monotonic()
            handoff_enabled = await s.db.get_bool(KEY_HANDOFF, cfg.handoff_enabled)
            system_prompt = build_system_prompt(
                await s.db.get_setting(KEY_SYSTEM_PROMPT, cfg.default_system_prompt) or "",
                await s.db.get_setting(KEY_KNOWLEDGE, "") or "",
                await s.db.get_setting(KEY_TIMEZONE, cfg.timezone) or cfg.timezone,
                handoff_enabled,
            )
            history_size = await s.db.get_int(KEY_HISTORY_SIZE, cfg.default_history_size)
            history = await s.db.get_history(connection_id, chat_id, history_size)
            try:
                result = await s.ai.generate_reply(system_prompt, history, batch.parts)
                reply_text = result.text
            except AIError as exc:
                status = f"error: {clip(str(exc), 200)}"
                logger.error("AI error for chat %s: %s", chat_id, exc)
            # حداقل مدت نمایش «در حال تایپ» برای تجربه‌ی طبیعی‌تر
            elapsed = time.monotonic() - started
            if reply_text and elapsed < cfg.min_typing_seconds:
                await asyncio.sleep(cfg.min_typing_seconds - elapsed)
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

        # تشخیص درخواست تحویل به انسان
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
                reply_text = None  # نشانه بود ولی قابلیت خاموش است؛ چیزی نمی‌فرستیم

        to_send = reply_text or (cfg.fallback_message or None)
        sent_ok = False
        if to_send:
            try:
                await _send_text(context, s, key, to_send)
                sent_ok = True
            except TelegramError as exc:
                status = f"send_error: {clip(str(exc), 200)}"
                handoff = False
                logger.error("Failed to send reply to chat %s: %s", chat_id, exc)

        await s.db.add_message(connection_id, chat_id, "user", user_text)
        if sent_ok and to_send:
            await s.db.add_message(connection_id, chat_id, "assistant", to_send)

        await s.db.add_log(
            connection_id=connection_id,
            chat_id=chat_id,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            user_text=user_text,
            bot_reply=to_send,
            status=status,
            latency_ms=getattr(result, "latency_ms", None),
            prompt_tokens=getattr(result, "prompt_tokens", None),
            completion_tokens=getattr(result, "completion_tokens", None),
            model=getattr(result, "model", None),
        )

        if handoff:
            minutes = (
                await s.db.get_int(KEY_PAUSE_MINUTES, cfg.default_pause_minutes)
                or HANDOFF_DEFAULT_PAUSE_MINUTES
            )
            until = int(time.time() + minutes * 60)
            await s.db.set_pause(connection_id, chat_id, until, "handoff", user.full_name)
            name = html.escape(user.full_name or str(user.id))
            await notify_admins(
                context,
                s,
                "🙋 <b>درخواست دخالت انسانی</b>\n"
                f"کاربر: <a href=\"tg://user?id={user.id}\">{name}</a> (<code>{user.id}</code>)\n"
                f"آخرین پیام: {html.escape(clip(user_text, 200))}\n"
                f"ربات تا {fmt_ts(until)} UTC در این چت ساکت می‌ماند.",
            )


async def on_business_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نقطه‌ی ورود عمومی برای به‌روزرسانی‌های Business (مستقل از نسخه‌ی کتابخانه)."""
    if update.business_connection is not None:
        await handle_business_connection(update, context)
    elif update.business_message is not None:
        await handle_business_message(update, context)
