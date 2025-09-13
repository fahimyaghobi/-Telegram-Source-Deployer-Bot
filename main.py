#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Full-featured Telegram runner bot that creates user folders, Dockerfiles,
builds per-user images (or uses base image), runs isolated containers, streams logs,
handles input() from user's scripts, expiry, admin panel, codes, etc.

Requirements:
  pip install python-telegram-bot==20.3 docker

Run:
  export BOT_TOKEN="your_bot_token_here"
  python3 bot.py
"""

import os
import sys
import sqlite3
import shutil
import zipfile
import secrets
import string
import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# Docker SDK
try:
    import docker
except Exception as e:
    print("Docker SDK not installed. Run: pip install docker")
    raise

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "7629417973:AAHmqVw6mR7sri5NV0roVCUOlJfZpiA-HIk")
ADMINS = set(map(int, os.getenv("ADMINS", "6707313716").split(",")))  # comma separated ids
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "t.me/Meafghan")

BASE = Path(".").resolve()
DB_PATH = BASE / "db.sqlite3"
USERS_DIR = BASE / "users"
DATA_DIR = BASE / "data"

# runtime config
IMAGE_BASE = os.getenv("IMAGE_BASE", "python:3.12-slim")  # base image to use
BUILD_PER_USER_IMAGE = os.getenv("BUILD_PER_USER_IMAGE", "0") == "1"  # if True, build image from user Dockerfile; otherwise use base image
MAX_UPLOADS_PER_USER = int(os.getenv("MAX_UPLOADS_PER_USER", "10"))
MAX_SOURCE_SIZE_BYTES = int(os.getenv("MAX_SOURCE_SIZE_BYTES", 1 * 1024 * 1024))  # 1MB default
PROCESS_TIMEOUT = int(os.getenv("PROCESS_TIMEOUT", "0"))  # seconds, 0 means no auto-timeout
MAX_CONCURRENT_RUNS_PER_USER = int(os.getenv("MAX_CONCURRENT_RUNS_PER_USER", "5"))

# ensure dirs
for d in (USERS_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)

# docker client
docker_client = docker.from_env()

# running map: user_id_str -> filename -> info
running: Dict[str, Dict[str, dict]] = {}
waiting_input: Dict[str, dict] = {}

# ========== DB helpers & migration ==========
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS codes (
        code TEXT PRIMARY KEY,
        expires TEXT,
        days INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0,
        owner TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        created TEXT,
        expires TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        name TEXT,
        created TEXT,
        logpath TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        k TEXT PRIMARY KEY,
        v TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats (
        k TEXT PRIMARY KEY,
        v INTEGER DEFAULT 0
    )""")
    for k in ("runs", "total_users", "expired"):
        cur.execute("INSERT OR IGNORE INTO stats(k,v) VALUES(?,?)", (k, 0))
    conn.commit()
    conn.close()

def db_exec(query: str, params: Tuple = ()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    conn.close()

def db_query(query: str, params: Tuple = ()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

# ========== code helpers ==========
def generate_code(n: int = 12) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))

def create_codes(days: int, count: int):
    created = []
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    for _ in range(count):
        code = generate_code()
        db_exec("INSERT INTO codes(code,expires,days,used,owner) VALUES(?,?,?,?,?)",
                (code, expires, days, 0, None))
        created.append(code)
    return created

def get_code_info(code: str) -> Optional[dict]:
    rows = db_query("SELECT code,expires,days,used,owner FROM codes WHERE code=?", (code,))
    if not rows:
        return None
    c, expires, days, used, owner = rows[0]
    return {"code": c, "expires": expires, "days": days, "used": bool(used), "owner": owner}

def mark_code_used(code: str, owner: str):
    db_exec("UPDATE codes SET used=1, owner=? WHERE code=?", (owner, code))

# ========== user helpers ==========
def create_user(user_id: str, expires_iso: str):
    created = datetime.utcnow().isoformat()
    db_exec("INSERT OR REPLACE INTO users(user_id,created,expires) VALUES(?,?,?)",
            (user_id, created, expires_iso))
    db_exec("UPDATE stats SET v = v + 1 WHERE k='total_users'")

