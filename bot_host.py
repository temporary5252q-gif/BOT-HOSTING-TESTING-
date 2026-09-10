import os
import re
import sys
import ast
import time
import json
import signal
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# TELEGRAM PYTHON HOSTING MANAGER
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"
VERSION = "3.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()

try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW)
except Exception:
    OWNER_CHAT_ID = 0


# ============================================================
# DIRECTORIES
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv("DATA_DIR", str(BASE_DIR / "host_data"))
).expanduser()

CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "hosting.db"

CLIENTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# GLOBALS
# ============================================================

processes = {}
process_lock = threading.RLock()

shutdown_event = threading.Event()


# ============================================================
# DATABASE
# ============================================================

def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(DB_FILE),
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db()

    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            chat_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            pid INTEGER DEFAULT 0,
            auto_restart INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# CLIENT FUNCTIONS
# ============================================================

def save_client(user):

    chat_id = int(user["id"])
    username = user.get("username") or ""
    first_name = user.get("first_name") or ""
    last_name = user.get("last_name") or ""

    now = int(time.time())

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT chat_id FROM clients WHERE chat_id = ?",
        (chat_id,)
    )

    exists = cur.fetchone() is not None

    if exists:

        cur.execute("""
            UPDATE clients
            SET username = ?,
                first_name = ?,
                last_name = ?,
                last_seen = ?
            WHERE chat_id = ?
        """, (
            username,
            first_name,
            last_name,
            now,
            chat_id
        ))

    else:

        cur.execute("""
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
            now,
            now
        ))

    conn.commit()
    conn.close()

    return not exists


def get_client(chat_id):

    conn = db()

    row = conn.execute(
        "SELECT * FROM clients WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()

    conn.close()

    return row


def client_enabled(chat_id):

    row = get_client(chat_id)

    if not row:
        return False

    return bool(row["enabled"])


def client_display(row):

    if not row:
        return "Unknown Client"

    username = row["username"] or ""

    if username:
        return f"@{username}"

    name = " ".join(
        x for x in [
            row["first_name"],
            row["last_name"]
        ]
        if x
    ).strip()

    if name:
        return name

    return str(row["chat_id"])


def user_display(user):

    username = user.get("username")

    if username:
        return f"@{username}"

    name = " ".join(
        x for x in [
            user.get("first_name"),
            user.get("last_name")
        ]
        if x
    ).strip()

    if name:
        return name

    return str(user.get("id"))


# ============================================================
# TELEGRAM API
# ============================================================

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, timeout=40):

    try:

        response = requests.post(
            f"{API_URL}/{method}",
            data=data or {},
            timeout=timeout
        )

        return response.json()

    except Exception as e:

        print(
            f"[TELEGRAM ERROR] {method}: {e}",
            flush=True
        )

        return {
            "ok": False,
            "description": str(e)
        }


def send_message(chat_id, text, reply_markup=None):

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    return telegram("sendMessage", data)


def edit_message(chat_id, message_id, text, reply_markup=None):

    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

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


def send_typing(chat_id):

    return telegram(
        "sendChatAction",
        {
            "chat_id": chat_id,
            "action": "typing"
        }
    )


def send_long_message(chat_id, text):

    if not text:
        text = "No output."

    limit = 3900

    chunks = [
        text[i:i + limit]
        for i in range(0, len(text), limit)
    ]

    for chunk in chunks:

        send_message(
            chat_id,
            f"<pre>{escape_html(chunk)}</pre>"
        )


def escape_html(text):

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# UI KEYBOARDS
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
                    "text": "📊 Status",
                    "callback_data": "owner_status"
                },
                {
                    "text": "🛑 Stop All",
                    "callback_data": "owner_stopall"
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
                    "callback_data": "client_bots"
                },
                {
                    "text": "📊 Status",
                    "callback_data": "client_status"
                }
            ],
            [
                {
                    "text": "ℹ️ Help",
                    "callback_data": "client_help"
                }
            ]
        ]
    }


def back_owner_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "⬅️ Owner Panel",
                    "callback_data": "owner_panel"
                }
            ]
        ]
    }


def back_client_keyboard():

    return {
        "inline_keyboard": [
            [
                {
                    "text": "⬅️ My Panel",
                    "callback_data": "client_panel"
                }
            ]
        ]
    }


# ============================================================
# BOT DATABASE FUNCTIONS
# ============================================================

def create_bot(owner_chat_id, filename):

    now = int(time.time())

    safe_name = Path(filename).stem

    safe_name = re.sub(
        r"[^a-zA-Z0-9_\-]+",
        "_",
        safe_name
    ).strip("_")

    if not safe_name:
        safe_name = "main"

    conn = db()

    cur = conn.cursor()

    cur.execute("""
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
        VALUES (?, ?, ?, '', 'stopped', 0, 1, ?, ?)
    """, (
        owner_chat_id,
        safe_name,
        filename,
        now,
        now
    ))

    bot_id = cur.lastrowid

    folder = CLIENTS_DIR / str(owner_chat_id) / f"bot_{bot_id}"

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    cur.execute(
        "UPDATE bots SET folder = ? WHERE id = ?",
        (str(folder), bot_id)
    )

    conn.commit()
    conn.close()

    return bot_id, folder


def get_bot(bot_id):

    conn = db()

    row = conn.execute(
        "SELECT * FROM bots WHERE id = ?",
        (bot_id,)
    ).fetchone()

    conn.close()

    return row


def get_client_bots(chat_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        WHERE owner_chat_id = ?
        ORDER BY id DESC
    """, (chat_id,)).fetchall()

    conn.close()

    return rows


def get_all_bots():

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return rows


def update_bot_status(bot_id, status, pid=0):

    conn = db()

    conn.execute("""
        UPDATE bots
        SET status = ?,
            pid = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        status,
        pid,
        int(time.time()),
        bot_id
    ))

    conn.commit()
    conn.close()


def set_auto_restart(bot_id, value):

    conn = db()

    conn.execute("""
        UPDATE bots
        SET auto_restart = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        1 if value else 0,
        int(time.time()),
        bot_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# DEPENDENCY SYSTEM
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

    "psutil": "psutil",
    "PIL": "Pillow"
}


