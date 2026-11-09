"""
database.py — لایه‌ی دیتابیس SQLite.

جدول‌ها:
  * settings              : تنظیمات پویا (فعال/غیرفعال، پرامپت، دانش، ساعت کاری، ...)
  * access_list           : لیست سیاه و لیست سفید کاربران
  * business_connections  : اتصال‌های Business که به ربات داده شده‌اند
  * logs                  : لاگ مکالمات برای پنل مدیریت (با تاخیر و مصرف توکن)
  * messages              : تاریخچه‌ی مکالمه‌ی هر چت (کاربر / ربات / صاحب حساب) برای زمینه‌ی AI
  * chat_state            : چت‌هایی که ربات موقتاً در آن‌ها ساکت شده است (تحویل به انسان)
  * quick_replies         : پاسخ‌های سریع بر اساس کلمه‌ی کلیدی (بدون مصرف AI)

همه‌ی متدهای عمومی coroutine هستند؛ کوئری‌ها با asyncio.to_thread در ترد جدا و
زیر یک Lock اجرا می‌شوند تا حلقه‌ی asyncio مسدود نشود.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# --- کلیدهای جدول settings
KEY_ENABLED = "enabled"
KEY_SYSTEM_PROMPT = "system_prompt"
KEY_KNOWLEDGE = "knowledge"
KEY_LIST_MODE = "list_mode"
KEY_COOLDOWN = "cooldown_seconds"
KEY_DEBOUNCE = "debounce_seconds"
KEY_HISTORY_SIZE = "history_size"
KEY_PAUSE_MINUTES = "pause_minutes"
KEY_RATE_LIMIT = "rate_limit_per_hour"
KEY_HANDOFF = "handoff_enabled"
KEY_HANDOFF_MESSAGE = "handoff_message"
KEY_SCHEDULE_MODE = "schedule_mode"
KEY_WORK_START = "work_start"
KEY_WORK_END = "work_end"
KEY_WORK_DAYS = "work_days"
KEY_TIMEZONE = "timezone"
KEY_VISION = "vision_enabled"
KEY_VOICE = "voice_enabled"
KEY_MARK_READ = "mark_as_read"

# --- نوع لیست‌ها و حالت‌های فیلتر
LIST_BLACK = "blacklist"
LIST_WHITE = "whitelist"
MODE_OFF = "off"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_list (
    user_id  INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind)
);

CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    user_chat_id  INTEGER,
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    can_reply     INTEGER NOT NULL DEFAULT 1,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    connection_id     TEXT,
    chat_id           INTEGER,
    user_id           INTEGER,
    username          TEXT,
    full_name         TEXT,
    user_text         TEXT,
    bot_reply         TEXT,
    status            TEXT NOT NULL,
    latency_ms        INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    model             TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_chat ON logs (connection_id, chat_id, id);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    connection_id TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    role          TEXT    NOT NULL,
    content       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (connection_id, chat_id, id);

CREATE TABLE IF NOT EXISTS chat_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT    NOT NULL,
    chat_id       INTEGER NOT NULL,
    paused_until  INTEGER NOT NULL,
    reason        TEXT,
    name          TEXT,
    UNIQUE (connection_id, chat_id)
);

CREATE TABLE IF NOT EXISTS quick_replies (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords TEXT NOT NULL,
    response TEXT NOT NULL
);
"""

# ستون‌هایی که در نسخه‌های قدیمی جدول logs نبودند (مهاجرت خودکار)
_LOG_EXTRA_COLUMNS = (
    ("latency_ms", "INTEGER"),
    ("prompt_tokens", "INTEGER"),
    ("completion_tokens", "INTEGER"),
    ("model", "TEXT"),
)


