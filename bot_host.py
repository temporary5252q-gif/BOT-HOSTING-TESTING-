# ============================================================
# KRUTIK CYBER EXPERT
# Multi Client Telegram Python Bot Hosting Manager
# Version 4.0
# ============================================================

import os
import re
import sys
import json
import time
import signal
import sqlite3
import subprocess
import threading
import ast
import shutil
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ============================================================
# CONFIG
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"
VERSION = "4.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        str(BASE_DIR / "host_data")
    )
).expanduser()

CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "hosting.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# OWNER CHAT ID
# ============================================================

try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW)
except Exception:
    OWNER_CHAT_ID = 0


# ============================================================
# GLOBALS
# ============================================================

processes = {}
process_lock = threading.Lock()

pending_actions = {}
pending_lock = threading.Lock()

offset = 0


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            chat_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            last_seen TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            pid INTEGER DEFAULT 0,
            auto_restart INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # ========================================================
    # NEW: HOSTING ACCESS CONTROL
    # ========================================================

    conn.execute("""
        CREATE TABLE IF NOT EXISTS hosting_access (
            chat_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# TIME
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# TELEGRAM API
# ============================================================

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, files=None, timeout=60):
    try:
        if files:
            r = requests.post(
                f"{API_URL}/{method}",
                data=data or {},
                files=files,
                timeout=timeout
            )
        else:
            r = requests.post(
                f"{API_URL}/{method}",
                json=data or {},
                timeout=timeout
            )

        return r.json()

    except Exception as e:
        print(f"[Telegram Error] {e}")
        return None


def send_message(
    chat_id,
    text,
    reply_markup=None,
    parse_mode="HTML"
):
    data = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }

    if parse_mode:
        data["parse_mode"] = parse_mode

    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    return telegram("sendMessage", data)


def edit_message(
    chat_id,
    message_id,
    text,
    reply_markup=None,
    parse_mode="HTML"
):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }

    if parse_mode:
        data["parse_mode"] = parse_mode

    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    return telegram("editMessageText", data)


def answer_callback(callback_id, text=""):
    return telegram(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": False
        }
    )


def get_file(file_id):
    return telegram(
        "getFile",
        {"file_id": file_id}
    )


def download_telegram_file(file_id, destination):
    result = get_file(file_id)

    if not result or not result.get("ok"):
        raise RuntimeError("Telegram file information unavailable.")

    file_path = result["result"]["file_path"]

    url = (
        f"https://api.telegram.org/file/bot"
        f"{BOT_TOKEN}/{file_path}"
    )

    r = requests.get(url, timeout=120)
    r.raise_for_status()

    destination = Path(destination)

    with open(destination, "wb") as f:
        f.write(r.content)

    return destination


# ============================================================
# HELPERS
# ============================================================

def html_escape(value):
    value = str(value or "")

    return (
        value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def user_display(row):
    username = row["username"] or ""

    if username:
        return f"@{html_escape(username)}"

    name = (
        f"{row['first_name'] or ''} "
        f"{row['last_name'] or ''}"
    ).strip()

    return html_escape(name or "Unknown")


def is_owner(chat_id):
    try:
        return int(chat_id) == OWNER_CHAT_ID
    except Exception:
        return False


# ============================================================
# CLIENT ACCESS
# ============================================================

def save_client(user):
    chat_id = int(user["id"])

    username = user.get("username", "") or ""
    first_name = user.get("first_name", "") or ""
    last_name = user.get("last_name", "") or ""

    conn = get_db()

    existing = conn.execute(
        "SELECT chat_id FROM clients WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE clients
            SET username=?,
                first_name=?,
                last_name=?,
                last_seen=?
            WHERE chat_id=?
        """, (
            username,
            first_name,
            last_name,
            now(),
            chat_id
        ))

        is_new = False

    else:
        conn.execute("""
            INSERT INTO clients
            (
                chat_id,
                username,
                first_name,
                last_name,
                enabled,
                created_at,
                last_seen
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """, (
            chat_id,
            username,
            first_name,
            last_name,
            now(),
            now()
        ))

        is_new = True

    conn.commit()
    conn.close()

    return is_new


# ============================================================
# NEW HOSTING ACCESS SYSTEM
# ============================================================

def has_hosting_access(chat_id):
    chat_id = int(chat_id)

    # Owner always has access.
    if is_owner(chat_id):
        return True

    conn = get_db()

    row = conn.execute("""
        SELECT enabled
        FROM hosting_access
        WHERE chat_id=?
    """, (chat_id,)).fetchone()

    conn.close()

    return bool(row and row["enabled"] == 1)


def grant_hosting_access(chat_id, user=None):
    chat_id = int(chat_id)

    username = ""
    first_name = ""
    last_name = ""

    if user:
        username = user.get("username", "") or ""
        first_name = user.get("first_name", "") or ""
        last_name = user.get("last_name", "") or ""

    conn = get_db()

    existing = conn.execute(
        "SELECT chat_id FROM hosting_access WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE hosting_access
            SET username=?,
                first_name=?,
                last_name=?,
                enabled=1,
                updated_at=?
            WHERE chat_id=?
        """, (
            username,
            first_name,
            last_name,
            now(),
            chat_id
        ))

    else:
        conn.execute("""
            INSERT INTO hosting_access
            (
                chat_id,
                username,
                first_name,
                last_name,
                enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
        """, (
            chat_id,
            username,
            first_name,
            last_name,
            now(),
            now()
        ))

    conn.commit()
    conn.close()


def revoke_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False

    conn = get_db()

    conn.execute("""
        UPDATE hosting_access
        SET enabled=0,
            updated_at=?
        WHERE chat_id=?
    """, (
        now(),
        chat_id
    ))

    conn.commit()
    conn.close()

    return True


def delete_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False

    conn = get_db()

    conn.execute(
        "DELETE FROM hosting_access WHERE chat_id=?",
        (chat_id,)
    )

    conn.commit()
    conn.close()

    return True


def get_allowed_users():
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM hosting_access
        WHERE enabled=1
        ORDER BY created_at DESC
    """).fetchall()

    conn.close()

    return rows


def get_access_user(chat_id):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM hosting_access
        WHERE chat_id=?
    """, (int(chat_id),)).fetchone()

    conn.close()

    return row


# ============================================================
# ACCESS DENIED
# ============================================================

def send_access_denied(chat_id):
    text = f"""
🚫 <b>Hosting Access Denied</b>

Aapke Chat ID ko abhi hosting access nahi diya gaya.

<b>Chat ID:</b>
<code>{chat_id}</code>

Owner se hosting access lene ke baad aap bot host kar sakte hain.

👑 <b>{APP_NAME}</b>
"""

    send_message(chat_id, text)


# ============================================================
# CLIENT ENABLE / DISABLE
# ============================================================

def client_enabled(chat_id):
    if is_owner(chat_id):
        return True

    conn = get_db()

    row = conn.execute("""
        SELECT enabled
        FROM clients
        WHERE chat_id=?
    """, (int(chat_id),)).fetchone()

    conn.close()

    if not row:
        return True

    return row["enabled"] == 1


def set_client_enabled(chat_id, enabled):
    conn = get_db()

    conn.execute("""
        UPDATE clients
        SET enabled=?
        WHERE chat_id=?
    """, (
        1 if enabled else 0,
        int(chat_id)
    ))

    conn.commit()
    conn.close()


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(owner_chat_id, name, filename, folder):
    conn = get_db()

    cur = conn.execute("""
        INSERT INTO bots
        (
            owner_chat_id,
            name,
            filename,
            folder,
            status,
            pid,
            auto_restart,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 'stopped', 0, 1, ?, ?)
    """, (
        int(owner_chat_id),
        name,
        filename,
        str(folder),
        now(),
        now()
    ))

    bot_id = cur.lastrowid

    conn.commit()
    conn.close()

    return bot_id


def get_bot(bot_id):
    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bots
        WHERE id=?
    """, (int(bot_id),)).fetchone()

    conn.close()

    return row


def get_user_bots(chat_id):
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        WHERE owner_chat_id=?
        ORDER BY id DESC
    """, (int(chat_id),)).fetchall()

    conn.close()

    return rows


def get_all_bots():
    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return rows


def update_bot_status(bot_id, status, pid=0):
    conn = get_db()

    conn.execute("""
        UPDATE bots
        SET status=?,
            pid=?,
            updated_at=?
        WHERE id=?
    """, (
        status,
        int(pid or 0),
        now(),
        int(bot_id)
    ))

    conn.commit()
    conn.close()


# ============================================================
# DEPENDENCY DETECTION
# ============================================================

IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot==22.5",
    "telegram.ext": "python-telegram-bot==22.5",

    "openai": "openai>=1.50.0,<2",

    "requests": "requests",
    "httpx": "httpx",
    "aiohttp": "aiohttp",

    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",

    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",

    "dotenv": "python-dotenv",
    "yaml": "PyYAML",

    "Crypto": "pycryptodome",

    "numpy": "numpy",
    "pandas": "pandas",

    "qrcode": "qrcode",
    "schedule": "schedule",
    "rich": "rich",
    "colorama": "colorama",

    "selenium": "selenium",
    "jwt": "PyJWT",

    "google": "google-api-python-client",

    "discord": "discord.py",
    "psutil": "psutil"
}


def get_stdlib_modules():
    try:
        return set(sys.stdlib_module_names)
    except Exception:
        return {
            "os",
            "sys",
            "time",
            "json",
            "re",
            "math",
            "random",
            "datetime",
            "pathlib",
            "sqlite3",
            "threading",
            "subprocess",
            "asyncio",
            "logging",
            "typing",
            "collections",
            "itertools",
            "functools",
            "signal",
            "shutil",
            "socket",
            "http",
            "urllib",
            "hashlib",
            "base64"
        }


def detect_imports(script_path):
    script_path = Path(script_path)

    source = script_path.read_text(
        encoding="utf-8",
        errors="ignore"
    )

    tree = ast.parse(source)

    modules = set()

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])

    return modules


def detect_dependencies(script_path):
    modules = detect_imports(script_path)

    stdlib = get_stdlib_modules()

    local_modules = {
        p.stem
        for p in Path(script_path).parent.glob("*.py")
    }

    packages = set()

    for module in modules:

        if module in stdlib:
            continue

        if module in local_modules:
            continue

        package = IMPORT_TO_PACKAGE.get(module)

        if package:
            packages.add(package)

        else:
            # Best effort:
            # module name ko package name maana jayega.
            if re.match(r"^[A-Za-z0-9_.-]+$", module):
                packages.add(module)

    return sorted(packages)


# ============================================================
# VENV
# ============================================================

def venv_dir(bot_folder):
    return Path(bot_folder) / ".venv"


def venv_python(bot_folder):
    venv = venv_dir(bot_folder)

    if os.name == "nt":
        return venv / "Scripts" / "python.exe"

    return venv / "bin" / "python"


def ensure_venv(bot_folder):
    bot_folder = Path(bot_folder)

    venv = venv_dir(bot_folder)
    python = venv_python(bot_folder)

    if not python.exists():

        print(
            f"[VENV] Creating virtual environment: {venv}"
        )

        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                str(venv)
            ],
            cwd=str(bot_folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300
        )

    if not python.exists():
        raise RuntimeError(
            "Could not create bot virtual environment."
        )

    return python


def install_dependencies(bot_folder, script_path):
    python = ensure_venv(bot_folder)

    packages = detect_dependencies(script_path)

    if not packages:
        return True, []

    print(
        f"[DEPENDENCIES] {bot_folder}: "
        f"{packages}"
    )

    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip"
    ]

    try:
        subprocess.run(
            command,
            cwd=str(bot_folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600
        )

        result = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install"
            ] + packages,
            cwd=str(bot_folder),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1200
        )

        log_file = Path(bot_folder) / "pip_install.log"

        log_file.write_text(
            result.stdout or "",
            encoding="utf-8"
        )

        if result.returncode != 0:
            return False, packages

        return True, packages

    except Exception as e:

        log_file = Path(bot_folder) / "pip_install.log"

        log_file.write_text(
            str(e),
            encoding="utf-8"
        )

        return False, packages


# ============================================================
# SYNTAX CHECK
# ============================================================

def syntax_check(script_path):
    try:
        source = Path(script_path).read_text(
            encoding="utf-8",
            errors="ignore"
        )

        compile(
            source,
            str(script_path),
            "exec"
        )

        return True, ""

    except Exception as e:
        return False, str(e)


# ============================================================
# PROCESS CONTROL
# ============================================================

def write_log(bot_folder, text):
    log_file = Path(bot_folder) / "bot.log"

    with open(
        log_file,
        "a",
        encoding="utf-8",
        errors="ignore"
    ) as f:

        f.write(
            f"\n[{now()}] {text}\n"
        )


def start_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    owner_id = int(bot["owner_chat_id"])

    if not client_enabled(owner_id):
        return False, "Client is disabled."

    if not has_hosting_access(owner_id):
        return False, "Hosting access revoked."

    folder = Path(bot["folder"])
    script = folder / bot["filename"]

    if not folder.exists():
        return False, "Bot folder does not exist."

    if not script.exists():
        return False, "Bot Python file does not exist."

    # ========================================================
    # SYNTAX CHECK
    # ========================================================

    ok, error = syntax_check(script)

    if not ok:
        update_bot_status(bot_id, "error", 0)

        write_log(
            folder,
            f"SYNTAX ERROR: {error}"
        )

        return False, f"Syntax Error:\n{error}"

    # ========================================================
    # DEPENDENCIES
    # ========================================================

    ok, packages = install_dependencies(
        folder,
        script
    )

    if not ok:

        update_bot_status(
            bot_id,
            "dependency_error",
            0
        )

        return False, (
            "Dependency installation failed.\n\n"
            + "\n".join(packages)
        )

    python = venv_python(folder)

    if not python.exists():
        return False, "Bot Python environment missing."

    # Already running?
    with process_lock:

        old = processes.get(int(bot_id))

        if old and old.poll() is None:
            return True, "Bot already running."

    log_file = folder / "bot.log"

    log_handle = open(
        log_file,
        "a",
        encoding="utf-8",
        errors="ignore"
    )

    log_handle.write(
        f"\n\n===== START {now()} =====\n"
    )

    log_handle.flush()

    try:

        kwargs = {
            "cwd": str(folder),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT
        }

        if os.name != "nt":
            kwargs["start_new_session"] = True

        process = subprocess.Popen(
            [
                str(python),
                str(script)
            ],
            **kwargs
        )

    except Exception as e:

        log_handle.close()

        update_bot_status(
            bot_id,
            "error",
            0
        )

        write_log(
            folder,
            f"START ERROR: {e}"
        )

        return False, str(e)

    with process_lock:
        processes[int(bot_id)] = process

    update_bot_status(
        bot_id,
        "running",
        process.pid
    )

    threading.Thread(
        target=watch_process,
        args=(int(bot_id), process, log_handle),
        daemon=True
    ).start()

    return True, (
        f"Bot started successfully.\n"
        f"PID: {process.pid}"
    )


def stop_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    pid = int(bot["pid"] or 0)

    with process_lock:
        process = processes.get(int(bot_id))

    if process and process.poll() is None:

        try:

            if os.name != "nt":

                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGTERM
                )

            else:
                process.terminate()

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:

                if os.name != "nt":

                    try:
                        os.killpg(
                            os.getpgid(process.pid),
                            signal.SIGKILL
                        )
                    except Exception:
                        pass

                else:
                    process.kill()

        except Exception as e:

            write_log(
                Path(bot["folder"]),
                f"STOP ERROR: {e}"
            )

    elif pid > 0:

        try:

            os.kill(pid, signal.SIGTERM)

        except Exception:
            pass

    with process_lock:
        processes.pop(int(bot_id), None)

    update_bot_status(
        bot_id,
        "stopped",
        0
    )

    return True, "Bot stopped."


def restart_bot(bot_id):
    stop_bot(bot_id)

    time.sleep(1)

    return start_bot(bot_id)


# ============================================================
# PROCESS WATCHER
# ============================================================

def watch_process(bot_id, process, log_handle):

    try:
        return_code = process.wait()

    except Exception as e:

        return_code = -999

        print(
            f"[WATCH] Error bot {bot_id}: {e}"
        )

    try:
        log_handle.write(
            f"\n===== EXIT {now()} "
            f"CODE={return_code} =====\n"
        )

        log_handle.flush()
        log_handle.close()

    except Exception:
        pass

    with process_lock:

        current = processes.get(bot_id)

        if current is process:
            processes.pop(bot_id, None)

    bot = get_bot(bot_id)

    if not bot:
        return

    update_bot_status(
        bot_id,
        "stopped",
        0
    )

    # ========================================================
    # AUTO RESTART
    # ========================================================

    if (
        int(bot["auto_restart"]) == 1
        and client_enabled(int(bot["owner_chat_id"]))
        and has_hosting_access(int(bot["owner_chat_id"]))
    ):

        write_log(
            Path(bot["folder"]),
            "Auto restart scheduled."
        )

        time.sleep(2)

        try:
            start_bot(bot_id)
        except Exception as e:
            write_log(
                Path(bot["folder"]),
                f"Auto restart failed: {e}"
            )


# ============================================================
# LOGS
# ============================================================

def read_logs(bot_id, lines=60):
    bot = get_bot(bot_id)

    if not bot:
        return "Bot not found."

    log_file = Path(bot["folder"]) / "bot.log"

    if not log_file.exists():
        return "No logs available."

    try:

        content = log_file.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        data = content.splitlines()

        return "\n".join(
            data[-lines:]
        )[-12000:]

    except Exception as e:

        return f"Could not read logs:\n{e}"


# ============================================================
# DELETE BOT
# ============================================================

def permanently_delete_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    stop_bot(bot_id)

    folder = Path(bot["folder"])

    with process_lock:
        processes.pop(int(bot_id), None)

    try:

        if folder.exists():
            shutil.rmtree(folder)

    except Exception as e:

        return False, (
            f"Could not delete bot files:\n{e}"
        )

    conn = get_db()

    conn.execute(
        "DELETE FROM bots WHERE id=?",
        (int(bot_id),)
    )

    conn.commit()
    conn.close()

    return True, "Bot permanently deleted."


# ============================================================
# DELETE CLIENT
# ============================================================

def permanently_delete_client(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False, "Owner cannot be deleted."

    bots = get_user_bots(chat_id)

    for bot in bots:
        permanently_delete_bot(int(bot["id"]))

    client_folder = CLIENTS_DIR / str(chat_id)

    try:

        if client_folder.exists():
            shutil.rmtree(client_folder)

    except Exception as e:

        return False, (
            f"Could not delete client files:\n{e}"
        )

    conn = get_db()

    conn.execute(
        "DELETE FROM bots WHERE owner_chat_id=?",
        (chat_id,)
    )

    conn.execute(
        "DELETE FROM clients WHERE chat_id=?",
        (chat_id,)
    )

    conn.execute(
        "DELETE FROM hosting_access WHERE chat_id=?",
        (chat_id,)
    )

    conn.commit()
    conn.close()

    return True, "Client permanently deleted."


# ============================================================
# KEYBOARDS
# ============================================================

def owner_panel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "👥 Clients",
                    "callback_data": "owner_clients"
                },
                {
                    "text": "🤖 All Bots",
                    "callback_data": "owner_bots"
                }
            ],
            [
                {
                    "text": "🔐 Hosting Access",
                    "callback_data": "access_panel"
                }
            ],
            [
                {
                    "text": "➕ Grant Access",
                    "callback_data": "grant_access"
                },
                {
                    "text": "➖ Revoke Access",
                    "callback_data": "revoke_access"
                }
            ],
            [
                {
                    "text": "🛑 Stop All Bots",
                    "callback_data": "stop_all"
                }
            ]
        ]
    }


def client_panel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🤖 My Bots",
                    "callback_data": "my_bots"
                }
            ],
            [
                {
                    "text": "📤 Upload Python Bot",
                    "callback_data": "upload_help"
                }
            ],
            [
                {
                    "text": "🔄 Refresh",
                    "callback_data": "client_panel"
                }
            ]
        ]
    }


def bot_actions_keyboard(bot_id, owner=False):
    rows = [
        [
            {
                "text": "▶️ Start",
                "callback_data": f"start_bot:{bot_id}"
            },
            {
                "text": "⏹ Stop",
                "callback_data": f"stop_bot:{bot_id}"
            }
        ],
        [
            {
                "text": "🔄 Restart",
                "callback_data": f"restart_bot:{bot_id}"
            },
            {
                "text": "📜 Logs",
                "callback_data": f"logs_bot:{bot_id}"
            }
        ],
        [
            {
                "text": "🗑 Delete",
                "callback_data": f"delete_bot:{bot_id}"
            }
        ]
    ]

    if owner:
        rows.append([
            {
                "text": "👤 Owner",
                "callback_data": f"bot_owner:{bot_id}"
            }
        ])

    return {
        "inline_keyboard": rows
    }


# ============================================================
# PANELS
# ============================================================

def owner_panel(chat_id):
    text = f"""