def get_user(user_id: str) -> Optional[dict]:
    rows = db_query("SELECT user_id,created,expires FROM users WHERE user_id=?", (user_id,))
    if not rows:
        return None
    u, c, e = rows[0]
    return {"user_id": u, "created": c, "expires": e}

def revoke_user(user_id: str):
    # delete from users; note: cleanup of filesystem+container done elsewhere
    db_exec("DELETE FROM users WHERE user_id=?", (user_id,))
    db_exec("UPDATE stats SET v = v + 1 WHERE k='expired'")

def get_setting(k: str) -> Optional[str]:
    rows = db_query("SELECT v FROM settings WHERE k=?", (k,))
    return rows[0][0] if rows else None

def set_setting(k: str, v: str):
    db_exec("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))

def inc_stat(k: str, n: int = 1):
    db_exec("UPDATE stats SET v = v + ? WHERE k=?", (n, k))

def get_stats() -> dict:
    rows = db_query("SELECT k,v FROM stats")
    return {k: v for k, v in rows}

# ========== FS helpers ==========
def ensure_user_folder(user_id: int) -> Tuple[Path, Path, Path, Path]:
    user_dir = USERS_DIR / str(user_id)
    src = user_dir / "src"
    logs = user_dir / "logs"
    user_dir.mkdir(parents=True, exist_ok=True)
    src.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    # runner & Dockerfile template
    runner = user_dir / "runner.py"
    if not runner.exists():
        runner.write_text(RUNNER_PY, encoding="utf-8")
    dockerfile = user_dir / "Dockerfile.user"
    if not dockerfile.exists():
        dockerfile.write_text(DOCKERFILE_USER.format(base_image=IMAGE_BASE), encoding="utf-8")
    return user_dir, src, logs, runner

def user_upload_count(user_id: int) -> int:
    _, src, _ = ensure_user_folder(user_id)[:3]
    return len([p for p in src.iterdir() if p.is_file()])

# ========== Runner wrapper (placed in user folder) ==========
RUNNER_PY = r'''
import builtins, sys
orig_input = builtins.input
def bot_input(prompt=''):
    print("__BOT_INPUT_REQUEST__" + (str(prompt) if prompt is not None else ""))
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        return ""
    return line.rstrip("\n")
builtins.input = bot_input

if __name__ == "__main__":
    import sys, traceback
    if len(sys.argv) < 2:
        print("No script specified.")
        sys.exit(1)
    script = sys.argv[1]
    try:
        with open(script, "r", encoding="utf-8") as f:
            code = f.read()
        g = {"__name__": "__main__"}
        exec(compile(code, script, "exec"), g)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
'''

DOCKERFILE_USER = """
FROM {base_image}
WORKDIR /home/user
# non-root user
RUN useradd -m -s /bin/bash runner || true
USER runner
# copy runner and src on mount, so nothing copied here; we just ensure python exists
CMD ["tail","-f","/dev/null"]
"""

# ========== Docker helpers ==========
def build_user_image(user_id: int) -> Optional[str]:
    """
    Build a user-specific image from user's Dockerfile.user (optional).
    Returns tag or None on failure.
    """
    user_dir = USERS_DIR / str(user_id)
    dockerfile = user_dir / "Dockerfile.user"
    tag = f"bot_user_image:{user_id}"
    try:
        print(f"[docker] building image for user {user_id} ...")
        # use low-level API to avoid blocking too long; here we use client.images.build
        img, logs = docker_client.images.build(path=str(user_dir), tag=tag, rm=True)
        return tag
    except Exception as e:
        print("Error building image:", e)
        return None

def create_user_container(user_id: int, image_tag: str = None) -> Optional[str]:
    """
    Create and start container for user. Returns container id or None.
    """
    name = f"bot_user_{user_id}"
    user_dir = USERS_DIR / str(user_id)
    try:
        # if exists remove older container
        try:
            old = docker_client.containers.get(name)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass
        image = image_tag or IMAGE_BASE
        # run container, mount user_dir to /home/user
        container = docker_client.containers.run(
            image,
            command=["tail", "-f", "/dev/null"],
            name=name,
            detach=True,
            stdin_open=True,
            tty=True,
            network_disabled=True,
            security_opt=["no-new-privileges"],
            volumes={str(user_dir.resolve()): {"bind": "/home/user", "mode": "rw"}}
        )
        return container.id
    except Exception as e:
        print("Error creating container:", e)
        return None

def stop_and_remove_container(user_id: int):
    name = f"bot_user_{user_id}"
    try:
        c = docker_client.containers.get(name)
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        print("Error stopping container:", e)

# ========== Execution: run script inside ephemeral container using user's image and runner ==========
async def stream_container_exec(container_cmd: list, uid: int, filename: str, user_dir: Path, logs_dir: Path, context: ContextTypes.DEFAULT_TYPE):
    """
    Use docker run -i ... runner.py script approach by calling subprocess to get stdout/stdin.
    (Using docker SDK's attach is possible but subprocess gives a simple streaming that works on many systems.)
    """
    uid_key = str(uid)
    logfile = logs_dir / f"{filename}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)

    # Start process via subprocess (docker run ...) for streaming stdin/stdout.
    # Build command
    try:
        proc = await asyncio.create_subprocess_exec(*container_cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.PIPE)
    except Exception as e:
        await context.bot.send_message(chat_id=int(uid), text=f"❌ خطا در اجرای کانتینر: {e}")
        return

    # register
    running.setdefault(uid_key, {})
    running[uid_key][filename] = {"proc": proc, "stdin": proc.stdin, "task": None, "log": logfile}

    try:
        with logfile.open("a", encoding="utf-8") as lf:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                lf.write(text + "\n")
                lf.flush()

                # detect sentinel for input
                if text.startswith("__BOT_INPUT_REQUEST__"):
                    prompt = text.replace("__BOT_INPUT_REQUEST__", "", 1)
                    waiting_input[uid_key] = {"filename": filename, "proc_stdin": proc.stdin}
                    try:
                        await context.bot.send_message(chat_id=int(uid), text=f"🔐 اسکریپت از شما ورودی خواست:\n{prompt}\nلطفاً پاسخ را ارسال کنید.")
                    except Exception:
                        pass
                    continue

                # send log line (throttle if needed)
                try:
                    await context.bot.send_message(chat_id=int(uid), text=f"📜 {text}")
                except Exception:
                    pass

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            await proc.wait()
        except Exception:
            pass
        # cleanup
        if uid_key in running:
            running[uid_key].pop(filename, None)
            if not running[uid_key]:
                running.pop(uid_key, None)
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"✅ اجرای `{filename}` پایان یافت.")
        except Exception:
            pass

