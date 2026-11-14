"""
commands.py — پنل مدیریت متنی (جایگزین پنل دکمه‌ای ربات).

در حساب شخصی تلگرام امکان «دکمه‌ی شیشه‌ای (Inline Keyboard)» وجود ندارد؛ پس همان صفحه‌ها و
تنظیمات پنل قبلی به دستورهای متنی تبدیل شده‌اند. دستورها را فقط در گفتگوی «Saved Messages»
(پیام‌های ذخیره‌شده) خودتان بنویسید؛ این گفتگو فقط برای خود شماست و هیچ‌کس دیگری نمی‌بیند.
شروع همه‌ی دستورها با `/ai` است؛ `/ai help` فهرست کامل را نشان می‌دهد.

(هیچ دستوری در گفتگو با دیگران پردازش نمی‌شود.)
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any, Awaitable, Callable

from database import (
    ACCOUNT,
    CHAT_OVERRIDE_KEYS,
    KEY_COOLDOWN,
    KEY_DEBOUNCE,
    KEY_ENABLED,
    KEY_HANDOFF,
    KEY_HANDOFF_MESSAGE,
    KEY_HISTORY_SIZE,
    KEY_KNOWLEDGE,
    KEY_LIST_MODE,
    KEY_MARK_READ,
    KEY_MAX_MEDIA_MB,
    KEY_MAX_STREAK,
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
from engine import Engine
from media import MediaTooLarge, _download_bytes
from schedule import (
    MODE_INSIDE,
    MODE_OUTSIDE,
    days_to_str,
    format_days,
    format_local_now,
    is_valid_tz,
    local_day_start_ts,
    normalize_digits,
    parse_days,
    parse_hours,
)
from services import Services, clip, fmt_ts, load_schedule, schedule_state
from tglayer.models import GatewayError, OwnerMessage

logger = logging.getLogger(__name__)

PREFIX = "/ai"

HELP_TEXT = """🤖 دستورهای دستیار (فقط در «Saved Messages» بنویسید)

▫️ کلی
/ai status — وضعیت کلی
/ai on | /ai off — روشن/خاموش‌کردن کل دستیار

▫️ پرامپت و دانش
/ai prompt show | set <متن> | reset
/ai knowledge show | set <متن> | add <متن> | clear
(یک فایل txt/md را با کپشن «/ai knowledge set» یا «/ai knowledge add» بفرستید)

▫️ کنترل دسترسی
/ai filter off | black | white
/ai black list | add <شناسه…> | remove <شناسه…>
/ai white list | add <شناسه…> | remove <شناسه…>

▫️ ساعت کاری
/ai schedule off | inside | outside
/ai hours 09:00-18:00
/ai days mon,tue,wed,thu,fri  (یا all)
/ai tz Asia/Tehran

▫️ پاسخ سریع
/ai quick list
/ai quick add سلام|درود => سلام! چطور کمک کنم؟
/ai quick del <شماره>

▫️ تنظیمات عددی
/ai set cooldown|debounce|history|pause|rate|streak|media <عدد>
/ai toggle vision|voice|handoff|markread on|off
/ai handoffmsg <متن>

▫️ گفتگوها
/ai chats — گفتگوهای اخیر
/ai paused — گفتگوهای ساکت‌شده
/ai pause <گفتگو> [دقیقه] — ساکت‌کردن دستی
/ai resume <گفتگو|all> — فعال‌سازی دوباره
/ai chat <گفتگو> info | on | off | default
/ai chat <گفتگو> prompt <متن> | prompt clear
/ai chat <گفتگو> set <history|cooldown|debounce|rate|pause|streak> <عدد>
/ai chat <گفتگو> unset <کلید> | forget
(«گفتگو» = شناسه‌ی عددی یا @username)

▫️ گزارش
/ai stats — آمار
/ai logs [تعداد] — آخرین لاگ‌ها
/ai csv — خروجی CSV
/ai clearlogs — پاک‌کردن لاگ‌ها
/ai forget <گفتگو> — حذف تاریخچه‌ی یک نفر"""

_NUMERIC_SETTINGS: dict[str, tuple[str, type, float, float, str]] = {
    "cooldown": (KEY_COOLDOWN, float, 0, 300, "ثانیه فاصله‌ی حداقلی بین پاسخ‌ها"),
    "debounce": (KEY_DEBOUNCE, float, 0, 30, "ثانیه انتظار برای جمع‌کردن پیام‌های پشت‌سرهم"),
    "history": (KEY_HISTORY_SIZE, int, 0, 40, "تعداد پیام‌های تاریخچه برای مدل"),
    "pause": (KEY_PAUSE_MINUTES, int, 0, 10080, "دقیقه توقف بعد از دخالت شما (۰ = بدون توقف)"),
    "rate": (KEY_RATE_LIMIT, int, 0, 1000, "حداکثر پاسخ در ساعت برای هر گفتگو (۰ = نامحدود)"),
    "streak": (KEY_MAX_STREAK, int, 0, 1000, "حداکثر پاسخ متوالی دستیار (۰ = نامحدود)"),
    "media": (KEY_MAX_MEDIA_MB, int, 1, 100, "مگابایت سقف حجم رسانه"),
}
_CHAT_KEY_ALIASES = {
    "history": "history_size", "cooldown": "cooldown_seconds", "debounce": "debounce_seconds",
    "rate": "rate_limit_per_hour", "pause": "pause_minutes", "streak": "max_consecutive_replies",
}
_TOGGLES = {
    "vision": (KEY_VISION, "تحلیل تصویر"),
    "voice": (KEY_VOICE, "تبدیل ویس به متن"),
    "handoff": (KEY_HANDOFF, "تحویل به انسان"),
    "markread": (KEY_MARK_READ, "علامت‌گذاری «خوانده‌شده»"),
}
_ON = {"on", "1", "true", "yes", "روشن", "فعال"}
_OFF = {"off", "0", "false", "no", "خاموش", "غیرفعال"}


def _parse_toggle(raw: str) -> bool | None:
    raw = raw.strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    return None


def _split(text: str) -> tuple[str, str, str]:
    """مثل str.partition ولی روی هر فاصله‌ی سفید (از جمله خط جدید)؛ متن چندخطی سالم می‌ماند."""
    parts = text.strip().split(None, 1)
    if not parts:
        return "", "", ""
    return parts[0], " " if len(parts) > 1 else "", parts[1] if len(parts) > 1 else ""


def _csv_safe(value: Any) -> Any:
    """جلوگیری از «تزریق فرمول» در اکسل: سلول‌هایی که با = + - @ شروع شوند خنثی می‌شوند."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


