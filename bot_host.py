import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import threading
import ast
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI CLIENT TELEGRAM PYTHON HOSTING MANAGER
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"
VERSION = "6.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()

try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW)
except Exception:
    OWNER_CHAT_ID = 0

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
# GLOBAL RUNTIME STATE
# ============================================================

processes = {}
process_lock = threading.RLock()

# Bots intentionally stopped.
# Watchers will NOT auto restart these.
stop_requested = set()

# Callback confirmation state
pending_delete_bot = {}
pending_delete_client = {}

# Telegram offset
telegram_offset = 0


# ============================================================
# IMPORT -> PACKAGE MAP
# ============================================================

IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot==22.5",
    "telegram.ext": "python-telegram-bot==22.5",

    "openai": "openai>=1.50.0,<2",

    "requests": "requests>=2.31.0",
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
}


STDLIB_MODULES = set(
    getattr(sys, "stdlib_module_names", set())
)

STDLIB_MODULES.update({
    "os", "sys", "re", "json", "time", "math",
    "random", "datetime", "calendar", "sqlite3",
    "subprocess", "threading", "signal", "pathlib",
    "typing", "asyncio", "logging", "traceback",
    "collections", "itertools", "functools",
    "statistics", "hashlib", "secrets", "uuid",
    "base64", "urllib", "http", "email", "socket",
    "ssl", "csv", "io", "tempfile", "shutil",
    "zipfile", "glob", "inspect", "dataclasses",
    "enum", "argparse", "configparser", "copy",
    "pickle", "struct", "string", "textwrap",
    "warnings", "platform"
})


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.RLock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def init_db():
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    last_seen TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_chat_id INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    filename TEXT DEFAULT '',
                    folder TEXT DEFAULT '',
                    status TEXT DEFAULT 'stopped',
                    pid INTEGER DEFAULT 0,
                    auto_restart INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS hosting_access (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS global_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                INSERT OR IGNORE INTO global_settings
                (key, value)
                VALUES ('global_client_lock', '0')
            """)

            conn.commit()

        finally:
            conn.close()


# ============================================================
# GLOBAL CLIENT LOCK
# ============================================================

def is_global_client_lock():
    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT value
                FROM global_settings
                WHERE key='global_client_lock'
            """).fetchone()

            if not row:
                return False

            return str(row["value"]) == "1"

        finally:
            conn.close()


def set_global_client_lock(locked):
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                INSERT INTO global_settings
                (key, value)
                VALUES ('global_client_lock', ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
            """, ("1" if locked else "0",))

            conn.commit()

        finally:
            conn.close()


# ============================================================
# TELEGRAM
# ============================================================

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, timeout=60):
    try:
        response = requests.post(
            f"{TG_API}/{method}",
            data=data or {},
            timeout=timeout
        )

        if not response.ok:
            print(
                f"[TELEGRAM HTTP {response.status_code}] "
                f"{method}: {response.text[:500]}"
            )
            return None

        try:
            return response.json()
        except Exception:
            return None

    except Exception as e:
        print(
            f"[TELEGRAM ERROR] {method}: {e}"
        )
        return None


def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    return telegram(
        "sendMessage",
        data
    )


def edit_message(
    chat_id,
    message_id,
    text,
    reply_markup=None
):
    data = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    result = telegram(
        "editMessageText",
        data
    )

    # Message may not need editing if identical.
    return result


def answer_callback(callback_id, text=""):
    return telegram(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text[:190],
            "show_alert": False
        }
    )


def escape_html(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_long_message(
    chat_id,
    text,
    reply_markup=None
):
    limit = 3900

    if len(text) <= limit:
        return send_message(
            chat_id,
            text,
            reply_markup
        )

    chunks = [
        text[i:i + limit]
        for i in range(
            0,
            len(text),
            limit
        )
    ]

    for index, chunk in enumerate(chunks):
        send_message(
            chat_id,
            chunk,
            reply_markup
            if index == len(chunks) - 1
            else None
        )


# ============================================================
# USER
# ============================================================

def user_info(user):
    return {
        "chat_id": int(user.get("id", 0)),
        "username": user.get("username", "") or "",
        "first_name": user.get("first_name", "") or "",
        "last_name": user.get("last_name", "") or ""
    }


def display_username(row):
    username = row["username"] or ""

    if username:
        return "@" + username

    name = (
        f"{row['first_name'] or ''} "
        f"{row['last_name'] or ''}"
    ).strip()

    return name or str(row["chat_id"])


def is_owner(chat_id):
    return int(chat_id) == OWNER_CHAT_ID


# ============================================================
# HOSTING ACCESS
# ============================================================

def has_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return True

    if is_global_client_lock():
        return False

    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT enabled
                FROM hosting_access
                WHERE chat_id=?
            """, (chat_id,)).fetchone()

            return bool(
                row and
                int(row["enabled"]) == 1
            )

        finally:
            conn.close()


def grant_hosting_access_by_id(
    chat_id,
    username="",
    first_name="",
    last_name=""
):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
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

                ON CONFLICT(chat_id)
                DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    enabled=1,
                    updated_at=excluded.updated_at
            """, (
                chat_id,
                username,
                first_name,
                last_name,
                now(),
                now()
            ))

            conn.commit()

        finally:
            conn.close()


def grant_hosting_access(user):
    info = user_info(user)

    grant_hosting_access_by_id(
        info["chat_id"],
        info["username"],
        info["first_name"],
        info["last_name"]
    )

    send_message(
        info["chat_id"],
        (
            f"🎉 <b>{APP_NAME}</b>\n\n"
            "✅ Hosting access granted.\n\n"
            "Use /panel to open your panel."
        )
    )


def revoke_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False

    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
                UPDATE hosting_access
                SET enabled=0,
                    updated_at=?
                WHERE chat_id=?
            """, (
                now(),
                chat_id
            ))

            conn.commit()

            return cursor.rowcount > 0

        finally:
            conn.close()


def get_access_users():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM hosting_access
                ORDER BY updated_at DESC
            """).fetchall()

        finally:
            conn.close()


# ============================================================
# CLIENT DATABASE
# ============================================================

def upsert_client(user):
    info = user_info(user)

    created = False

    with db_lock:
        conn = get_db()

        try:
            existing = conn.execute("""
                SELECT chat_id
                FROM clients
                WHERE chat_id=?
            """, (
                info["chat_id"],
            )).fetchone()

            if existing is None:
                created = True

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
                    info["chat_id"],
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    now()
                ))

            else:
                conn.execute("""
                    UPDATE clients
                    SET username=?,
                        first_name=?,
                        last_name=?,
                        last_seen=?
                    WHERE chat_id=?
                """, (
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    info["chat_id"]
                ))

            conn.commit()

        finally:
            conn.close()

    if created and not is_owner(info["chat_id"]):
        notify_owner_new_client(info)

    return created


def get_client(chat_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                WHERE chat_id=?
            """, (
                int(chat_id),
            )).fetchone()

        finally:
            conn.close()


def get_clients():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                ORDER BY created_at DESC
            """).fetchall()

        finally:
            conn.close()


def set_client_enabled(chat_id, enabled):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                UPDATE clients
                SET enabled=?
                WHERE chat_id=?
            """, (
                1 if enabled else 0,
                chat_id
            ))

            conn.commit()

        finally:
            conn.close()

    if not enabled:
        stop_all_bots_of_client(
            chat_id
        )


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(
    owner_chat_id,
    name,
    filename,
    folder
):
    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
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
                folder,
                now(),
                now()
            ))

            conn.commit()

            return cursor.lastrowid

        finally:
            conn.close()