async def run_user_script(uid: int, filename: str, context: ContextTypes.DEFAULT_TYPE):
    user_dir, src_dir, logs_dir, runner = ensure_user_folder(uid)
    path = src_dir / filename
    if not path.exists():
        return None, "file_not_found"
    if user_upload_count(uid) > MAX_UPLOADS_PER_USER:
        return None, "quota_exceeded"
    uid_key = str(uid)
    if len(running.get(uid_key, {})) >= MAX_CONCURRENT_RUNS_PER_USER:
        return None, "too_many_runs"

    # choose image: build if configured otherwise use base image
    image_tag = None
    if BUILD_PER_USER_IMAGE:
        tag = build_user_image(uid)
        if tag:
            image_tag = tag

    # prepare docker run command (ephemeral) that runs /home/user/runner.py /home/user/src/filename
    user_host_dir = str((USERS_DIR / str(uid)).resolve())
    docker_cmd = [
        "docker", "run", "--rm", "-i",
        "--network", "none",
        "--security-opt", "no-new-privileges",
        "-v", f"{user_host_dir}:/home/user:rw",
        image_tag or IMAGE_BASE,
        "python", "-u", f"/home/user/runner.py", f"/home/user/src/{filename}"
    ]

    # start streaming
    asyncio_task = asyncio.create_task(stream_container_exec(docker_cmd, uid, filename, user_dir, logs_dir, context))
    running.setdefault(uid_key, {})
    running[uid_key][filename] = running[uid_key].get(filename, {})
    running[uid_key][filename]["task"] = asyncio_task
    inc_stat("runs", 1)
    return asyncio_task, "started"

