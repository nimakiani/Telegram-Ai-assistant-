"""
admin.py — پنل مدیریت داخل تلگرام (فقط برای ADMIN_IDS).

ساختار: هر «صفحه» یک تابع `page_*` است که (متن HTML، کیبورد) برمی‌گرداند. دکمه‌های
`page:<name>` صفحه را نمایش می‌دهند و بقیه‌ی دکمه‌ها یک عملیات انجام می‌دهند.
عملیاتی که ورودی متنی می‌خواهند، `context.user_data["awaiting"]` را تنظیم می‌کنند و
پیام بعدیِ مدیر توسط `on_admin_text` (یا `on_admin_document` برای فایل) پردازش می‌شود.

صفحه‌ها: منوی اصلی، پرامپت، دانش کسب‌وکار، فیلتر/لیست‌ها، ساعت کاری، پاسخ‌های سریع،
تنظیمات پیشرفته، چت‌های ساکت‌شده، آمار، لاگ‌ها (با خروجی CSV)، اتصال‌ها.
"""

from __future__ import annotations

import csv
import html
import io
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

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
    KEY_SCHEDULE_MODE,
    KEY_SYSTEM_PROMPT,
    KEY_TIMEZONE,
    KEY_VISION,
    KEY_VOICE,
    KEY_WORK_DAYS,
    KEY_WORK_END,
    KEY_WORK_START,
    LIST_BLACK,
    LIST_WHITE,
    MODE_OFF,
)
from schedule import (
    MODE_INSIDE,
    MODE_OUTSIDE,
    SCHEDULE_MODES,
    days_to_str,
    fmt_time,
    format_days,
    format_local_now,
    is_valid_tz,
    local_day_start_ts,
    normalize_digits,
    parse_days,
    parse_hours,
)
from services import Services, clip, fmt_ts, is_admin, load_schedule, schedule_state, svc

MAX_PROMPT_LEN = 3500
TELEGRAM_MAX_LEN = 4096

KINDS = {"bl": LIST_BLACK, "wl": LIST_WHITE}
KIND_TITLES = {LIST_BLACK: "لیست سیاه", LIST_WHITE: "لیست سفید"}
MODE_TITLES = {
    MODE_OFF: "خاموش (پاسخ به همه)",
    LIST_BLACK: "لیست سیاه (به لیست‌شده‌ها پاسخ نده)",
    LIST_WHITE: "لیست سفید (فقط به لیست‌شده‌ها پاسخ بده)",
}
FILTER_CYCLE = [MODE_OFF, LIST_BLACK, LIST_WHITE]
SCHEDULE_TITLES = {
    MODE_OFF: "خاموش (همیشه پاسخ بده)",
    MODE_INSIDE: "فقط در ساعت کاری",
    MODE_OUTSIDE: "فقط خارج از ساعت کاری (وقتی دور هستم)",
}

# تنظیمات عددی قابل ویرایش: نام → (کلید، نوع، حداقل، حداکثر، توضیح)
NUM_SETTINGS: dict[str, tuple[str, type, float, float, str]] = {
    "cooldown": (KEY_COOLDOWN, float, 0, 300, "تاخیر بین پاسخ‌ها (ثانیه)"),
    "debounce": (KEY_DEBOUNCE, float, 0, 30, "مهلت جمع‌کردن پیام‌های پشت‌سرهم (ثانیه)"),
    "history": (KEY_HISTORY_SIZE, int, 0, 40, "تعداد پیام‌های قبلی در زمینه"),
    "pause": (KEY_PAUSE_MINUTES, int, 0, 10080, "سکوت ربات پس از پاسخ دستی/تحویل (دقیقه؛ ۰ = غیرفعال)"),
    "rate": (KEY_RATE_LIMIT, int, 0, 1000, "سقف پیام در ساعت برای هر چت (۰ = نامحدود)"),
}
# کلیدهای روشن/خاموش: نام → (کلید، عنوان، پیش‌فرض)
TOGGLES: dict[str, tuple[str, str]] = {
    "vision": (KEY_VISION, "👁 تحلیل تصویر"),
    "voice": (KEY_VOICE, "🎙 تبدیل صدا به متن"),
    "handoff": (KEY_HANDOFF, "🙋 تحویل به انسان"),
    "markread": (KEY_MARK_READ, "✔️ علامت «خوانده‌شد»"),
}
TOGGLE_DEFAULTS = {"vision": True, "voice": True, "handoff": True, "markread": False}


