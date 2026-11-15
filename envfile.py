"""
envfile.py — خواندن/نوشتن امن فایل `.env` (بدون نیاز به ویرایش دستی).

* کامنت‌ها و کلیدهای ناشناخته دست‌نخورده می‌مانند.
* نوشتن اتمی است (فایل موقت + rename) و دسترسی فایل همیشه 600 است.
* مقدارهای حساس هرگز چاپ نمی‌شوند؛ برای نمایش از `mask()` استفاده کنید.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

SENSITIVE_KEYS = {"TG_API_HASH", "AI_API_KEY", "AI_FALLBACK_API_KEY", "TRANSCRIPTION_API_KEY", "PANEL_BOT_TOKEN"}
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _quote(value: str) -> str:
    if value == "" or re.fullmatch(r"[A-Za-z0-9_./:@+,\-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            values[m.group(1)] = _unquote(m.group(2))
    return values


def update_env(path: Path, updates: dict[str, str], template: Path | None = None) -> None:
    """کلیدها را در .env به‌روز می‌کند (در صورت نبودن فایل، از template می‌سازد)."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif template is not None and template.exists():
        lines = template.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in pending:
            key = m.group(1)
            out.append(f"{key}={_quote(pending.pop(key))}")
        else:
            out.append(line)
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{k}={_quote(v)}" for k, v in pending.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    os.chmod(path, 0o600)


def mask(key: str, value: str) -> str:
    """مقدار حساس را برای نمایش مخفی می‌کند (فقط ۴ نویسه‌ی آخر)."""
    if key in SENSITIVE_KEYS and value:
        return "•" * 8 + value[-4:] if len(value) > 8 else "•" * 8
    return value
