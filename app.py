# -*- coding: utf-8 -*-
"""
Gojgui Pro - لوحة تحكم استضافة احترافية (Python + Node.js)
- إنشاء حسابات برامات ومساحة تخزين مخصصة
- مراقبة وإلزام حدود الرام لكل عملية
- إدارة ملفات شاملة (مجلدات، نسخ، نقل، ضغط، فك ضغط)
- استضافة Python و Node.js
"""
import os
import json
import re
import subprocess
import psutil
import socket
import sys
import signal
import hashlib
import secrets
import time
import shutil
import zipfile
import tarfile
import threading
from datetime import datetime, timedelta
from flask import Flask, send_from_directory, request, jsonify, session, redirect, url_for, make_response, send_file

try:
    import py7zr  # اختياري: دعم 7z
except ImportError:
    py7zr = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# دليل البيانات: على Railway صل ب Volume وحدد DATA_DIR=/data حتى لا تُفقد الحسابات عند إعادة النشر
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or BASE_DIR)
USERS_DIR = os.path.join(DATA_DIR, "USERS")
os.makedirs(USERS_DIR, exist_ok=True)

app = Flask(__name__, static_folder=BASE_DIR)
# SECRET_KEY ثابت من البيئة = بقاء الجلسات بعد إعادة التشغيل (مهم على Railway)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024  # حد الرفع 512MB

running_procs = {}          # proc_key -> Popen
ram_monitors = {}           # proc_key -> Thread
USERS_FILE = os.path.join(DATA_DIR, "users.json")
REMEMBER_TOKENS_FILE = os.path.join(DATA_DIR, "remember_tokens.json")

# الحساب الرئيسي (المسؤول) — يمكن تغييره من متغيرات البيئة على Railway
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Ziad555")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Ziad555")

# الحدود الافتراضية للمستخدمين الجدد
DEFAULT_RAM_LIMIT_MB = 512        # رام
DEFAULT_DISK_LIMIT_MB = 1024      # مساحة التخزين (MB)
UNLIMITED = -1

ARCHIVE_EXTS = {
    ".zip": "zip", ".tar": "tar", ".gz": "tar", ".tgz": "tar",
    ".bz2": "tar", ".tbz2": "tar", ".xz": "tar",
    ".7z": "7z", ".rar": "rar"
}

# ============== Helper Functions ==============

def init_users_db():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            admin_data = {
                ADMIN_USERNAME: {
                    "password": hash_password(ADMIN_PASSWORD),
                    "created_at": datetime.now().isoformat(),
                    "last_login": None,
                    "theme": "premium",
                    "is_admin": True,
                    "can_create_users": True,
                    "ram_limit_mb": UNLIMITED,
                    "disk_limit_mb": UNLIMITED
                }
            }
            json.dump(admin_data, f, indent=2)

def init_tokens_db():
    if not os.path.exists(REMEMBER_TOKENS_FILE):
        with open(REMEMBER_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)