def get_stdlib_modules():

    try:

        if hasattr(sys, "stdlib_module_names"):
            return set(sys.stdlib_module_names)

    except Exception:
        pass

    return {
        "os",
        "sys",
        "json",
        "time",
        "re",
        "math",
        "random",
        "datetime",
        "asyncio",
        "threading",
        "sqlite3",
        "subprocess",
        "pathlib",
        "typing",
        "collections",
        "itertools",
        "functools",
        "logging",
        "traceback",
        "signal",
        "socket",
        "http",
        "urllib",
        "hashlib",
        "base64",
        "secrets",
        "string",
        "shutil",
        "csv",
        "io",
        "tempfile",
        "uuid",
        "statistics",
        "decimal",
        "contextlib",
        "dataclasses"
    }


def detect_imports(script):

    imports = set()

    try:

        tree = ast.parse(
            script.read_text(
                encoding="utf-8",
                errors="ignore"
            )
        )

        for node in ast.walk(tree):

            if isinstance(node, ast.Import):

                for item in node.names:

                    imports.add(
                        item.name.split(".")[0]
                    )

            elif isinstance(node, ast.ImportFrom):

                if node.module:

                    imports.add(
                        node.module.split(".")[0]
                    )

    except Exception as e:

        print(
            f"[DEPENDENCY SCAN ERROR] {e}",
            flush=True
        )

    return imports


def get_required_packages(script, folder):

    imports = detect_imports(script)

    stdlib = get_stdlib_modules()

    packages = set()

    for module in imports:

        if module in stdlib:
            continue

        local_file = folder / f"{module}.py"

        local_folder = folder / module

        if local_file.exists() or local_folder.exists():
            continue

        package = IMPORT_TO_PACKAGE.get(
            module,
            module
        )

        packages.add(package)

    return sorted(packages)


def venv_python(folder):

    if os.name == "nt":

        return folder / ".venv" / "Scripts" / "python.exe"

    return folder / ".venv" / "bin" / "python"


def create_venv(folder, log):

    python_path = venv_python(folder)

    if python_path.exists():
        return True

    log.write(
        "\n[HOST] Creating virtual environment...\n"
    )
    log.flush()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            str(folder / ".venv")
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        timeout=300
    )

    if result.returncode != 0:
        return False

    return python_path.exists()


def install_dependencies(folder, script, log):

    packages = get_required_packages(
        script,
        folder
    )

    if not packages:

        log.write(
            "[HOST] No external dependencies detected.\n"
        )

        log.flush()

        return True

    log.write(
        "\n[HOST] Detected dependencies:\n"
    )

    for package in packages:

        log.write(
            f"  - {package}\n"
        )

    log.write("\n[HOST] Installing dependencies...\n")
    log.flush()

    python_path = venv_python(folder)

    # Upgrade pip
    try:

        subprocess.run(
            [
                str(python_path),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip"
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=300
        )

    except Exception as e:

        log.write(
            f"[PIP WARNING] {e}\n"
        )

    command = [
        str(python_path),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check"
    ] + packages

    try:

        result = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=900
        )

        if result.returncode != 0:

            log.write(
                "\n[HOST] Dependency installation FAILED.\n"
            )

            log.flush()

            return False

    except subprocess.TimeoutExpired:

        log.write(
            "\n[HOST] Dependency installation timed out.\n"
        )

        log.flush()

        return False

    except Exception as e:

        log.write(
            f"\n[HOST] Dependency error: {e}\n"
        )

        log.flush()

        return False

    log.write(
        "\n[HOST] Dependencies installed successfully.\n"
    )

    log.flush()

    return True


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def is_process_running(bot_id):

    with process_lock:

        proc = processes.get(bot_id)

        if not proc:
            return False

        return proc.poll() is None


def start_bot(bot_id, requester=None):

    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    owner_id = int(bot["owner_chat_id"])

    if not client_enabled(owner_id):
        return False, "Client is disabled by owner."

    folder = Path(bot["folder"])
    script = folder / bot["filename"]

    if not script.exists():
        return False, "Bot Python file not found."

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    with process_lock:

        old = processes.get(bot_id)

        if old and old.poll() is None:
            return False, "Bot is already running."

    log_file = folder / "bot.log"

    try:

        log = open(
            log_file,
            "a",
            encoding="utf-8",
            buffering=1
        )

    except Exception as e:

        return False, f"Cannot open log: {e}"

    log.write("\n")
    log.write("=" * 70 + "\n")
    log.write(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] START REQUEST\n"
    )
    log.write("=" * 70 + "\n")
    log.flush()

    # --------------------------------------------------------
    # Syntax check
    # --------------------------------------------------------

    try:

        source = script.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        compile(
            source,
            str(script),
            "exec"
        )

    except Exception as e:

        log.write(
            f"[SYNTAX ERROR] {e}\n"
        )

        log.flush()
        log.close()

        update_bot_status(
            bot_id,
            "error",
            0
        )

        return False, f"Syntax error: {e}"

    # --------------------------------------------------------
    # VENV
    # --------------------------------------------------------

    try:

        if not create_venv(folder, log):

            log.write(
                "[HOST] Failed to create virtual environment.\n"
            )

            log.close()

            update_bot_status(
                bot_id,
                "error",
                0
            )

            return False, "Virtual environment creation failed."

    except Exception as e:

        log.write(
            f"[VENV ERROR] {e}\n"
        )

        log.close()

        update_bot_status(
            bot_id,
            "error",
            0
        )

        return False, f"Venv error: {e}"

    # --------------------------------------------------------
    # DEPENDENCIES
    # --------------------------------------------------------

    if not install_dependencies(
        folder,
        script,
        log
    ):

        log.write(
            "\n[HOST] Bot not started because dependencies failed.\n"
        )

        log.close()

        update_bot_status(
            bot_id,
            "error",
            0
        )

        return False, "Dependency installation failed. Check logs."

    # --------------------------------------------------------
    # START PROCESS
    # --------------------------------------------------------

    python_path = venv_python(folder)

    try:

        kwargs = {
            "cwd": str(folder),
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL
        }

        if os.name == "posix":

            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [
                str(python_path),
                "-u",
                str(script)
            ],
            **kwargs
        )

        with process_lock:
            processes[bot_id] = proc

        update_bot_status(
            bot_id,
            "running",
            proc.pid
        )

        log.write(
            f"\n[HOST] Bot started successfully. PID={proc.pid}\n"
        )

        log.flush()

        threading.Thread(
            target=watch_process,
            args=(bot_id, proc),
            daemon=True
        ).start()

        return True, f"Bot started. PID: {proc.pid}"

    except Exception as e:

        log.write(
            f"\n[START ERROR] {e}\n"
        )

        log.flush()
        log.close()

        update_bot_status(
            bot_id,
            "error",
            0
        )

        return False, str(e)


