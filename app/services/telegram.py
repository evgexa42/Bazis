import json
import os
import queue
import sys
import threading
from typing import List, Optional

import certifi
from telegram import Bot
from telegram.utils.request import Request

import app as bazis_app
import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.database import load_messages as load_messages_from_db
from app.dal.database import replace_messages as replace_messages_in_db
from app.paths import get_base_dir

bot: Optional[Bot] = None
messages: List[dict] = []
messages_lock = threading.Lock()
tg_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_disabled_warning_logged = False
_config_state_logged = False


def _runtime_mode() -> str:
    return "frozen" if getattr(sys, "frozen", False) else "source"


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _log_config_state() -> None:
    global _config_state_logged
    if _config_state_logged:
        return
    _config_state_logged = True

    base_dir = get_base_dir()
    config_path = os.path.join(base_dir, "config.json")
    env_path = os.path.join(base_dir, ".env")
    logger.info(
        "[TG] runtime=%s base_dir=%s config=%s env=%s token=%s chat_id=%s",
        _runtime_mode(),
        base_dir,
        config_path,
        env_path,
        _mask(app_config.TELEGRAM_TOKEN),
        app_config.CHAT_ID or "(empty)",
    )


def _telegram_configured() -> bool:
    configured = bot is not None and bool(app_config.CHAT_ID)
    if not configured:
        _log_config_state()
    return configured


def _log_disabled_once() -> None:
    global _disabled_warning_logged
    if _disabled_warning_logged:
        return
    _disabled_warning_logged = True
    logger.warning(
        "[TG] Бот не настроен: пропускаем задачи отправки (token=%s, chat_id=%s)",
        _mask(app_config.TELEGRAM_TOKEN),
        app_config.CHAT_ID or "(empty)",
    )


def enqueue(fn, *args, **kwargs) -> None:
    if not _telegram_configured():
        _log_disabled_once()
        return
    try:
        tg_queue.put_nowait((fn, args, kwargs))
    except queue.Full:
        logger.warning("[TG queue] Очередь заполнена, задача отброшена: %s", fn)


def _worker():
    while True:
        func, args, kwargs = tg_queue.get()
        try:
            func(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - логирование ошибок воркера
            logger.exception("[TG queue] Ошибка выполнения задачи %s", exc_info=exc)
        finally:
            tg_queue.task_done()

_worker_started = False
_worker_lock = threading.Lock()

def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True).start()
        _worker_started = True

def init_bot(token: str) -> None:
    global bot, _disabled_warning_logged
    _disabled_warning_logged = False
    _log_config_state()

    if token and app_config.CHAT_ID:
        request = Request(
            con_pool_size=8,
            read_timeout=20,
            connect_timeout=20,
            request_kwargs={"verify": certifi.where()},
        )
        bot = Bot(token=token, request=request)
        start_worker_once()
        logger.info(
            "[TG] Бот инициализирован (mode=%s, token=%s, chat_id=%s, ca=%s)",
            _runtime_mode(),
            _mask(token),
            app_config.CHAT_ID,
            certifi.where(),
        )
    else:
        bot = None
        logger.warning(
            "[TG] Бот не инициализирован (token=%s, chat_id=%s)",
            _mask(token),
            app_config.CHAT_ID or "(empty)",
        )


def load_messages_storage() -> None:
    global messages

    stored = load_messages_from_db(level="telegram")
    cleaned = []
    for entry in stored:
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
    with messages_lock:
        messages = cleaned



def save_messages(msgs: List[dict]) -> None:
    try:
        serialized = [json.loads(json.dumps(entry, ensure_ascii=False)) for entry in msgs]
        replace_messages_in_db(serialized, level="telegram")
    except Exception as exc:
        logger.exception("[save_messages] Ошибка записи сообщений в БД", exc_info=exc)


def folder_has_ready_marker(folder_name: str) -> bool:
    if not app_config.TECHNOLOGIST_MARKERS:
        return "[$]" in (folder_name or "")
    for marker in app_config.TECHNOLOGIST_MARKERS:
        if f"[{marker}]" in folder_name:
            return True
    return "[$]" in (folder_name or "")


def technologist_from_folder(folder_name: str) -> str:
    for marker, name in app_config.TECHNOLOGIST_MARKERS.items():
        if f"[{marker}]" in folder_name:
            return name
    if "[$]" in (folder_name or ""):
        return "Технолог"
    return "Неизвестно"


def _ignored_folders() -> set[str]:
    # защита от пустых и дублирующихся значений в конфиге
    return {name for name in app_config.TELEGRAM_IGNORED_FOLDERS if name}


