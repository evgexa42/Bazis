import os
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask import abort
from flask import current_app
from telegram import Bot
import threading
import atexit

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# === Настройки ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

FOLDER_PATH = r"\\SERVER\homag\ПРИСАДКА КЛИЕНТА"  # Путь к папке заказов
TELEGRAM_TOKEN = "8367286754:AAGg6IlGCR7Cqz1gukXQuNvByImFp37Z17U"
CHAT_ID = "703087159"
CLIENTS_FILE = os.path.join(BASE_DIR, "clients.json")
FACADES_DIR = r"\\Server\базис"
FACADES_FILE = os.path.join(FACADES_DIR, "facades_list.txt")
WATCHED_PATH = os.path.normcase(os.path.abspath(FOLDER_PATH))

# === Flask и Telegram ===
app = Flask(__name__, template_folder=TEMPLATES_DIR)
bot = Bot(token=TELEGRAM_TOKEN)

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


def should_notify(folder_name):
    if not folder_name or folder_name.startswith('.'):
        return False
    if folder_name in IGNORED_FOLDERS:
        return False
    if not any(char.isdigit() for char in folder_name) or " " not in folder_name:
        return False
    if "[J]" in folder_name or "[I]" in folder_name:
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

        if "[J]" in dest_name or "[I]" in dest_name:
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
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")


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
    if not os.path.isdir(FOLDER_PATH):
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
    folder_data = []
    for folder_name in os.listdir(FOLDER_PATH):
        folder_path = os.path.join(FOLDER_PATH, folder_name)
        if not os.path.isdir(folder_path) or folder_name in ["Архив", "2025"]:
            continue

        modified_time = os.path.getmtime(folder_path)
        modified_date = datetime.fromtimestamp(modified_time)
        days_ago = (datetime.now() - modified_date).days
        status = "Готов" if "[J]" in folder_name or "[I]" in folder_name else "Новый"
        manager = get_manager_from_name(folder_name)

        # Определяем технолога
        technologist = None
        if "[J]" in folder_name:
            technologist = "Женя"
        elif "[I]" in folder_name:
            technologist = "Игорь"

        folder_data.append({
            "name": folder_name,
            "status": status,
            "manager": manager,
            "technologist": technologist or "Неизвестно",
            "modified": modified_date.strftime("%d.%m.%Y %H:%M"),
            "days": days_ago
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
    manager_stats = {"Игорь": 0, "Кристина": 0, "Валерия": 0, "Неизвестно": 0}
    for f in folders:
        if f["manager"] in manager_stats:
            manager_stats[f["manager"]] += 1
        else:
            manager_stats["Неизвестно"] += 1

    # Подсчёт по технологам
    tech_stats = {"Женя": 0, "Игорь": 0, "Неизвестно": 0}
    for f in folders:
        if f["technologist"] in tech_stats:
            tech_stats[f["technologist"]] += 1
        else:
            tech_stats["Неизвестно"] += 1

    # --- Фильтр по менеджерам ---
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

# === Поиск заказов по сетевым папкам ===
SEARCH_FOLDERS = {
    "Присадка ARHIMOB": r"\\SERVER\homag\ПРИСАДКА ARHIMOB\2025",
    "DESENE CPU": r"\\SERVER\homag\DESENE CPU\2025",
    "Присадка Клиента": r"\\SERVER\homag\ПРИСАДКА КЛИЕНТА\2025"
}

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

    with open(FACADES_FILE, "w", encoding="utf-8") as f:
        f.write("# position(optional) height width count side hinges\n")
        for line in lines:
            f.write(line + "\n")

    return jsonify({"status": "ok", "file": os.path.basename(FACADES_FILE)})


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

# === Запуск ===
if __name__ == "__main__":
    initialize_known_state()

    # Исправление бага с дублированием:
    # Запускаем поток мониторинга только в основном процессе,
    # который запускается Flask'ом (когда WERKZEUG_RUN_MAIN == 'true').
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
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
    print("Сервер запущен: http://192.168.100.114:5000")
    app.run(host="192.168.100.114", port=5000, debug=True)