👑 <b>{APP_NAME}</b>

<b>Owner Control Panel</b>

🔐 Hosting Access Control enabled

Yahan se aap:
• Clients manage kar sakte ho
• Chat IDs ko hosting access de sakte ho
• Access revoke kar sakte ho
• Sabhi bots manage kar sakte ho
• Logs dekh sakte ho
• Bots stop/restart kar sakte ho
• Permanent delete kar sakte ho

<b>Owner ID:</b>
<code>{OWNER_CHAT_ID}</code>
"""

    send_message(
        chat_id,
        text,
        owner_panel_keyboard()
    )


def client_panel(chat_id):
    if not has_hosting_access(chat_id):
        send_access_denied(chat_id)
        return

    if not client_enabled(chat_id):
        send_message(
            chat_id,
            "🚫 <b>Your hosting account is disabled.</b>"
        )
        return

    bots = get_user_bots(chat_id)

    text = f"""
🤖 <b>{APP_NAME}</b>

<b>Client Hosting Panel</b>

Chat ID:
<code>{chat_id}</code>

Bots: <b>{len(bots)}</b>

📤 Aap ek Python <code>.py</code> file upload karke
apna bot host kar sakte ho.

⚙️ Dependencies automatically detect/install hongi.
"""

    send_message(
        chat_id,
        text,
        client_panel_keyboard()
    )


# ============================================================
# OWNER CLIENTS
# ============================================================

def show_clients(chat_id):

    conn = get_db()

    rows = conn.execute("""
        SELECT *
        FROM clients
        ORDER BY created_at DESC
    """).fetchall()

    conn.close()

    if not rows:
        send_message(
            chat_id,
            "👥 <b>No clients found.</b>"
        )
        return

    text = "👥 <b>CLIENTS</b>\n\n"

    for row in rows:

        cid = row["chat_id"]

        access = has_hosting_access(cid)

        status = (
            "🟢 Enabled"
            if row["enabled"]
            else "🔴 Disabled"
        )

        access_status = (
            "🟢 Hosting Allowed"
            if access
            else "🔴 Hosting Denied"
        )

        text += (
            f"👤 <b>{user_display(row)}</b>\n"
            f"🆔 <code>{cid}</code>\n"
            f"{status}\n"
            f"{access_status}\n"
            f"🕒 {row['last_seen'] or '-'}\n\n"
        )

    send_message(
        chat_id,
        text[:12000],
        {
            "inline_keyboard": [
                [
                    {
                        "text": "🔐 Allowed Users",
                        "callback_data": "access_panel"
                    }
                ],
                [
                    {
                        "text": "🔙 Owner Panel",
                        "callback_data": "owner_panel"
                    }
                ]
            ]
        }
    )


# ============================================================
# HOSTING ACCESS PANEL
# ============================================================

def access_panel(chat_id):

    rows = get_allowed_users()

    text = """
🔐 <b>HOSTING ACCESS CONTROL</b>

Sirf jin Chat IDs ko access diya gaya hai,
wahi users bots host kar sakte hain.

"""

    if not rows:

        text += (
            "❌ <b>No users have hosting access.</b>\n\n"
        )

    else:

        for row in rows:

            name = user_display(row)

            text += (
                f"👤 {name}\n"
                f"🆔 <code>{row['chat_id']}</code>\n"
                f"🟢 Hosting Allowed\n\n"
            )

    keyboard = [
        [
            {
                "text": "➕ Grant Access",
                "callback_data": "grant_access"
            }
        ],
        [
            {
                "text": "➖ Revoke Access",
                "callback_data": "revoke_access"
            }
        ],
        [
            {
                "text": "🔙 Owner Panel",
                "callback_data": "owner_panel"
            }
        ]
    ]

    send_message(
        chat_id,
        text[:12000],
        {"inline_keyboard": keyboard}
    )


# ============================================================
# ALL BOTS
# ============================================================

def show_all_bots(chat_id):

    bots = get_all_bots()

    if not bots:

        send_message(
            chat_id,
            "🤖 <b>No bots found.</b>"
        )

        return

    text = "🤖 <b>ALL HOSTED BOTS</b>\n\n"

    for bot in bots:

        text += (
            f"🆔 Bot ID: <code>{bot['id']}</code>\n"
            f"📛 <b>{html_escape(bot['name'])}</b>\n"
            f"👤 Owner: <code>{bot['owner_chat_id']}</code>\n"
            f"📄 {html_escape(bot['filename'])}\n"
            f"📊 Status: <b>{bot['status']}</b>\n"
            f"🔁 Auto Restart: "
            f"{'ON' if bot['auto_restart'] else 'OFF'}\n\n"
        )

    send_message(
        chat_id,
        text[:12000],
        {
            "inline_keyboard": [
                [
                    {
                        "text": "🔙 Owner Panel",
                        "callback_data": "owner_panel"
                    }
                ]
            ]
        }
    )


# ============================================================
# MY BOTS
# ============================================================

def show_my_bots(chat_id):

    if not has_hosting_access(chat_id):
        send_access_denied(chat_id)
        return

    bots = get_user_bots(chat_id)

    if not bots:

        send_message(
            chat_id,
            """