class CommandHandler:
    def __init__(self, services: Services, engine: Engine) -> None:
        self.s = services
        self.engine = engine
        self._table: dict[str, Callable[[str, OwnerMessage], Awaitable[str | None]]] = {
            "": self._cmd_status, "help": self._cmd_help, "status": self._cmd_status,
            "on": self._cmd_on, "off": self._cmd_off,
            "prompt": self._cmd_prompt, "knowledge": self._cmd_knowledge,
            "filter": self._cmd_filter, "black": self._cmd_black, "white": self._cmd_white,
            "schedule": self._cmd_schedule, "hours": self._cmd_hours, "days": self._cmd_days,
            "tz": self._cmd_tz, "quick": self._cmd_quick, "set": self._cmd_set,
            "toggle": self._cmd_toggle, "handoffmsg": self._cmd_handoffmsg,
            "chats": self._cmd_chats, "paused": self._cmd_paused, "pause": self._cmd_pause,
            "resume": self._cmd_resume, "chat": self._cmd_chat, "stats": self._cmd_stats,
            "logs": self._cmd_logs, "csv": self._cmd_csv, "clearlogs": self._cmd_clearlogs,
            "forget": self._cmd_forget,
        }

    # ------------------------------------------------------------------
    async def handle(self, msg: OwnerMessage) -> bool:
        """اگر پیام یک دستور باشد پردازش می‌کند و True برمی‌گرداند."""
        text = normalize_digits((msg.text or "").strip())
        if not (text == PREFIX or text.startswith(PREFIX + " ") or text.startswith(PREFIX + "\n")):
            return False
        name, _, args = _split(text[len(PREFIX):])
        name = name.lower()
        handler = self._table.get(name)
        if handler is None:
            await self.engine.send_to_self(f"❓ دستور «{name}» را نمی‌شناسم. /ai help")
            return True
        try:
            reply = await handler(args.strip(), msg)
        except Exception:  # noqa: BLE001
            logger.exception("Command %r failed", name)
            reply = "⚠️ اجرای دستور با خطا مواجه شد (جزئیات در لاگ برنامه)."
        if reply:
            await self.engine.send_to_self(reply)
        return True

    # ------------------------------------------------------------------
    # کمکی‌ها
    # ------------------------------------------------------------------
    async def _resolve_chat(self, ref: str) -> tuple[int | None, str]:
        ref = normalize_digits(ref.strip())
        if not ref:
            return None, "شناسه‌ی گفتگو را بدهید (عدد یا @username)."
        if ref.lstrip("-").isdigit():
            return int(ref), ""
        if ref.startswith("@"):
            row = await self.s.db.find_chat_by_username(ref)
            if row:
                return int(row["chat_id"]), ""
            if self.s.gateway is not None:
                info = await self.s.gateway.resolve_chat(ref)
                if info:
                    return info.chat_id, ""
            return None, f"{ref} پیدا نشد."
        return None, "قالب شناسه نامعتبر است؛ عدد یا @username بدهید."

    async def _chat_label(self, chat_id: int) -> str:
        row = await self.s.db.get_chat(chat_id)
        if row and row["name"]:
            handle = f" @{row['username']}" if row["username"] else ""
            return f"{row['name']}{handle} ({chat_id})"
        return str(chat_id)

    async def _ids(self, args: str) -> tuple[list[int], list[str]]:
        ids, bad = [], []
        for token in args.replace(",", " ").replace("،", " ").split():
            cid, err = await self._resolve_chat(token)
            (ids if cid is not None else bad).append(cid if cid is not None else token)
        return ids, bad

    # ------------------------------------------------------------------
    # کلی
    # ------------------------------------------------------------------
    async def _cmd_help(self, args: str, msg: OwnerMessage) -> str:
        return HELP_TEXT

    async def _cmd_status(self, args: str, msg: OwnerMessage) -> str:
        s, cfg = self.s, self.s.config
        enabled = await s.db.get_bool(KEY_ENABLED, True)
        mode = await s.db.get_setting(KEY_LIST_MODE, MODE_OFF)
        mode_fa = {"off": "همه‌ی گفتگوهای خصوصی", LIST_BLACK: "همه به‌جز لیست سیاه",
                   LIST_WHITE: "فقط لیست سفید"}.get(mode or "", mode)
        sched_mode, working, allowed = await schedule_state(s)
        pauses = await s.db.list_pauses()
        sched = await load_schedule(s)
        knowledge = await s.db.get_setting(KEY_KNOWLEDGE, "") or ""
        quick = len(await s.db.get_quick_replies())
        return (
            "🤖 وضعیت دستیار\n\n"
            f"وضعیت: {'🟢 روشن' if enabled else '🔴 خاموش'}\n"
            f"حساب: {s.me.name if s.me else '—'} ({s.me.user_id if s.me else '—'})\n"
            f"مدل: {cfg.ai_model}" + (f" (جایگزین: {cfg.ai_fallback_model})" if cfg.ai_fallback_model else "") + "\n"
            f"دسترسی: {mode_fa}\n"
            f"ساعت کاری: {sched_mode} | الان {'در' if working else 'خارج از'} ساعت کاری "
            f"→ {'پاسخ می‌دهد' if allowed else 'پاسخ نمی‌دهد'}\n"
            f"({sched['start']:%H:%M}-{sched['end']:%H:%M}, {format_days(sched['days'])}, {sched['tz']})\n"
            f"گفتگوهای ساکت: {len(pauses)} | پاسخ سریع: {quick} | دانش: {len(knowledge)} نویسه\n"
            f"زمان محلی: {format_local_now(sched['tz'])}\n\n"
            "راهنما: /ai help"
        )

    async def _cmd_on(self, args: str, msg: OwnerMessage) -> str:
        await self.s.db.set_setting(KEY_ENABLED, "1")
        return "🟢 دستیار روشن شد."

    async def _cmd_off(self, args: str, msg: OwnerMessage) -> str:
        await self.s.db.set_setting(KEY_ENABLED, "0")
        # پاسخ‌های در حال ساخت هم قبل از ارسال با کنترل KEY_ENABLED لغو می‌شوند
        return "🔴 دستیار خاموش شد؛ تا وقتی /ai on نزنید پاسخی ارسال نمی‌شود."

    # ------------------------------------------------------------------
    # پرامپت و دانش
    # ------------------------------------------------------------------
    async def _cmd_prompt(self, args: str, msg: OwnerMessage) -> str:
        sub, _, text = _split(args)
        sub = sub.lower()
        cfg = self.s.config
        if sub in ("", "show"):
            current = await self.s.db.get_setting(KEY_SYSTEM_PROMPT, cfg.default_system_prompt) or ""
            return f"📝 پرامپت فعلی:\n\n{current}"
        if sub == "set":
            if not text.strip():
                return "متن پرامپت را بعد از set بنویسید."
            if len(text) > 8000:
                return "پرامپت بیش‌ازحد طولانی است (حداکثر ۸۰۰۰ نویسه)."
            await self.s.db.set_setting(KEY_SYSTEM_PROMPT, text.strip())
            return f"✅ پرامپت ذخیره شد ({len(text.strip())} نویسه)."
        if sub == "reset":
            await self.s.db.set_setting(KEY_SYSTEM_PROMPT, cfg.default_system_prompt)
            return "✅ پرامپت به پیش‌فرض برگشت."
        return "استفاده: /ai prompt show | set <متن> | reset"

    async def _apply_knowledge(self, mode: str, text: str) -> str:
        limit = self.s.config.knowledge_max_chars
        text = text.strip()
        if mode == "add":
            old = (await self.s.db.get_setting(KEY_KNOWLEDGE, "") or "").strip()
            text = f"{old}\n\n{text}".strip() if old else text
        if len(text) > limit:
            return f"❌ حجم دانش ({len(text)} نویسه) از سقف {limit} بیشتر است؛ چیزی ذخیره نشد."
        await self.s.db.set_setting(KEY_KNOWLEDGE, text)
        return f"✅ دانش ذخیره شد ({len(text)} از {limit} نویسه)."

    async def _cmd_knowledge(self, args: str, msg: OwnerMessage) -> str:
        sub, _, text = _split(args)
        sub = sub.lower()
        if sub in ("", "show"):
            k = (await self.s.db.get_setting(KEY_KNOWLEDGE, "") or "").strip()
            return f"📚 دانش اختصاصی ({len(k)} نویسه):\n\n{k}" if k else "📚 دانشی تعریف نشده است."
        if sub == "clear":
            await self.s.db.set_setting(KEY_KNOWLEDGE, "")
            return "🗑 دانش پاک شد."
        if sub in ("set", "add"):
            if msg.media is not None:  # فایل ضمیمه
                return await self._knowledge_from_file(sub, msg)
            if not text.strip():
                return "متن را بعد از دستور بنویسید یا یک فایل txt/md با این کپشن بفرستید."
            return await self._apply_knowledge(sub, text)
        return "استفاده: /ai knowledge show | set <متن> | add <متن> | clear"

    async def _knowledge_from_file(self, mode: str, msg: OwnerMessage) -> str:
        media = msg.media
        assert media is not None
        name = (media.file_name or "").lower()
        if not (name.endswith((".txt", ".md")) or (media.mime or "").startswith("text/")):
            return "فقط فایل متنی (txt یا md) پذیرفته می‌شود."
        limit_bytes = self.s.config.knowledge_max_chars * 4 + 1024
        try:
            data = await _download_bytes(self.s, media, limit_bytes)
        except MediaTooLarge:
            return "❌ فایل بزرگ‌تر از حد مجاز است."
        except GatewayError as exc:
            return f"❌ دانلود فایل ناموفق بود: {clip(str(exc), 120)}"
        return await self._apply_knowledge(mode, data.decode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # کنترل دسترسی
    # ------------------------------------------------------------------
    async def _cmd_filter(self, args: str, msg: OwnerMessage) -> str:
        mapping = {"off": MODE_OFF, "black": LIST_BLACK, "white": LIST_WHITE}
        choice = args.strip().lower()
        if choice not in mapping:
            cur = await self.s.db.get_setting(KEY_LIST_MODE, MODE_OFF)
            return f"حالت فعلی: {cur}\nاستفاده: /ai filter off | black | white"
        await self.s.db.set_setting(KEY_LIST_MODE, mapping[choice])
        return f"✅ حالت دسترسی: {choice}"

    async def _list_cmd(self, kind: str, title: str, args: str) -> str:
        sub, _, rest = _split(args)
        sub = sub.lower()
        if sub in ("", "list"):
            ids = await self.s.db.get_list(kind)
            if not ids:
                return f"{title}: خالی است."
            lines = [await self._chat_label(i) for i in ids]
            return f"{title} ({len(ids)}):\n" + "\n".join(f"• {x}" for x in lines)
        if sub in ("add", "remove"):
            ids, bad = await self._ids(rest)
            if not ids and not bad:
                return "شناسه‌ها را بعد از دستور بنویسید."
            changed = 0
            for cid in ids:
                ok = await (self.s.db.add_to_list(cid, kind) if sub == "add" else self.s.db.remove_from_list(cid, kind))
                changed += int(bool(ok))
            out = f"✅ {changed} مورد {'اضافه' if sub == 'add' else 'حذف'} شد."
            if bad:
                out += f"\nنامعتبر/ناشناخته: {', '.join(map(str, bad))}"
            return out
        return f"استفاده: /ai {'black' if kind == LIST_BLACK else 'white'} list | add <شناسه…> | remove <شناسه…>"

    async def _cmd_black(self, args: str, msg: OwnerMessage) -> str:
        return await self._list_cmd(LIST_BLACK, "⛔ لیست سیاه", args)

    async def _cmd_white(self, args: str, msg: OwnerMessage) -> str:
        return await self._list_cmd(LIST_WHITE, "✅ لیست سفید", args)

    # ------------------------------------------------------------------
    # ساعت کاری
    # ------------------------------------------------------------------
    async def _cmd_schedule(self, args: str, msg: OwnerMessage) -> str:
        choice = args.strip().lower()
        if choice not in (MODE_OFF, MODE_INSIDE, MODE_OUTSIDE):
            sched = await load_schedule(self.s)
            return (
                f"حالت فعلی: {sched['mode']}\n"
                "off = همیشه پاسخ بده | inside = فقط داخل ساعت کاری | outside = فقط خارج از ساعت کاری\n"
                "استفاده: /ai schedule off | inside | outside"
            )
        await self.s.db.set_setting(KEY_SCHEDULE_MODE, choice)
        return f"✅ حالت زمان‌بندی: {choice}"

    async def _cmd_hours(self, args: str, msg: OwnerMessage) -> str:
        parsed = parse_hours(args)
        if parsed is None:
            return "قالب نامعتبر. مثال: /ai hours 09:00-18:00"
        await self.s.db.set_setting(KEY_WORK_START, f"{parsed[0]:%H:%M}")
        await self.s.db.set_setting(KEY_WORK_END, f"{parsed[1]:%H:%M}")
        return f"✅ ساعت کاری: {parsed[0]:%H:%M} تا {parsed[1]:%H:%M}"

    async def _cmd_days(self, args: str, msg: OwnerMessage) -> str:
        days = parse_days(args)
        if days is None:
            return "قالب نامعتبر. مثال: /ai days mon,tue,wed,thu,fri  یا  /ai days all"
        await self.s.db.set_setting(KEY_WORK_DAYS, days_to_str(days))
        return f"✅ روزهای کاری: {format_days(days)}"

    async def _cmd_tz(self, args: str, msg: OwnerMessage) -> str:
        name = args.strip()
        if not is_valid_tz(name):
            return "منطقه‌ی زمانی نامعتبر. مثال: /ai tz Asia/Tehran"
        await self.s.db.set_setting(KEY_TIMEZONE, name)
        return f"✅ منطقه‌ی زمانی: {name}\nالان: {format_local_now(name)}"

    # ------------------------------------------------------------------
    # پاسخ سریع
    # ------------------------------------------------------------------
    async def _cmd_quick(self, args: str, msg: OwnerMessage) -> str:
        sub, _, rest = _split(args)
        sub = sub.lower()
        if sub in ("", "list"):
            rows = await self.s.db.get_quick_replies()
            if not rows:
                return "⚡ پاسخ سریعی تعریف نشده است."
            return "⚡ پاسخ‌های سریع:\n" + "\n".join(
                f"{r['id']}. [{r['keywords']}] ⟵ {clip(r['response'], 80)}" for r in rows
            )
        if sub == "add":
            # چند پاسخ در یک پیام: هر خط «کلمات => پاسخ»
            lines = [ln.strip() for ln in rest.splitlines() if ln.strip()]
            lines = [
                ln[len("/ai quick add"):].strip() if ln.lower().startswith("/ai quick add") else ln
                for ln in lines
            ]
            if sum(1 for ln in lines if "=>" in ln) >= 2:
                added = bad = 0
                for ln in lines:
                    kws, sep, resp = ln.partition("=>")
                    kws = "|".join(k.strip() for k in kws.split("|") if k.strip())
                    if not sep or not kws or not resp.strip():
                        bad += 1
                        continue
                    await self.s.db.add_quick_reply(kws, resp.strip())
                    added += 1
                return f"✅ {added} پاسخ سریع اضافه شد." + (f" ({bad} خط نامعتبر نادیده گرفته شد)" if bad else "")
            if "=>" not in rest:
                return "قالب: /ai quick add کلمه۱|کلمه۲ => متن پاسخ"
            kws, _, resp = rest.partition("=>")
            kws = "|".join(k.strip() for k in kws.split("|") if k.strip())
            if not kws or not resp.strip():
                return "هم کلمه‌ی کلیدی و هم متن پاسخ لازم است."
            await self.s.db.add_quick_reply(kws, resp.strip())
            return "✅ پاسخ سریع اضافه شد."
        if sub in ("del", "delete", "remove"):
            digits = normalize_digits(rest.strip())
            if not digits.isdigit():
                return "شماره‌ی پاسخ را بدهید: /ai quick del 3"
            return "🗑 حذف شد." if await self.s.db.delete_quick_reply(int(digits)) else "چنین شماره‌ای نیست."
        return "استفاده: /ai quick list | add … | del <شماره>"

    # ------------------------------------------------------------------
    # تنظیمات
    # ------------------------------------------------------------------
    async def _cmd_set(self, args: str, msg: OwnerMessage) -> str:
        name, _, raw = _split(args)
        spec = _NUMERIC_SETTINGS.get(name.lower())
        if spec is None:
            return "استفاده: /ai set <" + "|".join(_NUMERIC_SETTINGS) + "> <عدد>"
        key, typ, lo, hi, desc = spec
        try:
            value = typ(normalize_digits(raw.strip()))
        except ValueError:
            return f"مقدار باید عدد باشد ({desc})."
        if not lo <= value <= hi:
            return f"مقدار باید بین {lo:g} و {hi:g} باشد ({desc})."
        await self.s.db.set_setting(key, str(value))
        return f"✅ {name} = {value:g} — {desc}"

    async def _cmd_toggle(self, args: str, msg: OwnerMessage) -> str:
        name, _, raw = _split(args)
        spec = _TOGGLES.get(name.lower())
        state = _parse_toggle(raw)
        if spec is None or state is None:
            return "استفاده: /ai toggle <" + "|".join(_TOGGLES) + "> on|off"
        await self.s.db.set_setting(spec[0], "1" if state else "0")
        return f"✅ {spec[1]}: {'روشن' if state else 'خاموش'}"

    async def _cmd_handoffmsg(self, args: str, msg: OwnerMessage) -> str:
        if not args.strip():
            cur = await self.s.db.get_setting(KEY_HANDOFF_MESSAGE, self.s.config.handoff_message)
            return f"پیام تحویل به انسان:\n{cur}"
        await self.s.db.set_setting(KEY_HANDOFF_MESSAGE, args.strip())
        return "✅ پیام تحویل به انسان ذخیره شد."

    # ------------------------------------------------------------------
    # گفتگوها
    # ------------------------------------------------------------------
    async def _cmd_chats(self, args: str, msg: OwnerMessage) -> str:
        rows = await self.s.db.list_chats(20)
        if not rows:
            return "هنوز گفتگویی دیده نشده است."
        lines = []
        for r in rows:
            cs = await self.s.db.get_chat_settings(r["chat_id"])
            flag = "" if cs["ai_enabled"] is None else (" ✅" if cs["ai_enabled"] else " 🚫")
            paused = " ⏸" if await self.s.db.get_pause_until(ACCOUNT, r["chat_id"]) else ""
            lines.append(f"• {await self._chat_label(r['chat_id'])}{flag}{paused} — {fmt_ts(r['last_activity'])}")
        return "💬 گفتگوهای اخیر (UTC):\n" + "\n".join(lines)

    async def _cmd_paused(self, args: str, msg: OwnerMessage) -> str:
        rows = await self.s.db.list_pauses()
        if not rows:
            return "هیچ گفتگوی ساکت‌شده‌ای نیست."
        now = time.time()
        reasons = {"owner": "دخالت شما", "handoff": "تحویل به انسان", "limit": "سقف پاسخ متوالی", "manual": "دستی"}
        lines = [
            f"• {await self._chat_label(r['chat_id'])} — {reasons.get(r['reason'], r['reason'])} — "
            f"{max(1, int((r['paused_until'] - now) / 60))} دقیقه‌ی دیگر"
            for r in rows
        ]
        return "⏸ گفتگوهای ساکت‌شده:\n" + "\n".join(lines)

    async def _cmd_pause(self, args: str, msg: OwnerMessage) -> str:
        ref, _, mins = _split(args)
        cid, err = await self._resolve_chat(ref)
        if cid is None:
            return err
        digits = normalize_digits(mins.strip())
        minutes = int(digits) if digits.isdigit() else (
            await self.s.db.get_int(KEY_PAUSE_MINUTES, self.s.config.default_pause_minutes) or 60
        )
        rt = self.s.runtime(cid)
        async with rt.send_lock:
            rt.epoch += 1  # پاسخ‌های در حال ساخت لغو شوند
            for task in list(rt.tasks):
                task.cancel()
            row = await self.s.db.get_chat(cid)
            await self.s.db.set_pause(ACCOUNT, cid, int(time.time() + minutes * 60), "manual", row["name"] if row else None)
        return f"⏸ دستیار در گفتگوی {await self._chat_label(cid)} به مدت {minutes} دقیقه ساکت شد."

    async def _cmd_resume(self, args: str, msg: OwnerMessage) -> str:
        if args.strip().lower() == "all":
            n = await self.s.db.clear_all_pauses()
            return f"▶️ توقف {n} گفتگو برداشته شد."
        cid, err = await self._resolve_chat(args)
        if cid is None:
            return err
        removed = await self.s.db.clear_pause_for_chat(cid)
        return f"▶️ دستیار در {await self._chat_label(cid)} دوباره فعال شد." if removed else "این گفتگو ساکت نبود."

    async def _cmd_chat(self, args: str, msg: OwnerMessage) -> str:
        ref, _, rest = _split(args)
        cid, err = await self._resolve_chat(ref)
        if cid is None:
            return err
        sub, _, val = _split(rest.strip())
        sub, val = sub.lower(), val.strip()
        db = self.s.db
        label = await self._chat_label(cid)

        if sub in ("", "info"):
            cs = await db.get_chat_settings(cid)
            pause = await db.get_pause_until(ACCOUNT, cid)
            state = {None: "پیش‌فرض (تابع تنظیم کلی)", True: "همیشه روشن", False: "خاموش"}[cs["ai_enabled"]]
            ov = ", ".join(f"{k}={v:g}" for k, v in cs["overrides"].items()) or "—"
            return (
                f"💬 {label}\nهوش مصنوعی: {state}\n"
                f"توقف: {'تا ' + str(max(1, int((pause - time.time()) / 60))) + ' دقیقه‌ی دیگر' if pause else 'ندارد'}\n"
                f"پاسخ‌های متوالی: {cs['ai_streak']}\nتنظیمات اختصاصی: {ov}\n"
                f"دستورالعمل اختصاصی: {clip(cs['custom_prompt'], 200) or '—'}"
            )
        if sub == "on":
            await db.set_chat_ai_enabled(cid, True)
            return f"✅ دستیار در {label} فعال است."
        if sub == "off":
            await db.set_chat_ai_enabled(cid, False)
            rt = self.s.runtime(cid)
            async with rt.send_lock:
                rt.epoch += 1
                for task in list(rt.tasks):
                    task.cancel()
            return f"🚫 دستیار در {label} خاموش شد."
        if sub == "default":
            await db.set_chat_ai_enabled(cid, None)
            return f"↩️ {label} به تنظیم کلی برگشت."
        if sub == "prompt":
            if val.lower() == "clear":
                await db.set_chat_prompt(cid, "")
                return "🗑 دستورالعمل اختصاصی پاک شد."
            if not val:
                return "استفاده: /ai chat <گفتگو> prompt <متن> | prompt clear"
            if len(val) > 4000:
                return "متن بیش‌ازحد طولانی است (حداکثر ۴۰۰۰ نویسه)."
            await db.set_chat_prompt(cid, val)
            return f"✅ دستورالعمل اختصاصی {label} ذخیره شد."
        if sub in ("set", "unset"):
            name, _, num = _split(val)
            key = _CHAT_KEY_ALIASES.get(name.lower(), name.lower())
            spec = CHAT_OVERRIDE_KEYS.get(key)
            if spec is None:
                return "کلیدهای مجاز: " + ", ".join(_CHAT_KEY_ALIASES)
            if sub == "unset":
                await db.set_chat_override(cid, key, None)
                return f"↩️ {name} برای {label} به تنظیم کلی برگشت."
            try:
                value = spec[0](normalize_digits(num.strip()))
            except ValueError:
                return "مقدار باید عدد باشد."
            if not spec[1] <= value <= spec[2]:
                return f"مقدار باید بین {spec[1]:g} و {spec[2]:g} باشد."
            await db.set_chat_override(cid, key, value)
            return f"✅ {name} = {value:g} فقط برای {label}"
        if sub == "forget":
            removed = await db.delete_user_data(cid)
            return f"🗑 {removed} ردیف مربوط به {label} حذف شد."
        return "استفاده: /ai chat <گفتگو> info | on | off | default | prompt … | set … | unset … | forget"

    async def _cmd_forget(self, args: str, msg: OwnerMessage) -> str:
        cid, err = await self._resolve_chat(args)
        if cid is None:
            return err
        removed = await self.s.db.delete_user_data(cid)
        return f"🗑 {removed} ردیف (تاریخچه/لاگ/تنظیمات) مربوط به {cid} حذف شد."

    # ------------------------------------------------------------------
    # گزارش‌ها
    # ------------------------------------------------------------------
    async def _cmd_stats(self, args: str, msg: OwnerMessage) -> str:
        s = self.s
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

        return "📊 آمار\n\nامروز\n" + block(today) + "\n\nکل لاگ‌های نگهداری‌شده\n" + block(total)

    async def _cmd_logs(self, args: str, msg: OwnerMessage) -> str:
        digits = normalize_digits(args.strip())
        n = min(max(int(digits), 1), 15) if digits.isdigit() else 6
        logs = await self.s.db.get_recent_logs(n)
        if not logs:
            return "هنوز لاگی ثبت نشده است."
        out = []
        for r in logs:
            who = r["full_name"] or r["username"] or "؟"
            out.append(
                f"🕒 {fmt_ts(r['ts'])} | 👤 {who} ({r['user_id']})\n"
                f"❓ {clip(r['user_text'], 120)}\n🤖 {clip(r['bot_reply'], 160) or '—'}\n"
                f"وضعیت: {clip(r['status'], 60)}"
            )
        return "📜 آخرین لاگ‌ها (UTC)\n\n" + "\n\n".join(out)

    async def _cmd_csv(self, args: str, msg: OwnerMessage) -> str | None:
        rows = await self.s.db.export_logs()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["time_utc", "user_id", "username", "full_name", "message", "reply", "status",
                         "latency_ms", "prompt_tokens", "completion_tokens", "model"])
        for r in rows:
            writer.writerow([_csv_safe(v) for v in (
                fmt_ts(r["ts"]), r["user_id"], r["username"], r["full_name"], r["user_text"],
                r["bot_reply"], r["status"], r["latency_ms"], r["prompt_tokens"],
                r["completion_tokens"], r["model"])])
        await self.engine.send_file_to_self(
            buf.getvalue().encode("utf-8-sig"), "logs.csv", f"{len(rows)} ردیف لاگ"  # BOM برای اکسل
        )
        return None

    async def _cmd_clearlogs(self, args: str, msg: OwnerMessage) -> str:
        return f"🗑 {await self.s.db.clear_logs()} ردیف لاگ حذف شد."