def stop_bot(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    with process_lock:
        proc = processes.get(bot_id)

    if not proc:

        update_bot_status(
            bot_id,
            "stopped",
            0
        )

        return True, "Bot is already stopped."

    try:

        if proc.poll() is None:

            if os.name == "posix":

                try:

                    os.killpg(
                        os.getpgid(proc.pid),
                        signal.SIGTERM
                    )

                except Exception:

                    proc.terminate()

            else:

                proc.terminate()

            try:

                proc.wait(timeout=10)

            except subprocess.TimeoutExpired:

                if os.name == "posix":

                    try:

                        os.killpg(
                            os.getpgid(proc.pid),
                            signal.SIGKILL
                        )

                    except Exception:
                        proc.kill()

                else:

                    proc.kill()

                proc.wait(timeout=5)

    except Exception as e:

        return False, str(e)

    finally:

        with process_lock:
            processes.pop(bot_id, None)

        update_bot_status(
            bot_id,
            "stopped",
            0
        )

    return True, "Bot stopped."


def restart_bot(bot_id):

    ok, message = stop_bot(bot_id)

    if not ok:
        return False, message

    time.sleep(1)

    return start_bot(bot_id)


def watch_process(bot_id, proc):

    try:

        return_code = proc.wait()

    except Exception:
        return_code = -1

    bot = get_bot(bot_id)

    with process_lock:

        if processes.get(bot_id) is proc:
            processes.pop(bot_id, None)

    if not bot:
        return

    update_bot_status(
        bot_id,
        "stopped",
        0
    )

    folder = Path(bot["folder"])

    log_file = folder / "bot.log"

    try:

        with open(
            log_file,
            "a",
            encoding="utf-8"
        ) as log:

            log.write(
                f"\n[HOST] Process exited with code {return_code}\n"
            )

    except Exception:
        pass

    # --------------------------------------------------------
    # AUTO RESTART
    # --------------------------------------------------------

    if (
        int(bot["auto_restart"]) == 1
        and client_enabled(int(bot["owner_chat_id"]))
        and not shutdown_event.is_set()
    ):

        time.sleep(2)

        latest = get_bot(bot_id)

        if not latest:
            return

        if int(latest["auto_restart"]) != 1:
            return

        if not client_enabled(
            int(latest["owner_chat_id"])
        ):
            return

        start_bot(bot_id)


# ============================================================
# LOGS
# ============================================================

def read_logs(bot_id, max_chars=12000):

    bot = get_bot(bot_id)

    if not bot:
        return "Bot not found."

    log_file = Path(bot["folder"]) / "bot.log"

    if not log_file.exists():
        return "No logs available."

    try:

        size = log_file.stat().st_size

        with open(
            log_file,
            "rb"
        ) as f:

            if size > max_chars:

                f.seek(-max_chars, os.SEEK_END)

            data = f.read()

        return data.decode(
            "utf-8",
            errors="replace"
        )

    except Exception as e:

        return f"Cannot read logs: {e}"


def trim_log(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return

    log_file = Path(bot["folder"]) / "bot.log"

    if not log_file.exists():
        return

    try:

        if log_file.stat().st_size <= 3 * 1024 * 1024:
            return

        text = read_logs(
            bot_id,
            1024 * 1024
        )

        log_file.write_text(
            text,
            encoding="utf-8"
        )

    except Exception:
        pass


# ============================================================
# DELETE FUNCTIONS
# ============================================================

def permanent_delete_bot(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    stop_bot(bot_id)

    folder = Path(bot["folder"])

    try:

        if folder.exists():

            shutil.rmtree(
                folder,
                ignore_errors=True
            )

    except Exception as e:

        return False, f"Folder delete failed: {e}"

    conn = db()

    conn.execute(
        "DELETE FROM bots WHERE id = ?",
        (bot_id,)
    )

    conn.commit()
    conn.close()

    with process_lock:
        processes.pop(bot_id, None)

    return True, "Bot permanently deleted."


def permanent_delete_client(chat_id):

    bots = get_client_bots(chat_id)

    for bot in bots:
        permanent_delete_bot(
            int(bot["id"])
        )

    client_folder = CLIENTS_DIR / str(chat_id)

    try:

        if client_folder.exists():

            shutil.rmtree(
                client_folder,
                ignore_errors=True
            )

    except Exception as e:

        return False, str(e)

    conn = db()

    conn.execute(
        "DELETE FROM clients WHERE chat_id = ?",
        (chat_id,)
    )

    conn.commit()
    conn.close()

    return True, "Client permanently deleted."


# ============================================================
# OWNER FUNCTIONS
# ============================================================

def set_client_enabled(chat_id, enabled):

    conn = db()

    conn.execute("""
        UPDATE clients
        SET enabled = ?
        WHERE chat_id = ?
    """, (
        1 if enabled else 0,
        chat_id
    ))

    conn.commit()
    conn.close()

    if not enabled:

        bots = get_client_bots(chat_id)

        for bot in bots:

            stop_bot(
                int(bot["id"])
            )


def notify_new_client(user):

    if not OWNER_CHAT_ID:
        return

    name = user_display(user)

    username = user.get("username")

    username_text = (
        f"@{username}"
        if username
        else "Not set"
    )

    text = (
        f"🔔 <b>{APP_NAME}</b>\n\n"
        f"🆕 <b>NEW CLIENT</b>\n\n"
        f"👤 Client: <b>{escape_html(name)}</b>\n"
        f"🔗 Username: <b>{escape_html(username_text)}</b>\n"
        f"🆔 ID: <code>{user['id']}</code>\n"
        f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    send_message(
        OWNER_CHAT_ID,
        text,
        owner_panel_keyboard()
    )


# ============================================================
# OWNER UI
# ============================================================

def owner_clients():

    conn = db()

    clients = conn.execute("""
        SELECT *
        FROM clients
        ORDER BY created_at DESC
    """).fetchall()

    conn.close()

    if not clients:

        return (
            "👥 <b>CLIENTS</b>\n\n"
            "No clients registered."
        ), back_owner_keyboard()

    buttons = []

    for client in clients:

        cid = int(client["chat_id"])

        label = client_display(client)

        status = (
            "🟢"
            if int(client["enabled"])
            else "🔴"
        )

        buttons.append([
            {
                "text": f"{status} {label}",
                "callback_data": f"owner_client:{cid}"
            }
        ])

    buttons.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "owner_panel"
        }
    ])

    return (
        f"👥 <b>CLIENTS</b>\n\n"
        f"Total: <b>{len(clients)}</b>"
    ), {
        "inline_keyboard": buttons
    }


def owner_client_view(chat_id):

    client = get_client(chat_id)

    if not client:
        return (
            "❌ Client not found."
        ), back_owner_keyboard()

    bots = get_client_bots(chat_id)

    status = (
        "🟢 Enabled"
        if int(client["enabled"])
        else "🔴 Disabled"
    )

    username = (
        f"@{client['username']}"
        if client["username"]
        else "Not set"
    )

    text = (
        f"👤 <b>CLIENT</b>\n\n"
        f"Username: <b>{escape_html(username)}</b>\n"
        f"Name: <b>{escape_html(client_display(client))}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"Bots: <b>{len(bots)}</b>"
    )

    buttons = [
        [
            {
                "text": "🤖 View Bots",
                "callback_data": f"owner_client_bots:{chat_id}"
            }
        ],
        [
            {
                "text": (
                    "🔴 Disable"
                    if int(client["enabled"])
                    else "🟢 Enable"
                ),
                "callback_data": f"toggle_client:{chat_id}"
            }
        ],
        [
            {
                "text": "🛑 Stop All Bots",
                "callback_data": f"stop_client:{chat_id}"
            }
        ],
        [
            {
                "text": "🗑️ Delete Client",
                "callback_data": f"delete_client:{chat_id}"
            }
        ],
        [
            {
                "text": "⬅️ Clients",
                "callback_data": "owner_clients"
            }
        ]
    ]

    return text, {
        "inline_keyboard": buttons
    }


def owner_bots():

    bots = get_all_bots()

    if not bots:

        return (
            "🤖 <b>ALL BOTS</b>\n\n"
            "No bots found."
        ), back_owner_keyboard()

    buttons = []

    for bot in bots:

        owner = get_client(
            int(bot["owner_chat_id"])
        )

        owner_name = client_display(owner)

        status = {
            "running": "🟢",
            "stopped": "⚪",
            "error": "🔴"
        }.get(
            bot["status"],
            "⚪"
        )

        buttons.append([
            {
                "text": (
                    f"{status} #{bot['id']} "
                    f"{bot['name']} — {owner_name}"
                )[:64],
                "callback_data": f"owner_bot:{bot['id']}"
            }
        ])

    buttons.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "owner_panel"
        }
    ])

    return (
        f"🤖 <b>ALL BOTS</b>\n\n"
        f"Total: <b>{len(bots)}</b>"
    ), {
        "inline_keyboard": buttons
    }


def owner_bot_view(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return (
            "❌ Bot not found."
        ), back_owner_keyboard()

    owner = get_client(
        int(bot["owner_chat_id"])
    )

    owner_name = client_display(owner)

    status = bot["status"]

    auto_restart = (
        "ON"
        if int(bot["auto_restart"])
        else "OFF"
    )

    text = (
        f"🤖 <b>BOT #{bot['id']}</b>\n\n"
        f"Name: <b>{escape_html(bot['name'])}</b>\n"
        f"File: <code>{escape_html(bot['filename'])}</code>\n"
        f"Client: <b>{escape_html(owner_name)}</b>\n"
        f"Client ID: <code>{bot['owner_chat_id']}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"PID: <code>{bot['pid']}</code>\n"
        f"Auto Restart: <b>{auto_restart}</b>"
    )

    buttons = []

    if status == "running":

        buttons.append([
            {
                "text": "🛑 Stop",
                "callback_data": f"owner_stop:{bot_id}"
            },
            {
                "text": "🔄 Restart",
                "callback_data": f"owner_restart:{bot_id}"
            }
        ])

    else:

        buttons.append([
            {
                "text": "▶️ Start",
                "callback_data": f"owner_start:{bot_id}"
            }
        ])

    buttons.append([
        {
            "text": (
                "🔄 Auto Restart OFF"
                if int(bot["auto_restart"])
                else "🔄 Auto Restart ON"
            ),
            "callback_data": f"toggle_auto:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "📜 Logs",
            "callback_data": f"owner_logs:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "🗑️ Permanent Delete",
            "callback_data": f"owner_delete_bot:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "⬅️ All Bots",
            "callback_data": "owner_bots"
        }
    ])

    return text, {
        "inline_keyboard": buttons
    }


# ============================================================
# CLIENT UI
# ============================================================

def client_bots(chat_id):

    bots = get_client_bots(chat_id)

    if not bots:

        return (
            "🤖 <b>MY BOTS</b>\n\n"
            "No bots yet.\n\n"
            "Send a Python <code>.py</code> file to create a bot."
        ), back_client_keyboard()

    buttons = []

    for bot in bots:

        status = {
            "running": "🟢",
            "stopped": "⚪",
            "error": "🔴"
        }.get(
            bot["status"],
            "⚪"
        )

        buttons.append([
            {
                "text": (
                    f"{status} #{bot['id']} "
                    f"{bot['name']}"
                )[:64],
                "callback_data": f"client_bot:{bot['id']}"
            }
        ])

    buttons.append([
        {
            "text": "⬅️ My Panel",
            "callback_data": "client_panel"
        }
    ])

    return (
        f"🤖 <b>MY BOTS</b>\n\n"
        f"Total: <b>{len(bots)}</b>"
    ), {
        "inline_keyboard": buttons
    }


def client_bot_view(bot_id, chat_id):

    bot = get_bot(bot_id)

    if not bot:
        return (
            "❌ Bot not found."
        ), back_client_keyboard()

    if int(bot["owner_chat_id"]) != int(chat_id):

        return (
            "❌ Access denied."
        ), back_client_keyboard()

    auto_restart = (
        "ON"
        if int(bot["auto_restart"])
        else "OFF"
    )

    text = (
        f"🤖 <b>BOT #{bot['id']}</b>\n\n"
        f"Name: <b>{escape_html(bot['name'])}</b>\n"
        f"File: <code>{escape_html(bot['filename'])}</code>\n"
        f"Status: <b>{bot['status']}</b>\n"
        f"PID: <code>{bot['pid']}</code>\n"
        f"Auto Restart: <b>{auto_restart}</b>"
    )

    buttons = []

    if bot["status"] == "running":

        buttons.append([
            {
                "text": "🛑 Stop",
                "callback_data": f"client_stop:{bot_id}"
            },
            {
                "text": "🔄 Restart",
                "callback_data": f"client_restart:{bot_id}"
            }
        ])

    else:

        buttons.append([
            {
                "text": "▶️ Start",
                "callback_data": f"client_start:{bot_id}"
            }
        ])

    buttons.append([
        {
            "text": (
                "🔄 Auto Restart OFF"
                if int(bot["auto_restart"])
                else "🔄 Auto Restart ON"
            ),
            "callback_data": f"toggle_auto:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "📜 Logs",
            "callback_data": f"client_logs:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "🗑️ Permanent Delete",
            "callback_data": f"client_delete_bot:{bot_id}"
        }
    ])

    buttons.append([
        {
            "text": "⬅️ My Bots",
            "callback_data": "client_bots"
        }
    ])

    return text, {
        "inline_keyboard": buttons
    }


# ============================================================
# CONFIRMATION UI
# ============================================================

def confirm_bot_delete(bot_id, owner_mode=False):

    prefix = (
        "confirm_owner_delete"
        if owner_mode
        else "confirm_client_delete"
    )

    back = (
        f"owner_bot:{bot_id}"
        if owner_mode
        else f"client_bot:{bot_id}"
    )

    return {
        "inline_keyboard": [
            [
                {
                    "text": "❌ CANCEL",
                    "callback_data": back
                },
                {
                    "text": "🗑️ DELETE",
                    "callback_data": f"{prefix}:{bot_id}"
                }
            ]
        ]
    }


def confirm_client_delete(chat_id):

    return {
        "inline_keyboard": [
            [
                {
                    "text": "❌ CANCEL",
                    "callback_data": f"owner_client:{chat_id}"
                },
                {
                    "text": "🗑️ DELETE",
                    "callback_data": f"confirm_client:{chat_id}"
                }
            ]
        ]
    }


# ============================================================
# MESSAGE HANDLING
# ============================================================

def help_text(owner=False):

    if owner:

        return (
            f"🛠️ <b>{APP_NAME}</b>\n\n"
            "Owner Commands:\n\n"
            "/start - Open panel\n"
            "/panel - Owner panel\n"
            "/clients - Clients\n"
            "/bots - All bots\n"
            "/status - Hosting status\n\n"
            "Owner has complete access to client and bot controls."
        )

    return (
        f"🤖 <b>{APP_NAME}</b>\n\n"
        "Client Commands:\n\n"
        "/start - Open panel\n"
        "/panel - My panel\n"
        "/status - Bot status\n"
        "/help - Help\n\n"
        "📤 Send one Python <code>.py</code> file "
        "to create a hosted bot."
    )


def handle_document(message, chat_id):

    document = message.get("document")

    if not document:
        return

    if not client_enabled(chat_id):

        send_message(
            chat_id,
            "❌ Your hosting account is disabled by the owner."
        )

        return

    filename = document.get("file_name", "")

    if not filename.lower().endswith(".py"):

        send_message(
            chat_id,
            "❌ Only <code>.py</code> Python files are allowed."
        )

        return

    file_size = int(
        document.get("file_size", 0)
    )

    # 10 MB maximum

    if file_size > 10 * 1024 * 1024:

        send_message(
            chat_id,
            "❌ File too large.\nMaximum size: 10 MB."
        )

        return

    send_message(
        chat_id,
        "⏳ Upload received.\nCreating your bot..."
    )

    bot_id, folder = create_bot(
        chat_id,
        filename
    )

    target = folder / filename

    try:

        file_info = telegram(
            "getFile",
            {
                "file_id": document["file_id"]
            }
        )

        if not file_info.get("ok"):

            permanent_delete_bot(bot_id)

            send_message(
                chat_id,
                "❌ Telegram file download failed."
            )

            return

        file_path = file_info["result"]["file_path"]

        download_url = (
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{file_path}"
        )

        response = requests.get(
            download_url,
            timeout=120
        )

        response.raise_for_status()

        target.write_bytes(
            response.content
        )

        # Final syntax check

        compile(
            target.read_text(
                encoding="utf-8",
                errors="ignore"
            ),
            str(target),
            "exec"
        )

    except Exception as e:

        permanent_delete_bot(bot_id)

        send_message(
            chat_id,
            f"❌ Upload failed:\n<code>{escape_html(e)}</code>"
        )

        return

    send_message(
        chat_id,
        (
            f"✅ <b>Bot Created</b>\n\n"
            f"🤖 Bot ID: <code>#{bot_id}</code>\n"
            f"📄 File: <code>{escape_html(filename)}</code>\n\n"
            "Starting bot and automatically installing "
            "detected dependencies..."
        )
    )

    ok, result = start_bot(
        bot_id
    )

    if ok:

        send_message(
            chat_id,
            (
                f"🟢 <b>Bot is running!</b>\n\n"
                f"Bot ID: <code>#{bot_id}</code>\n"
                f"{escape_html(result)}"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "🤖 Manage Bot",
                            "callback_data": f"client_bot:{bot_id}"
                        }
                    ]
                ]
            }
        )

    else:

        send_message(
            chat_id,
            (
                f"🔴 <b>Bot could not start.</b>\n\n"
                f"{escape_html(result)}\n\n"
                "Open bot logs for details."
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "📜 Logs",
                            "callback_data": f"client_logs:{bot_id}"
                        }
                    ],
                    [
                        {
                            "text": "🤖 Manage Bot",
                            "callback_data": f"client_bot:{bot_id}"
                        }
                    ]
                ]
            }
        )