🤖 <b>My Bots</b>

Abhi koi bot hosted nahi hai.

📤 Apni <code>.py</code> file upload karo.
"""
        )

        return

    for bot in bots:

        text = (
            f"🤖 <b>{html_escape(bot['name'])}</b>\n\n"
            f"🆔 ID: <code>{bot['id']}</code>\n"
            f"📄 File: <code>{html_escape(bot['filename'])}</code>\n"
            f"📊 Status: <b>{bot['status']}</b>\n"
            f"🔁 Auto Restart: "
            f"{'ON' if bot['auto_restart'] else 'OFF'}"
        )

        send_message(
            chat_id,
            text,
            bot_actions_keyboard(
                int(bot["id"])
            )
        )


# ============================================================
# BOT DETAILS
# ============================================================

def show_bot_owner(chat_id, bot_id):

    if not is_owner(chat_id):
        return

    bot = get_bot(bot_id)

    if not bot:
        send_message(
            chat_id,
            "❌ Bot not found."
        )
        return

    conn = get_db()

    client = conn.execute("""
        SELECT *
        FROM clients
        WHERE chat_id=?
    """, (
        int(bot["owner_chat_id"]),
    )).fetchone()

    conn.close()

    if client:

        owner_name = user_display(client)

    else:

        owner_name = "Unknown"

    text = (
        "👤 <b>BOT OWNER</b>\n\n"
        f"Name: {owner_name}\n"
        f"Chat ID: <code>{bot['owner_chat_id']}</code>\n"
        f"Bot ID: <code>{bot['id']}</code>\n"
        f"Bot: <b>{html_escape(bot['name'])}</b>"
    )

    send_message(
        chat_id,
        text
    )


# ============================================================
# STOP ALL
# ============================================================

def stop_all_bots(chat_id):

    if not is_owner(chat_id):
        return

    bots = get_all_bots()

    stopped = 0

    for bot in bots:

        ok, _ = stop_bot(
            int(bot["id"])
        )

        if ok:
            stopped += 1

    send_message(
        chat_id,
        f"🛑 <b>All bots stopped.</b>\n\n"
        f"Stopped: <b>{stopped}</b>"
    )


# ============================================================
# UPLOAD HELP
# ============================================================

def upload_help(chat_id):

    if not has_hosting_access(chat_id):
        send_access_denied(chat_id)
        return

    send_message(
        chat_id,
        """
📤 <b>UPLOAD PYTHON BOT</b>

Sirf ek Python file upload karo:

<code>main.py</code>

Ya koi bhi:

<code>bot.py</code>

Requirements.txt ki zarurat nahi hai.

⚙️ Host automatically imports detect karega
aur required packages install karega.

Example:

<code>
import requests
from telegram import Update
from telegram.ext import Application
</code>

Upload ke baad bot automatically register hoga.

⚠️ Sirf trusted Python code upload karo.
"""
    )


# ============================================================
# DOCUMENT UPLOAD
# ============================================================

def handle_document(message):

    chat_id = int(message["chat"]["id"])

    # ========================================================
    # CRITICAL ACCESS CHECK
    # ========================================================

    if not has_hosting_access(chat_id):
        send_access_denied(chat_id)
        return

    if not client_enabled(chat_id):
        send_message(
            chat_id,
            "🚫 <b>Your hosting account is disabled.</b>"
        )
        return

    document = message.get("document")

    if not document:
        return

    filename = document.get("file_name", "")

    if not filename.lower().endswith(".py"):

        send_message(
            chat_id,
            "❌ Sirf <code>.py</code> file allowed hai."
        )

        return

    # Prevent path traversal.
    safe_filename = Path(filename).name

    if safe_filename != filename:
        filename = safe_filename

    # ========================================================
    # SIZE LIMIT
    # ========================================================

    file_size = int(
        document.get("file_size", 0)
    )

    # 10 MB
    if file_size > 10 * 1024 * 1024:

        send_message(
            chat_id,
            "❌ Python file maximum 10 MB ho sakti hai."
        )

        return

    # ========================================================
    # BOT NAME
    # ========================================================

    stem = Path(filename).stem

    clean_name = re.sub(
        r"[^a-zA-Z0-9_-]+",
        "_",
        stem
    ).strip("_")

    if not clean_name:
        clean_name = "python_bot"

    # ========================================================
    # UNIQUE BOT FOLDER
    # ========================================================

    conn = get_db()

    cur = conn.execute("""
        SELECT COALESCE(MAX(id), 0) + 1 AS next_id
        FROM bots
    """)

    next_id = int(
        cur.fetchone()["next_id"]
    )

    conn.close()

    bot_folder = (
        CLIENTS_DIR
        / str(chat_id)
        / f"bot_{next_id}"
    )

    bot_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    script_path = bot_folder / filename

    # ========================================================
    # DOWNLOAD
    # ========================================================

    send_message(
        chat_id,
        "⏳ <b>Uploading your Python bot...</b>"
    )

    try:

        download_telegram_file(
            document["file_id"],
            script_path
        )

    except Exception as e:

        shutil.rmtree(
            bot_folder,
            ignore_errors=True
        )

        send_message(
            chat_id,
            f"❌ Upload failed:\n<code>{html_escape(e)}</code>"
        )

        return

    # ========================================================
    # CREATE BOT DB RECORD
    # ========================================================

    bot_id = create_bot(
        chat_id,
        clean_name,
        filename,
        bot_folder
    )

    # ========================================================
    # CHECK SYNTAX
    # ========================================================

    ok, error = syntax_check(
        script_path
    )

    if not ok:

        update_bot_status(
            bot_id,
            "syntax_error",
            0
        )

        write_log(
            bot_folder,
            f"Syntax error: {error}"
        )

        send_message(
            chat_id,
            f"""
❌ <b>Python Syntax Error</b>

Bot ID:
<code>{bot_id}</code>

<code>{html_escape(error)}</code>

Fix the file and upload it again.
"""
        )

        return

    # ========================================================
    # START
    # ========================================================

    send_message(
        chat_id,
        f"""
📦 <b>Bot uploaded successfully!</b>

Bot ID:
<code>{bot_id}</code>

📄 File:
<code>{html_escape(filename)}</code>

⚙️ Checking dependencies...
"""
    )

    ok, result = start_bot(
        bot_id
    )

    if ok:

        send_message(
            chat_id,
            f"""
✅ <b>BOT HOSTING STARTED</b>

🤖 Bot ID:
<code>{bot_id}</code>

📄 File:
<code>{html_escape(filename)}</code>

🚀 {html_escape(result)}

🔁 Auto Restart: ON
"""
        )

    else:

        send_message(
            chat_id,
            f"""
⚠️ <b>Bot created but could not start.</b>

Bot ID:
<code>{bot_id}</code>

Reason:
<code>{html_escape(result)}</code>

Use Logs to inspect the problem.
"""
        )

    # ========================================================
    # OWNER NOTIFICATION
    # ========================================================

    if OWNER_CHAT_ID:

        conn = get_db()

        client = conn.execute("""
            SELECT *
            FROM clients
            WHERE chat_id=?
        """, (
            chat_id,
        )).fetchone()

        conn.close()

        username = (
            f"@{client['username']}"
            if client and client["username"]
            else "No username"
        )

        send_message(
            OWNER_CHAT_ID,
            f"""
📤 <b>NEW BOT HOSTED</b>

👤 User:
<b>{html_escape(username)}</b>

🆔 Chat ID:
<code>{chat_id}</code>

🤖 Bot ID:
<code>{bot_id}</code>

📄 File:
<code>{html_escape(filename)}</code>

🕒 Time:
<code>{now()}</code>
"""
        )


# ============================================================
# NEW CLIENT NOTIFICATION
# ============================================================

def notify_new_client(user):

    chat_id = int(user["id"])

    if is_owner(chat_id):
        return

    username = user.get("username") or "No username"

    name = (
        f"{user.get('first_name', '')} "
        f"{user.get('last_name', '')}"
    ).strip()

    text = f"""
🔔 <b>NEW CLIENT</b>

👤 Username:
<b>@{html_escape(username)}</b>

📝 Name:
<b>{html_escape(name or 'Unknown')}</b>

🆔 Chat ID:
<code>{chat_id}</code>

🕒 Time:
<code>{now()}</code>

⚠️ Hosting access is currently:
<b>NOT GRANTED</b>

Owner panel se access grant karo.
"""

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Grant Hosting Access",
                    "callback_data": f"quick_grant:{chat_id}"
                }
            ],
            [
                {
                    "text": "🔐 Access Panel",
                    "callback_data": "access_panel"
                }
            ]
        ]
    }

    send_message(
        OWNER_CHAT_ID,
        text,
        keyboard
    )


# ============================================================
# OWNER ACTION STATES
# ============================================================

def set_pending(chat_id, action):
    with pending_lock:
        pending_actions[int(chat_id)] = action


def get_pending(chat_id):
    with pending_lock:
        return pending_actions.get(int(chat_id))


def clear_pending(chat_id):
    with pending_lock:
        pending_actions.pop(int(chat_id), None)


# ============================================================
# GRANT ACCESS
# ============================================================

def ask_grant_access(chat_id):

    if not is_owner(chat_id):
        return

    set_pending(
        chat_id,
        "grant_access"
    )

    send_message(
        chat_id,
        """
➕ <b>GRANT HOSTING ACCESS</b>

Jis Telegram Chat ID ko hosting access dena hai,
woh numeric Chat ID send karo.

Example:

<code>123456789</code>

❌ Username nahi.
✅ Numeric Chat ID.
"""
    )


def ask_revoke_access(chat_id):

    if not is_owner(chat_id):
        return

    set_pending(
        chat_id,
        "revoke_access"
    )

    send_message(
        chat_id,
        """
➖ <b>REVOKE HOSTING ACCESS</b>

Jis Chat ID ka hosting access remove karna hai,
numeric Chat ID send karo.

Example:

<code>123456789</code>
"""
    )


def process_owner_text(chat_id, text):

    action = get_pending(chat_id)

    if not action:
        return False

    if not text.isdigit():

        send_message(
            chat_id,
            "❌ Valid numeric Telegram Chat ID send karo."
        )

        return True

    target_id = int(text)

    if target_id == OWNER_CHAT_ID:

        send_message(
            chat_id,
            "👑 Owner ka access remove nahi kiya ja sakta."
        )

        clear_pending(chat_id)

        return True

    if action == "grant_access":

        grant_hosting_access(
            target_id
        )

        # Also create/update client record.
        conn = get_db()

        existing = conn.execute(
            "SELECT chat_id FROM clients WHERE chat_id=?",
            (target_id,)
        ).fetchone()

        if not existing:

            conn.execute("""
                INSERT INTO clients
                (
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    enabled,
                    created_at,
                    last_seen
                )
                VALUES (?, '', '', '', 1, ?, ?)
            """, (
                target_id,
                now(),
                now()
            ))

        conn.commit()
        conn.close()

        send_message(
            chat_id,
            f"""
✅ <b>HOSTING ACCESS GRANTED</b>

Chat ID:
<code>{target_id}</code>

