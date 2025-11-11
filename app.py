import os
import json
import threading
import atexit
from copy import deepcopy
from datetime import datetime

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    redirect,
    url_for,
    abort,
    current_app,
)
from telegram import Bot

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# === Настройки ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
CLIENTS_FILE = os.path.join(BASE_DIR, "clients.json")
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 5000, "debug": False},
    "telegram": {"token": "", "chat_id": ""},
    "paths": {
        "orders": "",
        "facades_dir": "",
        "search": {},
    },
    "managers": [],
    "technologists": {},
    "features": {
        "order_confirmation": False,
    },
}


def deep_merge(base, extra):
    result = deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    config = deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            if isinstance(file_data, dict):
                config = deep_merge(config, file_data)
        except Exception as exc:
            print(f"[config] Не удалось прочитать config.json: {exc}")
    else:
        save_config(config)
    return config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


CONFIG = load_config()
ORDER_CONFIRMATION_ENABLED = False


def apply_config(config):
    global FOLDER_PATH, FACADES_DIR, FACADES_FILE, WATCHED_PATH
    global TELEGRAM_TOKEN, CHAT_ID, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global SEARCH_FOLDERS, MANAGER_NAMES, TECHNOLOGIST_MARKERS
    global ORDER_CONFIRMATION_ENABLED

    server = config.get("server", {})
    telegram = config.get("telegram", {})
    paths = config.get("paths", {})

    SERVER_HOST = server.get("host", "127.0.0.1")
    SERVER_PORT = server.get("port", 5000)
    DEBUG_MODE = bool(server.get("debug", False))

    TELEGRAM_TOKEN = telegram.get("token", "")
    CHAT_ID = str(telegram.get("chat_id", "")).strip()

    FOLDER_PATH = paths.get("orders") or ""
    FACADES_DIR = paths.get("facades_dir") or ""
    FACADES_FILE = os.path.join(FACADES_DIR, "facades_list.txt") if FACADES_DIR else "facades_list.txt"
    WATCHED_PATH = os.path.normcase(os.path.abspath(FOLDER_PATH)) if FOLDER_PATH else ""

    search_folders = paths.get("search")
    SEARCH_FOLDERS = search_folders if isinstance(search_folders, dict) else {}

    MANAGER_NAMES = [name.strip() for name in config.get("managers", []) if name.strip()]

    raw_markers = config.get("technologists", {})
    if isinstance(raw_markers, dict):
        TECHNOLOGIST_MARKERS = {
            marker.strip(): value.strip()
            for marker, value in raw_markers.items()
            if marker and value
        }
    else:
        TECHNOLOGIST_MARKERS = {}

    features = config.get("features", {}) if isinstance(config.get("features"), dict) else {}
    ORDER_CONFIRMATION_ENABLED = bool(features.get("order_confirmation"))


apply_config(CONFIG)

# === Flask и Telegram ===
app = Flask(__name__, template_folder=TEMPLATES_DIR)
bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# === Вспомогательные функции для Jinja2 ===
def replace_slashes(text):
    """Заменяет обратные слеши на прямые для корректных URL-адресов file://."""
    return text.replace("\\", "/")

# Регистрация пользовательского фильтра 'replace_slashes'
app.jinja_env.filters['replace_slashes'] = replace_slashes 

