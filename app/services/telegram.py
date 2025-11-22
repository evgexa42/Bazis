import threading
from typing import List, Optional

from telegram import Bot

import app as bazis_app
from app import get_manager_from_name, logger
from app.dal.json_store import load_json_file, save_json_atomic

bot: Optional[Bot] = None
messages: List[dict] = []
messages_lock = threading.Lock()
messages_file_path: Optional[str] = None

IGNORED_FOLDERS = {"Архив", "2025"}


def init_bot(token: str) -> None:
    global bot
    bot = Bot(token=token) if token else None


def load_messages_storage(path: str) -> None:
    global messages_file_path, messages
    messages_file_path = path

    default_messages: List[dict] = []
    try:
        data = load_json_file(path)
    except Exception as exc:
        logger.exception("[load_messages] Ошибка чтения %s", path, exc_info=exc)
        data = None

    if isinstance(data, list):
        cleaned = []
        for entry in data:
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
        messages = cleaned
    else:
        messages = default_messages

    if data is None:
        save_messages(messages)


def save_messages(msgs: List[dict]) -> None:
    if not messages_file_path:
        return
    try:
        save_json_atomic(messages_file_path, msgs)
    except Exception as exc:
        logger.exception("[save_messages] Ошибка записи %s", messages_file_path, exc_info=exc)


def folder_has_ready_marker(folder_name: str) -> bool:
    if not bazis_app.TECHNOLOGIST_MARKERS:
        return False
    for marker in bazis_app.TECHNOLOGIST_MARKERS:
        if f"[{marker}]" in folder_name:
            return True
    return False


def technologist_from_folder(folder_name: str) -> str:
    for marker, name in bazis_app.TECHNOLOGIST_MARKERS.items():
        if f"[{marker}]" in folder_name:
            return name
    return "Неизвестно"


def should_notify(folder_name: str) -> bool:
    if not folder_name or folder_name.startswith("."):
        return False
    if folder_name in IGNORED_FOLDERS:
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
    if bot is None or not bazis_app.CHAT_ID:
        logger.warning("[TG] Бот не настроен. Сообщение не отправлено для %s", folder_name)
        return
    try:
        sent = bot.send_message(chat_id=bazis_app.CHAT_ID, text=msg)
        entry = {
            "folder": folder_name,
            "order_key": order_key_from_name(folder_name),
            "chat_id": bazis_app.CHAT_ID,
            "message_id": sent.message_id,
        }
        with messages_lock:
            messages.append(entry)
            save_messages(messages)
        logger.info(
            "[TG] Сообщение отправлено и сохранено для %s -> id %s",
            folder_name,
            sent.message_id,
        )
    except Exception as exc:
        logger.exception("[Telegram Error] %s", exc)


def delete_telegram_message(folder_name: str) -> None:
    global messages
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
        save_messages(messages)


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
    if bot is None:
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
            save_messages(messages)
    return True


def ensure_message_for_folder(folder_name: str) -> None:
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
        save_messages(messages)