# ایمیج سبک و استاندارد پایتون
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/assistant.db

WORKDIR /app

# کاربر غیر root + پوشه‌ی داده برای دیتابیس SQLite (باید روی Volume پایدار mount شود)
RUN useradd --create-home --uid 1000 appuser \
    && mkdir /data \
    && chown appuser:appuser /data

# ابتدا وابستگی‌ها (برای استفاده‌ی بهتر از کش لایه‌های Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

USER appuser
VOLUME ["/data"]

# ربات با Long Polling کار می‌کند؛ نیازی به باز کردن پورت نیست.
CMD ["python", "main.py"]
