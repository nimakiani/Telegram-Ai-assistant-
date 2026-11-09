"""
ai_handler.py — اتصال به API هوش مصنوعی سازگار با OpenAI.

قابلیت‌ها:
  * پاسخ‌گویی متنی و **تحلیل تصویر** (Vision) با Chat Completions
  * **تبدیل صدا به متن** (Whisper یا هر سرویس سازگار) برای پیام‌های صوتی
  * **ارائه‌دهنده‌ی پشتیبان (Fallback)**: اگر سرویس اصلی از کار افتاد یا محدود شد،
    به‌صورت خودکار سراغ مدل/سرویس دوم می‌رود
  * تلاش مجدد با backoff نمایی برای خطاهای موقتی (۴۲۹، 5xx، قطعی شبکه، تایم‌اوت)
    و رعایت هدر Retry-After
  * اگر مدل تصویر را نپذیرد، به‌صورت خودکار بدون تصویر دوباره تلاش می‌کند
  * ثبت تاخیر و مصرف توکن برای آمار

این ماژول هرگز باعث کرش ربات نمی‌شود؛ فراخوان فقط AIError را مدیریت می‌کند.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from config import Config

logger = logging.getLogger(__name__)

# یک «بخش» از پیام کاربر: ("text", متن) یا ("image", data-URL)
Part = tuple[str, str]

_RETRIABLE_STATUS = {408, 409, 425}
_MAX_BACKOFF_SECONDS = 20.0
_MAX_RETRY_AFTER_SECONDS = 30.0


class AIError(RuntimeError):
    """خطای دریافت پاسخ از سرویس هوش مصنوعی (پس از اتمام تلاش‌های مجدد)."""


class TranscriptionError(AIError):
    """خطای تبدیل صدا به متن."""


@dataclass
class AIResult:
    """خروجی تولید پاسخ همراه با اطلاعات آماری."""

    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class _Provider:
    name: str
    client: AsyncOpenAI
    model: str
    vision_model: str


class AIHandler:
    """تولید پاسخ (متن/تصویر) و تبدیل صدا به متن."""

    def __init__(self, config: Config) -> None:
        self._temperature = config.ai_temperature
        self._max_tokens = config.ai_max_tokens
        self._max_retries = config.ai_max_retries
        self._timeout = config.ai_timeout
        # max_retries=0: تلاش مجدد را خودمان با کنترل کامل انجام می‌دهیم.
        self._providers: list[_Provider] = [
            _Provider(
                "primary",
                AsyncOpenAI(
                    api_key=config.ai_api_key,
                    base_url=config.ai_base_url,
                    timeout=config.ai_timeout,
                    max_retries=0,
                ),
                config.ai_model,
                config.ai_vision_model or config.ai_model,
            )
        ]
        if config.ai_fallback_model:
            self._providers.append(
                _Provider(
                    "fallback",
                    AsyncOpenAI(
                        api_key=config.ai_fallback_api_key or config.ai_api_key,
                        base_url=config.ai_fallback_base_url or config.ai_base_url,
                        timeout=config.ai_timeout,
                        max_retries=0,
                    ),
                    config.ai_fallback_model,
                    config.ai_fallback_model,
                )
            )
        self._stt_client = AsyncOpenAI(
            api_key=config.stt_api_key,
            base_url=config.stt_base_url,
            timeout=config.ai_timeout,
            max_retries=0,
        )
        self._stt_model = config.stt_model
        self._stt_language = config.stt_language

    # ------------------------------------------------------------------
    # ساخت پیام‌ها
    # ------------------------------------------------------------------
    @staticmethod
    def _build_messages(
        system_prompt: str,
        history: Sequence[Mapping[str, str]],
        parts: Sequence[Part],
        include_images: bool,
    ) -> list[dict[str, Any]]:
        """system + تاریخچه + پیام جدید (متن و در صورت نیاز تصاویر) در قالب Chat Completions."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for item in history:
            messages.append({"role": item["role"], "content": item["content"]})

        text = "\n".join(v for k, v in parts if k == "text").strip()
        images = [v for k, v in parts if k == "image"]
        if images and include_images:
            content: list[dict[str, Any]] = [
                {"type": "text", "text": text or "(کاربر فقط یک تصویر فرستاده است)"}
            ]
            for url in images:
                content.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": content})
        else:
            if images:  # حالت جایگزین: بدون تصویر
                text = (text + "\n[کاربر تصویری فرستاد که قابل پردازش نبود]").strip()
            messages.append({"role": "user", "content": text or "..."})
        return messages

    # ------------------------------------------------------------------
    # تلاش مجدد
    # ------------------------------------------------------------------
    @staticmethod
    def _retry_delay(attempt: int, exc: Exception) -> float:
        """تاخیر قبل از تلاش بعدی: Retry-After سرور یا backoff نمایی (۱، ۲، ۴، ... ثانیه)."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            raw = headers.get("retry-after")
            if raw:
                try:
                    return min(max(float(raw), 0.0), _MAX_RETRY_AFTER_SECONDS)
                except ValueError:
                    pass
        return min(2.0 ** attempt, _MAX_BACKOFF_SECONDS)

    async def _retry(
        self, call: Callable[[], Awaitable[Any]], error_cls: type[AIError] = AIError
    ) -> Any:
        """`call` را با تلاش مجدد روی خطاهای موقتی اجرا می‌کند؛ در شکست error_cls پرتاب می‌شود."""
        attempts = self._max_retries + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            delay = 0.0
            try:
                return await call()
            except AIError:
                raise
            except (RateLimitError, InternalServerError, APIConnectionError) as exc:
                # خطاهای موقتی: محدودیت نرخ، خطای سرور، قطعی/تایم‌اوت شبکه
                last_error = exc
                delay = self._retry_delay(attempt, exc)
            except APIStatusError as exc:
                if exc.status_code in _RETRIABLE_STATUS:
                    last_error = exc
                    delay = self._retry_delay(attempt, exc)
                else:
                    # خطای دائمی (۴۰۱، ۴۰۴، ۴۰۰ و ...): تکرار فایده‌ای ندارد
                    raise error_cls(f"HTTP {exc.status_code}: {exc.message}") from exc
            except Exception as exc:  # noqa: BLE001 — ربات نباید کرش کند
                raise error_cls(f"خطای غیرمنتظره: {exc!r}") from exc

            if attempt < attempts - 1:
                logger.warning(
                    "AI request failed (%s). Retry %d/%d in %.1fs",
                    type(last_error).__name__,
                    attempt + 1,
                    attempts - 1,
                    delay,
                )
                await asyncio.sleep(delay)

        raise error_cls(
            f"پس از {attempts} تلاش پاسخی دریافت نشد: {type(last_error).__name__}: {last_error}"
        )

    # ------------------------------------------------------------------
    # تولید پاسخ
    # ------------------------------------------------------------------
    async def _call_provider(
        self, provider: _Provider, messages: list[dict[str, Any]], vision: bool
    ) -> AIResult:
        model = provider.vision_model if vision else provider.model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if self._max_tokens > 0:
            kwargs["max_tokens"] = self._max_tokens

        async def call() -> AIResult:
            started = time.monotonic()
            response = await provider.client.chat.completions.create(**kwargs)
            text = ""
            if response.choices:
                text = (response.choices[0].message.content or "").strip()
            if not text:
                raise AIError("سرویس هوش مصنوعی پاسخ خالی برگرداند.")
            usage = getattr(response, "usage", None)
            return AIResult(
                text=text,
                model=model,
                latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
            )

        return await self._retry(call)

    async def _run_chain(
        self,
        system_prompt: str,
        history: Sequence[Mapping[str, str]],
        parts: Sequence[Part],
        include_images: bool,
    ) -> AIResult:
        """ارائه‌دهنده‌ها را به ترتیب امتحان می‌کند تا یکی جواب بدهد."""
        has_images = include_images and any(k == "image" for k, _ in parts)
        messages = self._build_messages(system_prompt, history, parts, include_images)
        errors: list[str] = []
        for provider in self._providers:
            try:
                return await self._call_provider(provider, messages, vision=has_images)
            except AIError as exc:
                errors.append(f"{provider.name}: {exc}")
                logger.warning("Provider %s failed: %s", provider.name, exc)
        raise AIError(" | ".join(errors))

    async def generate_reply(
        self,
        system_prompt: str,
        history: Sequence[Mapping[str, str]],
        parts: Sequence[Part],
    ) -> AIResult:
        """پاسخ مدل را برمی‌گرداند یا در صورت شکست کامل AIError پرتاب می‌کند."""
        has_images = any(k == "image" for k, _ in parts)
        try:
            return await self._run_chain(system_prompt, history, parts, include_images=True)
        except AIError as exc:
            if not has_images:
                raise
            # شاید مدل از تصویر پشتیبانی نمی‌کند؛ یک بار بدون تصویر تلاش می‌کنیم.
            logger.warning("Vision request failed, retrying text-only: %s", exc)
            return await self._run_chain(system_prompt, history, parts, include_images=False)

    # ------------------------------------------------------------------
    # تبدیل صدا به متن
    # ------------------------------------------------------------------
    async def transcribe(self, data: bytes, filename: str = "voice.ogg") -> str:
        """فایل صوتی را به متن تبدیل می‌کند (در شکست TranscriptionError)."""
        kwargs: dict[str, Any] = {"model": self._stt_model}
        if self._stt_language:
            kwargs["language"] = self._stt_language

        async def call() -> str:
            result = await self._stt_client.audio.transcriptions.create(
                file=(filename, data), **kwargs
            )
            text = getattr(result, "text", result)
            text = text.strip() if isinstance(text, str) else ""
            if not text:
                raise TranscriptionError("متن قابل تشخیصی در صدا پیدا نشد.")
            return text

        return await self._retry(call, TranscriptionError)