# ========== expiry worker ==========
async def send_zip_and_cleanup(user_id: str, context: ContextTypes.DEFAULT_TYPE):
    user_dir = USERS_DIR / str(user_id)
    if not user_dir.exists():
        revoke_user(user_id)
        return
    zip_path = DATA_DIR / f"user_{user_id}_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(user_dir):
            for f in files:
                fp = Path(root) / f
                arc = fp.relative_to(user_dir.parent)
                zf.write(fp, arc)
    # send zip to admins
    for a in ADMINS:
        try:
            await context.bot.send_document(chat_id=int(a), document=InputFile(str(zip_path)),
                                            caption=f"📦 backup user {user_id} (expired)")
        except Exception:
            pass
    # send to user if possible
    try:
        await context.bot.send_document(chat_id=int(user_id), document=InputFile(str(zip_path)),
                                        caption="📦 backup before account removal (expired)")
    except Exception:
        pass

    # stop running procs and remove containers + folder
    uid_key = str(user_id)
    if uid_key in running:
        for fname, info in list(running[uid_key].items()):
            try:
                info["proc"].kill()
            except Exception:
                pass
            t = info.get("task")
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
        running.pop(uid_key, None)

    stop_and_remove_container(user_id)
    try:
        shutil.rmtree(user_dir)
    except Exception:
        pass
    revoke_user(user_id)

async def expiry_worker(context: ContextTypes.DEFAULT_TYPE):
    rows = db_query("SELECT user_id,expires FROM users")
    now = datetime.utcnow()
    removed = []
    for uid, expires in rows:
        try:
            exp_dt = datetime.fromisoformat(expires)
        except Exception:
            continue
        if now >= exp_dt:
            removed.append(uid)
            await send_zip_and_cleanup(uid, context)
    if removed:
        for a in ADMINS:
            try:
                await context.bot.send_message(chat_id=int(a), text=f"🔔 expired users removed: {', '.join(removed)}")
            except Exception:
                pass

# ========== Keyboards ==========
menu1 = ReplyKeyboardMarkup([[KeyboardButton("💳 خرید کد")], [KeyboardButton("🔑 وارد کردن کد")]], resize_keyboard=True)
menu2 = ReplyKeyboardMarkup([
    [KeyboardButton("📤 استقرار سورس"), KeyboardButton("📋 استقرارهای من")],
    [KeyboardButton("📜 لاگ‌ها"), KeyboardButton("📘 راهنما")]
], resize_keyboard=True)
admin_menu = ReplyKeyboardMarkup([
    [KeyboardButton("📢 ارسال پیام همگانی"), KeyboardButton("🖼 ارسال پیام همگانی با عکس")],
    [KeyboardButton("📝 تنظیم راهنما"), KeyboardButton("⛔ ابطال کاربر")],
    [KeyboardButton("🆕 ساخت کد"), KeyboardButton("🗑 ابطال کد")],
    [KeyboardButton("📊 آمار"), KeyboardButton("💾 اکسپورت DB")]
], resize_keyboard=True)

