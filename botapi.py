"""
botapi.py — کلاینت سبک Telegram Bot API (فقط برای «پنل کنترل» اختیاری).

* فقط با کتابخانه‌ی استاندارد پایتون (urllib)؛ هیچ وابستگی تازه‌ای اضافه نمی‌کند.
* توکن هرگز در پیام خطا یا لاگ نمی‌آید.
* انتقال شبکه (transport) قابل تزریق است تا در تست‌ها بدون اینترنت اجرا شود.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
# transport(method, body, content_type, timeout) -> (status, response_bytes)
Transport = Callable[[str, bytes, str, float], tuple[int, bytes]]


class BotApiError(RuntimeError):
    def __init__(self, method: str, description: str, code: int = 0, retry_after: int = 0) -> None:
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.code = code
        self.retry_after = retry_after


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{safe}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class BotApi:
    def __init__(self, token: str, transport: Transport | None = None, timeout: float = 15.0) -> None:
        self._token = token
        self._transport = transport or self._urllib_transport
        self._timeout = timeout

    # --- انتقال واقعی (همگام؛ در thread اجرا می‌شود)
    def _urllib_transport(self, method: str, body: bytes, content_type: str, timeout: float) -> tuple[int, bytes]:
        req = urllib.request.Request(
            f"{API_ROOT}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (آدرس ثابت HTTPS)
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    async def _send(self, method: str, body: bytes, content_type: str, timeout: float) -> Any:
        try:
            status, raw = await asyncio.to_thread(self._transport, method, body, content_type, timeout)
        except Exception as exc:  # noqa: BLE001 — پیام اصلی ممکن است شامل آدرس/توکن باشد
            raise BotApiError(method, f"network error ({type(exc).__name__})") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BotApiError(method, f"invalid response (HTTP {status})", status) from None
        if not payload.get("ok"):
            params = payload.get("parameters") or {}
            raise BotApiError(
                method, str(payload.get("description", "error")),
                int(payload.get("error_code", status)), int(params.get("retry_after", 0)),
            )
        return payload.get("result")

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        return await self._send(method, body, "application/json", timeout or self._timeout)

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, caption: str | None = None
    ) -> Any:
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        body, ctype = _multipart(fields, "document", filename, data)
        return await self._send("sendDocument", body, ctype, 60.0)

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        info = await self.call("getFile", {"file_id": file_id})
        size = info.get("file_size")
        if size is not None and size > max_bytes:
            raise BotApiError("getFile", "file too large")
        path = info.get("file_path")
        if not path:
            raise BotApiError("getFile", "no file_path")

        def fetch() -> bytes:
            with urllib.request.urlopen(  # noqa: S310
                f"{API_ROOT}/file/bot{self._token}/{path}", timeout=60
            ) as resp:
                data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("too large")
            return data

        try:
            return await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001
            raise BotApiError("download", f"download failed ({type(exc).__name__})") from None
