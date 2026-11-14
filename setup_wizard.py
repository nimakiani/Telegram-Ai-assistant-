"""
setup_wizard.py — راهنمای تعاملی و مرحله‌به‌مرحله‌ی راه‌اندازی (فارسی).

۱۲ مرحله: API ID ← API HASH ← شماره ← کد تأیید ← رمز دومرحله‌ای ← ذخیره‌ی Session ←
گفتگوهای مجاز ← سرویس هوش مصنوعی ← API Key ← مدل ← آزمایش اتصال ← گزارش.

* هیچ مقدار حساسی در خروجی چاپ نمی‌شود (API HASH، کلید API و رمز با getpass دریافت می‌شوند).
* اجرای مجدد امن است: مقدارهای قبلی نمایش/نگه‌داشته می‌شوند (Enter = نگه‌داشتن).
* ورودی/خروجی قابل تزریق است تا wizard بدون ترمینال هم تست شود.
"""

from __future__ import annotations

import asyncio
import getpass
import re
from pathlib import Path
from typing import Any, Callable

from envfile import mask, read_env, update_env
from schedule import normalize_digits
from tglayer.models import AuthError, ChatInfo, GatewayError
from tglayer.telethon_gateway import TelethonGateway, login_interactive

TOTAL_STEPS = 12

PROVIDERS: list[dict[str, Any]] = [
    {"key": "openai", "title": "OpenAI", "base_url": "", "model": "gpt-4o-mini",
     "stt": True, "vision": True, "note": ""},
    {"key": "openrouter", "title": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
     "model": "openai/gpt-4o-mini", "stt": False, "vision": True,
     "note": "OpenRouter تبدیل ویس به متن ندارد؛ برای ویس باید جداگانه TRANSCRIPTION_* را تنظیم کنید."},
    {"key": "groq", "title": "Groq", "base_url": "https://api.groq.com/openai/v1",
     "model": "llama-3.3-70b-versatile", "stt": True, "vision": False,
     "note": "مدل‌های متنی Groq معمولاً تحلیل تصویر ندارند (در صورت خطا بدون تصویر تلاش می‌شود)."},
    {"key": "ollama", "title": "Ollama (روی همین سیستم)", "base_url": "http://localhost:11434/v1",
     "model": "llama3.1", "stt": False, "vision": False,
     "note": "برای Ollama کلید لازم نیست؛ ویس و تصویر به‌صورت پیش‌فرض خاموش می‌شود."},
    {"key": "custom", "title": "سرویس سازگار با OpenAI (آدرس دلخواه)", "base_url": None,
     "model": "", "stt": False, "vision": False, "note": ""},
]


