# انتخاب پایتون
FROM python:3.12-slim

# ست کردن دایرکتوری کاری
WORKDIR /app

# کپی کردن فایل‌ها
COPY . /app

# نصب پکیج‌ها
RUN pip install --no-cache-dir docker python-telegram-bot==20.6 aiosqlite

# ران شدن برنامه
CMD ["python", "main.py"]نامه
CMD ["python", "main.py"]