def get_bot(bot_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM bots
                WHERE id=?
            """, (
                int(bot_id),
            )).fetchone()

        finally:
            conn.close()


def get_bots(owner_chat_id=None):
    with db_lock:
        conn = get_db()

        try:
            if owner_chat_id is None:
                return conn.execute("""
                    SELECT *
                    FROM bots
                    ORDER BY id DESC
                """).fetchall()

            return conn.execute("""
                SELECT *
                FROM bots
                WHERE owner_chat_id=?
                ORDER BY id DESC
            """, (
                int(owner_chat_id),
            )).fetchall()

        finally:
            conn.close()


def update_bot(bot_id, **fields):
    allowed = {
        "name",
        "filename",
        "folder",
        "status",
        "pid",
        "auto_restart",
        "updated_at"
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not fields:
        return

    fields["updated_at"] = now()

    assignments = []
    values = []

    for key, value in fields.items():
        assignments.append(
            f"{key}=?"
        )
        values.append(value)

    values.append(int(bot_id))

    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                f"""
                UPDATE bots
                SET {", ".join(assignments)}
                WHERE id=?
                """,
                values
            )

            conn.commit()

        finally:
            conn.close()


# ============================================================
# FILES
# ============================================================

def client_folder(chat_id):
    folder = (
        CLIENTS_DIR /
        str(int(chat_id))
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def bot_folder(chat_id, bot_id):
    folder = (
        client_folder(chat_id) /
        f"bot_{int(bot_id)}"
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def bot_script(row):
    return (
        Path(row["folder"]) /
        row["filename"]
    )


def bot_log_file(row):
    return (
        Path(row["folder"]) /
        "bot.log"
    )


# ============================================================
# LOG
# ============================================================

def write_log(path, text):
    try:
        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with path.open(
            "a",
            encoding="utf-8",
            errors="ignore"
        ) as file:
            file.write(text)

    except Exception as e:
        print(
            f"[LOG ERROR] {e}"
        )


def read_log(row, max_chars=12000):
    path = bot_log_file(row)

    if not path.exists():
        return "No logs available."

    try:
        data = path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        if len(data) > max_chars:
            data = (
                "… older logs trimmed …\n\n"
                + data[-max_chars:]
            )

        return data or "Log is empty."

    except Exception as e:
        return f"Log error: {e}"


# ============================================================
# IMPORT DETECTION
# ============================================================

def detect_imports(script):
    modules = set()

    try:
        source = script.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        tree = ast.parse(
            source,
            filename=str(script)
        )

    except Exception as e:
        raise RuntimeError(
            f"Python syntax/import parsing failed: {e}"
        )

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(
                    alias.name.split(".")[0]
                )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(
                    node.module.split(".")[0]
                )

    return modules


def detect_packages(script):
    modules = detect_imports(script)

    packages = set()

    for module in modules:

        if module in STDLIB_MODULES:
            continue

        package = IMPORT_TO_PACKAGE.get(
            module
        )

        if package:
            packages.add(package)

    return sorted(packages)


# ============================================================
# VENV
# ============================================================

def venv_python(folder):
    if os.name == "nt":
        return (
            folder /
            ".venv" /
            "Scripts" /
            "python.exe"
        )

    return (
        folder /
        ".venv" /
        "bin" /
        "python"
    )


def ensure_venv(folder, log_file):
    python = venv_python(folder)

    if python.exists():
        return python

    write_log(
        log_file,
        "\n[HOST] Creating .venv...\n"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            str(folder / ".venv")
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300
    )

    write_log(
        log_file,
        result.stdout
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Virtual environment creation failed."
        )

    return python


def dependency_hash(packages):
    value = "\n".join(
        sorted(packages)
    )

    return hashlib.sha256(
        value.encode()
    ).hexdigest()


def install_dependencies(
    folder,
    script,
    log_file
):
    packages = detect_packages(
        script
    )

    marker = (
        folder /
        ".dependencies_hash"
    )

    current_hash = dependency_hash(
        packages
    )

    if marker.exists():

        try:
            old_hash = marker.read_text(
                encoding="utf-8"
            ).strip()

            if old_hash == current_hash:
                write_log(
                    log_file,
                    "[HOST] Dependencies unchanged.\n"
                )
                return

        except Exception:
            pass

    if not packages:

        write_log(
            log_file,
            "[HOST] No known external packages detected.\n"
        )

        marker.write_text(
            current_hash,
            encoding="utf-8"
        )

        return

    python = ensure_venv(
        folder,
        log_file
    )

    write_log(
        log_file,
        (
            "\n[HOST] Packages detected:\n"
            + "\n".join(
                f"  - {p}"
                for p in packages
            )
            + "\n"
        )
    )

    write_log(
        log_file,
        "\n[HOST] Installing dependencies...\n"
    )

    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            *packages
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=1200
    )

    write_log(
        log_file,
        result.stdout
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Dependency installation failed."
        )

    marker.write_text(
        current_hash,
        encoding="utf-8"
    )


# ============================================================
# PROCESS HELPERS
# ============================================================

def process_alive(process):
    try:
        return (
            process is not None
            and process.poll() is None
        )
    except Exception:
        return False


def terminate_process(process):
    if not process:
        return

    try:
        if os.name == "posix":

            try:
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGTERM
                )

            except ProcessLookupError:
                pass

            except Exception:
                try:
                    process.terminate()
                except Exception:
                    pass

        else:
            try:
                process.terminate()
            except Exception:
                pass

    except Exception as e:
        print(
            f"[TERMINATE ERROR] {e}"
        )


def kill_process(process):
    if not process:
        return

    try:
        if os.name == "posix":

            try:
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGKILL
                )

            except ProcessLookupError:
                pass

            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        else:
            try:
                process.kill()
            except Exception:
                pass

    except Exception as e:
        print(
            f"[KILL ERROR] {e}"
        )


def set_bot_status(
    bot_id,
    status,
    pid=0
):
    update_bot(
        bot_id,
        status=status,
        pid=pid
    )


# ============================================================
# START BOT
# ============================================================

def start_bot(bot_id):
    bot_id = int(bot_id)

    row = get_bot(bot_id)

    if not row:
        return False, "Bot not found."

    owner_id = int(
        row["owner_chat_id"]
    )

    client = get_client(
        owner_id
    )

    if client and not bool(
        client["enabled"]
    ):
        return (
            False,
            "Client hosting is disabled."
        )

    folder = Path(
        row["folder"]
    )

    script = bot_script(
        row
    )

    if not script.exists():
        set_bot_status(
            bot_id,
            "error",
            0
        )

        return (
            False,
            "Python file not found."
        )

    with process_lock:

        old = processes.get(
            bot_id
        )

        if old and process_alive(old):
            return (
                False,
                "Bot is already running."
            )

        stop_requested.discard(
            bot_id
        )

    log_file = bot_log_file(
        row
    )

    write_log(
        log_file,
        (
            "\n"
            + "=" * 60
            + "\n"
            + f"[HOST] STARTING BOT #{bot_id}\n"
            + f"[HOST] Time: {now()}\n"
            + "=" * 60
            + "\n"
        )
    )

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

        python = ensure_venv(
            folder,
            log_file
        )

        install_dependencies(
            folder,
            script,
            log_file
        )

        output = open(
            log_file,
            "a",
            encoding="utf-8",
            errors="ignore"
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        if os.name == "posix":

            process = subprocess.Popen(
                [
                    str(python),
                    str(script)
                ],
                cwd=str(folder),
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True
            )

        else:

            process = subprocess.Popen(
                [
                    str(python),
                    str(script)
                ],
                cwd=str(folder),
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env
            )

        with process_lock:
            processes[bot_id] = process
            stop_requested.discard(
                bot_id
            )

        set_bot_status(
            bot_id,
            "running",
            process.pid
        )

        write_log(
            log_file,
            (
                f"[HOST] PID: {process.pid}\n"
                "[HOST] Bot started.\n\n"
            )
        )

        threading.Thread(
            target=watch_process,
            args=(bot_id, process),
            daemon=True
        ).start()

        return (
            True,
            f"Bot started. PID: {process.pid}"
        )

    except Exception as e:

        write_log(
            log_file,
            (
                "\n[HOST ERROR]\n"
                f"{e}\n"
            )
        )

        set_bot_status(
            bot_id,
            "error",
            0
        )

        return (
            False,
            str(e)
        )


# ============================================================
# WATCHER
# ============================================================

def watch_process(
    bot_id,
    process
):
    try:
        exit_code = process.wait()

    except Exception as e:
        print(
            f"[WATCHER ERROR] #{bot_id}: {e}"
        )
        exit_code = -1

    row = get_bot(
        bot_id
    )

    if row:
        write_log(
            bot_log_file(row),
            (
                "\n"
                f"[HOST] Process exited."
                f" Code={exit_code}\n"
                f"[HOST] {now()}\n"
            )
        )

    with process_lock:

        if processes.get(bot_id) is process:
            processes.pop(
                bot_id,
                None
            )

        intentional = (
            bot_id in stop_requested
        )

    row = get_bot(
        bot_id
    )

    if not row:
        with process_lock:
            stop_requested.discard(
                bot_id
            )
        return

    client = get_client(
        int(row["owner_chat_id"])
    )

    client_enabled = True

    if client:
        client_enabled = bool(
            client["enabled"]
        )

    if intentional:

        set_bot_status(
            bot_id,
            "stopped",
            0
        )

        with process_lock:
            stop_requested.discard(
                bot_id
            )

        return

    if not client_enabled:

        set_bot_status(
            bot_id,
            "stopped",
            0
        )

        return

    if bool(row["auto_restart"]):

        set_bot_status(
            bot_id,
            "restarting",
            0
        )

        time.sleep(2)

        with process_lock:

            if bot_id in stop_requested:
                stop_requested.discard(
                    bot_id
                )

                set_bot_status(
                    bot_id,
                    "stopped",
                    0
                )

                return

        success, message = start_bot(
            bot_id
        )

        if not success:

            set_bot_status(
                bot_id,
                "error",
                0
            )

            print(
                f"[AUTO RESTART FAILED] "
                f"#{bot_id}: {message}"
            )

    else:

        set_bot_status(
            bot_id,
            "stopped",
            0
        )


# ============================================================
# STOP BOT
# ============================================================

def stop_bot(
    bot_id,
    intentional=True
):
    bot_id = int(bot_id)

    row = get_bot(
        bot_id
    )

    if not row:
        return False, "Bot not found."

    # VERY IMPORTANT:
    # Mark intentional stop BEFORE killing process.
    if intentional:

        with process_lock:
            stop_requested.add(
                bot_id
            )

        update_bot(
            bot_id,
            auto_restart=0
        )

    with process_lock:
        process = processes.get(
            bot_id
        )

    if process and process_alive(process):

        terminate_process(
            process
        )

        deadline = (
            time.time() + 5
        )

        while time.time() < deadline:

            if not process_alive(process):
                break

            time.sleep(0.2)

        if process_alive(process):
            kill_process(
                process
            )

        try:
            process.wait(
                timeout=3
            )
        except Exception:
            pass

    with process_lock:
        processes.pop(
            bot_id,
            None
        )

    set_bot_status(
        bot_id,
        "stopped",
        0
    )

    return True, "Bot stopped."


# ============================================================
# RESTART BOT
# ============================================================

def restart_bot(bot_id):
    bot_id = int(bot_id)

    row = get_bot(
        bot_id
    )

    if not row:
        return False, "Bot not found."

    # Prevent watcher from auto restarting.
    with process_lock:
        stop_requested.add(
            bot_id
        )

    update_bot(
        bot_id,
        auto_restart=1
    )

    with process_lock:
        process = processes.get(
            bot_id
        )

    if process and process_alive(process):

        terminate_process(
            process
        )

        deadline = (
            time.time() + 5
        )

        while time.time() < deadline:

            if not process_alive(process):
                break

            time.sleep(0.2)

        if process_alive(process):
            kill_process(
                process
            )

        try:
            process.wait(
                timeout=3
            )
        except Exception:
            pass

    with process_lock:
        processes.pop(
            bot_id,
            None
        )
        stop_requested.discard(
            bot_id
        )

    set_bot_status(
        bot_id,
        "stopped",
        0
    )

    time.sleep(0.5)

    return start_bot(
        bot_id
    )


# ============================================================
# STOP ALL
# ============================================================

def stop_all_bots():
    rows = get_bots()

    # FIRST mark every bot as intentional.
    # This is the important fix.
    with process_lock:
        for row in rows:
            stop_requested.add(
                int(row["id"])
            )

    for row in rows:
        update_bot(
            int(row["id"]),
            auto_restart=0
        )

    with process_lock:
        running = list(
            processes.items()
        )

    # Graceful termination
    for bot_id, process in running:

        try:
            if process_alive(process):
                terminate_process(
                    process
                )
        except Exception:
            pass

    deadline = (
        time.time() + 7
    )

    while time.time() < deadline:

        alive = False

        with process_lock:
            current = list(
                processes.values()
            )

        for process in current:

            if process_alive(process):
                alive = True
                break

        if not alive:
            break

        time.sleep(0.2)

    # Force kill anything remaining.
    with process_lock:
        current = list(
            processes.items()
        )

    for bot_id, process in current:

        try:
            if process_alive(process):
                kill_process(
                    process
                )
        except Exception:
            pass

    for row in rows:

        set_bot_status(
            int(row["id"]),
            "stopped",
            0
        )

    with process_lock:
        processes.clear()

    # Keep flags for watchers briefly.
    def cleanup():
        time.sleep(5)

        with process_lock:
            for row in rows:
                stop_requested.discard(
                    int(row["id"])
                )

    threading.Thread(
        target=cleanup,
        daemon=True
    ).start()

    return len(rows)


def stop_all_bots_of_client(chat_id):
    rows = get_bots(
        chat_id
    )

    with process_lock:
        for row in rows:
            stop_requested.add(
                int(row["id"])
            )

    count = 0

    for row in rows:

        update_bot(
            int(row["id"]),
            auto_restart=0
        )

        success, _ = stop_bot(
            int(row["id"]),
            intentional=True
        )

        if success:
            count += 1

    return count


# ============================================================
# DELETE BOT
# ============================================================

def delete_bot_permanently(bot_id):
    bot_id = int(bot_id)

    row = get_bot(
        bot_id
    )

    if not row:
        return False, "Bot not found."

    stop_bot(
        bot_id,
        intentional=True
    )

    folder = Path(
        row["folder"]
    )

    with process_lock:
        processes.pop(
            bot_id,
            None
        )
        stop_requested.discard(
            bot_id
        )

    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                "DELETE FROM bots WHERE id=?",
                (bot_id,)
            )

            conn.commit()

        finally:
            conn.close()

    try:
        if folder.exists():
            shutil.rmtree(
                folder
            )
    except Exception as e:
        print(
            f"[BOT DELETE ERROR] {e}"
        )

    return True, "Bot permanently deleted."


# ============================================================
# DELETE CLIENT
# ============================================================

def delete_client_permanently(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False, "Owner cannot be deleted."

    rows = get_bots(
        chat_id
    )

    for row in rows:
        delete_bot_permanently(
            int(row["id"])
        )

    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                "DELETE FROM clients WHERE chat_id=?",
                (chat_id,)
            )

            conn.execute(
                "DELETE FROM hosting_access WHERE chat_id=?",
                (chat_id,)
            )

            conn.commit()

        finally:
            conn.close()

    folder = (
        CLIENTS_DIR /
        str(chat_id)
    )

    try:
        if folder.exists():
            shutil.rmtree(
                folder
            )
    except Exception as e:
        print(
            f"[CLIENT DELETE ERROR] {e}"
        )

    return True, "Client permanently deleted."


# ============================================================
# STATUS
# ============================================================

def bot_icon(row):
    status = str(
        row["status"]
    ).lower()

    if status == "running":
        return "🟢"

    if status == "restarting":
        return "🟡"

    if status == "error":
        return "🔴"

    return "⚪"


def bot_status_text(row):
    return (
        f"{bot_icon(row)} "
        f"{str(row['status']).upper()}"
    )


# ============================================================
# OWNER PANEL
# ============================================================

def owner_panel_keyboard():
    if is_global_client_lock():

        lock_button = {
            "text": "🔓 UNLOCK ALL CLIENTS",
            "callback_data": "unlockall"
        }

    else:

        lock_button = {
            "text": "🔒 LOCK ALL CLIENTS",
            "callback_data": "lockall"
        }

    return {
        "inline_keyboard": [
            [
                {
                    "text": "👥 Clients",
                    "callback_data": "clients"
                },
                {
                    "text": "🤖 All Bots",
                    "callback_data": "allbots"
                }
            ],
            [
                {
                    "text": "🔐 Hosting Access",
                    "callback_data": "access"
                }
            ],
            [
                {
                    "text": "➕ Grant Access",
                    "callback_data": "grantmenu"
                },
                {
                    "text": "🚫 Revoke Access",
                    "callback_data": "revokemenu"
                }
            ],
            [
                lock_button
            ],
            [
                {
                    "text": "🛑 STOP ALL BOTS",
                    "callback_data": "stopall"
                }
            ]
        ]
    }


def show_owner_panel(
    chat_id,
    message_id=None
):
    lock_status = (
        "🔒 GLOBAL CLIENT LOCK: ON"
        if is_global_client_lock()
        else "🟢 GLOBAL CLIENT LOCK: OFF"
    )

    clients = get_clients()
    bots = get_bots()

    running = sum(
        1
        for bot in bots
        if bot["status"] == "running"
    )

    text = (
        f"👑 <b>{APP_NAME}</b>\n\n"
        f"Version: <code>{VERSION}</code>\n"
        f"{lock_status}\n\n"
        f"👥 Clients: <b>{len(clients)}</b>\n"
        f"🤖 Bots: <b>{len(bots)}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
    )

    keyboard = owner_panel_keyboard()

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT PANEL
# ============================================================

def client_panel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🤖 My Bots",
                    "callback_data": "mybots"
                }
            ],
            [
                {
                    "text": "📤 Upload .py",
                    "callback_data": "uploadhelp"
                }
            ],
            [
                {
                    "text": "🔄 Refresh",
                    "callback_data": "panel"
                }
            ]
        ]
    }


def show_client_panel(
    chat_id,
    message_id=None
):
    if is_global_client_lock():

        text = (
            f"🔒 <b>{APP_NAME}</b>\n\n"
            "⛔ <b>HOSTING LOCKED</b>\n\n"
            "Owner ne sabhi client hosting "
            "controls temporarily lock kiye hain.\n\n"
            "🔓 Owner ke unlock karne ke baad "
            "aap dobara hosting use kar sakte hain."
        )

        keyboard = {
            "inline_keyboard": []
        }

        if message_id:
            edit_message(
                chat_id,
                message_id,
                text,
                keyboard
            )
        else:
            send_message(
                chat_id,
                text,
                keyboard
            )

        return

    if not has_hosting_access(chat_id):

        send_message(
            chat_id,
            (
                "🔐 <b>Access Denied</b>\n\n"
                "Aapke account ko hosting access "
                "nahi diya gaya hai."
            )
        )

        return

    client = get_client(
        chat_id
    )

    bots = get_bots(
        chat_id
    )

    username = (
        display_username(client)
        if client
        else str(chat_id)
    )

    text = (
        f"👤 <b>{escape_html(username)}</b>\n\n"
        f"🤖 Bots: <b>{len(bots)}</b>\n\n"
        "Hosting Panel ready."
    )

    keyboard = client_panel_keyboard()

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT LIST OWNER
# ============================================================

def show_clients(
    owner_chat_id,
    message_id=None
):
    if not is_owner(owner_chat_id):
        return

    clients = get_clients()

    text = (
        "👥 <b>CLIENTS</b>\n\n"
        "Client select karo:"
    )

    rows = []

    for client in clients:

        status = (
            "🟢"
            if client["enabled"]
            else "🔴"
        )

        name = display_username(
            client
        )

        rows.append([
            {
                "text": (
                    f"{status} "
                    f"{name[:25]} "
                    f"#{client['chat_id']}"
                ),
                "callback_data":
                    f"client:{client['chat_id']}"
            }
        ])

    if not clients:
        text += "\n\nNo clients yet."

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER CLIENT DETAIL
# ============================================================

def show_owner_client(
    owner_chat_id,
    client_id,
    message_id=None
):
    if not is_owner(owner_chat_id):
        return

    client_id = int(client_id)

    client = get_client(
        client_id
    )

    if not client:
        send_message(
            owner_chat_id,
            "❌ Client not found."
        )
        return

    bots = get_bots(
        client_id
    )

    name = display_username(
        client
    )

    status = (
        "🟢 ENABLED"
        if client["enabled"]
        else "🔴 DISABLED"
    )

    text = (
        "👤 <b>CLIENT</b>\n\n"
        f"Username: <b>{escape_html(name)}</b>\n"
        f"Chat ID: <code>{client_id}</code>\n"
        f"Status: <b>{status}</b>\n"
        f"Bots: <b>{len(bots)}</b>\n\n"
        "🤖 <b>CLIENT BOTS</b>\n"
    )

    if not bots:
        text += "\nNo bots."

    else:
        for bot in bots:
            text += (
                f"\n{bot_status_text(bot)} "
                f"<b>#{bot['id']} "
                f"{escape_html(bot['name'])}</b>"
            )

    rows = []

    for bot in bots:
        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:28]}"
                ),
                "callback_data":
                    f"obot:{bot['id']}"
            }
        ])

    if client["enabled"]:

        rows.append([
            {
                "text": "🚫 Disable Client",
                "callback_data":
                    f"disable:{client_id}"
            }
        ])

    else:

        rows.append([
            {
                "text": "✅ Enable Client",
                "callback_data":
                    f"enable:{client_id}"
            }
        ])

    rows.append([
        {
            "text": "🛑 Stop This Client's Bots",
            "callback_data":
                f"stopclient:{client_id}"
        }
    ])

    rows.append([
        {
            "text": "🗑️ Delete Client Permanently",
            "callback_data":
                f"dclient:{client_id}"
        }
    ])

    rows.append([
        {
            "text": "⬅️ Clients",
            "callback_data": "clients"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER BOT DETAIL
# ============================================================

def show_owner_bot(
    owner_chat_id,
    bot_id,
    message_id=None
):
    if not is_owner(owner_chat_id):
        return

    row = get_bot(
        bot_id
    )

    if not row:
        send_message(
            owner_chat_id,
            "❌ Bot not found."
        )
        return

    client = get_client(
        int(row["owner_chat_id"])
    )

    client_name = (
        display_username(client)
        if client
        else str(row["owner_chat_id"])
    )

    text = (
        "🤖 <b>BOT CONTROL</b>\n\n"
        f"Bot ID: <code>{row['id']}</code>\n"
        f"Name: <b>{escape_html(row['name'])}</b>\n"
        f"File: <code>{escape_html(row['filename'])}</code>\n"
        f"Client: <b>{escape_html(client_name)}</b>\n"
        f"Client ID: <code>{row['owner_chat_id']}</code>\n"
        f"Status: <b>{bot_status_text(row)}</b>\n"
        f"PID: <code>{row['pid'] or 0}</code>\n"
        f"Auto Restart: "
        f"<b>{'ON' if row['auto_restart'] else 'OFF'}</b>\n"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "▶️ Start",
                    "callback_data":
                        f"ob:s:{row['id']}"
                },
                {
                    "text": "⏹ Stop",
                    "callback_data":
                        f"ob:t:{row['id']}"
                }
            ],
            [
                {
                    "text": "🔄 Restart",
                    "callback_data":
                        f"ob:r:{row['id']}"
                },
                {
                    "text": "📜 Logs",
                    "callback_data":
                        f"ob:l:{row['id']}"
                }
            ],
            [
                {
                    "text": "🗑️ Delete",
                    "callback_data":
                        f"ob:d:{row['id']}"
                }
            ],
            [
                {
                    "text": "⬅️ Back to Client",
                    "callback_data":
                        f"client:{row['owner_chat_id']}"
                }
            ]
        ]
    }

    if message_id:
        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# ALL BOTS
# ============================================================

def show_all_bots(
    owner_chat_id,
    message_id=None
):
    if not is_owner(owner_chat_id):
        return

    bots = get_bots()

    text = (
        "🤖 <b>ALL BOTS</b>\n\n"
        f"Total: <b>{len(bots)}</b>\n"
    )

    rows = []

    for bot in bots:

        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:30]}"
                ),
                "callback_data":
                    f"obot:{bot['id']}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# MY BOTS
# ============================================================

def show_my_bots(
    chat_id,
    message_id=None
):
    if is_global_client_lock():

        show_client_panel(
            chat_id,
            message_id
        )

        return

    if not has_hosting_access(
        chat_id
    ):
        send_message(
            chat_id,
            "❌ Hosting access denied."
        )
        return

    bots = get_bots(
        chat_id
    )

    text = (
        "🤖 <b>MY BOTS</b>\n\n"
    )

    rows = []

    for bot in bots:

        text += (
            f"{bot_status_text(bot)} "
            f"<b>#{bot['id']} "
            f"{escape_html(bot['name'])}</b>\n"
            f"📄 {escape_html(bot['filename'])}\n"
            f"PID: <code>{bot['pid'] or 0}</code>\n\n"
        )

        rows.append([
            {
                "text": (
                    f"{bot_icon(bot)} "
                    f"#{bot['id']} "
                    f"{bot['name'][:30]}"
                ),
                "callback_data":
                    f"cbot:{bot['id']}"
            }
        ])

    if not bots:
        text += (
            "No bots yet.\n\n"
            "📤 Upload a .py file to create one."
        )

    rows.append([
        {
            "text": "📤 Upload .py",
            "callback_data": "uploadhelp"
        }
    ])

    rows.append([
        {
            "text": "⬅️ Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# CLIENT BOT DETAIL
# ============================================================

def show_client_bot(
    chat_id,
    bot_id,
    message_id=None
):
    if is_global_client_lock():

        show_client_panel(
            chat_id,
            message_id
        )

        return

    if not has_hosting_access(
        chat_id
    ):
        send_message(
            chat_id,
            "❌ Hosting access denied."
        )
        return

    row = get_bot(
        bot_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ Bot not found."
        )
        return

    if int(
        row["owner_chat_id"]
    ) != int(chat_id):

        send_message(
            chat_id,
            "❌ You can only control your own bots."
        )

        return

    text = (
        "🤖 <b>MY BOT</b>\n\n"
        f"ID: <code>{row['id']}</code>\n"
        f"Name: <b>{escape_html(row['name'])}</b>\n"
        f"File: <code>{escape_html(row['filename'])}</code>\n"
        f"Status: <b>{bot_status_text(row)}</b>\n"
        f"PID: <code>{row['pid'] or 0}</code>\n"
        f"Auto Restart: "
        f"<b>{'ON' if row['auto_restart'] else 'OFF'}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "▶️ Start",
                    "callback_data":
                        f"cb:s:{row['id']}"
                },
                {
                    "text": "⏹ Stop",
                    "callback_data":
                        f"cb:t:{row['id']}"
                }
            ],
            [
                {
                    "text": "🔄 Restart",
                    "callback_data":
                        f"cb:r:{row['id']}"
                },
                {
                    "text": "📜 Logs",
                    "callback_data":
                        f"cb:l:{row['id']}"
                }
            ],
            [
                {
                    "text": "🗑️ Delete",
                    "callback_data":
                        f"cb:d:{row['id']}"
                }
            ],
            [
                {
                    "text": "⬅️ My Bots",
                    "callback_data": "mybots"
                }
            ]
        ]
    }

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


# ============================================================
# ACCESS LIST
# ============================================================

def show_access(
    owner_chat_id,
    message_id=None
):
    if not is_owner(owner_chat_id):
        return

    users = get_access_users()

    lock_text = (
        "🔒 GLOBAL LOCK ACTIVE"
        if is_global_client_lock()
        else "🟢 GLOBAL LOCK OFF"
    )

    text = (
        "🔐 <b>HOSTING ACCESS</b>\n\n"
        f"{lock_text}\n\n"
    )

    rows = []

    for user in users:

        status = (
            "🟢"
            if user["enabled"]
            else "🔴"
        )

        name = display_username(
            user
        )

        text += (
            f"{status} <b>{escape_html(name)}</b>\n"
            f"ID: <code>{user['chat_id']}</code>\n\n"
        )

        rows.append([
            {
                "text": (
                    f"{status} "
                    f"{name[:30]}"
                ),
                "callback_data":
                    f"accessuser:{user['chat_id']}"
            }
        ])

    if not users:
        text += "No hosting access users."

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            owner_chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            owner_chat_id,
            text,
            keyboard
        )


# ============================================================
# OWNER ACCESS USER
# ============================================================

def show_access_user(
    owner_chat_id,
    user_id
):
    if not is_owner(owner_chat_id):
        return

    user_id = int(user_id)

    users = get_access_users()

    target = None

    for user in users:
        if int(user["chat_id"]) == user_id:
            target = user
            break

    if not target:
        send_message(
            owner_chat_id,
            "❌ Access user not found."
        )
        return

    enabled = bool(
        target["enabled"]
    )

    text = (
        "🔐 <b>ACCESS USER</b>\n\n"
        f"User: <b>{escape_html(display_username(target))}</b>\n"
        f"Chat ID: <code>{user_id}</code>\n"
        f"Status: "
        f"<b>{'ENABLED' if enabled else 'DISABLED'}</b>"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": (
                        "🚫 Revoke Access"
                        if enabled
                        else "✅ Grant Access"
                    ),
                    "callback_data":
                        (
                            f"revoke:{user_id}"
                            if enabled
                            else f"grant:{user_id}"
                        )
                }
            ],
            [
                {
                    "text": "⬅️ Access List",
                    "callback_data": "access"
                }
            ]
        ]
    }

    send_message(
        owner_chat_id,
        text,
        keyboard
    )


# ============================================================
# NEW CLIENT NOTIFICATION
# ============================================================

def notify_owner_new_client(info):
    if not OWNER_CHAT_ID:
        return

    username = (
        f"@{info['username']}"
        if info["username"]
        else "No username"
    )

    full_name = (
        f"{info['first_name']} "
        f"{info['last_name']}"
    ).strip()

    text = (
        "🆕 <b>NEW CLIENT</b>\n\n"
        f"👤 Username: "
        f"<b>{escape_html(username)}</b>\n"
        f"📝 Name: "
        f"<b>{escape_html(full_name or 'Unknown')}</b>\n"
        f"🆔 Chat ID: "
        f"<code>{info['chat_id']}</code>\n"
        f"🕒 Time: "
        f"<code>{now()}</code>\n\n"
        "⚠️ Hosting access is not granted "
        "automatically."
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Grant Hosting",
                    "callback_data":
                        f"grant:{info['chat_id']}"
                }
            ],
            [
                {
                    "text": "👤 Open Client",
                    "callback_data":
                        f"client:{info['chat_id']}"
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
# BOT ACTION
# ============================================================

def execute_bot_action(
    chat_id,
    bot_id,
    action,
    owner_view
):
    bot_id = int(bot_id)

    row = get_bot(
        bot_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ Bot not found."
        )
        return

    bot_owner = int(
        row["owner_chat_id"]
    )

    if owner_view:

        if not is_owner(chat_id):
            send_message(
                chat_id,
                "❌ Owner only."
            )
            return

    else:

        if is_global_client_lock():

            send_message(
                chat_id,
                (
                    "🔒 Hosting is currently locked "
                    "by owner."
                )
            )
            return

        if not has_hosting_access(
            chat_id
        ):
            send_message(
                chat_id,
                "❌ Hosting access denied."
            )
            return

        if bot_owner != int(chat_id):
            send_message(
                chat_id,
                "❌ You can only control your own bots."
            )
            return

    # ----------------------------
    # START
    # ----------------------------

    if action == "s":

        success, message = start_bot(
            bot_id
        )

        send_message(
            chat_id,
            (
                "▶️ <b>START</b>\n\n"
                + escape_html(message)
            )
        )

    # ----------------------------
    # STOP
    # ----------------------------

    elif action == "t":

        success, message = stop_bot(
            bot_id,
            intentional=True
        )

        send_message(
            chat_id,
            (
                "⏹ <b>STOP</b>\n\n"
                + escape_html(message)
            )
        )

    # ----------------------------
    # RESTART
    # ----------------------------

    elif action == "r":

        success, message = restart_bot(
            bot_id
        )

        send_message(
            chat_id,
            (
                "🔄 <b>RESTART</b>\n\n"
                + escape_html(message)
            )
        )

    # ----------------------------
    # LOGS
    # ----------------------------

    elif action == "l":

        logs = read_log(
            row
        )

        send_long_message(
            chat_id,
            (
                f"📜 <b>LOGS - "
                f"{escape_html(row['name'])}</b>\n\n"
                f"<pre>{escape_html(logs)}</pre>"
            )
        )

    # ----------------------------
    # DELETE
    # ----------------------------

    elif action == "d":

        warning = (
            "⚠️ <b>PERMANENT DELETE</b>\n\n"
            f"Bot: <b>{escape_html(row['name'])}</b>\n"
            f"ID: <code>{bot_id}</code>\n\n"
            "This will permanently delete:\n"
            "• Python file\n"
            "• .venv\n"
            "• logs\n"
            "• bot folder\n"
            "• database record\n\n"
            "❗ <b>THIS CANNOT BE UNDONE.</b>"
        )

        confirm_prefix = (
            "confirmob"
            if owner_view
            else "confirmcb"
        )

        cancel_callback = (
            f"obot:{bot_id}"
            if owner_view
            else f"cbot:{bot_id}"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, DELETE PERMANENTLY",
                        "callback_data":
                            f"{confirm_prefix}:{bot_id}"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            cancel_callback
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            warning,
            keyboard
        )


# ============================================================
# UPLOAD
# ============================================================

def upload_help(chat_id):
    if is_global_client_lock():

        send_message(
            chat_id,
            (
                "🔒 <b>HOSTING LOCKED</b>\n\n"
                "Owner ne client hosting lock ki hai."
            )
        )

        return

    if not has_hosting_access(
        chat_id
    ):
        send_message(
            chat_id,
            "❌ Hosting access denied."
        )
        return

    send_message(
        chat_id,
        (
            "📤 <b>UPLOAD PYTHON BOT</b>\n\n"
            "Sirf ek Python file bhejo.\n\n"
            "Example:\n"
            "<code>main.py</code>\n\n"
            "❌ ZIP nahi\n"
            "❌ requirements.txt ki zarurat nahi\n\n"
            "Host automatically known imports "
            "detect karke dependencies install karega."
        )
    )


def download_document(
    chat_id,
    document
):
    if is_global_client_lock():

        send_message(
            chat_id,
            "🔒 Hosting is locked by owner."
        )

        return

    if not has_hosting_access(
        chat_id
    ):
        send_message(
            chat_id,
            "❌ Hosting access denied."
        )
        return

    filename = (
        document.get("file_name")
        or "main.py"
    )

    if not filename.lower().endswith(
        ".py"
    ):
        send_message(
            chat_id,
            "❌ Sirf .py file allowed hai."
        )
        return

    # Safe filename
    filename = Path(
        filename
    ).name

    if not filename:
        send_message(
            chat_id,
            "❌ Invalid filename."
        )
        return

    file_id = document.get(
        "file_id"
    )

    if not file_id:
        send_message(
            chat_id,
            "❌ Telegram file ID missing."
        )
        return

    send_message(
        chat_id,
        "⏳ File download ho rahi hai..."
    )

    result = telegram(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if not result or not result.get("ok"):
        send_message(
            chat_id,
            "❌ Telegram file information nahi mili."
        )
        return

    file_path = (
        result
        .get("result", {})
        .get("file_path")
    )

    if not file_path:
        send_message(
            chat_id,
            "❌ Telegram file path missing."
        )
        return

    try:
        response = requests.get(
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{file_path}",
            timeout=120
        )

        response.raise_for_status()

    except Exception as e:
        send_message(
            chat_id,
            (
                "❌ Download failed:\n"
                f"<code>{escape_html(e)}</code>"
            )
        )
        return

    # Temporary bot ID is created first.
    folder = None

    try:

        # Safe initial folder
        temp_folder = client_folder(
            chat_id
        ) / (
            "upload_" +
            hashlib.sha256(
                f"{time.time()}".encode()
            ).hexdigest()[:12]
        )

        temp_folder.mkdir(
            parents=True,
            exist_ok=True
        )

        temp_script = (
            temp_folder /
            filename
        )

        temp_script.write_bytes(
            response.content
        )

        # Validate syntax BEFORE creating DB bot.
        source = temp_script.read_text(
            encoding="utf-8",
            errors="ignore"
        )

        compile(
            source,
            str(temp_script),
            "exec"
        )

        # Create actual bot.
        bot_name = Path(
            filename
        ).stem

        # Create DB record with temporary folder.
        bot_id = create_bot(
            chat_id,
            bot_name,
            filename,
            str(temp_folder)
        )

        final_folder = (
            client_folder(chat_id) /
            f"bot_{bot_id}"
        )

        temp_folder.rename(
            final_folder
        )

        update_bot(
            bot_id,
            folder=str(final_folder)
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT CREATED</b>\n\n"
                f"🤖 Bot ID: <code>{bot_id}</code>\n"
                f"📄 File: "
                f"<code>{escape_html(filename)}</code>\n\n"
                "Use My Bots to start it."
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "▶️ Start Bot",
                            "callback_data":
                                f"cb:s:{bot_id}"
                        }
                    ],
                    [
                        {
                            "text": "🤖 My Bots",
                            "callback_data": "mybots"
                        }
                    ]
                ]
            }
        )

    except SyntaxError as e:

        if folder:
            try:
                shutil.rmtree(
                    folder,
                    ignore_errors=True
                )
            except Exception:
                pass

        send_message(
            chat_id,
            (
                "❌ <b>Python Syntax Error</b>\n\n"
                f"<code>{escape_html(e)}</code>"
            )
        )

    except Exception as e:

        send_message(
            chat_id,
            (
                "❌ <b>Upload Error</b>\n\n"
                f"<code>{escape_html(e)}</code>"
            )
        )


# ============================================================
# TELEGRAM UPDATE HANDLER
# ============================================================

def handle_message(message):
    if not message:
        return

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if chat_id is None:
        return

    user = message.get(
        "from",
        {}
    )

    text = (
        message.get("text")
        or ""
    ).strip()

    # Always register client.
    if user:
        upsert_client(
            user
        )

    # ----------------------------
    # /start
    # ----------------------------

    if text.startswith("/start"):

        if is_owner(chat_id):

            show_owner_panel(
                chat_id
            )

            return

        if is_global_client_lock():

            show_client_panel(
                chat_id
            )

            return

        if not has_hosting_access(
            chat_id
        ):

            send_message(
                chat_id,
                (
                    "🔐 <b>Hosting Access Required</b>\n\n"
                    "Aapka account registered hai, "
                    "lekin owner ne abhi hosting access "
                    "grant nahi kiya hai."
                )
            )

            return

        show_client_panel(
            chat_id
        )

        return

    # ----------------------------
    # /panel
    # ----------------------------

    if text.startswith("/panel"):

        if is_owner(chat_id):

            show_owner_panel(
                chat_id
            )

            return

        show_client_panel(
            chat_id
        )

        return

    # ----------------------------
    # /clients
    # ----------------------------

    if text.startswith("/clients"):

        if is_owner(chat_id):
            show_clients(
                chat_id
            )
        else:
            send_message(
                chat_id,
                "❌ Owner only."
            )

        return

    # ----------------------------
    # /bots
    # ----------------------------

    if text.startswith("/bots"):

        if is_owner(chat_id):
            show_all_bots(
                chat_id
            )
        else:
            show_my_bots(
                chat_id
            )

        return

    # ----------------------------
    # /status
    # ----------------------------

    if text.startswith("/status"):

        if is_owner(chat_id):

            bots = get_bots()

            running = sum(
                1
                for bot in bots
                if bot["status"] == "running"
            )

            send_message(
                chat_id,
                (
                    "📊 <b>HOST STATUS</b>\n\n"
                    f"Clients: <b>{len(get_clients())}</b>\n"
                    f"Bots: <b>{len(bots)}</b>\n"
                    f"Running: <b>{running}</b>\n"
                    f"Global Lock: "
                    f"<b>{'ON' if is_global_client_lock() else 'OFF'}</b>"
                )
            )

        else:

            if not has_hosting_access(
                chat_id
            ):
                send_message(
                    chat_id,
                    "❌ Hosting access denied."
                )
                return

            bots = get_bots(
                chat_id
            )

            running = sum(
                1
                for bot in bots
                if bot["status"] == "running"
            )

            send_message(
                chat_id,
                (
                    "📊 <b>MY STATUS</b>\n\n"
                    f"Bots: <b>{len(bots)}</b>\n"
                    f"Running: <b>{running}</b>"
                )
            )

        return

    # ----------------------------
    # /help
    # ----------------------------

    if text.startswith("/help"):

        if is_owner(chat_id):

            send_message(
                chat_id,
                (
                    "👑 <b>OWNER COMMANDS</b>\n\n"
                    "/panel\n"
                    "/clients\n"
                    "/bots\n"
                    "/status\n\n"
                    "Owner Panel se complete control available hai."
                )
            )

        else:

            send_message(
                chat_id,
                (
                    "🤖 <b>CLIENT COMMANDS</b>\n\n"
                    "/panel\n"
                    "/bots\n"
                    "/status\n"
                    "/help"
                )
            )

        return

    # ----------------------------
    # Document
    # ----------------------------

    document = message.get(
        "document"
    )

    if document:

        download_document(
            chat_id,
            document
        )

        return


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(query):
    if not query:
        return

    callback_id = query.get(
        "id"
    )

    data = (
        query.get("data")
        or ""
    )

    message = query.get(
        "message"
    )

    if not message:
        answer_callback(
            callback_id
        )
        return

    chat = message.get(
        "chat",
        {}
    )

    chat_id = int(
        chat.get("id")
    )

    message_id = int(
        message.get("message_id")
    )

    # ========================================================
    # OWNER PANEL
    # ========================================================

    if data == "panel":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_owner_panel(
                chat_id,
                message_id
            )

        else:

            show_client_panel(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # CLIENTS
    # ========================================================

    if data == "clients":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):
            show_clients(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # SELECT CLIENT
    # ========================================================

    if data.startswith("client:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # ALL BOTS
    # ========================================================

    if data == "allbots":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):

            show_all_bots(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # MY BOTS
    # ========================================================

    if data == "mybots":

        answer_callback(
            callback_id
        )

        show_my_bots(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # OWNER BOT
    # ========================================================

    if data.startswith("obot:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            bot_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        show_owner_bot(
            chat_id,
            bot_id,
            message_id
        )

        return

    # ========================================================
    # CLIENT BOT
    # ========================================================

    if data.startswith("cbot:"):

        answer_callback(
            callback_id
        )

        try:
            bot_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        show_client_bot(
            chat_id,
            bot_id,
            message_id
        )

        return

    # ========================================================
    # OWNER BOT ACTION
    # ========================================================

    if data.startswith("ob:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        parts = data.split(":")

        if len(parts) != 3:
            return

        action = parts[1]

        try:
            bot_id = int(
                parts[2]
            )
        except Exception:
            return

        execute_bot_action(
            chat_id,
            bot_id,
            action,
            True
        )

        return

    # ========================================================
    # CLIENT BOT ACTION
    # ========================================================

    if data.startswith("cb:"):

        answer_callback(
            callback_id
        )

        if is_global_client_lock():

            send_message(
                chat_id,
                "🔒 Hosting is locked by owner."
            )

            return

        parts = data.split(":")

        if len(parts) != 3:
            return

        action = parts[1]

        try:
            bot_id = int(
                parts[2]
            )
        except Exception:
            return

        execute_bot_action(
            chat_id,
            bot_id,
            action,
            False
        )

        return

    # ========================================================
    # UPLOAD HELP
    # ========================================================

    if data == "uploadhelp":

        answer_callback(
            callback_id
        )

        upload_help(
            chat_id
        )

        return

    # ========================================================
    # GLOBAL LOCK
    # ========================================================

    if data == "lockall":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        if is_global_client_lock():

            show_owner_panel(
                chat_id,
                message_id
            )

            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, LOCK ALL CLIENTS",
                        "callback_data":
                            "confirm_lockall"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data": "panel"
                    }
                ]
            ]
        }

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>LOCK ALL CLIENTS</b>\n\n"
                "Iske baad sabhi clients:\n\n"
                "❌ Panel controls use nahi kar sakenge\n"
                "❌ Upload nahi kar sakenge\n"
                "❌ Start nahi kar sakenge\n"
                "❌ Stop nahi kar sakenge\n"
                "❌ Restart nahi kar sakenge\n"
                "❌ Logs nahi dekh sakenge\n\n"
                "Existing running bots ko is action se "
                "<b>stop nahi kiya jayega</b>.\n\n"
                "Owner controls unaffected rahenge."
            ),
            keyboard
        )

        return

    # ========================================================
    # CONFIRM GLOBAL LOCK
    # ========================================================

    if data == "confirm_lockall":

        answer_callback(
            callback_id,
            "All clients locked."
        )

        if not is_owner(chat_id):
            return

        set_global_client_lock(
            True
        )

        show_owner_panel(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # GLOBAL UNLOCK
    # ========================================================

    if data == "unlockall":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        set_global_client_lock(
            False
        )

        show_owner_panel(
            chat_id,
            message_id
        )

        return

    # ========================================================
    # STOP ALL
    # ========================================================

    if data == "stopall":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, STOP ALL BOTS",
                        "callback_data":
                            "confirm_stopall"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data": "panel"
                    }
                ]
            ]
        }

        edit_message(
            chat_id,
            message_id,
            (
                "⚠️ <b>STOP ALL BOTS</b>\n\n"
                "Sabhi running bots stop honge.\n"
                "Auto-restart bhi disable hoga.\n\n"
                "Continue?"
            ),
            keyboard
        )

        return

    # ========================================================
    # CONFIRM STOP ALL
    # ========================================================

    if data == "confirm_stopall":

        answer_callback(
            callback_id,
            "Stopping all bots..."
        )

        if not is_owner(chat_id):
            return

        count = stop_all_bots()

        edit_message(
            chat_id,
            message_id,
            (
                "🛑 <b>STOP ALL COMPLETE</b>\n\n"
                f"Processed bots: <b>{count}</b>\n\n"
                "All bots are stopped."
            ),
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Owner Panel",
                            "callback_data": "panel"
                        }
                    ]
                ]
            }
        )

        return

    # ========================================================
    # ACCESS
    # ========================================================

    if data == "access":

        answer_callback(
            callback_id
        )

        if is_owner(chat_id):
            show_access(
                chat_id,
                message_id
            )

        return

    # ========================================================
    # ACCESS USER
    # ========================================================

    if data.startswith("accessuser:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            user_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        show_access_user(
            chat_id,
            user_id
        )

        return

    # ========================================================
    # GRANT MENU
    # ========================================================

    if data == "grantmenu":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        send_message(
            chat_id,
            (
                "➕ <b>GRANT HOSTING</b>\n\n"
                "User ka numeric Telegram Chat ID bhejo.\n\n"
                "Example:\n"
                "<code>123456789</code>"
            )
        )

        return

    # ========================================================
    # REVOKE MENU
    # ========================================================

    if data == "revokemenu":

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        users = get_access_users()

        rows = []

        for user in users:

            if not user["enabled"]:
                continue

            rows.append([
                {
                    "text":
                        f"🚫 {display_username(user)[:30]}",
                    "callback_data":
                        f"revoke:{user['chat_id']}"
                }
            ])

        rows.append([
            {
                "text": "⬅️ Owner Panel",
                "callback_data": "panel"
            }
        ])

        send_message(
            chat_id,
            "🚫 <b>SELECT USER TO REVOKE</b>",
            {
                "inline_keyboard": rows
            }
        )

        return

    # ========================================================
    # GRANT DIRECT
    # ========================================================

    if data.startswith("grant:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            user_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        client = get_client(
            user_id
        )

        if client:

            grant_hosting_access_by_id(
                user_id,
                client["username"] or "",
                client["first_name"] or "",
                client["last_name"] or ""
            )

        else:

            grant_hosting_access_by_id(
                user_id
            )

        send_message(
            chat_id,
            (
                "✅ Hosting access granted.\n"
                f"Chat ID: <code>{user_id}</code>"
            )
        )

        try:
            send_message(
                user_id,
                (
                    f"🎉 <b>{APP_NAME}</b>\n\n"
                    "✅ Owner ne aapko hosting access "
                    "grant kar diya hai.\n\n"
                    "Use /panel."
                )
            )
        except Exception:
            pass

        return

    # ========================================================
    # REVOKE
    # ========================================================

    if data.startswith("revoke:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            user_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        result = revoke_hosting_access(
            user_id
        )

        send_message(
            chat_id,
            (
                "🚫 <b>ACCESS REVOKED</b>\n\n"
                f"Chat ID: <code>{user_id}</code>"
            )
            if result
            else "❌ User access not found."
        )

        try:
            send_message(
                user_id,
                (
                    "🚫 <b>Hosting Access Revoked</b>\n\n"
                    "Owner ne aapka hosting access "
                    "revoke kar diya hai."
                )
            )
        except Exception:
            pass

        return

    # ========================================================
    # ENABLE CLIENT
    # ========================================================

    if data.startswith("enable:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        set_client_enabled(
            client_id,
            True
        )

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # DISABLE CLIENT
    # ========================================================

    if data.startswith("disable:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        set_client_enabled(
            client_id,
            False
        )

        show_owner_client(
            chat_id,
            client_id,
            message_id
        )

        return

    # ========================================================
    # STOP CLIENT
    # ========================================================

    if data.startswith("stopclient:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        count = stop_all_bots_of_client(
            client_id
        )

        send_message(
            chat_id,
            (
                "🛑 <b>CLIENT BOTS STOPPED</b>\n\n"
                f"Client ID: <code>{client_id}</code>\n"
                f"Processed: <b>{count}</b>"
            )
        )

        show_owner_client(
            chat_id,
            client_id
        )

        return

    # ========================================================
    # DELETE CLIENT REQUEST
    # ========================================================

    if data.startswith("dclient:"):

        answer_callback(
            callback_id
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        client = get_client(
            client_id
        )

        if not client:
            send_message(
                chat_id,
                "❌ Client not found."
            )
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text":
                            "⚠️ YES, DELETE CLIENT",
                        "callback_data":
                            f"confirmclient:{client_id}"
                    }
                ],
                [
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            f"client:{client_id}"
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            (
                "⚠️ <b>PERMANENT CLIENT DELETE</b>\n\n"
                f"Client: "
                f"<b>{escape_html(display_username(client))}</b>\n"
                f"ID: <code>{client_id}</code>\n\n"
                "This will permanently delete:\n"
                "• All client bots\n"
                "• Python files\n"
                "• .venv folders\n"
                "• Logs\n"
                "• Database records\n"
                "• Hosting access\n\n"
                "❗ <b>THIS CANNOT BE UNDONE.</b>"
            ),
            keyboard
        )

        return

    # ========================================================
    # CONFIRM DELETE CLIENT
    # ========================================================

    if data.startswith("confirmclient:"):

        answer_callback(
            callback_id,
            "Deleting client..."
        )

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        success, result = (
            delete_client_permanently(
                client_id
            )
        )

        send_message(
            chat_id,
            (
                "✅ <b>CLIENT DELETED</b>\n\n"
                "All client data was permanently removed."
                if success
                else
                f"❌ {escape_html(result)}"
            )
        )

        show_clients(
            chat_id
        )

        return

    # ========================================================
    # CONFIRM OWNER BOT DELETE
    # ========================================================

    if data.startswith("confirmob:"):

        answer_callback(
            callback_id,
            "Deleting bot..."
        )

        if not is_owner(chat_id):
            return

        try:
            bot_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        row = get_bot(
            bot_id
        )

        owner_id = (
            int(row["owner_chat_id"])
            if row
            else 0
        )

        success, result = (
            delete_bot_permanently(
                bot_id
            )
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT DELETED PERMANENTLY</b>"
                if success
                else
                f"❌ {escape_html(result)}"
            )
        )

        if owner_id:
            show_owner_client(
                chat_id,
                owner_id
            )

        return

    # ========================================================
    # CONFIRM CLIENT BOT DELETE
    # ========================================================

    if data.startswith("confirmcb:"):

        answer_callback(
            callback_id,
            "Deleting bot..."
        )

        if is_global_client_lock():
            send_message(
                chat_id,
                "🔒 Hosting is locked."
            )
            return

        try:
            bot_id = int(
                data.split(":", 1)[1]
            )
        except Exception:
            return

        row = get_bot(
            bot_id
        )

        if not row:
            send_message(
                chat_id,
                "❌ Bot not found."
            )
            return

        if int(
            row["owner_chat_id"]
        ) != int(chat_id):

            send_message(
                chat_id,
                "❌ Permission denied."
            )
            return

        success, result = (
            delete_bot_permanently(
                bot_id
            )
        )

        send_message(
            chat_id,
            (
                "✅ <b>BOT DELETED PERMANENTLY</b>"
                if success
                else
                f"❌ {escape_html(result)}"
            )
        )

        show_my_bots(
            chat_id
        )

        return


# ============================================================
# TELEGRAM POLLING
# ============================================================

def poll_updates():
    global telegram_offset

    while True:

        try:

            result = telegram(
                "getUpdates",
                {
                    "offset":
                        telegram_offset,
                    "timeout": 50,
                    "allowed_updates":
                        json.dumps([
                            "message",
                            "callback_query"
                        ])
                },
                timeout=60
            )

            if not result:
                time.sleep(2)
                continue

            if not result.get("ok"):
                print(
                    "[POLL] Telegram returned error."
                )
                time.sleep(3)
                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                telegram_offset = (
                    int(update["update_id"]) + 1
                )

                try:

                    if "message" in update:
                        handle_message(
                            update["message"]
                        )

                    elif "callback_query" in update:
                        handle_callback(
                            update["callback_query"]
                        )

                except Exception as e:

                    print(
                        "[UPDATE ERROR]",
                        repr(e)
                    )

                    try:
                        callback = update.get(
                            "callback_query"
                        )

                        if callback:
                            answer_callback(
                                callback.get("id"),
                                "An error occurred."
                            )

                    except Exception:
                        pass

        except Exception as e:

            print(
                "[POLL ERROR]",
                repr(e)
            )

            time.sleep(3)


# ============================================================
# RECOVER AUTO-RESTART BOTS
# ============================================================

def recover_bots():
    print(
        "[RECOVERY] Checking bots..."
    )

    bots = get_bots()

    for row in bots:

        if not bool(
            row["auto_restart"]
        ):
            continue

        client = get_client(
            int(row["owner_chat_id"])
        )

        if client and not bool(
            client["enabled"]
        ):
            continue

        # Reset stale runtime status.
        update_bot(
            int(row["id"]),
            status="stopped",
            pid=0
        )

        time.sleep(0.5)

        success, message = start_bot(
            int(row["id"])
        )

        print(
            f"[RECOVERY] "
            f"Bot #{row['id']}: "
            f"{success} {message}"
        )


# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        body = (
            f"{APP_NAME} "
            f"v{VERSION} - OK"
        ).encode()

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def log_message(
        self,
        format,
        *args
    ):
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
        f"[HEALTH] Listening on port {port}"
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(
        f"{APP_NAME} v{VERSION}"
    )
    print("=" * 60)

    if not BOT_TOKEN:

        print(
            "[FATAL] BOT_TOKEN is missing."
        )

        sys.exit(1)

    if not OWNER_CHAT_ID:

        print(
            "[FATAL] OWNER_CHAT_ID is missing."
        )

        sys.exit(1)

    init_db()

    print(
        f"[OWNER] {OWNER_CHAT_ID}"
    )

    print(
        f"[DATA] {DATA_DIR}"
    )

    print(
        f"[GLOBAL LOCK] "
        f"{is_global_client_lock()}"
    )

    # Render health server
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Recover bots after host restart.
    threading.Thread(
        target=recover_bots,
        daemon=True
    ).start()

    print(
        "[HOST] Telegram polling started."
    )

    poll_updates()


if __name__ == "__main__":
    main()