class Wizard:
    def __init__(
        self,
        project_dir: Path,
        ask: Callable[[str], str] = input,
        ask_secret: Callable[[str], str] = getpass.getpass,
        out: Callable[[str], None] = print,
        client_factory: Callable[..., Any] | None = None,
        gateway_factory: Callable[..., Any] | None = None,
        ai_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.dir = project_dir
        self.env_path = project_dir / ".env"
        self.example = project_dir / ".env.example"
        self._ask, self._ask_secret, self.out = ask, ask_secret, out
        self._client_factory = client_factory
        self._gateway_factory = gateway_factory or (
            lambda api_id, api_hash, session: TelethonGateway(api_id, api_hash, session, client_factory)
        )
        self._ai_factory = ai_factory
        self.env: dict[str, str] = read_env(self.env_path)
        self.report: list[str] = []

    # ------------------------------------------------------------------ ابزار
    def _step(self, n: int, title: str) -> None:
        self.out(f"\n━━━ مرحله {n} از {TOTAL_STEPS}: {title} ━━━")

    def _save(self, **values: str) -> None:
        update_env(self.env_path, values, template=self.example)
        self.env.update(values)

    def _prompt(self, text: str, current: str = "", secret: bool = False) -> str:
        suffix = f" [Enter = نگه‌داشتن {mask('TG_API_HASH', current) if secret else current}]" if current else ""
        asker = self._ask_secret if secret else self._ask
        value = asker(f"{text}{suffix}: ").strip()
        return normalize_digits(value) if not secret else value or current

    # ------------------------------------------------------------------ مراحل
    def step_api_id(self) -> None:
        self._step(1, "دریافت API ID")
        self.out(
            "برای اتصال به حساب شخصی، تلگرام یک «API ID» و «API HASH» رایگان به شما می‌دهد:\n"
            "  ۱) در مرورگر باز کنید: https://my.telegram.org\n"
            "  ۲) با شماره‌ی تلگرام خود وارد شوید (کد داخل خود تلگرام می‌آید)\n"
            "  ۳) روی «API development tools» بزنید و یک برنامه بسازید (نام و Short name دلخواه است)\n"
            "  ۴) مقدار «App api_id» (یک عدد) را اینجا وارد کنید."
        )
        while True:
            raw = self._prompt("API ID", self.env.get("TG_API_ID", ""))
            raw = raw or self.env.get("TG_API_ID", "")
            if raw.isdigit() and int(raw) > 0:
                self._save(TG_API_ID=raw)
                return
            self.out("❌ API ID باید فقط یک عدد باشد (مثلاً 1234567).")

    def step_api_hash(self) -> None:
        self._step(2, "دریافت API HASH")
        self.out("مقدار «App api_hash» (۳۲ نویسه‌ی انگلیسی/عدد) را وارد کنید. هنگام تایپ نمایش داده نمی‌شود.")
        while True:
            raw = self._prompt("API HASH", self.env.get("TG_API_HASH", ""), secret=True)
            if re.fullmatch(r"[0-9a-fA-F]{32}", raw or ""):
                self._save(TG_API_HASH=raw)
                return
            self.out("❌ API HASH باید دقیقاً ۳۲ نویسه‌ی هگزادسیمال باشد. دوباره کپی کنید.")

    def step_phone(self) -> None:
        self._step(3, "شماره‌ی تلفن تلگرام")
        self.out("شماره را با کد کشور وارد کنید؛ مثلاً +989121234567")
        while True:
            raw = self._prompt("شماره", self.env.get("TG_PHONE", ""))
            raw = (raw or self.env.get("TG_PHONE", "")).replace(" ", "").replace("-", "")
            if raw.startswith("00"):
                raw = "+" + raw[2:]
            if re.fullmatch(r"\+\d{8,15}", raw):
                self._save(TG_PHONE=raw)
                return
            self.out("❌ شماره نامعتبر است؛ باید با + و کد کشور شروع شود.")

    async def step_login(self) -> bool:
        cfg = self.env
        session = cfg.get("TG_SESSION_PATH") or "data/session/assistant"
        self._save(TG_SESSION_PATH=session)
        self._step(4, "اتصال به تلگرام و دریافت کد تأیید")

        async def ask_code() -> str:
            self._step(4, "وارد کردن کد تأیید")
            return await asyncio.to_thread(self._ask, "کدی که تلگرام فرستاد (۵ رقم): ")

        async def ask_password() -> str:
            self._step(5, "رمز تأیید دومرحله‌ای")
            return await asyncio.to_thread(self._ask_secret, "رمز دومرحله‌ای (نمایش داده نمی‌شود): ")

        try:
            info = await login_interactive(
                int(cfg["TG_API_ID"]), cfg["TG_API_HASH"], cfg["TG_PHONE"], session,
                ask_code, ask_password, notify=self.out, client_factory=self._client_factory,
            )
        except (AuthError, GatewayError) as exc:
            self.out(f"\n❌ ورود ناموفق بود: {exc}")
            return False
        self._step(6, "ذخیره‌ی امن Session")
        self.out(
            f"✅ وارد شدید: {info.name or '—'}"
            + (f" (@{info.username})" if info.username else "")
            + f"\nنشست در «{session}.session» با دسترسی 600 ذخیره شد؛ دفعه‌ی بعد نیازی به کد نیست.\n"
            "⚠️ این فایل مثل رمز حساب شماست؛ آن را به کسی ندهید و در Git قرار ندهید."
        )
        self.report.append(f"حساب تلگرام: {info.name or info.user_id}")
        return True

    async def step_chats(self) -> None:
        self._step(7, "انتخاب گفتگوهایی که دستیار اجازه‌ی پاسخ دارد")
        self.out(
            "دستیار هرگز خودش به کسی پیام نمی‌دهد و فقط به گفتگوهای خصوصی پاسخ می‌دهد.\n"
            "  1) همه‌ی گفتگوهای خصوصی (پیشنهادی)\n"
            "  2) همه به‌جز افرادی که انتخاب می‌کنم (لیست سیاه)\n"
            "  3) فقط افرادی که انتخاب می‌کنم (لیست سفید)"
        )
        choice = ""
        while choice not in {"1", "2", "3"}:
            choice = (self._prompt("انتخاب شما [1]") or "1")
        from database import LIST_BLACK, LIST_WHITE, MODE_OFF, KEY_LIST_MODE, Database

        mode = {"1": MODE_OFF, "2": LIST_BLACK, "3": LIST_WHITE}[choice]
        ids: list[int] = []
        if choice != "1":
            chats = await self._recent_chats()
            if chats:
                self.out("\nگفتگوهای اخیر شما:")
                for i, c in enumerate(chats, 1):
                    self.out(f"  {i:>2}) {c.name or '—'}" + (f" (@{c.username})" if c.username else ""))
                raw = self._prompt("شماره‌ها را با فاصله یا کاما وارد کنید (یا Enter برای رد‌شدن)")
                for tok in re.split(r"[\s,،]+", raw):
                    if tok.isdigit() and 1 <= int(tok) <= len(chats):
                        ids.append(chats[int(tok) - 1].chat_id)
            else:
                self.out("فهرست گفتگوها خوانده نشد؛ بعداً با دستور /ai black add یا /ai white add در Saved Messages اضافه کنید.")
        if choice == "3" and not ids:
            self.out("⚠️ لیست سفید خالی است؛ دستیار به هیچ‌کس پاسخ نمی‌دهد تا افرادی را با /ai white add اضافه کنید.")
        db_path = self.env.get("DATABASE_PATH") or "data/assistant.db"
        db = Database(str(self.dir / db_path) if not Path(db_path).is_absolute() else db_path)
        try:
            await db.set_setting(KEY_LIST_MODE, mode)
            for cid in ids:
                await db.add_to_list(cid, mode)
        finally:
            db.close()
        self.report.append({"1": "همه‌ی گفتگوهای خصوصی", "2": f"لیست سیاه ({len(ids)} نفر)",
                            "3": f"لیست سفید ({len(ids)} نفر)"}[choice])

    async def _recent_chats(self) -> list[ChatInfo]:
        env = self.env
        gw = self._gateway_factory(int(env["TG_API_ID"]), env["TG_API_HASH"], env["TG_SESSION_PATH"])
        try:
            await gw.start()
            return await gw.recent_private_chats(30)
        except GatewayError as exc:
            self.out(f"⚠️ خواندن گفتگوها ناموفق بود: {exc}")
            return []
        finally:
            await gw.stop()

    def step_ai(self) -> dict[str, Any]:
        self._step(8, "انتخاب سرویس هوش مصنوعی")
        for i, p in enumerate(PROVIDERS, 1):
            self.out(f"  {i}) {p['title']}")
        idx = 0
        while not 1 <= idx <= len(PROVIDERS):
            raw = self._prompt("انتخاب شما [1]") or "1"
            idx = int(raw) if raw.isdigit() else 0
        provider = PROVIDERS[idx - 1]
        base = provider["base_url"]
        if base is None:
            base = self._prompt("آدرس API (مثلاً https://example.com/v1)", self.env.get("AI_BASE_URL", ""))
            base = base or self.env.get("AI_BASE_URL", "")
        if provider["note"]:
            self.out(f"ℹ️ {provider['note']}")
        self._save(AI_BASE_URL=base)
        if provider["key"] == "groq":
            self._save(TRANSCRIPTION_MODEL="whisper-large-v3")
        if not provider["stt"]:
            self._save(VOICE_ENABLED="false")
        if not provider["vision"]:
            self._save(VISION_ENABLED="false")
        return provider

    def step_api_key(self, provider: dict[str, Any]) -> None:
        self._step(9, "دریافت API Key")
        if provider["key"] == "ollama":
            self._save(AI_API_KEY="none")
            self.out("برای Ollama کلید لازم نیست (مقدار «none» ذخیره شد).")
            return
        self.out("کلید API سرویس را وارد کنید. هنگام تایپ نمایش داده نمی‌شود و در لاگ‌ها هم نمی‌آید.")
        while True:
            key = self._prompt("API Key", self.env.get("AI_API_KEY", ""), secret=True)
            if key:
                self._save(AI_API_KEY=key)
                return
            self.out("❌ کلید خالی است.")

    def step_model(self, provider: dict[str, Any]) -> None:
        self._step(10, "انتخاب مدل هوش مصنوعی")
        default = self.env.get("AI_MODEL") if self.env.get("AI_MODEL") and provider["key"] == "custom" else provider["model"]
        while True:
            model = self._prompt("نام مدل", default or "") or default
            if model:
                self._save(AI_MODEL=model)
                return
            self.out("❌ نام مدل لازم است.")

    async def step_test(self) -> bool:
        self._step(11, "آزمایش اتصال تلگرام و هوش مصنوعی")
        from dotenv import load_dotenv

        load_dotenv(self.env_path, override=True)
        from config import ConfigError, load_config

        try:
            cfg = load_config()
        except ConfigError as exc:
            self.out(f"❌ تنظیمات ناقص است:\n{exc}")
            return False

        ok = True
        gw = self._gateway_factory(cfg.tg_api_id, cfg.tg_api_hash, cfg.session_path)
        try:
            me = await gw.start()
            self.out(f"✅ تلگرام: اتصال برقرار است ({me.name or me.user_id}).")
        except (AuthError, GatewayError) as exc:
            self.out(f"❌ تلگرام: {exc}")
            ok = False
        finally:
            await gw.stop()

        try:
            if self._ai_factory is not None:
                ai = self._ai_factory(cfg)
            else:
                from ai_handler import AIHandler

                ai = AIHandler(cfg)
            result = await asyncio.wait_for(
                ai.generate_reply("فقط یک کلمه پاسخ بده.", [], [("text", "سلام")]), timeout=90
            )
            self.out(f"✅ هوش مصنوعی: مدل «{result.model}» پاسخ داد.")
        except Exception as exc:  # noqa: BLE001
            self.out(f"❌ هوش مصنوعی: {type(exc).__name__}: {str(exc)[:200]}\n"
                     "   کلید، آدرس API و نام مدل را بررسی کنید (python main.py --setup برای تکرار).")
            ok = False
        return ok

    def step_report(self, ok: bool) -> None:
        self._step(12, "گزارش راه‌اندازی")
        env = self.env
        self.out(
            ("🎉 راه‌اندازی با موفقیت انجام شد.\n" if ok else "⚠️ راه‌اندازی انجام شد اما آزمایش‌ها خطا داشتند (بالا را ببینید).\n")
            + f"  • سرویس هوش مصنوعی: {env.get('AI_BASE_URL') or 'OpenAI'} / مدل {env.get('AI_MODEL')}\n"
            + "".join(f"  • {line}\n" for line in self.report)
            + "\nقدم بعدی:\n"
            "  ./start.sh            اجرای دستیار\n"
            "  ./service.sh install  اجرای خودکار با روشن‌شدن سیستم (systemd)\n"
            "برای مدیریت، در تلگرام به «Saved Messages» بروید و بنویسید:  /ai help"
        )

    # ------------------------------------------------------------------ اجرا
    async def run_async(self) -> int:
        self.out("═" * 54 + "\n  راه‌اندازی دستیار هوشمند تلگرام (حساب شخصی)\n" + "═" * 54)
        self.out("این برنامه با حساب عادی شما کار می‌کند؛ Premium و ساخت ربات (BotFather) لازم نیست.")
        self.step_api_id()
        self.step_api_hash()
        self.step_phone()
        if not await self.step_login():
            return 3
        await self.step_chats()
        provider = self.step_ai()
        self.step_api_key(provider)
        self.step_model(provider)
        ok = await self.step_test()
        self.step_report(ok)
        return 0 if ok else 2


def run_wizard(project_dir: Path) -> int:
    wizard = Wizard(project_dir)
    try:
        return asyncio.run(wizard.run_async())
    except (KeyboardInterrupt, EOFError):
        print("\nراه‌اندازی لغو شد؛ هر زمان خواستید دوباره ./start.sh --setup را اجرا کنید.")
        return 1