def handle_message(message):

    chat = message.get("chat") or {}
    user = message.get("from") or {}

    chat_id = int(
        chat.get("id", 0)
    )

    if not chat_id:
        return

    # --------------------------------------------------------
    # Save client
    # --------------------------------------------------------

    is_new = save_client(user)

    if (
        is_new
        and chat_id != OWNER_CHAT_ID
    ):

        notify_new_client(user)

    # --------------------------------------------------------
    # OWNER
    # --------------------------------------------------------

    is_owner = (
        chat_id == OWNER_CHAT_ID
    )

    text = message.get("text", "") or ""

    # --------------------------------------------------------
    # DOCUMENT
    # --------------------------------------------------------

    if message.get("document"):

        handle_document(
            message,
            chat_id
        )

        return

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    command = text.split()[0].lower() if text else ""

    if command == "/start":

        if is_owner:

            send_message(
                chat_id,
                (
                    f"👑 <b>{APP_NAME}</b>\n\n"
                    "Welcome Owner.\n\n"
                    "You have full hosting control."
                ),
                owner_panel_keyboard()
            )

        else:

            if not client_enabled(chat_id):

                send_message(
                    chat_id,
                    "❌ Your hosting account is disabled."
                )

                return

            send_message(
                chat_id,
                (
                    f"🤖 <b>{APP_NAME}</b>\n\n"
                    "Welcome!\n\n"
                    "📤 Send your Python <code>.py</code> "
                    "file to host it."
                ),
                client_panel_keyboard()
            )

        return

    if command == "/panel":

        if is_owner:

            send_message(
                chat_id,
                f"👑 <b>{APP_NAME} OWNER PANEL</b>",
                owner_panel_keyboard()
            )

        else:

            send_message(
                chat_id,
                f"🤖 <b>{APP_NAME}</b>",
                client_panel_keyboard()
            )

        return

    if command == "/help":

        send_message(
            chat_id,
            help_text(is_owner)
        )

        return

    if command == "/clients" and is_owner:

        text_out, keyboard = owner_clients()

        send_message(
            chat_id,
            text_out,
            keyboard
        )

        return

    if command == "/bots":

        if is_owner:

            text_out, keyboard = owner_bots()

        else:

            text_out, keyboard = client_bots(
                chat_id
            )

        send_message(
            chat_id,
            text_out,
            keyboard
        )

        return

    if command == "/status":

        if is_owner:

            text_out, keyboard = owner_status()

        else:

            text_out, keyboard = client_status(
                chat_id
            )

        send_message(
            chat_id,
            text_out,
            keyboard
        )

        return


