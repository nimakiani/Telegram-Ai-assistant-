"""
app.py — سیم‌کشی و چرخه‌ی اجرای برنامه (اتصال، نظارت، قطع/وصل مجدد، خاموشی تمیز).

کدهای خروج (برای systemd و اسکریپت‌ها):
  0 = خروج عادی     1 = خطای تنظیمات     3 = خطای ورود/نشست (نیاز به ورود دوباره)
  4 = قطع‌های پیاپی (اتصال برقرار نمی‌شود)    5 = نمونه‌ی دیگری در حال اجراست
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable

from ai_handler import AIHandler
from commands import CommandHandler
from config import Config
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
    MODE_OFF,
    Database,
)
from engine import Engine
from services import AIProtocol, Services
from tglayer.models import AuthError, Gateway, GatewayError
from tglayer.telethon_gateway import TelethonGateway

logger = logging.getLogger(__name__)

EXIT_OK, EXIT_CONFIG, EXIT_AUTH, EXIT_CONNECTION, EXIT_LOCKED = 0, 1, 3, 4, 5
MAX_CONSECUTIVE_FAILURES = 8
HEALTHY_AFTER_SECONDS = 60
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 60.0


def _flag(value: bool) -> str:
    return "1" if value else "0"


async def seed_defaults(db: Database, cfg: Config) -> None:
    """مقدار اولیه‌ی تنظیمات را (فقط برای کلیدهایی که قبلاً ذخیره نشده‌اند) می‌نویسد."""
    await db.init_defaults(
        {
            KEY_ENABLED: "1",
            KEY_SYSTEM_PROMPT: cfg.default_system_prompt,
            KEY_KNOWLEDGE: "",
            KEY_LIST_MODE: MODE_OFF,
            KEY_COOLDOWN: str(cfg.default_cooldown),
            KEY_DEBOUNCE: str(cfg.default_debounce),
            KEY_HISTORY_SIZE: str(cfg.default_history_size),
            KEY_PAUSE_MINUTES: str(cfg.default_pause_minutes),
            KEY_RATE_LIMIT: str(cfg.default_rate_limit),
            KEY_MAX_STREAK: str(cfg.default_max_streak),
            KEY_MAX_MEDIA_MB: str(max(1, cfg.max_media_bytes // (1024 * 1024))),
            KEY_HANDOFF: _flag(cfg.handoff_enabled),
            KEY_HANDOFF_MESSAGE: cfg.handoff_message,
            KEY_SCHEDULE_MODE: cfg.schedule_mode,
            KEY_WORK_START: cfg.work_start,
            KEY_WORK_END: cfg.work_end,
            KEY_WORK_DAYS: cfg.work_days,
            KEY_TIMEZONE: cfg.timezone,
            KEY_VISION: _flag(cfg.vision_enabled),
            KEY_VOICE: _flag(cfg.voice_enabled),
            KEY_MARK_READ: _flag(cfg.mark_as_read),
        }
    )


class InstanceLock:
    """قفل فایلی: دو نمونه‌ی هم‌زمان با یک Session باعث خطای AuthKeyDuplicated و قطع نشست می‌شوند."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))  # status.sh/stop.sh از این pid استفاده می‌کنند
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
            self._fh = None


def build_services(
    config: Config, db: Database, ai: AIProtocol, gateway: Gateway | None = None
) -> tuple[Services, Engine, CommandHandler]:
    services = Services(config=config, db=db, ai=ai, gateway=gateway)
    engine = Engine(services)
    commands = CommandHandler(services, engine)
    engine.commands = commands
    return services, engine, commands


async def run(
    config: Config,
    gateway_factory: Callable[[Config], Gateway] | None = None,
    install_signal_handlers: bool = True,
    stop: asyncio.Event | None = None,
) -> int:
    lock = InstanceLock(Path(config.lock_path).expanduser())
    if not lock.acquire():
        logger.error("نمونه‌ی دیگری از برنامه در حال اجراست (یا با همین Session وصل است).")
        return EXIT_LOCKED

    db = Database(config.database_path, log_max_rows=config.log_max_rows)
    gateway = (gateway_factory or _default_gateway)(config)
    stop = stop or asyncio.Event()
    engine: Engine | None = None
    try:
        await seed_defaults(db, config)
        ai = AIHandler(config)
        services, engine, _commands = build_services(config, db, ai, gateway)
        gateway.set_handlers(engine.on_incoming, engine.on_owner)

        if install_signal_handlers:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)

        try:
            services.me = await gateway.start()
        except AuthError as exc:
            logger.error("ورود به تلگرام ناموفق: %s", exc)
            return EXIT_AUTH
        except GatewayError as exc:
            logger.error("اتصال اولیه به تلگرام ناموفق: %s", exc)
            return EXIT_CONNECTION
        logger.info("وصل شد به حساب «%s» (id=%s)", services.me.name, services.me.user_id)
        return await _supervise(gateway, services, stop)
    finally:
        if engine is not None:
            await engine.shutdown()
        with contextlib.suppress(Exception):
            await gateway.stop()
        db.close()
        lock.release()


def _default_gateway(config: Config) -> Gateway:
    return TelethonGateway(config.tg_api_id, config.tg_api_hash, config.session_path)


async def _supervise(gateway: Gateway, services: Services, stop: asyncio.Event) -> int:
    """تا زمان درخواست توقف، اتصال را نگه می‌دارد؛ قطع‌شدن را با backoff جبران می‌کند."""
    failures = 0
    backoff = INITIAL_BACKOFF
    while not stop.is_set():
        connected_at = time.monotonic()
        runner = asyncio.ensure_future(gateway.run_until_disconnected())
        waiter = asyncio.ensure_future(stop.wait())
        done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            runner.cancel()
            waiter.cancel()
            with contextlib.suppress(BaseException):
                await runner
            return EXIT_OK
        waiter.cancel()
        error = runner.exception() if runner in done and not runner.cancelled() else None
        if isinstance(error, AuthError):
            logger.error("نشست تلگرام نامعتبر شد: %s — دوباره وارد شوید: ./start.sh --setup", error)
            return EXIT_AUTH

        if time.monotonic() - connected_at > HEALTHY_AFTER_SECONDS:
            failures, backoff = 0, INITIAL_BACKOFF
        failures += 1
        logger.warning("اتصال به تلگرام قطع شد (%s). تلاش مجدد %d/%d پس از %.0f ثانیه",
                       error or "بدون خطا", failures, MAX_CONSECUTIVE_FAILURES, backoff)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            return EXIT_CONNECTION
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return EXIT_OK
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, MAX_BACKOFF)
        with contextlib.suppress(Exception):
            await gateway.stop()
        try:
            # handlerها در gateway می‌مانند؛ start() رویدادها را روی کلاینت تازه ثبت می‌کند
            services.me = await gateway.start()
        except AuthError as exc:
            logger.error("نشست تلگرام نامعتبر شد: %s", exc)
            return EXIT_AUTH
        except GatewayError as exc:
            logger.warning("اتصال مجدد ناموفق: %s", exc)
    return EXIT_OK