# ========== Handlers ==========
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(str(uid))
    if uid in ADMINS:
        await update.message.reply_text("⚙️ پنل ادمین", reply_markup=admin_menu)
        return
    if not user:
        await update.message.reply_text("سلام! برای ادامه یکی از گزینه‌ها را انتخاب کنید:", reply_markup=menu1)
    else:
        try:
            exp_dt = datetime.fromisoformat(user["expires"])
            if datetime.utcnow() >= exp_dt:
                revoke_user(str(uid))
                await update.message.reply_text("⏳ اشتراک شما منقضی شده. لطفاً کد جدید وارد کنید.", reply_markup=menu1)
                return
            await update.message.reply_text("✅ خوش آمدید — منوی اصلی:", reply_markup=menu2)
        except Exception:
            revoke_user(str(uid))
            await update.message.reply_text("خطا در وضعیت اکانت — لطفاً دوباره کد وارد کنید.", reply_markup=menu1)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # deliver waiting input to process if any
    if str(uid) in waiting_input:
        info = waiting_input.pop(str(uid))
        proc_stdin = info.get("proc_stdin")
        if proc_stdin:
            try:
                proc_stdin.write((text + "\n").encode())
                await proc_stdin.drain()
                await update.message.reply_text("✅ پاسخ ارسال شد به اسکریپت.")
            except Exception:
                await update.message.reply_text("❌ خطا در ارسال ورودی به اسکریپت.")
            return

    # admin actions
    if uid in ADMINS:
        if text == "🆕 ساخت کد":
            context.user_data["creating_codes"] = {"step": "ask_days"}
            await update.message.reply_text("چند روز اعتبار برای هر کد؟ (عدد وارد کن)")
            return
        if context.user_data.get("creating_codes"):
            data = context.user_data["creating_codes"]
            if data["step"] == "ask_days":
                try:
                    days = int(text); data["days"] = days; data["step"] = "ask_count"
                    await update.message.reply_text("چند کد بسازم؟ (عدد وارد کن)")
                except:
                    await update.message.reply_text("عدد معتبر وارد کن.")
                return
            elif data["step"] == "ask_count":
                try:
                    count = int(text); days = data["days"]
                    codes = create_codes(days, count)
                    await update.message.reply_text("✅ کدها ساخته شد:\n" + "\n".join(codes))
                    context.user_data.pop("creating_codes", None)
                except Exception:
                    await update.message.reply_text("خطا در ساخت کد.")
                return
        if text == "⛔ ابطال کاربر":
            context.user_data["waiting_revoke_user"] = True
            await update.message.reply_text("آیدی عددی کاربر را برای ابطال ارسال کن:")
            return
        if context.user_data.get("waiting_revoke_user"):
            target = text.strip()
            context.user_data.pop("waiting_revoke_user", None)
            await send_zip_and_cleanup(target, context)
            await update.message.reply_text(f"✅ کاربر {target} ابطال شد.")
            return
        if text == "📢 ارسال پیام همگانی":
            context.user_data["waiting_broadcast_text"] = True
            await update.message.reply_text("متن پیام همگانی را ارسال کن:")
            return
        if context.user_data.get("waiting_broadcast_text"):
            msg = text; context.user_data.pop("waiting_broadcast_text", None)
            rows = db_query("SELECT user_id FROM users")
            count = 0
            for (u,) in rows:
                try:
                    await context.bot.send_message(chat_id=int(u), text=f"📢 پیام ادمین:\n\n{msg}")
                    count += 1
                except:
                    pass
            await update.message.reply_text(f"ارسال شد به {count} کاربر.")
            return
        if text == "📊 آمار":
            s = get_stats()
            rows = db_query("SELECT COUNT(*) FROM users")
            total = rows[0][0] if rows else 0
            await update.message.reply_text(f"آمار:\nکل کاربران ثبت‌شده: {total}\nکل اجراها: {s.get('runs',0)}\nمنقضی‌شده‌ها: {s.get('expired',0)}")
            return
        if text == "💾 اکسپورت DB":
            if DB_PATH.exists():
                await update.message.reply_document(document=InputFile(str(DB_PATH)))
            else:
                await update.message.reply_text("DB موجود نیست.")
            return

    # non-admin flow
    if not get_user(str(uid)):
        if text == "💳 خرید کد":
            await update.message.reply_text(f"برای خرید، لطفا به ادمین پیام دهید: {ADMIN_CONTACT}")
            return
        if text == "🔑 وارد کردن کد":
            context.user_data["waiting_for_code"] = True
            await update.message.reply_text("لطفاً کد یک‌بارمصرف را ارسال کنید.")
            return
        if context.user_data.get("waiting_for_code"):
            code = text.strip(); context.user_data.pop("waiting_for_code", None)
            info = get_code_info(code)
            if not info:
                await update.message.reply_text("❌ کد نامعتبر. بررسی کن و دوباره ارسال کن.")
                return
            if info["used"]:
                await update.message.reply_text("❌ این کد قبلاً استفاده شده.")
                return
            try:
                exp_dt = datetime.fromisoformat(info["expires"])
                if datetime.utcnow() >= exp_dt:
                    await update.message.reply_text("❌ این کد منقضی شده.")
                    return
            except Exception:
                await update.message.reply_text("خطا در تاریخ کد.")
                return
            mark_code_used(code, str(uid))
            create_user(str(uid), info["expires"])
            ensure_user_folder(uid)
            # build & create persistent container (optional)
            if BUILD_PER_USER_IMAGE:
                build_user_image(uid)
            create_user_container(uid, image_tag=None)
            await update.message.reply_text("✅ کد پذیرفته شد — منوی اصلی فعال شد.", reply_markup=menu2)
            for a in ADMINS:
                try:
                    await context.bot.send_message(chat_id=int(a), text=f"🔔 کاربر {uid} وارد ربات شد. انقضا: {info['expires']}")
                except Exception:
                    pass
            return
        await update.message.reply_text("برای استفاده ابتدا کد وارد کن یا کد بخر.", reply_markup=menu1)
        return

    # user active: check expiry
    user = get_user(str(uid))
    try:
        exp_dt = datetime.fromisoformat(user["expires"])
    except Exception:
        revoke_user(str(uid)); await update.message.reply_text("خطا در وضعیت اکانت — دوباره کد وارد کن.", reply_markup=menu1); return
    if datetime.utcnow() >= exp_dt:
        await update.message.reply_text("اشتراک شما منقضی شده. پاکسازی انجام خواهد شد.", reply_markup=menu1)
        return

    # user menu actions
    if text == "📤 استقرار سورس":
        await update.message.reply_text("لطفاً فایل .py را ارسال کنید (هر فایل < 1MB و حداکثر 10 سورس).")
        return
    if text == "📋 استقرارهای من":
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        files = sorted([p.name for p in src_dir.iterdir() if p.is_file()])
        if not files:
            await update.message.reply_text("شما هیچ سورسی آپلود نکرده‌اید.")
            return
        kb = []
        for fn in files:
            kb.append([InlineKeyboardButton(fn, callback_data=f"deploy_item|{fn}")])
        await update.message.reply_text("استقرارهای شما:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if text == "📜 لاگ‌ها":
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        logs = sorted([p.name for p in logs_dir.iterdir() if p.is_file()], key=lambda p: (logs_dir / p).stat().st_mtime, reverse=True)
        if not logs:
            await update.message.reply_text("لاگی موجود نیست.")
            return
        kb = [[InlineKeyboardButton(l, callback_data=f"logfile|{l}")] for l in logs[:10]]
        await update.message.reply_text("لاگ‌ها:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if text == "📘 راهنما":
        help_text = get_setting("help") or "راهنما تنظیم نشده است."
        await update.message.reply_text(help_text)
        return
    if text.startswith("install "):
        pkg = text.split(" ",1)[1].strip()
        await update.message.reply_text(f"درحال نصب `{pkg}` در محیط شما ...")
        user_dir, src_dir, logs_dir, runner = ensure_user_folder(uid)
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--security-opt", "no-new-privileges",
               "-v", f"{str(user_dir.resolve())}:/home/user:rw", IMAGE_BASE, "pip", "install", pkg]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=600)
            await update.message.reply_text(f"✅ نصب `{pkg}` خروجی:\n{out.decode(errors='replace')[:1000]}")
        except subprocess.CalledProcessError as e:
            await update.message.reply_text(f"❌ خطا در نصب `{pkg}`:\n{e.output.decode(errors='replace')[:1000]}")
        except Exception as e:
            await update.message.reply_text(f"❌ خطا: {e}")
        return

    await update.message.reply_text("دکمه‌ای انتخاب کن یا از منو استفاده کن.", reply_markup=menu2)