# ============================================================
# STATUS
# ============================================================

def owner_status():

    clients = db().execute(
        "SELECT COUNT(*) AS c FROM clients"
    ).fetchone()["c"]

    bots = get_all_bots()

    running = sum(
        1
        for bot in bots
        if bot["status"] == "running"
    )

    stopped = sum(
        1
        for bot in bots
        if bot["status"] == "stopped"
    )

    errors = sum(
        1
        for bot in bots
        if bot["status"] == "error"
    )

    text = (
        f"📊 <b>{APP_NAME} STATUS</b>\n\n"
        f"👥 Clients: <b>{clients}</b>\n"
        f"🤖 Total Bots: <b>{len(bots)}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
        f"⚪ Stopped: <b>{stopped}</b>\n"
        f"🔴 Errors: <b>{errors}</b>\n"
        f"🖥️ Host PID: <code>{os.getpid()}</code>"
    )

    return text, back_owner_keyboard()


def client_status(chat_id):

    bots = get_client_bots(chat_id)

    running = sum(
        1
        for bot in bots
        if bot["status"] == "running"
    )

    stopped = sum(
        1
        for bot in bots
        if bot["status"] == "stopped"
    )

    errors = sum(
        1
        for bot in bots
        if bot["status"] == "error"
    )

    text = (
        f"📊 <b>MY STATUS</b>\n\n"
        f"🤖 Total: <b>{len(bots)}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
        f"⚪ Stopped: <b>{stopped}</b>\n"
        f"🔴 Errors: <b>{errors}</b>"
    )

    return text, back_client_keyboard()


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(callback):

    callback_id = callback.get("id")

    data = callback.get("data", "")

    message = callback.get("message") or {}

    chat = message.get("chat") or {}

    chat_id = int(
        chat.get("id", 0)
    )

    message_id = int(
        message.get("message_id", 0)
    )

    is_owner = (
        chat_id == OWNER_CHAT_ID
    )

    answer_callback(
        callback_id
    )

    # --------------------------------------------------------
    # OWNER PANEL
    # --------------------------------------------------------

    if data == "owner_panel":

        if not is_owner:
            return

        edit_message(
            chat_id,
            message_id,
            f"👑 <b>{APP_NAME} OWNER PANEL</b>",
            owner_panel_keyboard()
        )

        return

    if data == "owner_clients":

        if not is_owner:
            return

        text, keyboard = owner_clients()

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data == "owner_bots":

        if not is_owner:
            return

        text, keyboard = owner_bots()

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data == "owner_status":

        if not is_owner:
            return

        text, keyboard = owner_status()

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data == "owner_stopall":

        if not is_owner:
            return

        bots = get_all_bots()

        count = 0

        for bot in bots:

            ok, _ = stop_bot(
                int(bot["id"])
            )

            if ok:
                count += 1

        edit_message(
            chat_id,
            message_id,
            (
                f"🛑 <b>STOP ALL</b>\n\n"
                f"Stopped: <b>{count}</b>"
            ),
            owner_panel_keyboard()
        )

        return

    # --------------------------------------------------------
    # OWNER CLIENT
    # --------------------------------------------------------

    if data.startswith("owner_client:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        text, keyboard = owner_client_view(
            cid
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data.startswith("owner_client_bots:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        bots = get_client_bots(cid)

        if not bots:

            text = "🤖 <b>NO BOTS</b>"
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Client",
                            "callback_data": f"owner_client:{cid}"
                        }
                    ]
                ]
            }

        else:

            client = get_client(cid)

            buttons = []

            for bot in bots:

                status = {
                    "running": "🟢",
                    "stopped": "⚪",
                    "error": "🔴"
                }.get(
                    bot["status"],
                    "⚪"
                )

                buttons.append([
                    {
                        "text": (
                            f"{status} #{bot['id']} "
                            f"{bot['name']}"
                        )[:64],
                        "callback_data": f"owner_bot:{bot['id']}"
                    }
                ])

            buttons.append([
                {
                    "text": "⬅️ Client",
                    "callback_data": f"owner_client:{cid}"
                }
            ])

            text = (
                f"🤖 <b>{escape_html(client_display(client))}</b>\n\n"
                f"Total bots: <b>{len(bots)}</b>"
            )

            keyboard = {
                "inline_keyboard": buttons
            }

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data.startswith("toggle_client:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        client = get_client(cid)

        if not client:
            return

        new_value = not bool(
            client["enabled"]
        )

        set_client_enabled(
            cid,
            new_value
        )

        text, keyboard = owner_client_view(
            cid
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data.startswith("stop_client:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        bots = get_client_bots(cid)

        count = 0

        for bot in bots:

            ok, _ = stop_bot(
                int(bot["id"])
            )

            if ok:
                count += 1

        text, keyboard = owner_client_view(
            cid
        )

        edit_message(
            chat_id,
            message_id,
            (
                f"🛑 Stopped <b>{count}</b> bots.\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("delete_client:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        client = get_client(cid)

        if not client:
            return

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>PERMANENT DELETE</b>\n\n"
                f"Client: <b>{escape_html(client_display(client))}</b>\n"
                f"ID: <code>{cid}</code>\n\n"
                "This will permanently delete:\n"
                "• Client record\n"
                "• All bots\n"
                "• Python files\n"
                "• Logs\n"
                "• Virtual environments\n\n"
                "<b>This action cannot be undone.</b>"
            ),
            confirm_client_delete(cid)
        )

        return

    if data.startswith("confirm_client:"):

        if not is_owner:
            return

        cid = int(
            data.split(":")[1]
        )

        ok, result = permanent_delete_client(
            cid
        )

        edit_message(
            chat_id,
            message_id,
            (
                f"🗑️ <b>{escape_html(result)}</b>"
            ),
            owner_panel_keyboard()
        )

        return

    # --------------------------------------------------------
    # OWNER BOT
    # --------------------------------------------------------

    if data.startswith("owner_bot:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        text, keyboard = owner_bot_view(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data.startswith("owner_start:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        ok, result = start_bot(
            bot_id
        )

        text, keyboard = owner_bot_view(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("owner_stop:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        ok, result = stop_bot(
            bot_id
        )

        text, keyboard = owner_bot_view(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("owner_restart:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        ok, result = restart_bot(
            bot_id
        )

        text, keyboard = owner_bot_view(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("owner_logs:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        logs = read_logs(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                f"📜 <b>BOT #{bot_id} LOGS</b>\n\n"
                f"<pre>{escape_html(logs[-11000:])}</pre>"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Bot",
                            "callback_data": f"owner_bot:{bot_id}"
                        }
                    ]
                ]
            }
        )

        return

    if data.startswith("owner_delete_bot:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>PERMANENT BOT DELETE</b>\n\n"
                f"Bot ID: <code>#{bot_id}</code>\n\n"
                "The bot process, Python file, logs and "
                "virtual environment will be permanently deleted.\n\n"
                "<b>This cannot be undone.</b>"
            ),
            confirm_bot_delete(
                bot_id,
                owner_mode=True
            )
        )

        return

    if data.startswith("confirm_owner_delete:"):

        if not is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        ok, result = permanent_delete_bot(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            f"🗑️ <b>{escape_html(result)}</b>",
            owner_panel_keyboard()
        )

        return

    # --------------------------------------------------------
    # CLIENT PANEL
    # --------------------------------------------------------

    if data == "client_panel":

        if is_owner:
            return

        if not client_enabled(chat_id):
            return

        edit_message(
            chat_id,
            message_id,
            f"🤖 <b>{APP_NAME}</b>",
            client_panel_keyboard()
        )

        return

    if data == "client_bots":

        if is_owner:
            return

        text, keyboard = client_bots(
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data == "client_status":

        if is_owner:
            return

        text, keyboard = client_status(
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data == "client_help":

        if is_owner:
            return

        edit_message(
            chat_id,
            message_id,
            help_text(False),
            back_client_keyboard()
        )

        return

    # --------------------------------------------------------
    # CLIENT BOT
    # --------------------------------------------------------

    if data.startswith("client_bot:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            return

        if int(bot["owner_chat_id"]) != chat_id:
            return

        text, keyboard = client_bot_view(
            bot_id,
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return

    if data.startswith("client_start:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        ok, result = start_bot(
            bot_id
        )

        text, keyboard = client_bot_view(
            bot_id,
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("client_stop:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        ok, result = stop_bot(
            bot_id
        )

        text, keyboard = client_bot_view(
            bot_id,
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("client_restart:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        ok, result = restart_bot(
            bot_id
        )

        text, keyboard = client_bot_view(
            bot_id,
            chat_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                ("🟢 " if ok else "🔴 ")
                + escape_html(result)
                + "\n\n"
                + text
            ),
            keyboard
        )

        return

    if data.startswith("client_logs:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        logs = read_logs(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            (
                f"📜 <b>BOT #{bot_id} LOGS</b>\n\n"
                f"<pre>{escape_html(logs[-11000:])}</pre>"
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Bot",
                            "callback_data": f"client_bot:{bot_id}"
                        }
                    ]
                ]
            }
        )

        return

    if data.startswith("client_delete_bot:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>PERMANENT BOT DELETE</b>\n\n"
                f"Bot ID: <code>#{bot_id}</code>\n\n"
                "This will permanently delete the Python file, "
                "logs and virtual environment.\n\n"
                "<b>This action cannot be undone.</b>"
            ),
            confirm_bot_delete(
                bot_id,
                owner_mode=False
            )
        )

        return

    if data.startswith("confirm_client_delete:"):

        if is_owner:
            return

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot or int(bot["owner_chat_id"]) != chat_id:
            return

        ok, result = permanent_delete_bot(
            bot_id
        )

        edit_message(
            chat_id,
            message_id,
            f"🗑️ <b>{escape_html(result)}</b>",
            client_panel_keyboard()
        )

        return

    # --------------------------------------------------------
    # AUTO RESTART
    # --------------------------------------------------------

    if data.startswith("toggle_auto:"):

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(bot_id)

        if not bot:
            return

        owner_id = int(
            bot["owner_chat_id"]
        )

        if not is_owner and owner_id != chat_id:
            return

        new_value = not bool(
            bot["auto_restart"]
        )

        set_auto_restart(
            bot_id,
            new_value
        )

        if is_owner:

            text, keyboard = owner_bot_view(
                bot_id
            )

        else:

            text, keyboard = client_bot_view(
                bot_id,
                chat_id
            )

        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )

        return


# ============================================================
# UPDATE LOOP
# ============================================================

def process_update(update):

    try:

        if update.get("message"):

            handle_message(
                update["message"]
            )

        elif update.get("callback_query"):

            handle_callback(
                update["callback_query"]
            )

    except Exception as e:

        print(
            f"[UPDATE ERROR] {e}",
            flush=True
        )


def polling():

    offset = None

    # Remove old webhook before polling

    telegram(
        "deleteWebhook",
        {
            "drop_pending_updates": False
        }
    )

    print(
        f"[{APP_NAME}] Polling started.",
        flush=True
    )

    while not shutdown_event.is_set():

        try:

            data = {
                "timeout": 30,
                "allowed_updates": json.dumps([
                    "message",
                    "callback_query"
                ])
            }

            if offset is not None:
                data["offset"] = offset

            result = telegram(
                "getUpdates",
                data,
                timeout=40
            )

            if not result.get("ok"):

                time.sleep(3)
                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"] + 1
                )

                process_update(
                    update
                )

        except KeyboardInterrupt:

            break

        except Exception as e:

            print(
                f"[POLLING ERROR] {e}",
                flush=True
            )

            time.sleep(3)