# === Загрузка базы клиентов ===
def load_clients():
    if os.path.exists(CLIENTS_FILE):
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_clients(data):
    with open(CLIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

clients = load_clients()


# === Определение менеджера по имени клиента ===
def get_manager_from_name(folder_name):
    for client, manager in clients.items():
        if client.lower() in folder_name.lower():
            return manager
    return "Неизвестно"


# === Telegram уведомления ===
known_folders = set()
known_folders_lock = threading.Lock()
messages_lock = threading.Lock()
observer = None


IGNORED_FOLDERS = {"Архив", "2025"}


def folder_has_ready_marker(folder_name):
    if not TECHNOLOGIST_MARKERS:
        return False
    for marker in TECHNOLOGIST_MARKERS:
        if f"[{marker}]" in folder_name:
            return True
    return False


def technologist_from_folder(folder_name):
    for marker, name in TECHNOLOGIST_MARKERS.items():
        if f"[{marker}]" in folder_name:
            return name
    return "Неизвестно"


def should_notify(folder_name):
    if not folder_name or folder_name.startswith('.'):
        return False
    if folder_name in IGNORED_FOLDERS:
        return False
    if not any(char.isdigit() for char in folder_name) or " " not in folder_name:
        return False
    if folder_has_ready_marker(folder_name):
        return False
    return True


def register_known_folder(folder_name):
    """Добавляет папку в known_folders. Возвращает True, если папка уже была известна."""
    with known_folders_lock:
        already_known = folder_name in known_folders
        known_folders.add(folder_name)
    return already_known


def unregister_known_folder(folder_name):
    with known_folders_lock:
        known_folders.discard(folder_name)


def move_known_folder(src_name, dest_name):
    with known_folders_lock:
        known_folders.discard(src_name)
        already_known = dest_name in known_folders
        known_folders.add(dest_name)
    return already_known


class OrderFolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            return
        parent = os.path.normcase(os.path.abspath(os.path.dirname(event.src_path)))
        if parent != WATCHED_PATH:
            return
        folder_name = os.path.basename(event.src_path)
        if register_known_folder(folder_name):
            return
        if should_notify(folder_name):
            send_telegram_message(build_order_message(folder_name), folder_name)

    def on_moved(self, event):
        if not event.is_directory:
            return
        src_name = os.path.basename(event.src_path)
        dest_name = os.path.basename(event.dest_path)
        src_in_watch = os.path.normcase(os.path.abspath(os.path.dirname(event.src_path))) == WATCHED_PATH
        dest_in_watch = os.path.normcase(os.path.abspath(os.path.dirname(event.dest_path))) == WATCHED_PATH

        if src_in_watch and not dest_in_watch:
            unregister_known_folder(src_name)
            delete_telegram_message(src_name)
            return

        if dest_in_watch and not src_in_watch:
            if register_known_folder(dest_name):
                return
            if should_notify(dest_name):
                send_telegram_message(build_order_message(dest_name), dest_name)
            return

        already_known = move_known_folder(src_name, dest_name)

        if folder_has_ready_marker(dest_name):
            delete_telegram_message(dest_name)
            return

        if update_message_for_folder(src_name, dest_name):
            return

        # Если папка переехала внутрь каталога или была переименована
        if not already_known and should_notify(dest_name):
            send_telegram_message(build_order_message(dest_name), dest_name)

    def on_deleted(self, event):
        if not event.is_directory:
            return
        parent = os.path.normcase(os.path.abspath(os.path.dirname(event.src_path)))
        if parent != WATCHED_PATH:
            return
        folder_name = os.path.basename(event.src_path)
        unregister_known_folder(folder_name)
        delete_telegram_message(folder_name)

# --- messages.json persistent storage ---


def order_key_from_name(name):
    """Берём ключ заказа — первый токен до пробела, в lower()."""
    if not name:
        return ""
    return name.split()[0].lower()


def load_messages():
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                cleaned = []
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    folder = entry.get("folder", "")
                    order_key = entry.get("order_key") or order_key_from_name(folder)
                    message_id = entry.get("message_id")
                    chat_id = entry.get("chat_id")
                    if message_id is None or chat_id is None:
                        continue
                    entry["folder"] = folder
                    entry["order_key"] = order_key
                    cleaned.append(entry)
                return cleaned
        except Exception as e:
            print(f"[load_messages] Ошибка чтения {MESSAGES_FILE}: {e}")
    return []  # список записей: {"folder": str, "order_key": str, "chat_id": ..., "message_id": ...}

def save_messages(msgs):
    try:
        with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[save_messages] Ошибка записи {MESSAGES_FILE}: {e}")

# в памяти
messages = load_messages()

def build_order_message(folder_name):
    manager = get_manager_from_name(folder_name)
    return f"📁 Новый заказ: {folder_name}\n👤 Менеджер: {manager}"


def send_telegram_message(msg, folder_name):
    """Отправить сообщение и сохранить запись для возможного удаления позже."""
    global messages
    if bot is None or not CHAT_ID:
        print("[TG] Бот не настроен. Сообщение не отправлено.")
        return
    try:
        sent = bot.send_message(chat_id=CHAT_ID, text=msg)
        entry = {
            "folder": folder_name,
            "order_key": order_key_from_name(folder_name),
            "chat_id": CHAT_ID,
            "message_id": sent.message_id
        }
        with messages_lock:
            messages.append(entry)
            save_messages(messages)
        print(f"[TG] Сообщение отправлено и сохранено для {folder_name} -> id {sent.message_id}")
    except Exception as e:
        print(f"[Telegram Error] {e}")

def delete_telegram_message(folder_name):
    """Удалить все сообщения из messages.json, соответствующие order_key или точному имени папки."""
    global messages
    key = order_key_from_name(folder_name)
    with messages_lock:
        candidates = [
            entry for entry in messages
            if entry.get("folder") == folder_name or entry.get("order_key") == key
        ]
    if not candidates:
        return

    for entry in candidates:
        try:
            if bot is not None:
                bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
                print(f"[TG] Удалено сообщение {entry['message_id']} для {entry['folder']}")
        except Exception as e:
            # Логируем, но продолжаем (возможно сообщение уже удалено)
            print(f"[TG delete error] {e} (folder={entry.get('folder')}, msg_id={entry.get('message_id')})")

    with messages_lock:
        messages = [m for m in messages if m not in candidates]
        save_messages(messages)


def find_message_entry(old_name, new_name=None):
    """Найти запись по имени папки или ключу заказа."""
    keys = set()
    if old_name:
        keys.add(order_key_from_name(old_name))
    if new_name:
        keys.add(order_key_from_name(new_name))

    with messages_lock:
        for entry in messages:
            if entry.get("folder") == old_name:
                return entry
            if keys and entry.get("order_key") in keys:
                return entry
    return None


def update_message_for_folder(old_name, new_name):
    """Обновляет текст сообщения в Telegram при переименовании папки."""
    entry = find_message_entry(old_name, new_name)
    if not entry:
        return False

    chat_id = entry.get("chat_id")
    message_id = entry.get("message_id")
    if chat_id is None or message_id is None:
        return False

    new_text = build_order_message(new_name)
    if bot is None:
        return False

    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
        print(f"[TG] Сообщение {message_id} обновлено для {new_name}")
    except Exception as e:
        print(f"[TG edit error] {e} (folder={old_name} -> {new_name})")
        return False

    with messages_lock:
        if entry in messages:
            entry["folder"] = new_name
            entry["order_key"] = order_key_from_name(new_name)
            save_messages(messages)
    return True


def ensure_message_for_folder(folder_name):
    """Гарантирует наличие записи и сообщения для существующей папки."""
    if not should_notify(folder_name):
        return

    key = order_key_from_name(folder_name)
    with messages_lock:
        for entry in messages:
            if entry.get("order_key") == key:
                current_name = entry.get("folder")
                break
        else:
            entry = None
            current_name = None

    if entry is None:
        send_telegram_message(build_order_message(folder_name), folder_name)
        return

    if current_name != folder_name:
        update_message_for_folder(current_name or folder_name, folder_name)


def cleanup_missing_messages(existing_keys):
    """Удаляет сообщения, если заказов больше не существует."""
    global messages
    with messages_lock:
        current_messages = list(messages)

    stale_entries = []
    for entry in current_messages:
        key = entry.get("order_key") or order_key_from_name(entry.get("folder"))
        if key not in existing_keys:
            stale_entries.append(entry)

    if not stale_entries:
        return

    if bot is not None:
        for entry in stale_entries:
            try:
                bot.delete_message(chat_id=entry.get("chat_id"), message_id=entry.get("message_id"))
                print(f"[TG] Удалено устаревшее сообщение {entry.get('message_id')} для {entry.get('folder')}")
            except Exception as e:
                print(f"[TG stale delete error] {e} (folder={entry.get('folder')}, msg_id={entry.get('message_id')})")

    with messages_lock:
        messages = [m for m in messages if m not in stale_entries]
        save_messages(messages)


def initialize_known_state():
    """Самовосстанавливает состояние известных папок и сообщений."""
    if not FOLDER_PATH or not os.path.isdir(FOLDER_PATH):
        print(f"[init] Путь не найден: {FOLDER_PATH}")
        return

    try:
        folder_names = os.listdir(FOLDER_PATH)
    except Exception as e:
        print(f"[init] Не удалось прочитать каталог: {e}")
        return

    actual_folders = []
    for name in folder_names:
        folder_path = os.path.join(FOLDER_PATH, name)
        if not os.path.isdir(folder_path):
            continue
        if name in IGNORED_FOLDERS:
            continue
        actual_folders.append(name)

    with known_folders_lock:
        known_folders.clear()
        known_folders.update(actual_folders)

    existing_keys = {order_key_from_name(name) for name in actual_folders}
    cleanup_missing_messages(existing_keys)

    for name in actual_folders:
        if should_notify(name):
            ensure_message_for_folder(name)
        else:
            delete_telegram_message(name)

@app.route("/update_client", methods=["POST"])
def update_client():
    data = request.get_json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    new_manager = data.get("new_manager")

    if old_name in clients:
        clients.pop(old_name)
        clients[new_name] = new_manager
        save_clients(clients)

    return jsonify({"status": "ok"})


@app.route("/delete_client", methods=["POST"])
def delete_client():
    data = request.get_json()
    name = data.get("name")

    if name in clients:
        clients.pop(name)
        save_clients(clients)

    return jsonify({"status": "ok"})

# === Получение списка заказов ===
def get_folders():
    if not FOLDER_PATH or not os.path.isdir(FOLDER_PATH):
        return []

    folder_data = []
    for folder_name in os.listdir(FOLDER_PATH):
        folder_path = os.path.join(FOLDER_PATH, folder_name)
        if not os.path.isdir(folder_path) or folder_name in IGNORED_FOLDERS:
            continue

        modified_time = os.path.getmtime(folder_path)
        modified_date = datetime.fromtimestamp(modified_time)
        days_ago = (datetime.now() - modified_date).days
        status = "Готов" if folder_has_ready_marker(folder_name) else "Новый"
        manager = get_manager_from_name(folder_name)

        # Определяем технолога
        technologist = technologist_from_folder(folder_name)

        is_confirmed = folder_name.endswith("+")
        order_number = folder_name.split()[0] if folder_name else ""
        if is_confirmed and order_number.endswith("+"):
            order_number = order_number.rstrip("+")

        folder_data.append({
            "name": folder_name,
            "status": status,
            "manager": manager,
            "technologist": technologist or "Неизвестно",
            "modified": modified_date.strftime("%d.%m.%Y %H:%M"),
            "days": days_ago,
            "confirmed": is_confirmed,
            "order_number": order_number,
        })

    folder_data.sort(key=lambda x: x["modified"], reverse=True)
    return folder_data


# === Маршруты Flask ===
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/facades")
def facades_page():
    template_path = os.path.join(current_app.template_folder or "", "facades.html")
    if template_path and not os.path.exists(template_path):
        app.logger.error("Шаблон фасадов не найден: %s", template_path)
        abort(500, description="Не найден шаблон facades.html. Убедитесь, что файл находится в папке templates.")
    return render_template("facades.html")

@app.route("/data")
def data():
    folders = get_folders()

    total_orders = len(folders)
    # Подсчёт по менеджерам
    manager_stats = {name: 0 for name in MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    for f in folders:
        manager_name = f.get("manager") or "Неизвестно"
        if manager_name not in manager_stats:
            manager_stats.setdefault(manager_name, 0)
        manager_stats[manager_name] += 1

    # Подсчёт по технологам
    tech_stats = {name: 0 for name in TECHNOLOGIST_MARKERS.values()}
    tech_stats["Неизвестно"] = tech_stats.get("Неизвестно", 0)
    for f in folders:
        technologist_name = f.get("technologist") or "Неизвестно"
        if technologist_name not in tech_stats:
            tech_stats.setdefault(technologist_name, 0)
        tech_stats[technologist_name] += 1

    # --- Фильтр по менеджерам --
    manager_filter = request.args.get("manager", "Все")
    if manager_filter != "Все":
        folders = [f for f in folders if f["manager"] == manager_filter]


    return jsonify({
        "folders": folders,
        "total": total_orders,
        "managers": manager_stats,
        "technologists": tech_stats
    })


# === Управление клиентами ===
@app.route("/clients")
def clients_page():
    # Перезагрузка клиентов перед отображением, чтобы учесть изменения
    # global clients
    # clients = load_clients() 
    return render_template("clients.html", clients=clients)

@app.route("/add_client", methods=["POST"])
def add_client():
    client_name = request.form.get("client").strip()
    manager = request.form.get("manager")

    if client_name:
        clients[client_name] = manager
        save_clients(clients)
    return redirect(url_for("clients_page"))

# === Словарь месяцев на румынском ===
MONTHS_RO = {
    1: "01. Ianuarie", 2: "02. Februarie", 3: "03. Martie", 4: "04. Aprilie",
    5: "05. Mai", 6: "06. Iunie", 7: "07. Iulie", 8: "08. August",
    9: "09. Septembrie", 10: "10. Octombrie", 11: "11. Noiembrie", 12: "12. Decembrie"
}

def get_recent_months():
    """Возвращает список последних двух месяцев в формате '10. Octombrie 2025'"""
    now = datetime.now()
    year = now.year
    months = []

    for i in range(2):
        month_num = now.month - i
        if month_num <= 0:
            month_num += 12
            year -= 1
        months.append(f"{MONTHS_RO[month_num]} {year}")

    return months


@app.route("/search", methods=["GET", "POST"])
def search_page():
    query = request.form.get("query", "").strip()
    results = {key: [] for key in SEARCH_FOLDERS.keys()}
    search_months = get_recent_months()

    if query:
        for key, base_folder in SEARCH_FOLDERS.items():
            if not os.path.exists(base_folder):
                continue

            for month_folder in search_months:
                month_path = os.path.join(base_folder, month_folder)
                if not os.path.isdir(month_path):
                    continue

                for folder_name in os.listdir(month_path):
                    if query.lower() in folder_name.lower():
                        manager = get_manager_from_name(folder_name)
                        results[key].append({
                            "name": folder_name,
                            "manager": manager,
                            "path": os.path.join(month_path, folder_name)
                        })

    return render_template("search.html", query=query, results=results, SEARCH_FOLDERS=SEARCH_FOLDERS)
    
@app.route("/facades/generate", methods=["POST"])
def generate_facades():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])

    lines = []
    errors = []

    for idx, item in enumerate(items, start=1):
        position = str(item.get("position", "")).strip()
        width = str(item.get("width", "")).strip()
        height = str(item.get("height", "")).strip()
        count = str(item.get("count", "")).strip()
        side = str(item.get("side", "")).strip().lower()
        hinges = str(item.get("hinges", "")).strip()

        if not width or not height or not count:
            errors.append(f"Строка {idx}: заполните высоту, ширину и количество.")
            continue

        try:
            width_int = int(width)
            height_int = int(height)
            count_int = int(count)
        except ValueError:
            errors.append(f"Строка {idx}: высота, ширина и количество должны быть числами.")
            continue

        if width_int <= 0 or height_int <= 0 or count_int <= 0:
            errors.append(f"Строка {idx}: значения должны быть больше нуля.")
            continue

        if side not in ("left", "right", "левая", "правая", "l", "r"):
            errors.append(f"Строка {idx}: выберите сторону (левая/правая).")
            continue

        try:
            hinges_int = int(hinges)
        except ValueError:
            errors.append(f"Строка {idx}: количество петель должно быть числом.")
            continue

        if hinges_int < 2 or hinges_int > 6:
            errors.append(f"Строка {idx}: количество петель должно быть от 2 до 6.")
            continue

        side_code = "L" if side.startswith("l") or side.startswith("л") else "R"

        line_parts = []
        if position:
            line_parts.append(position)

        line_parts.extend([
            str(height_int),
            str(width_int),
            str(count_int),
            side_code,
            str(hinges_int)
        ])

        lines.append(" ".join(line_parts))

    if errors:
        return jsonify({"status": "error", "errors": errors}), 400

    if not lines:
        return jsonify({"status": "error", "errors": ["Добавьте хотя бы один фасад перед генерацией."]}), 400

    target_dir = os.path.dirname(FACADES_FILE)
    if target_dir:
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            return jsonify({"status": "error", "errors": [f"Не удалось создать папку {target_dir}: {exc}"]}), 500

    try:
        with open(FACADES_FILE, "w", encoding="utf-8") as f:
            f.write("# position(optional) height width count side hinges\n")
            for line in lines:
                f.write(line + "\n")
    except OSError as exc:
        return jsonify({"status": "error", "errors": [f"Не удалось записать файл: {exc}"]}), 500

    return jsonify({
        "status": "ok",
        "file": os.path.basename(FACADES_FILE),
        "folder": FACADES_DIR or os.path.dirname(os.path.abspath(FACADES_FILE)),
    })