class Database:
    """پوشش نازک و امن‌برای‌ترد روی sqlite3 با API ناهمگام."""

    def __init__(self, path: str, log_max_rows: int = 10000) -> None:
        if path != ":memory:":
            # مسیر دیتابیس قابل تنظیم است (مثلاً دیسک پایدار ابری)؛ پوشه در صورت نیاز ساخته می‌شود.
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._log_max_rows = log_max_rows
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """ستون‌های جدید را به جدول‌های قدیمی اضافه می‌کند (اگر لازم باشد)."""
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(logs)")}
        for name, ddl in _LOG_EXTRA_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE logs ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------
    # ابزارهای داخلی
    # ------------------------------------------------------------------
    def _run(self, sql: str, params: Iterable[Any] = (), mode: str = "none") -> Any:
        """یک دستور SQL را (با قفل) اجرا و commit می‌کند.

        mode: "one" → یک ردیف، "all" → همه‌ی ردیف‌ها، "none" → تعداد ردیف‌های تغییریافته.
        """
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            if mode == "one":
                result: Any = cur.fetchone()
            elif mode == "all":
                result = cur.fetchall()
            else:
                result = cur.rowcount
            self._conn.commit()
            return result

    async def _arun(self, sql: str, params: Iterable[Any] = (), mode: str = "none") -> Any:
        return await asyncio.to_thread(self._run, sql, params, mode)

    # ------------------------------------------------------------------
    # تنظیمات
    # ------------------------------------------------------------------
    async def init_defaults(self, defaults: dict[str, str]) -> None:
        """مقادیر پیش‌فرض را فقط برای کلیدهایی که هنوز وجود ندارند درج می‌کند."""
        for key, value in defaults.items():
            await self._arun(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self._arun("SELECT value FROM settings WHERE key = ?", (key,), "one")
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._arun(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def get_bool(self, key: str, default: bool = False) -> bool:
        raw = await self.get_setting(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    async def get_float(self, key: str, default: float) -> float:
        raw = await self.get_setting(key)
        try:
            return float(raw) if raw is not None else default
        except ValueError:
            return default

    async def get_int(self, key: str, default: int) -> int:
        raw = await self.get_setting(key)
        try:
            return int(float(raw)) if raw is not None else default
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # لیست سیاه / سفید
    # ------------------------------------------------------------------
    async def add_to_list(self, user_id: int, kind: str) -> bool:
        """کاربر را به لیست اضافه می‌کند؛ اگر از قبل بوده False برمی‌گرداند."""
        changed = await self._arun(
            "INSERT OR IGNORE INTO access_list (user_id, kind, added_at) VALUES (?, ?, ?)",
            (user_id, kind, int(time.time())),
        )
        return changed > 0

    async def remove_from_list(self, user_id: int, kind: str) -> bool:
        changed = await self._arun(
            "DELETE FROM access_list WHERE user_id = ? AND kind = ?", (user_id, kind)
        )
        return changed > 0

    async def in_list(self, user_id: int, kind: str) -> bool:
        row = await self._arun(
            "SELECT 1 FROM access_list WHERE user_id = ? AND kind = ?", (user_id, kind), "one"
        )
        return row is not None

    async def get_list(self, kind: str) -> list[int]:
        rows = await self._arun(
            "SELECT user_id FROM access_list WHERE kind = ? ORDER BY added_at", (kind,), "all"
        )
        return [r["user_id"] for r in rows]

    # ------------------------------------------------------------------
    # اتصال‌های Business
    # ------------------------------------------------------------------
    async def upsert_connection(
        self,
        connection_id: str,
        user_id: int,
        user_chat_id: int | None,
        is_enabled: bool,
        can_reply: bool,
    ) -> None:
        await self._arun(
            "INSERT INTO business_connections "
            "(connection_id, user_id, user_chat_id, is_enabled, can_reply, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id) DO UPDATE SET "
            "user_id = excluded.user_id, user_chat_id = excluded.user_chat_id, "
            "is_enabled = excluded.is_enabled, can_reply = excluded.can_reply, "
            "updated_at = excluded.updated_at",
            (connection_id, user_id, user_chat_id, int(is_enabled), int(can_reply), int(time.time())),
        )

    async def get_connection(self, connection_id: str) -> sqlite3.Row | None:
        return await self._arun(
            "SELECT * FROM business_connections WHERE connection_id = ?", (connection_id,), "one"
        )

    async def list_connections(self) -> list[sqlite3.Row]:
        return await self._arun(
            "SELECT * FROM business_connections ORDER BY updated_at DESC", (), "all"
        )

    # ------------------------------------------------------------------
    # لاگ‌ها
    # ------------------------------------------------------------------
    async def add_log(
        self,
        connection_id: str | None,
        chat_id: int,
        user_id: int,
        username: str | None,
        full_name: str | None,
        user_text: str | None,
        bot_reply: str | None,
        status: str,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        model: str | None = None,
    ) -> None:
        """یک ردیف لاگ ثبت می‌کند و ردیف‌های قدیمی‌تر از سقف LOG_MAX_ROWS را حذف می‌کند."""
        await self._arun(
            "INSERT INTO logs (ts, connection_id, chat_id, user_id, username, full_name, "
            "user_text, bot_reply, status, latency_ms, prompt_tokens, completion_tokens, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()), connection_id, chat_id, user_id, username, full_name,
                user_text, bot_reply, status, latency_ms, prompt_tokens, completion_tokens, model,
            ),
        )
        await self._arun(
            "DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?", (self._log_max_rows,)
        )

    async def get_recent_logs(self, limit: int = 10) -> list[sqlite3.Row]:
        return await self._arun("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,), "all")

    async def export_logs(self) -> list[sqlite3.Row]:
        """همه‌ی لاگ‌های نگهداری‌شده به ترتیب زمانی (برای خروجی CSV)."""
        return await self._arun("SELECT * FROM logs ORDER BY id ASC", (), "all")

    async def clear_logs(self) -> int:
        return await self._arun("DELETE FROM logs")

    async def get_stats(self, since_ts: int) -> dict[str, Any]:
        """آمار لاگ‌ها از زمان `since_ts` به بعد."""
        row = await self._arun(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(status = 'ok'), 0) AS ok, "
            "COALESCE(SUM(status = 'quick'), 0) AS quick, "
            "COALESCE(SUM(status = 'handoff'), 0) AS handoff, "
            "COALESCE(SUM(status LIKE 'error%' OR status LIKE 'send_error%'), 0) AS errors, "
            "COUNT(DISTINCT chat_id) AS chats, "
            "AVG(CASE WHEN status IN ('ok', 'handoff') THEN latency_ms END) AS avg_latency, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM logs WHERE ts >= ?",
            (since_ts,),
            "one",
        )
        return dict(row)

    # ------------------------------------------------------------------
    # تاریخچه‌ی مکالمه (زمینه‌ی AI)
    # ------------------------------------------------------------------
    async def add_message(self, connection_id: str, chat_id: int, role: str, content: str) -> None:
        """role ∈ {user, assistant, owner}. قدیمی‌ترها به‌صورت خودکار پاک می‌شوند."""
        if not content:
            return
        await self._arun(
            "INSERT INTO messages (ts, connection_id, chat_id, role, content) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), connection_id, chat_id, role, content),
        )
        await self._arun(
            "DELETE FROM messages WHERE id <= (SELECT MAX(id) FROM messages) - ?",
            (self._log_max_rows * 5,),
        )

    async def get_history(
        self, connection_id: str, chat_id: int, limit: int
    ) -> list[dict[str, str]]:
        """آخرین `limit` پیام یک چت (قدیم → جدید) به‌صورت [{role, content}].

        پیام‌های دستیِ صاحب حساب (owner) به‌عنوان assistant به مدل داده می‌شوند.
        """
        if limit <= 0:
            return []
        rows = await self._arun(
            "SELECT role, content FROM messages WHERE connection_id = ? AND chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (connection_id, chat_id, limit),
            "all",
        )
        return [
            {"role": "user" if r["role"] == "user" else "assistant", "content": r["content"]}
            for r in reversed(rows)
        ]

    async def delete_user_data(self, user_id: int) -> int:
        """همه‌ی داده‌های یک کاربر (پیام‌ها، لاگ‌ها، توقف‌ها) را حذف می‌کند (حق فراموش‌شدن)."""
        total = 0
        total += await self._arun("DELETE FROM messages WHERE chat_id = ?", (user_id,))
        total += await self._arun(
            "DELETE FROM logs WHERE chat_id = ? OR user_id = ?", (user_id, user_id)
        )
        total += await self._arun("DELETE FROM chat_state WHERE chat_id = ?", (user_id,))
        return total

    # ------------------------------------------------------------------
    # توقف موقت چت‌ها (تحویل به انسان)
    # ------------------------------------------------------------------
    async def set_pause(
        self,
        connection_id: str,
        chat_id: int,
        paused_until: int,
        reason: str,
        name: str | None = None,
    ) -> None:
        await self._arun(
            "INSERT INTO chat_state (connection_id, chat_id, paused_until, reason, name) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id, chat_id) DO UPDATE SET "
            "paused_until = excluded.paused_until, reason = excluded.reason, "
            "name = COALESCE(excluded.name, chat_state.name)",
            (connection_id, chat_id, paused_until, reason, name),
        )

    async def get_pause_until(self, connection_id: str, chat_id: int) -> int | None:
        """زمان پایان توقف را برمی‌گرداند؛ اگر چت ساکت نیست None."""
        row = await self._arun(
            "SELECT paused_until FROM chat_state WHERE connection_id = ? AND chat_id = ?",
            (connection_id, chat_id),
            "one",
        )
        if row and row["paused_until"] > time.time():
            return int(row["paused_until"])
        return None

    async def list_pauses(self) -> list[sqlite3.Row]:
        return await self._arun(
            "SELECT * FROM chat_state WHERE paused_until > ? ORDER BY paused_until DESC",
            (int(time.time()),),
            "all",
        )

    async def clear_pause_by_id(self, pause_id: int) -> bool:
        return (await self._arun("DELETE FROM chat_state WHERE id = ?", (pause_id,))) > 0

    async def clear_all_pauses(self) -> int:
        return await self._arun("DELETE FROM chat_state")

    # ------------------------------------------------------------------
    # پاسخ‌های سریع
    # ------------------------------------------------------------------
    async def add_quick_reply(self, keywords: str, response: str) -> None:
        await self._arun(
            "INSERT INTO quick_replies (keywords, response) VALUES (?, ?)", (keywords, response)
        )

    async def get_quick_replies(self) -> list[sqlite3.Row]:
        return await self._arun("SELECT * FROM quick_replies ORDER BY id", (), "all")

    async def delete_quick_reply(self, reply_id: int) -> bool:
        return (await self._arun("DELETE FROM quick_replies WHERE id = ?", (reply_id,))) > 0

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()