# ============================================================
# BOT RECOVERY
# ============================================================

def recover_bots():

    print(
        "[HOST] Checking bots for recovery...",
        flush=True
    )

    bots = get_all_bots()

    for bot in bots:

        bot_id = int(bot["id"])

        update_bot_status(
            bot_id,
            "stopped",
            0
        )

    for bot in bots:

        if int(bot["auto_restart"]) != 1:
            continue

        owner_id = int(
            bot["owner_chat_id"]
        )

        if not client_enabled(owner_id):
            continue

        print(
            f"[HOST] Recovering bot #{bot['id']}...",
            flush=True
        )

        threading.Thread(
            target=start_bot,
            args=(int(bot["id"]),),
            daemon=True
        ).start()

        time.sleep(1)


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/":

            bots = get_all_bots()

            running = sum(
                1
                for bot in bots
                if bot["status"] == "running"
            )

            body = (
                f"{APP_NAME} ONLINE\n"
                f"Version: {VERSION}\n"
                f"Bots: {len(bots)}\n"
                f"Running: {running}\n"
            ).encode()

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(len(body))
            )
            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():

    port = int(
        os.getenv("PORT", "10000")
    )

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(
        f"[HEALTH] Server running on port {port}",
        flush=True
    )

    server.serve_forever()