# === Открытие папки ===
@app.route("/open_folder", methods=["POST"])
def open_folder():
    folder_path = request.form.get("path")
    if folder_path and os.path.exists(folder_path):
        try:
            os.startfile(folder_path)  # откроет в проводнике Windows
        except Exception as e:
            print(f"Ошибка открытия: {e}")
    return ("", 204)


@app.route("/confirm_order", methods=["POST"])
def confirm_order():
    if not ORDER_CONFIRMATION_ENABLED:
        return jsonify({"status": "error", "message": "Подтверждение заказов отключено."}), 400

    if not FOLDER_PATH or not os.path.isdir(FOLDER_PATH):
        return jsonify({"status": "error", "message": "Путь к папке заказов не настроен."}), 500

    payload = request.get_json(silent=True) or {}
    folder_name = (payload.get("folder") or "").strip()
    if not folder_name:
        return jsonify({"status": "error", "message": "Не указано имя заказа."}), 400

    current_path = os.path.join(FOLDER_PATH, folder_name)
    if not os.path.isdir(current_path):
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    if folder_name.endswith("+"):
        return jsonify({"status": "ok", "folder": folder_name, "message": "Заказ уже подтверждён."})

    new_name = f"{folder_name} +"
    new_path = os.path.join(FOLDER_PATH, new_name)
    if os.path.exists(new_path):
        return jsonify({"status": "error", "message": "Папка с подтверждённым заказом уже существует."}), 409

    try:
        os.rename(current_path, new_path)
    except OSError as exc:
        return jsonify({"status": "error", "message": f"Не удалось подтвердить заказ: {exc}"}), 500

    move_known_folder(folder_name, new_name)
    update_message_for_folder(folder_name, new_name)

    return jsonify({"status": "ok", "folder": new_name})


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    global CONFIG, bot

    status_message = None
    errors = []

    if request.method == "POST":
        def parse_list(value):
            return [item.strip() for item in value.splitlines() if item.strip()]

        def parse_mapping(value):
            mapping = {}
            for line in value.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key and val:
                    mapping[key] = val
            return mapping

        updated = deepcopy(CONFIG)

        orders_path = request.form.get("orders_path", "").strip()
        facades_dir = request.form.get("facades_dir", "").strip()
        search_raw = request.form.get("search_folders", "")
        managers_raw = request.form.get("managers", "")
        technologists_raw = request.form.get("technologists", "")

        server_host = request.form.get("server_host", "").strip()
        server_port = request.form.get("server_port", "").strip()
        debug_mode = request.form.get("debug_mode") == "on"
        order_confirmation = request.form.get("order_confirmation") == "on"

        telegram_token = request.form.get("telegram_token", "").strip()
        telegram_chat = request.form.get("telegram_chat_id", "").strip()

        if orders_path:
            updated.setdefault("paths", {})["orders"] = orders_path
        if facades_dir:
            updated.setdefault("paths", {})["facades_dir"] = facades_dir
        updated.setdefault("paths", {})["search"] = parse_mapping(search_raw)

        managers_list = parse_list(managers_raw)
        if managers_list:
            updated["managers"] = managers_list
        else:
            errors.append("Список менеджеров не может быть пустым.")

        technologists_map = parse_mapping(technologists_raw)
        updated["technologists"] = technologists_map

        server_config = updated.setdefault("server", {})
        if server_host:
            server_config["host"] = server_host
        if server_port:
            try:
                server_config["port"] = int(server_port)
            except (TypeError, ValueError):
                errors.append("Порт должен быть числом.")
        server_config["debug"] = debug_mode

        updated.setdefault("telegram", {})["token"] = telegram_token
        updated.setdefault("telegram", {})["chat_id"] = telegram_chat

        features_config = updated.setdefault("features", {})
        if not isinstance(features_config, dict):
            features_config = {}
            updated["features"] = features_config
        features_config["order_confirmation"] = order_confirmation

        if not errors:
            save_config(updated)
            CONFIG.clear()
            CONFIG.update(updated)
            apply_config(CONFIG)
            bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
            status_message = "Настройки сохранены. Некоторые изменения вступят в силу после перезапуска приложения."

    managers_text = "\n".join(MANAGER_NAMES)
    technologists_text = "\n".join(f"{marker}={name}" for marker, name in TECHNOLOGIST_MARKERS.items())
    search_text = "\n".join(f"{title}={path}" for title, path in SEARCH_FOLDERS.items())

    return render_template(
        "settings.html",
        config=CONFIG,
        managers_text=managers_text,
        technologists_text=technologists_text,
        search_text=search_text,
        status_message=status_message,
        errors=errors,
    )


@app.context_processor
def inject_config_data():
    return {
        "config_managers": MANAGER_NAMES,
        "config_facades_dir": FACADES_DIR,
        "order_confirmation_enabled": ORDER_CONFIRMATION_ENABLED,
    }

# === Запуск ===
if __name__ == "__main__":
    initialize_known_state()

    # Исправление бага с дублированием:
    # Запускаем поток мониторинга только в основном процессе,
    # который запускается Flask'ом (когда WERKZEUG_RUN_MAIN == 'true').
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' and FOLDER_PATH:
        print("Starting folder monitor observer...")
        initialize_known_state()

        handler = OrderFolderHandler()
        observer = Observer()
        observer.schedule(handler, FOLDER_PATH, recursive=False)
        observer.start()

        def stop_observer():
            if observer is not None:
                observer.stop()
                observer.join(timeout=5)

        atexit.register(stop_observer)
    print(f"Сервер запущен: http://{SERVER_HOST}:{SERVER_PORT}")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE)