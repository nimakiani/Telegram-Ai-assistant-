# اجرای اختیاری با Docker (برای نصب معمولی روی Ubuntu از ./install.sh استفاده کنید).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/assistant.db \
    TG_SESSION_PATH=/data/session/assistant \
    LOCK_PATH=/data/assistant.lock \
    TEMP_DIR=/data/tmp

WORKDIR /app

# کاربر غیر root + پوشه‌ی داده (دیتابیس، Session و فایل‌های موقت؛ باید روی Volume پایدار باشد)
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/session /data/tmp \
    && chown -R appuser:appuser /data \
    && chmod 700 /data/session

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY tglayer/ ./tglayer/

USER appuser
VOLUME ["/data"]

# اتصال MTProto خروجی است؛ نیازی به باز کردن پورت نیست.
# ورود اولیه (یک‌بار، تعاملی):  docker compose run --rm assistant python main.py --setup
CMD ["python", "main.py"]