Ab ye user:
• Hosting panel open kar sakta hai
• Python bot upload kar sakta hai
• Apne bots manage kar sakta hai
"""
        )

    elif action == "revoke_access":

        revoke_hosting_access(
            target_id
        )

        send_message(
            chat_id,
            f"""
✅ <b>HOSTING ACCESS REVOKED</b>

Chat ID:
<code>{target_id}</code>

Ab ye user new hosting operations nahi kar sakta.
"""
        )

    clear_pending(chat_id)

    return True


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(callback):

    callback_id = callback["id"]

    answer_callback(
        callback_id
    )

    data = callback.get("data", "")

    message = callback.get("message", {})

    chat = message.get("chat", {})

    chat_id = int(chat.get("id"))

    message_id = message.get("message_id")

    # ========================================================
    # OWNER CHECK
    # ========================================================

    owner_only_actions = (
        data.startswith("owner_")
        or data in {
            "access_panel",
            "grant_access",
            "revoke_access",
            "stop_all"
        }
        or data.startswith("quick_grant:")
        or data.startswith("bot_owner:")
    )

    if owner_only_actions and not is_owner(chat_id):

        send_access_denied(chat_id)
        return

    # ========================================================
    # OWNER PANEL
    # ========================================================

    if data == "owner_panel":

        edit_message(
            chat_id,
            message_id,
            f"""
👑 <b>{APP_NAME}</b>

<b>Owner Control Panel</b>

🔐 Hosting Access Control enabled.
""",
            owner_panel_keyboard()
        )

        return

    # ========================================================
    # CLIENT PANEL
    # ========================================================

    if data == "client_panel":

        if not has_hosting_access(chat_id):
            send_access_denied(chat_id)
            return

        edit_message(
            chat_id,
            message_id,
            f"""
🤖 <b>{APP_NAME}</b>

<b>Client Hosting Panel</b>

Your Chat ID:
<code>{chat_id}</code>
""",
            client_panel_keyboard()
        )

        return

    # ========================================================
    # CLIENTS
    # ========================================================

    if data == "owner_clients":

        show_clients(chat_id)
        return

    # ========================================================
    # ALL BOTS
    # ========================================================

    if data == "owner_bots":

        show_all_bots(chat_id)
        return

    # ========================================================
    # ACCESS PANEL
    # ========================================================

    if data == "access_panel":

        access_panel(chat_id)
        return

    # ========================================================
    # GRANT
    # ========================================================

    if data == "grant_access":

        ask_grant_access(chat_id)
        return

    # ========================================================
    # REVOKE
    # ========================================================

    if data == "revoke_access":

        ask_revoke_access(chat_id)
        return

    # ========================================================
    # QUICK GRANT
    # ========================================================

    if data.startswith("quick_grant:"):

        target_id = int(
            data.split(":", 1)[1]
        )

        grant_hosting_access(
            target_id
        )

        send_message(
            chat_id,
            f"""
✅ <b>HOSTING ACCESS GRANTED</b>

Chat ID:
<code>{target_id}</code>
"""
        )

        return

    # ========================================================
    # MY BOTS
    # ========================================================

    if data == "my_bots":

        show_my_bots(chat_id)
        return

    # ========================================================
    # UPLOAD HELP
    # ========================================================

    if data == "upload_help":

        upload_help(chat_id)
        return

    # ========================================================
    # START BOT
    # ========================================================

    if data.startswith("start_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if not is_owner(chat_id):
            if int(bot["owner_chat_id"]) != chat_id:
                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )
                return

        if not has_hosting_access(
            int(bot["owner_chat_id"])
        ):
            send_message(
                chat_id,
                "🚫 Hosting access has been revoked."
            )
            return

        ok, result = start_bot(bot_id)

        send_message(
            chat_id,
            (
                "✅ " if ok else "❌ "
            ) + html_escape(result)
        )

        return

    # ========================================================
    # STOP BOT
    # ========================================================

    if data.startswith("stop_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if not is_owner(chat_id):
            if int(bot["owner_chat_id"]) != chat_id:
                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )
                return

        ok, result = stop_bot(
            bot_id
        )

        send_message(
            chat_id,
            (
                "⏹ " if ok else "❌ "
            ) + html_escape(result)
        )

        return

    # ========================================================
    # RESTART BOT
    # ========================================================

    if data.startswith("restart_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if not is_owner(chat_id):
            if int(bot["owner_chat_id"]) != chat_id:
                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )
                return

        ok, result = restart_bot(
            bot_id
        )

        send_message(
            chat_id,
            (
                "🔄 " if ok else "❌ "
            ) + html_escape(result)
        )

        return

    # ========================================================
    # LOGS
    # ========================================================

    if data.startswith("logs_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if not is_owner(chat_id):
            if int(bot["owner_chat_id"]) != chat_id:
                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )
                return

        logs = read_logs(
            bot_id,
            80
        )

        send_message(
            chat_id,
            "<b>📜 BOT LOGS</b>\n\n"
            "<pre>"
            + html_escape(logs)
            + "</pre>"
        )

        return

    # ========================================================
    # DELETE BOT
    # ========================================================

    if data.startswith("delete_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if not is_owner(chat_id):
            if int(bot["owner_chat_id"]) != chat_id:
                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )
                return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "⚠️ YES, PERMANENT DELETE",
                        "callback_data":
                            f"confirm_delete_bot:{bot_id}"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data": "my_bots"
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            f"""
⚠️ <b>PERMANENT DELETE</b>

Bot:
<b>{html_escape(bot['name'])}</b>

Bot ID:
<code>{bot_id}</code>

This will permanently delete:

• Python file
• .venv
• Logs
• Bot database record
• All bot files