# ======================================================================
# ابزارهای رابط کاربری
# ======================================================================
def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """کیبورد شیشه‌ای از ردیف‌هایی از (عنوان، callback_data)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


BACK = [("🔙 منوی اصلی", "page:main")]
CANCEL_KB = kb([("❎ لغو", "page:main")])


def _cap(text: str) -> str:
    """متن را به سقف طول پیام تلگرام محدود می‌کند."""
    if len(text) <= TELEGRAM_MAX_LEN:
        return text
    return text[: TELEGRAM_MAX_LEN - 20].rsplit("\n", 1)[0] + "\n…"


async def _edit(query: Any, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    """پیام منو را ویرایش می‌کند؛ خطای «تغییری نکرده» نادیده گرفته می‌شود."""
    try:
        await query.edit_message_text(_cap(text), reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def _flag(value: bool) -> str:
    return "✅ روشن" if value else "⛔ خاموش"


async def _toggle_value(s: Services, name: str) -> bool:
    key, _title = TOGGLES[name]
    cfg_defaults = {
        "vision": s.config.vision_enabled,
        "voice": s.config.voice_enabled,
        "handoff": s.config.handoff_enabled,
        "markread": s.config.mark_as_read,
    }
    return await s.db.get_bool(key, cfg_defaults[name])


# ======================================================================
# صفحه‌ها
# ======================================================================
async def page_main(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await s.db.get_bool(KEY_ENABLED, True)
    filter_mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF) or MODE_OFF
    mode, working, allowed = await schedule_state(s)
    paused = len(await s.db.list_pauses())
    conns = await s.db.list_connections()
    active = sum(1 for c in conns if c["is_enabled"] and c["can_reply"])

    sched_line = f"زمان‌بندی: {html.escape(SCHEDULE_TITLES.get(mode, mode))}"
    if mode != MODE_OFF:
        sched_line += f" — اکنون {'ساعت کاری' if working else 'خارج از ساعت کاری'}"
    text = (
        "🤖 <b>پنل مدیریت دستیار هوشمند</b>\n\n"
        f"پاسخ‌دهی خودکار: {'✅ فعال' if enabled else '⛔ غیرفعال'}"
        f"{'' if allowed else ' (اما اکنون به‌خاطر زمان‌بندی ساکت است)'}\n"
        f"{sched_line}\n"
        f"فیلتر کاربران: {html.escape(MODE_TITLES.get(filter_mode, filter_mode))}\n"
        f"چت‌های ساکت‌شده: {paused}\n"
        f"اتصال‌های Business فعال: {active}\n"
        f"مدل: <code>{html.escape(s.config.ai_model)}</code>"
    )
    keyboard = kb(
        [("⛔ غیرفعال‌سازی" if enabled else "✅ فعال‌سازی", "act:toggle")],
        [("🧠 پرامپت", "page:prompt"), ("📚 دانش کسب‌وکار", "page:knowledge")],
        [("🕘 ساعت کاری", "page:schedule"), ("💬 پاسخ‌های سریع", "page:quick")],
        [("🚦 فیلتر کاربران", "page:lists"), ("🙋 چت‌های ساکت", "page:paused")],
        [("⚙️ تنظیمات پیشرفته", "page:settings"), ("📊 آمار", "page:stats")],
        [("📜 لاگ‌ها", "page:logs"), ("🔗 اتصال‌ها", "page:conns")],
        [("🔄 تازه‌سازی", "page:main")],
    )
    return text, keyboard


async def page_prompt(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    prompt = await s.db.get_setting(KEY_SYSTEM_PROMPT, s.config.default_system_prompt) or ""
    text = "🧠 <b>پرامپت فعلی سیستم:</b>\n\n<pre>" + html.escape(prompt[:MAX_PROMPT_LEN]) + "</pre>"
    return text, kb(
        [("✏️ ویرایش", "prompt:edit"), ("♻️ بازنشانی", "prompt:reset")], BACK
    )


async def page_knowledge(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    knowledge = await s.db.get_setting(KEY_KNOWLEDGE, "") or ""
    limit = s.config.knowledge_max_chars
    if knowledge:
        preview = html.escape(knowledge[:1500]) + ("…" if len(knowledge) > 1500 else "")
        body = f"<pre>{preview}</pre>"
    else:
        body = "— خالی —"
    text = (
        "📚 <b>دانش کسب‌وکار</b>\n"
        "اطلاعاتی مثل خدمات، قیمت‌ها، ساعت کاری، آدرس و سؤال‌های متداول. ربات هنگام پاسخ "
        "از این متن به‌عنوان منبع حقیقت استفاده می‌کند.\n\n"
        f"حجم فعلی: {len(knowledge)} از {limit} کاراکتر\n\n{body}\n\n"
        "💡 می‌توانید یک فایل <code>.txt</code> یا <code>.md</code> هم بفرستید."
    )
    return text, kb(
        [("✏️ جایگزینی", "kn:edit"), ("➕ افزودن", "kn:add")],
        [("🗑 پاک‌کردن", "kn:clear")],
        BACK,
    )


async def page_lists(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF) or MODE_OFF
    black = len(await s.db.get_list(LIST_BLACK))
    white = len(await s.db.get_list(LIST_WHITE))
    text = (
        "🚦 <b>فیلتر کاربران</b>\n\n"
        f"حالت فعلی: {html.escape(MODE_TITLES.get(mode, mode))}\n"
        f"لیست سیاه: {black} نفر — لیست سفید: {white} نفر\n\n"
        "کاربران با شناسه‌ی عددی تلگرام مشخص می‌شوند (در «لاگ‌ها» دیده می‌شود)."
    )
    return text, kb(
        [("🔁 تغییر حالت فیلتر", "list:mode")],
        [("⬛ نمایش سیاه", "list:show:bl"), ("⬜ نمایش سفید", "list:show:wl")],
        [("➕ سیاه", "list:add:bl"), ("➖ سیاه", "list:rm:bl")],
        [("➕ سفید", "list:add:wl"), ("➖ سفید", "list:rm:wl")],
        [("🔙 منوی اصلی", "page:main")],
    )


async def page_schedule(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    sched = await load_schedule(s)
    mode, working, allowed = await schedule_state(s)
    text = (
        "🕘 <b>ساعت کاری</b>\n\n"
        f"حالت: {html.escape(SCHEDULE_TITLES.get(mode, mode))}\n"
        f"ساعت کاری: {fmt_time(sched['start'])} تا {fmt_time(sched['end'])}\n"
        f"روزها: {html.escape(format_days(sched['days']))}\n"
        f"منطقه‌ی زمانی: <code>{html.escape(sched['tz'])}</code>\n"
        f"اکنون: {format_local_now(sched['tz'])} — "
        f"{'داخل ساعت کاری' if working else 'خارج از ساعت کاری'}\n"
        f"ربات اکنون {'پاسخ می‌دهد ✅' if allowed else 'ساکت است ⛔'}\n\n"
        "• «فقط در ساعت کاری»: مثلاً برای پاسخ‌گویی سازمانی.\n"
        "• «فقط خارج از ساعت کاری»: ربات وقتی شما دور هستید جواب می‌دهد."
    )
    return text, kb(
        [("🔁 تغییر حالت", "sch:mode")],
        [("⏰ ساعت‌ها", "sch:hours"), ("📅 روزها", "sch:days")],
        [("🌍 منطقه‌ی زمانی", "sch:tz")],
        BACK,
    )


async def page_quick(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    replies = await s.db.get_quick_replies()
    if replies:
        lines = [
            f"<b>{r['id']}.</b> <code>{html.escape(clip(r['keywords'], 40))}</code> ← "
            f"{html.escape(clip(r['response'], 60))}"
            for r in replies[:20]
        ]
        body = "\n".join(lines)
    else:
        body = "— هنوز پاسخ سریعی تعریف نشده —"
    text = (
        "💬 <b>پاسخ‌های سریع</b>\n"
        "اگر پیام کاربر شامل یکی از کلمات کلیدی باشد، بدون مصرف هوش مصنوعی همان پاسخ ثابت "
        "ارسال می‌شود.\n\n" + body
    )
    rows: list[list[tuple[str, str]]] = [[("➕ افزودن", "qr:add")]]
    dels = [(f"🗑 {r['id']}", f"qr:del:{r['id']}") for r in replies[:20]]
    for i in range(0, len(dels), 5):
        rows.append(dels[i : i + 5])
    rows.append(BACK)
    return text, kb(*rows)


async def page_settings(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    lines = []
    for name, (key, kind, _lo, _hi, title) in NUM_SETTINGS.items():
        default = {
            "cooldown": s.config.default_cooldown,
            "debounce": s.config.default_debounce,
            "history": s.config.default_history_size,
            "pause": s.config.default_pause_minutes,
            "rate": s.config.default_rate_limit,
        }[name]
        value = await (s.db.get_float(key, default) if kind is float else s.db.get_int(key, default))
        lines.append(f"• {title}: <b>{value:g}</b>")
    handoff_msg = await s.db.get_setting(KEY_HANDOFF_MESSAGE, s.config.handoff_message) or ""
    text = (
        "⚙️ <b>تنظیمات پیشرفته</b>\n\n"
        + "\n".join(lines)
        + f"\n• پیام تحویل به انسان: {html.escape(clip(handoff_msg, 80))}"
    )
    rows: list[list[tuple[str, str]]] = []
    toggles = []
    for name, (_key, title) in TOGGLES.items():
        on = await _toggle_value(s, name)
        toggles.append((f"{title}: {'✅' if on else '⛔'}", f"tog:{name}"))
    for i in range(0, len(toggles), 2):
        rows.append(toggles[i : i + 2])
    nums = [(title.split("(")[0].strip()[:28], f"set:{name}") for name, (*_x, title) in NUM_SETTINGS.items()]
    for i in range(0, len(nums), 2):
        rows.append(nums[i : i + 2])
    rows.append([("💬 متن تحویل به انسان", "set:handoffmsg")])
    rows.append(BACK)
    return text, kb(*rows)


async def page_paused(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    pauses = await s.db.list_pauses()
    reasons = {"owner": "پاسخ دستی شما", "handoff": "تحویل به انسان", "manual": "دستی (مدیر)"}
    if pauses:
        lines = [
            f"<b>{p['id']}.</b> <code>{p['chat_id']}</code> {html.escape(p['name'] or '')} — "
            f"{reasons.get(p['reason'], p['reason'] or '')} — تا {fmt_ts(p['paused_until'])} UTC"
            for p in pauses[:20]
        ]
        body = "\n".join(lines)
    else:
        body = "— هیچ چتی ساکت نیست —"
    text = (
        "🙋 <b>چت‌های ساکت‌شده</b>\n"
        "وقتی خودتان در چتی پاسخ می‌دهید یا ربات گفتگو را به شما تحویل می‌دهد، ربات تا مدتی "
        "در آن چت ساکت می‌ماند.\n\n" + body
    )
    rows: list[list[tuple[str, str]]] = [[("⏸ توقف دستی چت", "pz:add")]]
    if pauses:
        rows.append([("▶️ از سرگیری همه", "pz:all")])
    res = [(f"▶️ {p['id']}", f"pz:res:{p['id']}") for p in pauses[:20]]
    for i in range(0, len(res), 5):
        rows.append(res[i : i + 5])
    rows.append(BACK)
    return text, kb(*rows)


async def page_stats(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    tz = await s.db.get_setting(KEY_TIMEZONE, s.config.timezone) or s.config.timezone
    today = await s.db.get_stats(local_day_start_ts(tz))
    total = await s.db.get_stats(0)

    def block(st: dict[str, Any]) -> str:
        avg = st["avg_latency"]
        return (
            f"پاسخ هوش مصنوعی: {st['ok']} | سریع: {st['quick']} | تحویل به انسان: {st['handoff']}\n"
            f"خطا: {st['errors']} | چت‌های یکتا: {st['chats']}\n"
            f"میانگین تاخیر AI: {f'{avg / 1000:.1f}s' if avg else '—'}\n"
            f"توکن: ورودی {st['prompt_tokens']:,} / خروجی {st['completion_tokens']:,}"
        )

    text = (
        "📊 <b>آمار</b>\n\n<b>امروز</b>\n" + block(today)
        + "\n\n<b>کل لاگ‌های نگهداری‌شده</b>\n" + block(total)
    )
    return text, kb([("🔄 تازه‌سازی", "page:stats")], BACK)


async def page_logs(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    logs = await s.db.get_recent_logs(8)
    if not logs:
        body = "— هنوز لاگی ثبت نشده —"
    else:
        parts = []
        for row in logs:
            who = html.escape(row["full_name"] or row["username"] or "؟")
            parts.append(
                f"🕒 {fmt_ts(row['ts'])} | 👤 {who} (<code>{row['user_id']}</code>)\n"
                f"❓ {html.escape(clip(row['user_text'], 120))}\n"
                f"🤖 {html.escape(clip(row['bot_reply'], 160)) or '—'}\n"
                f"وضعیت: {html.escape(clip(row['status'], 60))}"
            )
        body = "\n\n".join(parts)
    text = "📜 <b>آخرین لاگ‌ها (UTC)</b>\n\n" + body
    return text, kb(
        [("📤 خروجی CSV", "logs:export"), ("🗑 پاک‌کردن", "logs:clear")], BACK
    )


async def page_conns(s: Services) -> tuple[str, InlineKeyboardMarkup]:
    connections = await s.db.list_connections()
    if not connections:
        body = "— هنوز اتصالی ثبت نشده. ربات را در Chat Automation تلگرام وصل کنید. —"
    else:
        lines = []
        for c in connections:
            state = "✅" if c["is_enabled"] and c["can_reply"] else "⛔"
            lines.append(
                f"{state} کاربر <code>{c['user_id']}</code> — "
                f"پاسخ‌دادن: {'بله' if c['can_reply'] else 'خیر'} — {fmt_ts(c['updated_at'])}"
            )
        body = "\n".join(lines)
    return "🔗 <b>اتصال‌های Business</b>\n\n" + body, kb(BACK)


PAGES: dict[str, Callable[[Services], Awaitable[tuple[str, InlineKeyboardMarkup]]]] = {
    "main": page_main,
    "prompt": page_prompt,
    "knowledge": page_knowledge,
    "lists": page_lists,
    "schedule": page_schedule,
    "quick": page_quick,
    "settings": page_settings,
    "paused": page_paused,
    "stats": page_stats,
    "logs": page_logs,
    "conns": page_conns,
}


async def _show_page(query: Any, s: Services, name: str, prefix: str = "") -> None:
    text, keyboard = await PAGES[name](s)
    await _edit(query, prefix + text, keyboard)


# ======================================================================
# دستورها
# ======================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: برای مدیر منوی مدیریت؛ برای بقیه یک توضیح کوتاه."""
    s = svc(context)
    message = update.effective_message
    if not is_admin(update, s):
        await message.reply_text(
            "👋 این ربات یک دستیار هوشمند است که از طریق قابلیت Chat Automation تلگرام "
            "به جای صاحب حساب به پیام‌ها پاسخ می‌دهد. پنل مدیریت فقط برای مدیر است."
        )
        return
    context.user_data.pop("awaiting", None)
    text, keyboard = await page_main(s)
    await message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel: لغو عملیات در حال انتظار (مثلاً ویرایش پرامپت)."""
    s = svc(context)
    if not is_admin(update, s):
        return
    context.user_data.pop("awaiting", None)
    text, keyboard = await page_main(s)
    await update.effective_message.reply_text(
        "❎ لغو شد.\n\n" + text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/forget <user_id>: حذف کامل پیام‌ها، لاگ‌ها و توقف‌های یک کاربر (حق فراموش‌شدن)."""
    s = svc(context)
    if not is_admin(update, s):
        return
    message = update.effective_message
    args = context.args or []
    if not args or not re.fullmatch(r"-?\d+", normalize_digits(args[0])):
        await message.reply_text("استفاده: /forget 123456789")
        return
    removed = await s.db.delete_user_data(int(normalize_digits(args[0])))
    await message.reply_text(f"🗑 {removed} رکورد مربوط به این کاربر حذف شد.")