# ============================================================
# CLEAN SHUTDOWN
# ============================================================

def shutdown():

    if shutdown_event.is_set():
        return

    print(
        f"[{APP_NAME}] Shutting down...",
        flush=True
    )

    shutdown_event.set()

    bots = get_all_bots()

    for bot in bots:

        try:

            stop_bot(
                int(bot["id"])
            )

        except Exception as e:

            print(
                f"[SHUTDOWN ERROR] {e}",
                flush=True
            )


def signal_handler(signum, frame):

    shutdown()


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print(f"STARTING {APP_NAME}")
    print("=" * 70)

    print(
        f"Version: {VERSION}",
        flush=True
    )

    print(
        f"PID: {os.getpid()}",
        flush=True
    )

    if not BOT_TOKEN:

        print(
            "ERROR: BOT_TOKEN environment variable is missing.",
            flush=True
        )

        sys.exit(1)

    if not OWNER_CHAT_ID:

        print(
            "ERROR: OWNER_CHAT_ID environment variable is missing.",
            flush=True
        )

        sys.exit(1)

    init_db()

    signal.signal(
        signal.SIGTERM,
        signal_handler
    )

    signal.signal(
        signal.SIGINT,
        signal_handler
    )

    # Health server

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Recover existing bots

    recover_bots()

    # Telegram polling

    polling()

    shutdown()


if __name__ == "__main__":
    main()