<b>THIS ACTION CANNOT BE UNDONE.</b>
""",
            keyboard
        )

        return

    # ========================================================
    # CONFIRM DELETE BOT
    # ========================================================

    if data.startswith("confirm_delete_bot:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            send_message(
                chat_id,
                "❌ Bot already deleted."
            )
            return

        if not is_owner(chat_id):

            if int(bot["owner_chat_id"]) != chat_id:

                send_message(
                    chat_id,
                    "🚫 You don't own this bot."
                )

                return

        ok, result = permanently_delete_bot(
            bot_id
        )

        send_message(
            chat_id,
            (
                "🗑️ " if ok else "❌ "
            ) + html_escape(result)
        )

        return

    # ========================================================
    # BOT OWNER
    # ========================================================

    if data.startswith("bot_owner:"):

        bot_id = int(
            data.split(":", 1)[1]
        )

        show_bot_owner(
            chat_id,
            bot_id
        )

        return

    # ========================================================
    # STOP ALL
    # ========================================================

    if data == "stop_all":

        stop_all_bots(chat_id)
        return


# ============================================================
# UPDATE HANDLER
# ============================================================

def handle_update(update):

    # ========================================================
    # CALLBACK
    # ========================================================

    if update.get("callback_query"):

        handle_callback(
            update["callback_query"]
        )

        return

    message = update.get("message")

    if not message:
        return

    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = int(
        chat.get("id")
    )

    # ========================================================
    # SAVE CLIENT
    # ========================================================

    is_new = save_client(
        user
    )

    # ========================================================
    # NEW CLIENT OWNER NOTIFICATION
    # ========================================================

    if is_new and not is_owner(chat_id):

        notify_new_client(
            user
        )

    # ========================================================
    # TEXT
    # ========================================================

    text = message.get("text", "")

    if text:

        # Owner pending action first.
        if is_owner(chat_id):

            if process_owner_text(
                chat_id,
                text
            ):
                return

        command = text.split()[0].lower()

        # ====================================================
        # START
        # ====================================================

        if command == "/start":

            if is_owner(chat_id):

                owner_panel(
                    chat_id
                )

            elif has_hosting_access(chat_id):

                client_panel(
                    chat_id
                )

            else:

                send_access_denied(
                    chat_id
                )

            return

        # ====================================================
        # PANEL
        # ====================================================

        if command == "/panel":

            if is_owner(chat_id):

                owner_panel(
                    chat_id
                )

            elif has_hosting_access(chat_id):

                client_panel(
                    chat_id
                )

            else:

                send_access_denied(
                    chat_id
                )

            return

        # ====================================================
        # OWNER COMMANDS
        # ====================================================

        if command == "/clients":

            if is_owner(chat_id):

                show_clients(
                    chat_id
                )

            else:

                send_access_denied(
                    chat_id
                )

            return

        if command == "/bots":

            if is_owner(chat_id):

                show_all_bots(
                    chat_id
                )

            elif has_hosting_access(chat_id):

                show_my_bots(
                    chat_id
                )

            else:

                send_access_denied(
                    chat_id
                )

            return

        # ====================================================
        # STATUS
        # ====================================================

        if command == "/status":

            if is_owner(chat_id):

                bots = get_all_bots()

            elif has_hosting_access(chat_id):

                bots = get_user_bots(chat_id)

            else:

                send_access_denied(
                    chat_id
                )

                return

            running = sum(
                1
                for b in bots
                if b["status"] == "running"
            )

            send_message(
                chat_id,
                f"""
📊 <b>HOSTING STATUS</b>

🤖 Total Bots:
<b>{len(bots)}</b>

🟢 Running:
<b>{running}</b>

🔴 Stopped:
<b>{len(bots) - running}</b>
"""
            )

            return

        # ====================================================
        # HELP
        # ====================================================

        if command == "/help":

            send_message(
                chat_id,
                f"""
<b>{APP_NAME}</b>

Commands:

/start - Open panel
/panel - Open panel
/bots - View bots
/status - Hosting status
/help - Help

Only users with hosting access can
upload and host Python bots.
"""
            )

            return

    # ========================================================
    # DOCUMENT
    # ========================================================

    if message.get("document"):

        handle_document(
            message
        )

        return


# ============================================================
# POLLING
# ============================================================

def polling_loop():

    global offset

    print("=" * 60)
    print(f"{APP_NAME} v{VERSION}")
    print("=" * 60)

    while True:

        try:

            result = telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
                },
                timeout=65
            )

            if not result:
                time.sleep(2)
                continue

            if not result.get("ok"):

                print(
                    "[POLL ERROR]",
                    result
                )

                time.sleep(5)
                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    int(update["update_id"]) + 1
                )

                try:

                    handle_update(
                        update
                    )

                except Exception as e:

                    print(
                        "[UPDATE ERROR]",
                        repr(e)
                    )

                    import traceback
                    traceback.print_exc()

        except KeyboardInterrupt:
            break

        except Exception as e:

            print(
                "[POLLING ERROR]",
                repr(e)
            )

            time.sleep(5)


# ============================================================
# RECOVER AUTO-RESTART BOTS
# ============================================================

def recover_bots():

    print(
        "[RECOVERY] Checking bots..."
    )

    bots = get_all_bots()

    for bot in bots:

        bot_id = int(bot["id"])

        if int(bot["auto_restart"]) != 1:
            continue

        owner_id = int(
            bot["owner_chat_id"]
        )

        if not client_enabled(owner_id):
            continue

        if not has_hosting_access(owner_id):
            continue

        print(
            f"[RECOVERY] Starting bot {bot_id}"
        )

        try:

            ok, result = start_bot(
                bot_id
            )

            print(
                f"[RECOVERY] {bot_id}: "
                f"{ok} {result}"
            )

        except Exception as e:

            print(
                f"[RECOVERY ERROR] "
                f"{bot_id}: {e}"
            )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

        self.wfile.write(
            f"{APP_NAME} v{VERSION} - OK".encode()
        )

    def log_message(self, format, *args):
        return


def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(
        f"[HEALTH] Server running on port {port}"
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(f"STARTING {APP_NAME}")
    print(f"VERSION: {VERSION}")
    print("=" * 60)

    if not BOT_TOKEN:

        print(
            "ERROR: BOT_TOKEN environment variable missing."
        )

        sys.exit(1)

    if not OWNER_CHAT_ID:

        print(
            "ERROR: OWNER_CHAT_ID environment variable missing."
        )

        sys.exit(1)

    init_db()

    # Health server for Render.
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Recover bots.
    threading.Thread(
        target=recover_bots,
        daemon=True
    ).start()

    print(
        f"OWNER_CHAT_ID: {OWNER_CHAT_ID}"
    )

    print(
        f"DATA_DIR: {DATA_DIR}"
    )

    print(
        "Hosting Access Control: ENABLED"
    )

    print(
        "Starting Telegram polling..."
    )

    polling_loop()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