def load_users():
    init_users_db()
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def create_remember_token(username):
    """إنشاء رمز تذكر جديد للمستخدم"""
    init_tokens_db()
    with open(REMEMBER_TOKENS_FILE, "r", encoding="utf-8") as f:
        tokens = json.load(f)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=30)).isoformat()
    tokens[token] = {
        "username": username,
        "created_at": datetime.now().isoformat(),
        "expires_at": expires,
        "last_used": datetime.now().isoformat()
    }
    with open(REMEMBER_TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    return token

def validate_remember_token(token):
    """التحقق من رمز التذكر"""
    if not os.path.exists(REMEMBER_TOKENS_FILE):
        return None
    with open(REMEMBER_TOKENS_FILE, "r", encoding="utf-8") as f:
        tokens = json.load(f)
    if token not in tokens:
        return None
    token_data = tokens[token]
    expires_at = datetime.fromisoformat(token_data["expires_at"])
    if datetime.now() > expires_at:
        del tokens[token]
        with open(REMEMBER_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)
        return None
    token_data["last_used"] = datetime.now().isoformat()
    tokens[token] = token_data
    with open(REMEMBER_TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    return token_data["username"]

def delete_all_user_tokens(username):
    """حذف جميع رموز التذكر للمستخدم"""
    if not os.path.exists(REMEMBER_TOKENS_FILE):
        return
    with open(REMEMBER_TOKENS_FILE, "r", encoding="utf-8") as f:
        tokens = json.load(f)
    tokens_to_delete = [t for t, d in tokens.items() if d["username"] == username]
    for token in tokens_to_delete:
        del tokens[token]
    if tokens_to_delete:
        with open(REMEMBER_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)

def register_user(username, password, ram_limit_mb=DEFAULT_RAM_LIMIT_MB,
                  disk_limit_mb=DEFAULT_DISK_LIMIT_MB, created_by_admin=False):
    """إنشاء مستخدم جديد مع حدود رام ومساحة مخصصة"""
    init_users_db()
    users = load_users()

    if not re.match(r'^[A-Za-z0-9_\-\.]{3,32}$', username):
        return False, "اسم المستخدم يجب أن يكون 3-32 حرفاً إنجليزياً أو أرقاماً فقط"

    if username in users:
        return False, "المستخدم موجود بالفعل"

    if len(password) < 6:
        return False, "كلمة المرور يجب أن تكون 6 أحرف على الأقل"

    try:
        ram_limit_mb = int(ram_limit_mb)
        disk_limit_mb = int(disk_limit_mb)
    except (TypeError, ValueError):
        return False, "قيم الرام والمساحة يجب أن تكون أرقاماً"

    if ram_limit_mb != UNLIMITED and (ram_limit_mb < 32 or ram_limit_mb > 1024 * 64):
        return False, "حد الرام يجب أن يكون بين 32 و 65536 ميجابايت (أو -1 للمسؤول بلا حدود)"

    if disk_limit_mb != UNLIMITED and (disk_limit_mb < 16 or disk_limit_mb > 1024 * 1024):
        return False, "حد المساحة يجب أن يكون بين 16 و 1048576 ميجابايت (أو -1 للمسؤول بلا حدود)"

    users[username] = {
        "password": hash_password(password),
        "created_at": datetime.now().isoformat(),
        "last_login": None,
        "theme": "blue",
        "is_admin": False,
        "created_by_admin": created_by_admin,
        "created_by": session.get('username') if 'username' in session else None,
        "ram_limit_mb": ram_limit_mb,
        "disk_limit_mb": disk_limit_mb
    }
    save_users(users)

    user_dir = os.path.join(USERS_DIR, username)
    os.makedirs(os.path.join(user_dir, "SERVERS"), exist_ok=True)
    return True, "تم إنشاء الحساب بنجاح"

def authenticate_user(username, password):
    init_users_db()
    users = load_users()
    if username not in users:
        return False, "المستخدم غير موجود"
    if users[username]["password"] != hash_password(password):
        return False, "كلمة المرور غير صحيحة"
    users[username]["last_login"] = datetime.now().isoformat()
    save_users(users)
    return True, "تم تسجيل الدخول بنجاح"

def is_admin(username):
    users = load_users()
    if username in users:
        return users[username].get("is_admin", False)
    return False

def get_user_quotas(username):
    users = load_users()
    u = users.get(username, {})
    return {
        "ram_limit_mb": u.get("ram_limit_mb", DEFAULT_RAM_LIMIT_MB),
        "disk_limit_mb": u.get("disk_limit_mb", DEFAULT_DISK_LIMIT_MB)
    }

def get_dir_size(path):
    """حساب حجم مجلد بالبايت"""
    total = 0
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for dirpath, dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total

def get_user_disk_usage(username):
    """حجم مساحة المستخدم المستخدمة بالبايت"""
    return get_dir_size(os.path.join(USERS_DIR, username))

def fmt_mb(bytes_val):
    return round(bytes_val / (1024 * 1024), 2)

def check_disk_quota(username, additional_bytes=0):
    """التحقق من مساحة المستخدم قبل أي عملية كتابة. يرجع (ok, message)"""
    quotas = get_user_quotas(username)
    limit = quotas["disk_limit_mb"]
    if limit == UNLIMITED:
        return True, ""
    used = get_user_disk_usage(username) + additional_bytes
    if used > limit * 1024 * 1024:
        over = fmt_mb(used - limit * 1024 * 1024)
        return False, f"تم تجاوز حد المساحة المسموح ({limit} MB) بـ {over} MB. تواصل مع المسؤول لزيادة المساحة"
    return True, ""

def get_user_servers_dir(username):
    return os.path.join(USERS_DIR, username, "SERVERS")

def ensure_user_servers_dir():
    if 'username' not in session:
        return None
    user_servers_dir = get_user_servers_dir(session['username'])
    os.makedirs(user_servers_dir, exist_ok=True)
    return user_servers_dir

def safe_join(base, *parts):
    """حماية من Path Traversal - يرجع المسار الآمن أو None"""
    target = os.path.abspath(os.path.join(base, *([p for p in parts if p])))
    base_abs = os.path.abspath(base)
    if target == base_abs or target.startswith(base_abs + os.sep):
        return target
    return None

def sanitize_folder_name(name):
    if not name: return ""
    name = name.strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^A-Za-z0-9\-\_\.]", "", name)
    return name[:200]

def sanitize_filename(name):
    if not name: return ""
    name = name.strip()
    # نسمح بمسار نسبي داخلي للمجلدات
    name = re.sub(r"[\\]", "/", name)
    parts = [re.sub(r"[^A-Za-z0-9\-\_\.]", "", p) for p in name.split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    return "/".join(parts)[:400]

def sanitize_rel_path(path):
    """تنظيف مسار نسبي آمن (يسمح بالمجلدات الفرعية)"""
    if not path:
        return ""
    path = str(path).replace("\\", "/")
    parts = [p.strip() for p in path.split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    return "/".join(parts)[:400]

def ensure_meta(folder):
    user_servers_dir = ensure_user_servers_dir()
    if not user_servers_dir:
        return None
    meta_path = os.path.join(user_servers_dir, folder, "meta.json")
    if not os.path.exists(meta_path):
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"display_name": folder, "startup_file": "", "language": "python"}, f)
    return meta_path

def load_meta(folder):
    meta_path = ensure_meta(folder)
    if not meta_path:
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_meta(folder, meta):
    meta_path = ensure_meta(folder)
    if not meta_path:
        return False
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    return True

def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

def get_runtimes():
    """فحص بيئات التشغيل المتاحة"""
    return {
        "python": {"available": True, "version": sys.version.split()[0]},
        "node": {"available": shutil.which("node") is not None,
                 "version": get_cmd_version("node")},
        "npm": {"available": shutil.which("npm") is not None,
                "version": get_cmd_version("npm")}
    }

def get_cmd_version(cmd):
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or ""
    except Exception:
        return ""

def proc_key_of(username, folder):
    return f"{username}_{folder}"

def kill_proc_tree(pid):
    try:
        p = psutil.Process(pid)
        for child in p.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        p.kill()
    except psutil.NoSuchProcess:
        pass
    except Exception:
        pass

def stop_ram_monitor(proc_key):
    mon = ram_monitors.pop(proc_key, None)
    if mon and hasattr(mon, "stop_event"):
        mon.stop_event.set()

def append_log(server_dir, text):
    try:
        with open(os.path.join(server_dir, "server.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

def ram_monitor_loop(proc_key, pid, ram_limit_mb, server_dir):
    """مراقبة استهلاك الرام للعملية وإيقافها عند التجاوز"""
    while True:
        time.sleep(5)
        mon = ram_monitors.get(proc_key)
        if mon and mon.stop_event.is_set():
            return
        proc = running_procs.get(proc_key)
        if proc is None or proc.pid != pid:
            return
        if ram_limit_mb == UNLIMITED:
            continue
        try:
            p = psutil.Process(pid)
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return
            rss = p.memory_info().rss
            for c in p.children(recursive=True):
                try:
                    rss += c.memory_info().rss
                except Exception:
                    pass
            if rss > ram_limit_mb * 1024 * 1024:
                append_log(server_dir, "=" * 50)
                append_log(server_dir, f"[SYSTEM] RAM LIMIT EXCEEDED: {fmt_mb(rss)} MB / {ram_limit_mb} MB limit")
                append_log(server_dir, "[SYSTEM] Process terminated by resource monitor.")
                kill_proc_tree(pid)
                running_procs.pop(proc_key, None)
                return
        except psutil.NoSuchProcess:
            running_procs.pop(proc_key, None)
            return
        except Exception:
            pass

def start_ram_monitor(proc_key, pid, ram_limit_mb, server_dir):
    mon = threading.Thread(target=ram_monitor_loop,
                           args=(proc_key, pid, ram_limit_mb, server_dir), daemon=True)
    mon.stop_event = threading.Event()
    ram_monitors[proc_key] = mon
    mon.start()

def load_servers_list(username=None):
    username = username or session.get('username')
    if not username:
        return []
    user_servers_dir = get_user_servers_dir(username)
    if not os.path.exists(user_servers_dir):
        return []
    try:
        entries = [d for d in os.listdir(user_servers_dir)
                   if os.path.isdir(os.path.join(user_servers_dir, d))]
    except Exception:
        entries = []

    servers = []
    for i, folder in enumerate(entries, start=1):
        meta = load_meta_for(username, folder)
        proc_key = proc_key_of(username, folder)
        proc = running_procs.get(proc_key)
        running = bool(proc and psutil.pid_exists(proc.pid))
        servers.append({
            "id": i,
            "title": meta.get("display_name", folder),
            "folder": folder,
            "subtitle": f"Node-{i} · {meta.get('language', 'python').upper()}",
            "startup_file": meta.get("startup_file", ""),
            "language": meta.get("language", "python"),
            "running": running
        })
    return servers

def load_meta_for(username, folder):
    meta_path = os.path.join(get_user_servers_dir(username), folder, "meta.json")
    if not os.path.exists(meta_path):
        return {"display_name": folder, "startup_file": "", "language": "python"}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"display_name": folder, "startup_file": "", "language": "python"}

# ============== Archive Helpers ==============

def get_archive_type(filename):
    """تحديد نوع الأرشيف من الامتداد"""
    lower = filename.lower()
    for ext, kind in ARCHIVE_EXTS.items():
        if lower.endswith(ext):
            return kind
    return None

def extract_zip(zip_path, dest):
    with zipfile.ZipFile(zip_path) as zf:
        dest_abs = os.path.abspath(dest)
        for member in zf.infolist():
            member_path = os.path.abspath(os.path.join(dest, member.filename))
            if not (member_path == dest_abs or member_path.startswith(dest_abs + os.sep)):
                raise ValueError("ملف ضار داخل الأرشيف (Zip Slip)")
        zf.extractall(dest)

def extract_tar(tar_path, dest):
    with tarfile.open(tar_path) as tf:
        try:
            tf.extractall(dest, filter='data')
        except TypeError:
            dest_abs = os.path.abspath(dest)
            for member in tf.getmembers():
                member_path = os.path.abspath(os.path.join(dest, member.name))
                if not member_path.startswith(dest_abs + os.sep):
                    raise ValueError("ملف ضار داخل الأرشيف")
                if member.issym() or member.islnk():
                    raise ValueError("الروابط الرمزية غير مسموحة")
            tf.extractall(dest)

def extract_7z(path, dest):
    if py7zr is None:
        raise ValueError("دعم 7z غير مثبت على الخادم (pip install py7zr)")
    with py7zr.SevenZipFile(path) as z:
        z.extractall(dest)

def extract_rar(path, dest):
    try:
        import rarfile
    except ImportError:
        raise ValueError("دعم RAR غير مثبت على الخادم (pip install rarfile)")
    rf = rarfile.RarFile(path)
    dest_abs = os.path.abspath(dest)
    for info in rf.infolist():
        member_path = os.path.abspath(os.path.join(dest, info.filename))
        if not member_path.startswith(dest_abs + os.sep):
            raise ValueError("ملف ضار داخل الأرشيف")
    rf.extractall(dest)

def extract_archive(archive_path, dest):
    """فك ضغط أي نوع مدعوم في المجلد المحدد"""
    kind = get_archive_type(os.path.basename(archive_path))
    if kind == "zip":
        extract_zip(archive_path, dest)
    elif kind == "tar":
        extract_tar(archive_path, dest)
    elif kind == "7z":
        extract_7z(archive_path, dest)
    elif kind == "rar":
        extract_rar(archive_path, dest)
    else:
        raise ValueError("نوع الأرشيف غير مدعوم")
    return kind

def create_zip(source_items, zip_path):
    """ضغط ملفات/مجلدات في ملف zip"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in source_items:
            if os.path.isdir(item):
                base = os.path.dirname(item)
                for dirpath, dirnames, filenames in os.walk(item):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        arcname = os.path.relpath(fp, base)
                        zf.write(fp, arcname)
            elif os.path.isfile(item):
                zf.write(item, os.path.basename(item))

# ============== Routes ==============

@app.before_request
def check_remember_token():
    """فحص رمز التذكر قبل كل طلب"""
    if 'username' in session:
        return
    remember_token = request.cookies.get('remember_token')
    if remember_token:
        username = validate_remember_token(remember_token)
        if username:
            session['username'] = username
            session.permanent = True

@app.route("/")
def home():
    if 'username' not in session:
        return redirect(url_for('login_page'))
    if is_admin(session['username']):
        return send_from_directory(BASE_DIR, "admin_panel.html")
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/index.html")
def serve_index():
    if 'username' not in session:
        return redirect(url_for('login_page'))
    # المسؤول يمكنه أيضاً استخدام لوحة الاستضافة مباشرة
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/login")
def login_page():
    if 'username' in session:
        return redirect(url_for('home'))
    return send_from_directory(BASE_DIR, "login.html")

@app.route("/admin")
def admin_panel():
    if 'username' not in session or not is_admin(session['username']):
        return redirect(url_for('login_page'))
    return send_from_directory(BASE_DIR, "admin_panel.html")

@app.route("/api/register", methods=["POST"])
def api_register():
    # فقط المسؤول يمكنه إنشاء حسابات
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    ram_limit_mb = data.get("ram_limit_mb", DEFAULT_RAM_LIMIT_MB)
    disk_limit_mb = data.get("disk_limit_mb", DEFAULT_DISK_LIMIT_MB)

    if not username or not password:
        return jsonify({"success": False, "message": "اسم المستخدم وكلمة المرور مطلوبان"})

    success, message = register_user(username, password,
                                     ram_limit_mb=ram_limit_mb,
                                     disk_limit_mb=disk_limit_mb,
                                     created_by_admin=True)
    return jsonify({"success": success, "message": message})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    remember_me = data.get("remember_me", False)

    if not username or not password:
        return jsonify({"success": False, "message": "اسم المستخدم وكلمة المرور مطلوبان"})

    success, message = authenticate_user(username, password)
    if success:
        session['username'] = username
        response_data = {
            "success": True,
            "message": message,
            "username": username,
            "is_admin": is_admin(username)
        }
        if remember_me:
            token = create_remember_token(username)
            response = make_response(jsonify(response_data))
            # secure ديناميكي: خلف Railway (HTTPS) يجب أن تكون الكوكي آمنة
            is_https = request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"
            response.set_cookie('remember_token', token,
                                max_age=30 * 24 * 60 * 60, httponly=True,
                                secure=is_https, samesite='Strict')
            return response
        return jsonify(response_data)

    return jsonify({"success": False, "message": message})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    username = session.get('username')
    if username:
        delete_all_user_tokens(username)
    session.pop('username', None)
    response = make_response(jsonify({"success": True, "message": "تم تسجيل الخروج"}))
    response.set_cookie('remember_token', '', expires=0)
    return response

@app.route("/api/current_user")
def api_current_user():
    if 'username' in session:
        admin = is_admin(session['username'])
        quotas = get_user_quotas(session['username'])
        return jsonify({
            "success": True,
            "username": session['username'],
            "is_admin": admin,
            "quotas": quotas,
            "has_remember_token": bool(request.cookies.get('remember_token'))
        })
    return jsonify({"success": False})

@app.route("/api/user/usage")
def api_user_usage():
    """استهلاك المستخدم الحالي من الرام والمساحة"""
    if 'username' not in session:
        return jsonify({"success": False}), 401
    username = session['username']
    quotas = get_user_quotas(username)
    used_bytes = get_user_disk_usage(username)
    disk_limit = quotas["disk_limit_mb"]
    return jsonify({
        "success": True,
        "disk_used_mb": fmt_mb(used_bytes),
        "disk_used_bytes": used_bytes,
        "disk_limit_mb": disk_limit,
        "disk_percent": min(100, round(used_bytes / (disk_limit * 1024 * 1024) * 100, 1)) if disk_limit != UNLIMITED else 0,
        "ram_limit_mb": quotas["ram_limit_mb"],
        "unlimited": disk_limit == UNLIMITED
    })

@app.route("/api/user/settings", methods=["GET", "POST"])
def user_settings():
    if 'username' not in session:
        return jsonify({"success": False}), 401

    if request.method == "GET":
        users = load_users()
        user_data = users.get(session['username'], {})
        quotas = get_user_quotas(session['username'])
        return jsonify({
            "success": True,
            "username": session['username'],
            "created_at": user_data.get("created_at"),
            "last_login": user_data.get("last_login"),
            "theme": user_data.get("theme", "blue"),
            "is_admin": user_data.get("is_admin", False),
            "quotas": quotas
        })

    data = request.get_json()
    theme = data.get("theme", "blue")
    users = load_users()
    if session['username'] in users:
        users[session['username']]["theme"] = theme
        save_users(users)
        return jsonify({"success": True, "message": "تم تحديث الإعدادات"})
    return jsonify({"success": False, "message": "المستخدم غير موجود"})

@app.route("/api/system/runtimes")
def api_runtimes():
    return jsonify({"success": True, "runtimes": get_runtimes()})

@app.route("/manifest.json")
def pwa_manifest():
    """بيانات تطبيق PWA (تثبيت اللوحة على الهاتف)"""
    return send_from_directory(BASE_DIR, "manifest.json", mimetype="application/manifest+json")

@app.route("/icons/<path:icon_name>")
def pwa_icons(icon_name):
    """أيقونات PWA"""
    return send_from_directory(os.path.join(BASE_DIR, "icons"), icon_name)

# ============== Protected Routes (Servers) ==============

@app.route("/servers")
def get_servers():
    if 'username' not in session:
        return jsonify({"success": False, "message": "غير مصرح"}), 401
    return jsonify({"success": True, "servers": load_servers_list()})

@app.route("/add", methods=["POST"])
def add_server():
    if 'username' not in session:
        return jsonify({"success": False, "message": "غير مصرح"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    language = data.get("language", "python")
    if language not in ("python", "nodejs"):
        language = "python"

    if language == "nodejs" and shutil.which("node") is None:
        return jsonify({"success": False, "message": "Node.js غير مثبت على الخادم"})

    folder = sanitize_folder_name(name)
    if not folder:
        return jsonify({"success": False, "message": "اسم غير صالح"}), 400

    user_servers_dir = ensure_user_servers_dir()
    target = os.path.join(user_servers_dir, folder)

    if os.path.exists(target):
        return jsonify({"success": False, "message": "Exists"}), 409

    # التحقق من مساحة القرص قبل الإنشاء
    ok, msg = check_disk_quota(session['username'])
    if not ok:
        return jsonify({"success": False, "message": msg})

    os.makedirs(target)
    save_meta(folder, {"display_name": folder, "startup_file": "", "language": language})
    open(os.path.join(target, "server.log"), "w").close()

    # ملفات بداية جاهزة
    if language == "nodejs":
        with open(os.path.join(target, "index.js"), "w", encoding="utf-8") as f:
            f.write("// Gojgui Node.js starter\nconst http = require('http');\n\n"
                    "const server = http.createServer((req, res) => {\n"
                    "  res.writeHead(200, {'Content-Type': 'text/plain; charset=utf-8'});\n"
                    "  res.end('Hello from Gojgui Node.js Hosting!');\n});\n\n"
                    "const port = process.env.PORT || 3000;\n"
                    "server.listen(port, () => console.log('Server running on port ' + port));\n")
        with open(os.path.join(target, "package.json"), "w", encoding="utf-8") as f:
            f.write('{\n  "name": "' + folder.lower() + '",\n  "version": "1.0.0",\n'
                    '  "main": "index.js",\n  "scripts": {\n    "start": "node index.js"\n  }\n}\n')
    else:
        with open(os.path.join(target, "main.py"), "w", encoding="utf-8") as f:
            f.write("# Gojgui Python starter\nfrom flask import Flask\n\napp = Flask(__name__)\n\n"
                    "@app.route('/')\ndef home():\n    return 'Hello from Gojgui Python Hosting!'\n\n"
                    "if __name__ == '__main__':\n    app.run(host='0.0.0.0', port=3000)\n")

    return jsonify({"success": True, "servers": load_servers_list()})

@app.route("/server/stats/<folder>")
def get_stats(folder):
    if 'username' not in session:
        return jsonify({"success": False, "message": "غير مصرح"}), 401

    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    if not server_dir or not os.path.isdir(server_dir):
        return jsonify({"success": False, "message": "غير موجود"}), 404

    proc_key = proc_key_of(username, folder)
    proc = running_procs.get(proc_key)
    running = False
    cpu, mem = "0%", "0 MB"

    if proc and psutil.pid_exists(proc.pid):
        try:
            p = psutil.Process(proc.pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                running = True
                cpu = f"{p.cpu_percent(interval=None)}%"
                mem = f"{p.memory_info().rss / 1024 / 1024:.1f} MB"
        except Exception:
            pass

    meta = load_meta(folder)
    quotas = get_user_quotas(username)
    disk_used = fmt_mb(get_dir_size(server_dir))
    log_path = os.path.join(server_dir, "server.log")
    logs = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                logs = f.read()[-100000:]
        except Exception:
            pass

    # هل يستقبل الخادم مدخلات من الكونسول؟
    interactive = bool(proc and proc.stdin is not None and not proc.stdin.closed)

    return jsonify({
        "status": "Running" if running else "Offline",
        "cpu": cpu,
        "mem": mem,
        "logs": logs,
        "ip": get_ip(),
        "language": meta.get("language", "python"),
        "startup_file": meta.get("startup_file", ""),
        "ram_limit_mb": quotas["ram_limit_mb"],
        "disk_used_mb": disk_used,
        "interactive": interactive
    })

@app.route("/server/action/<folder>/<act>", methods=["POST"])
def server_action(folder, act):
    if 'username' not in session:
        return jsonify({"success": False, "message": "غير مصرح"}), 401

    username = session['username']
    proc_key = proc_key_of(username, folder)
    server_dir = safe_join(get_user_servers_dir(username), folder)
    if not server_dir or not os.path.isdir(server_dir):
        return jsonify({"success": False, "message": "مجلد غير موجود"}), 404

    if proc_key in running_procs:
        try:
            kill_proc_tree(running_procs[proc_key].pid)
        except Exception:
            pass
        stop_ram_monitor(proc_key)
        if act in ("stop", "restart", "kill"):
            running_procs.pop(proc_key, None)

    if act == "stop":
        append_log(server_dir, "[SYSTEM] Server stopped by user.")
        return jsonify({"success": True})

    meta = load_meta(folder)
    language = meta.get("language", "python")
    startup = meta.get("startup_file")

    if not startup:
        return jsonify({"success": False, "message": "No main file set."})

    startup = sanitize_rel_path(startup)
    startup_path = safe_join(server_dir, startup)
    if not startup_path or not os.path.exists(startup_path):
        return jsonify({"success": False, "message": "الملف غير موجود"})

    log_path = os.path.join(server_dir, "server.log")
    open(log_path, "w").close()

    if language == "nodejs":
        if shutil.which("node") is None:
            return jsonify({"success": False, "message": "Node.js غير مثبت على الخادم"})
        cmd = ["node", startup]
    else:
        cmd = [sys.executable, "-u", startup]

    quotas = get_user_quotas(username)
    append_log(server_dir, f"[SYSTEM] Starting {language} app: {startup} (RAM limit: {quotas['ram_limit_mb']} MB)")
    append_log(server_dir, "[SYSTEM] Console input enabled - you can send text/commands from the panel.")

    log_file = open(log_path, "a")
    # stdin=PIPE: يسمح بإرسال نص/أوامر للخادم من الكونسول
    # start_new_session: مجموعة عمليات مستقلة حتى لا تؤثر إشارة Ctrl+C على لوحة التحكم نفسها
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    child_env.setdefault("FORCE_COLOR", "0")
    try:
        proc = subprocess.Popen(cmd, cwd=server_dir, stdout=log_file, stderr=log_file,
                                stdin=subprocess.PIPE, universal_newlines=True,
                                start_new_session=True, env=child_env)
    except Exception as e:
        append_log(server_dir, f"[ERROR] Failed to start: {e}")
        return jsonify({"success": False, "message": f"فشل التشغيل: {e}"})

    running_procs[proc_key] = proc
    if quotas["ram_limit_mb"] != UNLIMITED:
        start_ram_monitor(proc_key, proc.pid, quotas["ram_limit_mb"], server_dir)
    return jsonify({"success": True})

@app.route("/server/input/<folder>", methods=["POST"])
def server_input(folder):
    """إرسال نص/أوامر إلى الخادم عبر stdin - للأدوات التي تطلب مدخلات"""
    if 'username' not in session:
        return jsonify({"success": False, "message": "غير مصرح"}), 401

    username = session['username']
    proc_key = proc_key_of(username, folder)
    server_dir = safe_join(get_user_servers_dir(username), folder)
    if not server_dir or not os.path.isdir(server_dir):
        return jsonify({"success": False, "message": "غير موجود"}), 404

    proc = running_procs.get(proc_key)
    if not proc or not psutil.pid_exists(proc.pid):
        return jsonify({"success": False, "message": "الخادم غير يعمل - شغّله أولاً"}), 400
    try:
        if psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE:
            return jsonify({"success": False, "message": "الخادم غير يعمل"}), 400
    except psutil.NoSuchProcess:
        return jsonify({"success": False, "message": "الخادم غير يعمل"}), 400

    data = request.get_json() or {}
    special = (data.get("special") or "").strip()
    text = data.get("text", "")

    # إرسال إشارة Ctrl+C (SIGINT) لمجموعة عمليات الخادم فقط
    if special == "ctrl_c":
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGINT)
            append_log(server_dir, "[INPUT] ^C")
            return jsonify({"success": True, "message": "تم إرسال إشارة Ctrl+C للخادم"})
        except (ProcessLookupError, PermissionError, OSError) as e:
            return jsonify({"success": False, "message": f"فشل إرسال الإشارة: {e}"})

    # إرسال نص عادي عبر stdin
    if not isinstance(text, str) or text == "":
        return jsonify({"success": False, "message": "النص مطلوب"}), 400

    try:
        if proc.stdin is None or proc.stdin.closed:
            return jsonify({"success": False, "message": "الخادم لا يستقبل مدخلات"}), 400
        proc.stdin.write(text + "\n")
        proc.stdin.flush()
        append_log(server_dir, f"[INPUT] {text}")
        return jsonify({"success": True})
    except (BrokenPipeError, ValueError, OSError) as e:
        append_log(server_dir, f"[SYSTEM] Failed to deliver input: {e}")
        return jsonify({"success": False, "message": "تعذّر تسليم المدخل (الخادم ربما أغلق المدخلات)"}), 400

@app.route("/server/set-startup/<folder>", methods=["POST"])
def set_startup(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401
    data = request.get_json() or {}
    meta = load_meta(folder)
    meta["startup_file"] = sanitize_rel_path(data.get('file', ''))
    save_meta(folder, meta)
    return jsonify({"success": True})

@app.route("/server/rename/<folder>", methods=["POST"])
def rename_server(folder):
    """إعادة تسمية الخادم (الاسم المعروض فقط)"""
    if 'username' not in session:
        return jsonify({"success": False}), 401
    data = request.get_json() or {}
    new_name = (data.get("name") or "").strip()[:100]
    if not new_name:
        return jsonify({"success": False, "message": "الاسم مطلوب"})
    meta = load_meta(folder)
    meta["display_name"] = new_name
    save_meta(folder, meta)
    return jsonify({"success": True, "servers": load_servers_list()})

@app.route("/server/delete/<folder>", methods=["POST"])
def delete_server(folder):
    """حذف خادم بالكامل"""
    if 'username' not in session:
        return jsonify({"success": False}), 401
    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    if not server_dir or not os.path.isdir(server_dir):
        return jsonify({"success": False, "message": "غير موجود"}), 404

    proc_key = proc_key_of(username, folder)
    if proc_key in running_procs:
        try:
            kill_proc_tree(running_procs[proc_key].pid)
        except Exception:
            pass
        stop_ram_monitor(proc_key)
        running_procs.pop(proc_key, None)

    shutil.rmtree(server_dir, ignore_errors=True)
    return jsonify({"success": True, "servers": load_servers_list()})

@app.route("/files/install/<folder>", methods=["POST"])
def install_req(folder):
    """تثبيت المكاتب تلقائياً: pip للـ Python و npm للـ Node.js"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    if not server_dir or not os.path.isdir(server_dir):
        return jsonify({"success": False, "message": "غير موجود"}), 404

    meta = load_meta(folder)
    language = meta.get("language", "python")

    if language == "nodejs":
        pkg_path = os.path.join(server_dir, "package.json")
        if not os.path.exists(pkg_path):
            return jsonify({"success": False, "message": "ملف package.json غير موجود"})
        if shutil.which("npm") is None:
            return jsonify({"success": False, "message": "npm غير متوفر على الخادم"})
        cmd = ["npm", "install", "--production", "--no-audit", "--no-fund"]
        log_header = "[SYSTEM] Installing npm packages from package.json..."
    else:
        req_path = os.path.join(server_dir, "requirements.txt")
        if not os.path.exists(req_path):
            return jsonify({"success": False, "message": "ملف requirements.txt غير موجود"})
        cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
        log_header = "[SYSTEM] Installing pip packages from requirements.txt..."

    log_path = os.path.join(server_dir, "server.log")
    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write("[SYSTEM] Starting Installation...\n")
        log_file.write(log_header + "\n")
        log_file.write(f"[SYSTEM] Working directory: {server_dir}\n")
        log_file.write("=" * 50 + "\n")

    try:
        proc = subprocess.Popen(cmd, cwd=server_dir, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()
        proc.wait()
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write("\n" + "=" * 50 + "\n")
            if proc.returncode == 0:
                log_file.write("[SYSTEM] Installation completed successfully!\n")
            else:
                log_file.write(f"[SYSTEM] Installation failed with exit code: {proc.returncode}\n")
        return jsonify({"success": True, "message": "تم تنفيذ التثبيت، راقب سجل الكونسول"})
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n[ERROR] Failed to start installation: {str(e)}\n")
        return jsonify({"success": False, "message": f"فشل بدء التثبيت: {str(e)}"})

# ============== File Manager Routes ==============

@app.route("/files/list/<folder>")
def list_files(folder):
    """قائمة الملفات مع دعم المجلدات الفرعية"""
    if 'username' not in session:
        return jsonify([]), 401

    username = session['username']
    rel_path = sanitize_rel_path(request.args.get("path", ""))
    base_dir = safe_join(get_user_servers_dir(username), folder)
    if not base_dir or not os.path.isdir(base_dir):
        return jsonify({"success": False, "message": "غير موجود"}), 404

    target_dir = safe_join(base_dir, rel_path) if rel_path else base_dir
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"success": False, "message": "المجلد غير موجود"}), 404

    entries = []
    try:
        for name in sorted(os.listdir(target_dir)):
            if name in ["meta.json", "server.log", "node_modules"]:
                continue
            f_path = os.path.join(target_dir, name)
            is_dir = os.path.isdir(f_path)
            size_bytes = get_dir_size(f_path) if is_dir else os.path.getsize(f_path)
            ext = os.path.splitext(name)[1].lower()
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024 * 1024 else f"{size_bytes / 1024 / 1024:.2f} MB",
                "size_bytes": size_bytes,
                "modified": datetime.fromtimestamp(os.path.getmtime(f_path)).strftime("%Y-%m-%d %H:%M"),
                "is_archive": ext in ARCHIVE_EXTS,
                "archive_type": ARCHIVE_EXTS.get(ext),
                "ext": ext
            })
    except Exception:
        pass

    dirs_first = sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))
    return jsonify({"success": True, "files": dirs_first, "path": rel_path})

@app.route("/files/content/<folder>/<path:filename>")
def get_file_content(folder, filename):
    if 'username' not in session:
        return jsonify({"content": ""}), 401

    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    file_path = safe_join(server_dir, filename) if server_dir else None
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"success": False, "message": "الملف غير موجود"}), 404

    if os.path.getsize(file_path) > 5 * 1024 * 1024:
        return jsonify({"success": False, "message": "الملف كبير جداً للعرض (الحد 5MB)"}), 413

    try:
        with open(file_path, 'r', encoding='utf-8', errors="replace") as f:
            return jsonify({"success": True, "content": f.read()})
    except Exception:
        return jsonify({"success": False, "content": ""})

@app.route("/files/save/<folder>/<path:filename>", methods=["POST"])
def save_file_content(folder, filename):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    file_path = safe_join(server_dir, filename) if server_dir else None
    if not file_path:
        return jsonify({"success": False}), 403

    data = request.get_json() or {}
    content = data.get('content', '')

    ok, msg = check_disk_quota(username, len(content.encode('utf-8')))
    if not ok:
        return jsonify({"success": False, "message": msg})

    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({"success": True})

@app.route("/files/upload/<folder>", methods=["POST"])
def upload_file(folder):
    """رفع ملفات متعددة مع دعم المسارات الفرعية وفك الضغط التلقائي"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    rel_path = sanitize_rel_path(request.form.get("path", ""))
    target_dir = safe_join(server_dir, rel_path) if server_dir else None

    if not server_dir or not target_dir or not os.path.isdir(target_dir):
        return jsonify({"success": False, "message": "المجلد غير موجود"}), 404

    uploaded_files = request.files.getlist('files[]')
    auto_extract = request.form.get("auto_extract", "false") == "true"
    results, errors = [], []

    for f in uploaded_files:
        if not f or not f.filename:
            continue
        safe_name = sanitize_filename(os.path.basename(f.filename))
        if not safe_name:
            continue
        save_path = os.path.join(target_dir, safe_name)

        # التحقق من حصة المساحة قبل الحفظ
        f.stream.seek(0, 2)
        file_size = f.stream.tell()
        f.stream.seek(0)
        ok, msg = check_disk_quota(username, file_size)
        if not ok:
            errors.append(f"{safe_name}: {msg}")
            continue

        f.save(save_path)
        results.append({"name": safe_name, "size": f"{os.path.getsize(save_path) / 1024:.2f} KB"})

        # فك ضغط تلقائي بعد الرفع للأرشيفات
        if auto_extract and get_archive_type(safe_name):
            try:
                ok, msg = check_disk_quota(username, os.path.getsize(save_path) * 3)
                if not ok:
                    errors.append(f"{safe_name}: {msg}")
                    continue
                extract_dir = os.path.join(target_dir, os.path.splitext(safe_name)[0])
                os.makedirs(extract_dir, exist_ok=True)
                extract_archive(save_path, extract_dir)
                os.remove(save_path)
                results[-1]["extracted"] = True
            except Exception as e:
                errors.append(f"{safe_name}: فشل فك الضغط - {str(e)}")

    return jsonify({
        "success": len(results) > 0,
        "message": f"تم رفع {len(results)} ملف بنجاح" + (f" | أخطاء: {'; '.join(errors)}" if errors else ""),
        "uploaded_files": results,
        "errors": errors
    })

@app.route("/files/upload-single/<folder>", methods=["POST"])
def upload_single_file(folder):
    """رفع ملف واحد تلقائياً عند الاختيار"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    server_dir = safe_join(get_user_servers_dir(username), folder)
    rel_path = sanitize_rel_path(request.form.get("path", ""))
    target_dir = safe_join(server_dir, rel_path) if server_dir else None

    if not server_dir or not target_dir or not os.path.isdir(target_dir):
        return jsonify({"success": False, "message": "المجلد غير موجود"}), 404

    if 'file' not in request.files:
        return jsonify({"success": False, "message": "لم يتم اختيار ملف"})

    f = request.files['file']
    if f and f.filename:
        safe_name = sanitize_filename(os.path.basename(f.filename))
        if not safe_name:
            return jsonify({"success": False, "message": "اسم ملف غير صالح"})
        save_path = os.path.join(target_dir, safe_name)

        f.stream.seek(0, 2)
        file_size = f.stream.tell()
        f.stream.seek(0)
        ok, msg = check_disk_quota(username, file_size)
        if not ok:
            return jsonify({"success": False, "message": msg})

        f.save(save_path)
        return jsonify({
            "success": True,
            "message": "تم رفع الملف بنجاح",
            "file": {"name": safe_name, "size": f"{os.path.getsize(save_path) / 1024:.2f} KB"}
        })

    return jsonify({"success": False, "message": "فشل رفع الملف"})