def is_folder_ignored(folder_name: str) -> bool:
    return folder_name in _ignored_folders()


def should_notify(folder_name: str) -> bool:
    if not folder_name or folder_name.startswith("."):
        return False
    if is_folder_ignored(folder_name):
        return False
    if not any(char.isdigit() for char in folder_name) or " " not in folder_name:
        return False
    if folder_has_ready_marker(folder_name):
        return False
    return True


def order_key_from_name(name: str) -> str:
    if not name:
        return ""
    return name.split()[0].lower()


def build_order_message(folder_name: str) -> str:
    manager = get_manager_from_name(folder_name)
    return f"📁 Новый заказ: {folder_name}\n👤 Менеджер: {manager}"


def send_telegram_message(msg: str, folder_name: str) -> None:
    global messages
    if not _telegram_configured():
        _log_disabled_once()
        return
    try:
        logger.info("[TG] Отправка сообщения для %s", folder_name)
        sent = bot.send_message(chat_id=app_config.CHAT_ID, text=msg)
        entry = {
            "folder": folder_name,
            "order_key": order_key_from_name(folder_name),
            "chat_id": app_config.CHAT_ID,
            "message_id": sent.message_id,
        }
        with messages_lock:
            messages.append(entry)
            snapshot = list(messages)
        save_messages(snapshot)
        logger.info(
            "[TG] Сообщение отправлено и сохранено для %s -> id %s",
            folder_name,
            sent.message_id,
        )
    except Exception as exc:
        logger.exception(
            "[Telegram Error] Ошибка отправки сообщения (folder=%s, chat_id=%s)",
            folder_name,
            app_config.CHAT_ID,
            exc_info=exc,
        )


def delete_telegram_message(folder_name: str) -> None:
    global messages
    if not messages:
        return
    if not _telegram_configured():
        return
    key = order_key_from_name(folder_name)
    with messages_lock:
        candidates = [
            entry
            for entry in messages
            if entry.get("folder") == folder_name or entry.get("order_key") == key
        ]
    if not candidates:
        return

    for entry in candidates:
        try:
            if bot is not None:
                bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"])
                logger.info(
                    "[TG] Удалено сообщение %s для %s",
                    entry.get("message_id"),
                    entry.get("folder"),
                )
        except Exception as exc:
            logger.exception(
                "[TG delete error] %s (folder=%s, msg_id=%s)",
                exc,
                entry.get("folder"),
                entry.get("message_id"),
            )

    with messages_lock:
        messages = [m for m in messages if m not in candidates]
        snapshot = list(messages)

    save_messages(snapshot)


def find_message_entry(old_name: str, new_name: Optional[str] = None):
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


def update_message_for_folder(old_name: str, new_name: str) -> bool:
    entry = find_message_entry(old_name, new_name)
    if not entry:
        return False

    chat_id = entry.get("chat_id")
    message_id = entry.get("message_id")
    if chat_id is None or message_id is None:
        return False

    new_text = build_order_message(new_name)
    if not _telegram_configured():
        return False

    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
        logger.info("[TG] Сообщение %s обновлено для %s", message_id, new_name)
    except Exception as exc:
        logger.exception("[TG edit error] %s (folder=%s -> %s)", exc, old_name, new_name)
        return False

    with messages_lock:
        if entry in messages:
            entry["folder"] = new_name
            entry["order_key"] = order_key_from_name(new_name)
        snapshot = list(messages)

    save_messages(snapshot)
    return True


def handle_moved_notification(old_name: str, new_name: str, already_known: bool) -> None:
    if not _telegram_configured():
        return

    if folder_has_ready_marker(new_name):
        delete_telegram_message(new_name)
        return

    if update_message_for_folder(old_name, new_name):
        return

    if not already_known and should_notify(new_name):
        send_telegram_message(build_order_message(new_name), new_name)


def ensure_message_for_folder(folder_name: str) -> None:
    if not _telegram_configured():
        return

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


def cleanup_missing_messages(existing_keys) -> None:
    global messages
    if not messages:
        return
    if not _telegram_configured():
        return
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
                logger.info(
                    "[TG] Удалено устаревшее сообщение %s для %s",
                    entry.get("message_id"),
                    entry.get("folder"),
                )
            except Exception as exc:
                logger.exception(
                    "[TG stale delete error] %s (folder=%s, msg_id=%s)",
                    exc,
                    entry.get("folder"),
                    entry.get("message_id"),
                )

    with messages_lock:
        messages = [m for m in messages if m not in stale_entries]
        snapshot = list(messages)

    save_messages(snapshot)