# ========== File handler ==========
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(str(uid)):
        await update.message.reply_text("⚠️ ابتدا کد ورود را وارد کنید.", reply_markup=menu1)
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("فایلی ارسال نشده.")
        return
    if not doc.file_name.endswith(".py"):
        await update.message.reply_text("فقط فایل‌های .py پذیرفته می‌شوند.")
        return
    size = getattr(doc, "file_size", None)
    if size is not None and size > MAX_SOURCE_SIZE_BYTES:
        await update.message.reply_text("❌ فایل بزرگ‌تر از 1MB پذیرفته نمی‌شود.")
        return
    user_dir, src_dir, logs_dir, runner = ensure_user_folder(uid)
    if user_upload_count(uid) >= MAX_UPLOADS_PER_USER:
        await update.message.reply_text(f"⚠️ محدودیت: حداکثر {MAX_UPLOADS_PER_USER} سورس مجاز است. ابتدا یکی را حذف کنید.")
        return
    dest = src_dir / doc.file_name
    f = await doc.get_file()
    await f.download_to_drive(str(dest))
    if dest.stat().st_size > MAX_SOURCE_SIZE_BYTES:
        dest.unlink(missing_ok=True)
        await update.message.reply_text("❌ فایل پس از دانلود بزرگ‌تر از 1MB بود و حذف شد.")
        return
    db_exec("INSERT INTO deployments(user_id,name,created,logpath) VALUES(?,?,?,?)",
            (str(uid), doc.file_name, datetime.utcnow().isoformat(), str((logs_dir / (doc.file_name + '.log')).resolve())))
    await update.message.reply_text(f"✅ فایل `{doc.file_name}` ذخیره شد. می‌توانید آن را از 'استقرارهای من' اجرا کنید.")