@app.route("/files/create-folder/<folder>", methods=["POST"])
def create_folder(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    name = sanitize_rel_path(os.path.basename(data.get("name", "")))
    rel_path = sanitize_rel_path(data.get("path", ""))
    if not name:
        return jsonify({"success": False, "message": "اسم المجلد مطلوب"})

    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    target = safe_join(server_dir, rel_path, name) if server_dir else None
    if not target:
        return jsonify({"success": False}), 403

    if os.path.exists(target):
        return jsonify({"success": False, "message": "المجلد موجود بالفعل"})

    os.makedirs(target, exist_ok=True)
    return jsonify({"success": True, "message": f"تم إنشاء المجلد {name}"})

@app.route("/files/create-file/<folder>", methods=["POST"])
def create_file(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    name = sanitize_filename(os.path.basename(data.get("name", "")))
    rel_path = sanitize_rel_path(data.get("path", ""))
    if not name:
        return jsonify({"success": False, "message": "اسم الملف مطلوب"})

    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    target = safe_join(server_dir, rel_path, name) if server_dir else None
    if not target:
        return jsonify({"success": False}), 403

    if os.path.exists(target):
        return jsonify({"success": False, "message": "الملف موجود بالفعل"})

    open(target, "w", encoding="utf-8").close()
    return jsonify({"success": True, "message": f"تم إنشاء الملف {name}"})

@app.route("/files/rename/<folder>", methods=["POST"])
def rename_file(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    old_rel = sanitize_rel_path(data.get('old') or data.get('path', ''))
    new_name = sanitize_filename(os.path.basename(data.get('new', '')))
    if not old_rel or not new_name:
        return jsonify({"success": False, "message": "بيانات غير صالحة"})

    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    old_path = safe_join(server_dir, old_rel) if server_dir else None
    new_path = safe_join(server_dir, os.path.dirname(old_rel), new_name) if server_dir else None
    if not old_path or not new_path or not os.path.exists(old_path):
        return jsonify({"success": False, "message": "العنصر غير موجود"}), 404

    if os.path.exists(new_path):
        return jsonify({"success": False, "message": "الاسم مستخدم بالفعل"})

    os.rename(old_path, new_path)
    return jsonify({"success": True, "message": "تمت إعادة التسمية"})

@app.route("/files/delete/<folder>", methods=["POST"])
def delete_file(folder):
    """حذف ملف أو مجلد كامل"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    rel_path = sanitize_rel_path(data.get('path') or data.get('name', ''))
    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    target = safe_join(server_dir, rel_path) if server_dir else None

    if not target or not os.path.exists(target):
        return jsonify({"success": False, "message": "العنصر غير موجود"}), 404

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        return jsonify({"success": True, "message": "تم الحذف بنجاح"})
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل الحذف: {str(e)}"})

@app.route("/files/download/<folder>", methods=["GET"])
def download_file(folder):
    """تحميل ملف أو مجلد (المجلد يضغط zip)"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    rel_path = sanitize_rel_path(request.args.get("path", ""))
    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    target = safe_join(server_dir, rel_path) if server_dir else None

    if not target or not os.path.exists(target):
        return jsonify({"success": False, "message": "العنصر غير موجود"}), 404

    if os.path.isfile(target):
        return send_file(target, as_attachment=True, download_name=os.path.basename(target))

    # مجلد: ضغطه وتنزيله
    zip_name = os.path.basename(target) + ".zip"
    tmp_dir = os.path.join(BASE_DIR, "tmp_dl")
    os.makedirs(tmp_dir, exist_ok=True)
    zip_path = os.path.join(tmp_dir, f"dl_{secrets.token_hex(8)}_{zip_name}")
    try:
        create_zip([target], zip_path)
        resp = send_file(zip_path, as_attachment=True, download_name=zip_name)
        return resp
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل الضغط: {str(e)}"}), 500

@app.route("/files/copy/<folder>", methods=["POST"])
def copy_item(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    src_rel = sanitize_rel_path(data.get('path', ''))
    dst_dir_rel = sanitize_rel_path(data.get('dest', ''))
    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    src = safe_join(server_dir, src_rel) if server_dir else None
    dst_dir = safe_join(server_dir, dst_dir_rel) if server_dir else None

    if not src or not os.path.exists(src) or not dst_dir or not os.path.isdir(dst_dir):
        return jsonify({"success": False, "message": "المسار غير صحيح"}), 404

    item_size = get_dir_size(src)
    ok, msg = check_disk_quota(session['username'], item_size)
    if not ok:
        return jsonify({"success": False, "message": msg})

    base_name = os.path.basename(src)
    dst = os.path.join(dst_dir, base_name)
    counter = 1
    while os.path.exists(dst):
        root, ext = os.path.splitext(base_name)
        dst = os.path.join(dst_dir, f"{root}-copy{counter}{ext}")
        counter += 1

    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return jsonify({"success": True, "message": "تم النسخ بنجاح"})
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل النسخ: {str(e)}"})

@app.route("/files/move/<folder>", methods=["POST"])
def move_item(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    src_rel = sanitize_rel_path(data.get('path', ''))
    dst_dir_rel = sanitize_rel_path(data.get('dest', ''))
    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    src = safe_join(server_dir, src_rel) if server_dir else None
    dst_dir = safe_join(server_dir, dst_dir_rel) if server_dir else None

    if not src or not os.path.exists(src) or not dst_dir or not os.path.isdir(dst_dir):
        return jsonify({"success": False, "message": "المسار غير صحيح"}), 404

    if os.path.abspath(src) == os.path.abspath(dst_dir) or os.path.abspath(dst_dir).startswith(os.path.abspath(src) + os.sep):
        return jsonify({"success": False, "message": "لا يمكن نقل مجلد داخل نفسه"})

    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.exists(dst):
        return jsonify({"success": False, "message": "يوجد عنصر بنفس الاسم في المجلد الهدف"})

    try:
        shutil.move(src, dst)
        return jsonify({"success": True, "message": "تم النقل بنجاح"})
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل النقل: {str(e)}"})

@app.route("/files/duplicate/<folder>", methods=["POST"])
def duplicate_item(folder):
    if 'username' not in session:
        return jsonify({"success": False}), 401

    data = request.get_json() or {}
    rel_path = sanitize_rel_path(data.get('path', ''))
    server_dir = safe_join(get_user_servers_dir(session['username']), folder)
    src = safe_join(server_dir, rel_path) if server_dir else None
    if not src or not os.path.exists(src):
        return jsonify({"success": False, "message": "العنصر غير موجود"}), 404

    ok, msg = check_disk_quota(session['username'], get_dir_size(src))
    if not ok:
        return jsonify({"success": False, "message": msg})

    base_name = os.path.basename(src)
    root, ext = os.path.splitext(base_name)
    dst = os.path.join(os.path.dirname(src), f"{root}-copy{ext}")
    counter = 1
    while os.path.exists(dst):
        dst = os.path.join(os.path.dirname(src), f"{root}-copy{counter}{ext}")
        counter += 1

    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return jsonify({"success": True, "message": "تم إنشاء نسخة"})
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل النسخ: {str(e)}"})

@app.route("/files/extract/<folder>", methods=["POST"])
def extract_item(folder):
    """فك ضغط الأرشيفات: ZIP / TAR / GZ / BZ2 / XZ / 7Z / RAR"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    data = request.get_json() or {}
    rel_path = sanitize_rel_path(data.get('path', ''))
    server_dir = safe_join(get_user_servers_dir(username), folder)
    archive_path = safe_join(server_dir, rel_path) if server_dir else None

    if not archive_path or not os.path.isfile(archive_path):
        return jsonify({"success": False, "message": "الأرشيف غير موجود"}), 404

    if not get_archive_type(os.path.basename(archive_path)):
        return jsonify({"success": False, "message": "هذا الملف ليس أرشيفاً مدعوماً"})

    # حجز مساحة تقديرية 3x حجم الأرشيف للتحقق من الحصة
    archive_size = os.path.getsize(archive_path)
    ok, msg = check_disk_quota(username, archive_size * 3)
    if not ok:
        return jsonify({"success": False, "message": msg})

    base_name = os.path.splitext(os.path.basename(archive_path))[0]
    if base_name.endswith(('.tar', '.tgz')):
        base_name = os.path.splitext(base_name)[0]
    extract_dir = os.path.join(os.path.dirname(archive_path), base_name)
    counter = 1
    while os.path.exists(extract_dir):
        extract_dir = os.path.join(os.path.dirname(archive_path), f"{base_name}-{counter}")
        counter += 1

    try:
        os.makedirs(extract_dir, exist_ok=True)
        extract_archive(archive_path, extract_dir)
        extracted_count = sum(len(files) for _, _, files in os.walk(extract_dir))
        return jsonify({
            "success": True,
            "message": f"تم فك ضغط {extracted_count} ملف في مجلد {os.path.basename(extract_dir)}",
            "extracted_to": os.path.basename(extract_dir)
        })
    except Exception as e:
        shutil.rmtree(extract_dir, ignore_errors=True)
        return jsonify({"success": False, "message": f"فشل فك الضغط: {str(e)}"})

@app.route("/files/archive/<folder>", methods=["POST"])
def archive_items(folder):
    """ضغط ملفات/مجلدات محددة في ملف ZIP"""
    if 'username' not in session:
        return jsonify({"success": False}), 401

    username = session['username']
    data = request.get_json() or {}
    items = data.get('items', [])
    zip_name = sanitize_filename(os.path.basename(data.get('name') or 'archive.zip'))
    rel_path = sanitize_rel_path(data.get('path', ''))

    if not items:
        return jsonify({"success": False, "message": "لم يتم تحديد ملفات"})

    if not zip_name.lower().endswith('.zip'):
        zip_name += '.zip'

    server_dir = safe_join(get_user_servers_dir(username), folder)
    base_dir = safe_join(server_dir, rel_path) if server_dir else None
    if not base_dir or not os.path.isdir(base_dir):
        return jsonify({"success": False, "message": "المجلد غير موجود"}), 404

    real_items = []
    total_size = 0
    for item in items:
        item_rel = sanitize_rel_path(item)
        item_path = safe_join(base_dir, item_rel)
        if item_path and os.path.exists(item_path):
            real_items.append(item_path)
            total_size += get_dir_size(item_path)

    if not real_items:
        return jsonify({"success": False, "message": "لم يتم العثور على الملفات المحددة"})

    ok, msg = check_disk_quota(username, total_size)
    if not ok:
        return jsonify({"success": False, "message": msg})

    zip_path = os.path.join(base_dir, zip_name)
    counter = 1
    while os.path.exists(zip_path):
        root, ext = os.path.splitext(zip_name)
        zip_path = os.path.join(base_dir, f"{root}-{counter}{ext}")
        counter += 1

    try:
        create_zip(real_items, zip_path)
        return jsonify({"success": True, "message": f"تم إنشاء {os.path.basename(zip_path)} بنجاح"})
    except Exception as e:
        return jsonify({"success": False, "message": f"فشل الضغط: {str(e)}"})

# ============== Admin API Routes ==============

@app.route("/api/admin/users", methods=["GET"])
def get_all_users():
    """قائمة المستخدمين مع الحصص والاستهلاك (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    users = load_users()
    user_list = []
    for username, data in users.items():
        if username == ADMIN_USERNAME:
            continue
        disk_used = get_user_disk_usage(username)
        disk_limit = data.get("disk_limit_mb", DEFAULT_DISK_LIMIT_MB)
        servers_dir = get_user_servers_dir(username)
        servers_count = 0
        if os.path.exists(servers_dir):
            servers_count = len([d for d in os.listdir(servers_dir)
                                 if os.path.isdir(os.path.join(servers_dir, d))])
        user_list.append({
            "username": username,
            "created_at": data.get("created_at"),
            "last_login": data.get("last_login"),
            "created_by": data.get("created_by", "system"),
            "ram_limit_mb": data.get("ram_limit_mb", DEFAULT_RAM_LIMIT_MB),
            "disk_limit_mb": disk_limit,
            "disk_used_mb": fmt_mb(disk_used),
            "disk_percent": min(100, round(disk_used / (disk_limit * 1024 * 1024) * 100, 1)) if disk_limit != UNLIMITED else 0,
            "servers_count": servers_count
        })

    return jsonify({"success": True, "users": user_list})

@app.route("/api/admin/update-user", methods=["POST"])
def update_user():
    """تعديل حصص المستخدم (رام/مساحة) أو كلمة المرور (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"success": False, "message": "اسم المستخدم مطلوب"})

    users = load_users()
    if username not in users:
        return jsonify({"success": False, "message": "المستخدم غير موجود"})

    changes = []

    if "ram_limit_mb" in data:
        try:
            ram = int(data["ram_limit_mb"])
            if ram != UNLIMITED and not (32 <= ram <= 1024 * 64):
                return jsonify({"success": False, "message": "حد الرام يجب أن يكون بين 32 و 65536 MB"})
            users[username]["ram_limit_mb"] = ram
            changes.append(f"الرام: {ram if ram != UNLIMITED else 'بلا حدود'} MB")
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "قيمة الرام غير صالحة"})

    if "disk_limit_mb" in data:
        try:
            disk = int(data["disk_limit_mb"])
            if disk != UNLIMITED and not (16 <= disk <= 1024 * 1024):
                return jsonify({"success": False, "message": "حد المساحة يجب أن يكون بين 16 و 1048576 MB"})
            users[username]["disk_limit_mb"] = disk
            changes.append(f"المساحة: {disk if disk != UNLIMITED else 'بلا حدود'} MB")
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "قيمة المساحة غير صالحة"})

    if data.get("password"):
        new_pass = str(data["password"]).strip()
        if len(new_pass) < 6:
            return jsonify({"success": False, "message": "كلمة المرور يجب أن تكون 6 أحرف على الأقل"})
        users[username]["password"] = hash_password(new_pass)
        changes.append("كلمة المرور")

    if not changes:
        return jsonify({"success": False, "message": "لا توجد تغييرات"})

    save_users(users)
    # إيقاف عمليات المستخدم إذا تم تقليل الرام لفرض الحد الجديد
    return jsonify({"success": True, "message": f"تم تحديث: {', '.join(changes)}"})

@app.route("/api/admin/delete-user", methods=["POST"])
def delete_user():
    """حذف مستخدم (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    data = request.get_json() or {}
    username_to_delete = (data.get("username") or "").strip()

    if not username_to_delete or username_to_delete == ADMIN_USERNAME:
        return jsonify({"success": False, "message": "لا يمكن حذف هذا المستخدم"})

    users = load_users()
    if username_to_delete not in users:
        return jsonify({"success": False, "message": "المستخدم غير موجود"})

    # إيقاف جميع خوادم المستخدم أولاً
    for proc_key in list(running_procs.keys()):
        if proc_key.startswith(username_to_delete + "_"):
            try:
                kill_proc_tree(running_procs[proc_key].pid)
            except Exception:
                pass
            stop_ram_monitor(proc_key)
            running_procs.pop(proc_key, None)

    del users[username_to_delete]
    save_users(users)

    user_dir = os.path.join(USERS_DIR, username_to_delete)
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir, ignore_errors=True)

    return jsonify({"success": True, "message": "تم حذف المستخدم بنجاح"})

@app.route("/api/admin/servers", methods=["GET"])
def admin_list_servers():
    """جميع خوادم جميع المستخدمين (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    users = load_users()
    servers = []
    for username in users:
        servers_dir = get_user_servers_dir(username)
        if not os.path.exists(servers_dir):
            continue
        for folder in os.listdir(servers_dir):
            folder_path = os.path.join(servers_dir, folder)
            if not os.path.isdir(folder_path):
                continue
            meta = load_meta_for(username, folder)
            proc_key = proc_key_of(username, folder)
            proc = running_procs.get(proc_key)
            running = bool(proc and psutil.pid_exists(proc.pid))
            mem = "0 MB"
            if running:
                try:
                    mem = f"{psutil.Process(proc.pid).memory_info().rss / 1024 / 1024:.1f} MB"
                except Exception:
                    pass
            servers.append({
                "username": username,
                "folder": folder,
                "title": meta.get("display_name", folder),
                "language": meta.get("language", "python"),
                "startup_file": meta.get("startup_file", ""),
                "running": running,
                "mem": mem,
                "size_mb": fmt_mb(get_dir_size(folder_path))
            })
    return jsonify({"success": True, "servers": servers})

@app.route("/api/admin/stop-server", methods=["POST"])
def admin_stop_server():
    """إيقاف خادم أي مستخدم (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    data = request.get_json() or {}
    target_user = (data.get("username") or "").strip()
    folder = (data.get("folder") or "").strip()
    if not target_user or not folder:
        return jsonify({"success": False, "message": "بيانات ناقصة"})

    proc_key = proc_key_of(target_user, folder)
    if proc_key in running_procs:
        try:
            kill_proc_tree(running_procs[proc_key].pid)
        except Exception:
            pass
        stop_ram_monitor(proc_key)
        running_procs.pop(proc_key, None)
        return jsonify({"success": True, "message": "تم إيقاف الخادم"})
    return jsonify({"success": False, "message": "الخادم غير مشغل"})

@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    """إحصائيات النظام الشاملة (للمسؤول فقط)"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    users = load_users()
    total_disk_used = 0
    total_servers = 0
    running_servers = 0
    for username in users:
        total_disk_used += get_user_disk_usage(username)
        servers_dir = get_user_servers_dir(username)
        if os.path.exists(servers_dir):
            folders = [d for d in os.listdir(servers_dir)
                       if os.path.isdir(os.path.join(servers_dir, d))]
            total_servers += len(folders)
            for folder in folders:
                proc_key = proc_key_of(username, folder)
                proc = running_procs.get(proc_key)
                if proc and psutil.pid_exists(proc.pid):
                    running_servers += 1

    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(BASE_DIR)

    return jsonify({
        "success": True,
        "total_users": len(users),
        "total_servers": total_servers,
        "running_servers": running_servers,
        "disk_used_mb": fmt_mb(total_disk_used),
        "system": {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "ram_total_mb": round(vm.total / 1024 / 1024),
            "ram_used_percent": vm.percent,
            "disk_total_gb": round(disk.total / 1024 ** 3, 1),
            "disk_used_gb": round(disk.used / 1024 ** 3, 1),
            "disk_free_gb": round(disk.free / 1024 ** 3, 1),
            "python_version": sys.version.split()[0],
            "runtimes": get_runtimes()
        }
    })

@app.route("/api/admin/my-quotas", methods=["GET", "POST"])
def admin_my_quotas():
    """حصص حساب المسؤول نفسه للاستضافة - يمكنه تخصيص حجمه وراماته"""
    if 'username' not in session or not is_admin(session['username']):
        return jsonify({"success": False, "message": "غير مصرح"}), 403

    username = session['username']
    users = load_users()

    if request.method == "GET":
        data = users.get(username, {})
        return jsonify({
            "success": True,
            "username": username,
            "ram_limit_mb": data.get("ram_limit_mb", UNLIMITED),
            "disk_limit_mb": data.get("disk_limit_mb", UNLIMITED),
            "disk_used_mb": fmt_mb(get_user_disk_usage(username))
        })

    data = request.get_json() or {}
    updates = {}
    if "ram_limit_mb" in data:
        try:
            ram = int(data["ram_limit_mb"])
            if ram != UNLIMITED and not (32 <= ram <= 1024 * 64):
                return jsonify({"success": False, "message": "حد الرام غير صالح (32 - 65536 أو -1)"})
            updates["ram_limit_mb"] = ram
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "قيمة الرام غير صالحة"})
    if "disk_limit_mb" in data:
        try:
            disk = int(data["disk_limit_mb"])
            if disk != UNLIMITED and not (16 <= disk <= 1024 * 1024):
                return jsonify({"success": False, "message": "حد المساحة غير صالح (16 - 1048576 أو -1)"})
            updates["disk_limit_mb"] = disk
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "قيمة المساحة غير صالحة"})

    if updates:
        users[username].update(updates)
        save_users(users)
        return jsonify({"success": True, "message": "تم تحديث حصص حسابك بنجاح"})
    return jsonify({"success": False, "message": "لا توجد تغييرات"})

if __name__ == "__main__":
    # Railway يوفر PORT تلقائياً — نقرأه أولاً ثم SERVER_PORT للتوافق الخلفي
    port = int(os.environ.get("PORT") or os.environ.get("SERVER_PORT") or 8080)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