# ======================================================================
# دکمه‌ها
# ======================================================================
def _ask(context: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
    context.user_data["awaiting"] = awaiting


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مسیریاب دکمه‌های شیشه‌ای پنل مدیریت."""
    s = svc(context)
    query = update.callback_query
    if not is_admin(update, s):
        await query.answer("دسترسی ندارید.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data.startswith("page:"):
        context.user_data.pop("awaiting", None)
        name = data[5:]
        if name in PAGES:
            await _show_page(query, s, name)
        return

    if data == "act:toggle":
        current = await s.db.get_bool(KEY_ENABLED, True)
        await s.db.set_setting(KEY_ENABLED, "0" if current else "1")
        await _show_page(query, s, "main")

    # ---------------- پرامپت ----------------
    elif data == "prompt:edit":
        _ask(context, "prompt")
        await _edit(
            query,
            "✏️ متن جدید پرامپت سیستم را بفرستید.\n"
            f"(حداکثر {MAX_PROMPT_LEN} کاراکتر — برای لغو /cancel)",
            CANCEL_KB,
        )
    elif data == "prompt:reset":
        await s.db.set_setting(KEY_SYSTEM_PROMPT, s.config.default_system_prompt)
        await _show_page(query, s, "prompt", "♻️ به مقدار پیش‌فرض بازنشانی شد.\n\n")

    # ---------------- دانش ----------------
    elif data == "kn:edit":
        _ask(context, "knowledge")
        await _edit(
            query,
            "📚 متن جدید «دانش کسب‌وکار» را بفرستید (جایگزین قبلی می‌شود) یا یک فایل "
            ".txt/.md آپلود کنید.\nبرای لغو /cancel",
            CANCEL_KB,
        )
    elif data == "kn:add":
        _ask(context, "knowledge_add")
        await _edit(
            query,
            "➕ متنی که باید به انتهای دانش اضافه شود را بفرستید (یا فایل .txt/.md).\nبرای لغو /cancel",
            CANCEL_KB,
        )
    elif data == "kn:clear":
        await s.db.set_setting(KEY_KNOWLEDGE, "")
        await _show_page(query, s, "knowledge", "🗑 دانش پاک شد.\n\n")

    # ---------------- فیلتر و لیست‌ها ----------------
    elif data == "list:mode":
        mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF) or MODE_OFF
        nxt = FILTER_CYCLE[(FILTER_CYCLE.index(mode) + 1) % 3] if mode in FILTER_CYCLE else MODE_OFF
        await s.db.set_setting(KEY_LIST_MODE, nxt)
        await _show_page(query, s, "lists")
    elif data.startswith("list:show:"):
        kind = KINDS.get(data.rsplit(":", 1)[1])
        if kind:
            ids = await s.db.get_list(kind)
            body = "\n".join(f"<code>{i}</code>" for i in ids[:100]) or "— خالی —"
            await _edit(
                query,
                f"📋 <b>{KIND_TITLES[kind]}</b> ({len(ids)} مورد)\n\n{body}",
                kb([("🔙 بازگشت", "page:lists")]),
            )
    elif data.startswith(("list:add:", "list:rm:")):
        _, action, code = data.split(":")
        if code in KINDS:
            _ask(context, f"list_{action}:{code}")
            verb = "اضافه شوند" if action == "add" else "حذف شوند"
            await _edit(
                query,
                f"شناسه‌های عددی کاربرانی که باید به «{KIND_TITLES[KINDS[code]]}» {verb} را "
                "بفرستید (با فاصله یا کاما جدا کنید).\nبرای لغو /cancel",
                CANCEL_KB,
            )

    # ---------------- ساعت کاری ----------------
    elif data == "sch:mode":
        mode = await s.db.get_setting(KEY_SCHEDULE_MODE, s.config.schedule_mode) or MODE_OFF
        order = list(SCHEDULE_MODES)
        nxt = order[(order.index(mode) + 1) % len(order)] if mode in order else MODE_OFF
        await s.db.set_setting(KEY_SCHEDULE_MODE, nxt)
        await _show_page(query, s, "schedule")
    elif data == "sch:hours":
        _ask(context, "sch_hours")
        await _edit(query, "⏰ ساعت کاری را به شکل <code>09:00-18:00</code> بفرستید.\n(بازه‌ی شبانه مثل 22:00-06:00 هم مجاز است — لغو: /cancel)", CANCEL_KB)
    elif data == "sch:days":
        _ask(context, "sch_days")
        await _edit(
            query,
            "📅 روزهای کاری را بفرستید؛ مثلاً:\n"
            "<code>شنبه یکشنبه دوشنبه سه‌شنبه چهارشنبه</code>\n"
            "یا <code>mon,tue,wed,thu,fri</code> یا <code>همه</code>\nلغو: /cancel",
            CANCEL_KB,
        )
    elif data == "sch:tz":
        _ask(context, "sch_tz")
        await _edit(query, "🌍 نام منطقه‌ی زمانی را بفرستید، مثلاً <code>Asia/Tehran</code> یا <code>UTC</code>.\nلغو: /cancel", CANCEL_KB)

    # ---------------- پاسخ‌های سریع ----------------
    elif data == "qr:add":
        _ask(context, "qr_add")
        await _edit(
            query,
            "💬 به این شکل بفرستید:\n<code>کلمه۱|کلمه۲ => متن پاسخ</code>\n\n"
            "مثال:\n<code>قیمت|تعرفه => تعرفه‌ها در سایت ما موجود است.</code>\nلغو: /cancel",
            CANCEL_KB,
        )
    elif data.startswith("qr:del:"):
        await s.db.delete_quick_reply(int(data.rsplit(":", 1)[1]))
        await _show_page(query, s, "quick")

    # ---------------- تنظیمات پیشرفته ----------------
    elif data.startswith("tog:"):
        name = data[4:]
        if name in TOGGLES:
            current = await _toggle_value(s, name)
            await s.db.set_setting(TOGGLES[name][0], "0" if current else "1")
        await _show_page(query, s, "settings")
    elif data.startswith("set:"):
        name = data[4:]
        if name == "handoffmsg":
            _ask(context, "set:handoffmsg")
            await _edit(query, "💬 متنی که هنگام تحویل گفتگو به شما برای مشتری فرستاده می‌شود را بفرستید.\nلغو: /cancel", CANCEL_KB)
        elif name in NUM_SETTINGS:
            _key, _kind, lo, hi, title = NUM_SETTINGS[name]
            _ask(context, f"set:{name}")
            await _edit(query, f"{html.escape(title)}\nعدد جدید را بفرستید (بین {lo:g} تا {hi:g}).\nلغو: /cancel", CANCEL_KB)

    # ---------------- چت‌های ساکت ----------------
    elif data == "pz:all":
        await s.db.clear_all_pauses()
        await _show_page(query, s, "paused")
    elif data.startswith("pz:res:"):
        await s.db.clear_pause_by_id(int(data.rsplit(":", 1)[1]))
        await _show_page(query, s, "paused")
    elif data == "pz:add":
        _ask(context, "pause_add")
        await _edit(query, "⏸ شناسه‌ی عددی کاربر(ها) را بفرستید تا ربات در چتشان ساکت شود.\nلغو: /cancel", CANCEL_KB)

    # ---------------- لاگ‌ها ----------------
    elif data == "logs:clear":
        removed = await s.db.clear_logs()
        await _show_page(query, s, "logs", f"🗑 {removed} ردیف لاگ حذف شد.\n\n")
    elif data == "logs:export":
        rows = await s.db.export_logs()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["time_utc", "user_id", "username", "full_name", "message", "reply", "status",
             "latency_ms", "prompt_tokens", "completion_tokens", "model"]
        )
        for r in rows:
            writer.writerow(
                [fmt_ts(r["ts"]), r["user_id"], r["username"], r["full_name"], r["user_text"],
                 r["bot_reply"], r["status"], r["latency_ms"], r["prompt_tokens"],
                 r["completion_tokens"], r["model"]]
            )
        data_bytes = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM برای اکسل
        if query.message is not None:
            await query.message.reply_document(document=data_bytes, filename="logs.csv")


# ======================================================================
# ورودی متنی / فایل مدیر
# ======================================================================
def _page_for(awaiting: str) -> str:
    if awaiting == "prompt":
        return "prompt"
    if awaiting.startswith("knowledge"):
        return "knowledge"
    if awaiting.startswith("list_"):
        return "lists"
    if awaiting.startswith("sch_"):
        return "schedule"
    if awaiting == "qr_add":
        return "quick"
    if awaiting == "pause_add":
        return "paused"
    if awaiting.startswith("set:"):
        return "settings"
    return "main"


async def _apply_knowledge(s: Services, awaiting: str, text: str) -> tuple[bool, str]:
    text = text.strip()
    if not text:
        return False, "❗ متن خالی است."
    limit = s.config.knowledge_max_chars
    if awaiting == "knowledge_add":
        current = await s.db.get_setting(KEY_KNOWLEDGE, "") or ""
        text = (current + "\n\n" + text).strip()
    if len(text) > limit:
        return False, f"❗ حجم دانش ({len(text)} کاراکتر) از سقف {limit} بیشتر است."
    await s.db.set_setting(KEY_KNOWLEDGE, text)
    return True, f"✅ دانش ذخیره شد ({len(text)} کاراکتر)."


async def _apply_input(
    s: Services, awaiting: str, text: str
) -> tuple[bool, str]:
    """ورودی مدیر را بر اساس «awaiting» اعمال می‌کند؛ (موفق؟، پیام) را برمی‌گرداند."""
    text = text.strip()

    if awaiting == "prompt":
        if not text or len(text) > MAX_PROMPT_LEN:
            return False, f"❗ طول پرامپت باید بین ۱ تا {MAX_PROMPT_LEN} کاراکتر باشد."
        await s.db.set_setting(KEY_SYSTEM_PROMPT, text)
        return True, "✅ پرامپت سیستم ذخیره شد."

    if awaiting in ("knowledge", "knowledge_add"):
        return await _apply_knowledge(s, awaiting, text)

    if awaiting.startswith("list_"):
        action, code = awaiting[len("list_"):].split(":")
        kind = KINDS[code]
        ids = [int(x) for x in re.findall(r"-?\d+", normalize_digits(text))]
        if not ids:
            return False, "❗ هیچ شناسه‌ی عددی پیدا نشد. دوباره بفرستید یا /cancel بزنید."
        changed = 0
        for uid in ids:
            if action == "add":
                changed += int(await s.db.add_to_list(uid, kind))
            else:
                changed += int(await s.db.remove_from_list(uid, kind))
        verb = "اضافه" if action == "add" else "حذف"
        return True, f"✅ {changed} کاربر {verb} شد ({KIND_TITLES[kind]})."

    if awaiting == "sch_hours":
        hours = parse_hours(text)
        if hours is None:
            return False, "❗ قالب نامعتبر است. نمونه: 09:00-18:00"
        await s.db.set_setting(KEY_WORK_START, fmt_time(hours[0]))
        await s.db.set_setting(KEY_WORK_END, fmt_time(hours[1]))
        return True, f"✅ ساعت کاری روی {fmt_time(hours[0])} تا {fmt_time(hours[1])} تنظیم شد."

    if awaiting == "sch_days":
        days = parse_days(text)
        if days is None:
            return False, "❗ هیچ روزی تشخیص داده نشد. نمونه: شنبه یکشنبه دوشنبه یا mon,tue یا همه"
        await s.db.set_setting(KEY_WORK_DAYS, days_to_str(days))
        return True, f"✅ روزهای کاری: {format_days(days)}"

    if awaiting == "sch_tz":
        if not is_valid_tz(text):
            return False, "❗ منطقه‌ی زمانی نامعتبر است. نمونه: Asia/Tehran یا UTC"
        await s.db.set_setting(KEY_TIMEZONE, text)
        return True, f"✅ منطقه‌ی زمانی روی {text} تنظیم شد."

    if awaiting == "qr_add":
        if "=>" not in text:
            return False, "❗ قالب نادرست است. نمونه: کلمه۱|کلمه۲ => متن پاسخ"
        left, right = (p.strip() for p in text.split("=>", 1))
        keywords = "|".join(k.strip() for k in left.split("|") if k.strip())
        if not keywords or not right:
            return False, "❗ هم کلمه‌ی کلیدی و هم متن پاسخ لازم است."
        if len(right) > 3500:
            return False, "❗ متن پاسخ بیش‌ازحد طولانی است."
        await s.db.add_quick_reply(keywords, right)
        return True, "✅ پاسخ سریع اضافه شد."

    if awaiting == "pause_add":
        ids = [int(x) for x in re.findall(r"-?\d+", normalize_digits(text))]
        if not ids:
            return False, "❗ هیچ شناسه‌ی عددی پیدا نشد."
        conns = await s.db.list_connections()
        if not conns:
            return False, "❗ هنوز هیچ اتصال Business ثبت نشده است."
        minutes = await s.db.get_int(KEY_PAUSE_MINUTES, s.config.default_pause_minutes) or 60
        until = int(time.time() + minutes * 60)
        for uid in ids:
            for c in conns:
                await s.db.set_pause(c["connection_id"], uid, until, "manual")
        return True, f"✅ ربات برای {len(ids)} کاربر تا {minutes} دقیقه ساکت شد."

    if awaiting == "set:handoffmsg":
        if not text or len(text) > 500:
            return False, "❗ متن باید بین ۱ تا ۵۰۰ کاراکتر باشد."
        await s.db.set_setting(KEY_HANDOFF_MESSAGE, text)
        return True, "✅ متن تحویل به انسان ذخیره شد."

    if awaiting.startswith("set:"):
        name = awaiting[4:]
        if name not in NUM_SETTINGS:
            return False, "❗ تنظیم نامشخص."
        key, kind, lo, hi, _title = NUM_SETTINGS[name]
        try:
            value = float(normalize_digits(text).replace(",", ".").replace("٫", "."))
        except ValueError:
            return False, "❗ لطفاً یک عدد بفرستید، مثلاً 3 یا 2.5"
        if not lo <= value <= hi:
            return False, f"❗ عدد باید بین {lo:g} تا {hi:g} باشد."
        await s.db.set_setting(key, str(int(value)) if kind is int else str(value))
        return True, "✅ ذخیره شد."

    return False, "❗ عملیات نامشخص."


async def _finish_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, s: Services, awaiting: str, ok: bool, msg: str
) -> None:
    """پس از اعمال ورودی: در صورت موفقیت، به صفحه‌ی مرتبط برمی‌گردد؛ وگرنه فقط خطا را نشان می‌دهد."""
    message = update.effective_message
    if not ok:
        await message.reply_text(msg)
        return
    context.user_data.pop("awaiting", None)
    text, keyboard = await PAGES[_page_for(awaiting)](s)
    await message.reply_text(f"{msg}\n\n{text}", reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def on_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دریافت ورودی متنی مدیر پس از زدن دکمه‌ای که منتظر متن است."""
    s = svc(context)
    if not is_admin(update, s):
        return
    message = update.effective_message
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        await message.reply_text("برای باز کردن پنل مدیریت دستور /start را ارسال کنید.")
        return
    ok, msg = await _apply_input(s, awaiting, message.text or "")
    await _finish_input(update, context, s, awaiting, ok, msg)


async def on_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """آپلود فایل متنی (.txt/.md) به‌عنوان «دانش کسب‌وکار»."""
    s = svc(context)
    if not is_admin(update, s):
        return
    message = update.effective_message
    awaiting = context.user_data.get("awaiting")
    if awaiting not in ("knowledge", "knowledge_add"):
        await message.reply_text(
            "برای آپلود فایل دانش، ابتدا از پنل: «📚 دانش کسب‌وکار» ← «جایگزینی/افزودن» را بزنید."
        )
        return
    doc = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".txt", ".md")):
        await message.reply_text("❗ فقط فایل‌های .txt و .md پذیرفته می‌شوند.")
        return
    if doc.file_size and doc.file_size > 1_000_000:
        await message.reply_text("❗ حجم فایل بیش از ۱ مگابایت است.")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    raw = bytes(await tg_file.download_as_bytearray())
    ok, msg = await _apply_knowledge(s, awaiting, raw.decode("utf-8", errors="replace"))
    await _finish_input(update, context, s, awaiting, ok, msg)