# ========== CallbackQuery handler ==========
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("deploy_item|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        kb = [
            [InlineKeyboardButton("▶️ اجرا", callback_data=f"run|{filename}") ,
             InlineKeyboardButton("⏹️ توقف", callback_data=f"stop|{filename}")],
            [InlineKeyboardButton("📥 لاگ آخر", callback_data=f"log|{filename}"),
             InlineKeyboardButton("⬇️ دانلود سورس", callback_data=f"download|{filename}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"delete|{filename}")]
        ]
        await query.message.reply_text(f"مدیریت `{filename}`:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("run|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        res, status = await run_user_script(uid, filename, context)
        if res:
            await query.edit_message_text(f"▶️ اجرای `{filename}` آغاز شد — لاگ در همین چت ارسال می‌شود.")
        else:
            await query.edit_message_text(f"خطا در اجرای سورس: {status}")
        return

    if data.startswith("stop|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        uid_key = str(uid)
        entry = running.get(uid_key, {}).get(filename)
        if not entry:
            await query.edit_message_text("سورس در حال اجرا نیست.")
            return
        proc = entry.get("proc")
        try:
            proc.kill()
        except Exception:
            pass
        task = entry.get("task")
        if task:
            try:
                task.cancel()
            except Exception:
                pass
        running[uid_key].pop(filename, None)
        if not running.get(uid_key):
            running.pop(uid_key, None)
        await query.edit_message_text(f"⏹️ اجرای `{filename}` متوقف شد.")
        return

    if data.startswith("log|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        matches = sorted([p for p in logs_dir.iterdir() if p.name.startswith(filename)], key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            await query.edit_message_text("لاگ پیدا نشد.")
            return
        last = matches[0]
        if last.exists() and last.stat().st_size > 0:
            await query.message.reply_document(document=InputFile(str(last)))
        else:
            await query.edit_message_text("لاگ خالی است.")
        return

    if data.startswith("download|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        p = src_dir / filename
        if p.exists():
            await query.message.reply_document(document=InputFile(str(p)))
        else:
            await query.edit_message_text("سورس پیدا نشد.")
        return

    if data.startswith("delete|"):
        filename = data.split("|",1)[1]
        uid = query.from_user.id
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        # stop if running
        entry = running.get(str(uid), {}).get(filename)
        if entry:
            try:
                entry["proc"].kill()
            except Exception:
                pass
            t = entry.get("task")
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
            running[str(uid)].pop(filename, None)
            if not running.get(str(uid)):
                running.pop(str(uid), None)
        p = src_dir / filename
        if p.exists():
            p.unlink()
        l = logs_dir / (filename + ".log")
        if l.exists():
            l.unlink()
        await query.edit_message_text("✅ سورس و لاگ حذف شد.")
        return

    if data.startswith("logfile|"):
        logname = data.split("|",1)[1]
        uid = query.from_user.id
        user_dir, src_dir, logs_dir, _ = ensure_user_folder(uid)
        p = logs_dir / logname
        if p.exists():
            await query.message.reply_document(document=InputFile(str(p)))
        else:
            await query.edit_message_text("لاگ پیدا نشد.")
        return

    if data.startswith("admin:delcode|"):
        code = data.split("|",1)[1]
        db_exec("DELETE FROM codes WHERE code=?", (code,))
        await query.edit_message_text(f"✅ کد {code} حذف شد.")
        return

    if data.startswith("install_pkg|"):
        try:
            _, user_id, pkg = data.split("|",2)
        except ValueError:
            await query.edit_message_text("پارامتر نصب نامعتبر.")
            return
        vdir = USERS_DIR / user_id
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--security-opt", "no-new-privileges",
               "-v", f"{str(vdir.resolve())}:/home/user:rw", IMAGE_BASE, "pip", "install", pkg]
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=600)
            await query.edit_message_text(f"نصب `{pkg}` موفق:\n{out.decode(errors='replace')[:1000]}")
        except subprocess.CalledProcessError as e:
            await query.edit_message_text(f"خطا:\n{e.output.decode(errors='replace')[:1000]}")
        except Exception as e:
            await query.edit_message_text(f"خطا: {e}")
        return

    await query.edit_message_text("دکمه نامعتبر یا قدیمی است.")

# ========== Admin textual commands ==========
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS:
        await update.message.reply_text("فقط ادمین.")
        return
    await update.message.reply_text("پنل ادمین:", reply_markup=admin_menu)

async def create_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /create_codes <days> <count>")
        return
    days = int(context.args[0]); count = int(context.args[1])
    codes = create_codes(days, count)
    await update.message.reply_text("Created codes:\n" + "\n".join(codes))

async def list_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS: return
    rows = db_query("SELECT code,expires,used,owner FROM codes")
    if not rows:
        await update.message.reply_text("No codes.")
        return
    text = "\n".join([f"{r[0]} - expires:{r[1]} - used:{bool(r[2])} - owner:{r[3]}" for r in rows])
    if len(text) < 4000:
        await update.message.reply_text(text)
    else:
        p = DATA_DIR / "codes_export.txt"
        p.write_text(text, encoding="utf-8")
        await update.message.reply_document(document=InputFile(str(p)))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS:
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    rows = db_query("SELECT user_id FROM users")
    count = 0
    for (u,) in rows:
        try:
            await context.bot.send_message(chat_id=int(u), text=f"📢 پیام ادمین:\n\n{text}")
            count += 1
        except Exception:
            pass
    await update.message.reply_text(f"ارسال شد به {count} کاربر.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS: return
    s = get_stats()
    rows = db_query("SELECT COUNT(*) FROM users")
    total = rows[0][0] if rows else 0
    await update.message.reply_text(f"آمار:\nکل کاربران: {total}\nکل اجراها: {s.get('runs',0)}\nمنقضی‌شده‌ها: {s.get('expired',0)}")

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS: return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    target = context.args[0]
    await send_zip_and_cleanup(target, context)
    await update.message.reply_text(f"دسترسی {target} ابطال شد.")

async def export_db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS: return
    if DB_PATH.exists():
        await update.message.reply_document(document=InputFile(str(DB_PATH)))
    else:
        await update.message.reply_text("DB not found.")

# ========== Register & run ==========
def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("create_codes", create_codes_cmd))
    app.add_handler(CommandHandler("list_codes", list_codes_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("revoke", revoke_cmd))
    app.add_handler(CommandHandler("export_db", export_db_cmd))

def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        print("Set BOT_TOKEN environment variable or edit the file.")
        sys.exit(1)
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    register_handlers(app)
    # expiry check
    app.job_queue.run_repeating(expiry_worker, interval=60, first=10)
    print("Bot started — ensure Docker is installed and the current user can run docker commands.")
    app.run_polling()

if __name__ == "__main__":
    main()