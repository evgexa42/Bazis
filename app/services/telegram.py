import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime
from typing import List, Optional

from telegram import Bot

import app as bazis_app
import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.db import DB_PATH
from app.dal.external_orders import load_status_map
from app.dal.database import load_messages as load_messages_from_db
from app.dal.database import replace_messages as replace_messages_in_db

bot: Optional[Bot] = None
messages: List[dict] = []
messages_lock = threading.Lock()
tg_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_disabled_warning_logged = False
_polling_started = False
_polling_lock = threading.Lock()
_last_update_id: Optional[int] = None

MAX_NEW_ORDERS = 30


def _telegram_configured() -> bool:
    return bot is not None and bool(_get_chat_ids())


def _log_disabled_once() -> None:
    global _disabled_warning_logged
    if _disabled_warning_logged:
        return
    _disabled_warning_logged = True
    logger.warning("[TG] Бот не настроен: пропускаем задачи отправки.")


def _ensure_tls_bundle() -> None:
    """Гарантирует доступ к корневым сертификатам в frozen-билде.

    PyInstaller иногда теряет путь до certifi, поэтому принудительно прописываем
    переменные окружения, если они ещё не заданы.
    """

    if os.environ.get("SSL_CERT_FILE") and os.environ.get("REQUESTS_CA_BUNDLE"):
        return

    try:
        import certifi  # локальный импорт во избежание лишней зависимости при старте

        cafile = certifi.where()
        if cafile and os.path.exists(cafile):
            os.environ.setdefault("SSL_CERT_FILE", cafile)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
            logger.info("[TG] SSL_CERT_FILE/REQUESTS_CA_BUNDLE установлен: %s", cafile)
    except Exception as exc:  # pragma: no cover - защитное логирование
        logger.warning("[TG] Не удалось указать сертификаты TLS", exc_info=exc)


def _create_bot(token: str) -> Optional[Bot]:
    _ensure_tls_bundle()
    try:
        return Bot(token=token)
    except Exception as exc:  # pragma: no cover - логируем сбой и отключаем бота
        logger.exception("[TG] Не удалось инициализировать Bot", exc_info=exc)
        return None


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


def _get_chat_ids() -> list[str]:
    chat_ids = list(app_config.TELEGRAM_CHAT_IDS or [])
    if not chat_ids and app_config.CHAT_ID:
        chat_ids = [app_config.CHAT_ID]
    return chat_ids


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_datetime(value: Optional[str]) -> str:
    parsed = _parse_iso_datetime(value)
    if not parsed:
        return value or ""
    return parsed.strftime("%Y-%m-%d %H:%M")


def _fetch_order_times(limit: Optional[int] = None) -> list[dict]:
    rows: list[dict] = []
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        cur = conn.cursor()
        # Читаем данные только из SQLite (никакой файловой логики).
        sql = (
            "SELECT order_key, created_at, processed_at, last_display_name "
            "FROM order_times ORDER BY created_at DESC"
        )
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        for order_key, created_at, processed_at, last_display_name in cur.execute(sql, params).fetchall():
            rows.append(
                {
                    "order_key": order_key,
                    "created_at": created_at,
                    "processed_at": processed_at,
                    "last_display_name": last_display_name,
                }
            )
    except sqlite3.OperationalError:
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    return rows


def _build_status_message() -> str:
    orders = _fetch_order_times()
    status_map = load_status_map()
    total = len(orders)
    ready_count = 0
    processed_count = 0
    confirmed_count = 0
    manager_counts: dict[str, int] = {}
    tech_counts: dict[str, int] = {}

    for row in orders:
        display_name = row.get("last_display_name") or ""
        order_key = row.get("order_key") or ""
        processed_at = row.get("processed_at")
        status = status_map.get(order_key)

        is_cancelled = bool(status and status.cancelled)
        is_confirmed = bool(status and status.approved and not is_cancelled)
        has_ready_marker = folder_has_ready_marker(display_name)

        if processed_at:
            processed_count += 1
        if is_confirmed:
            confirmed_count += 1
        if has_ready_marker and not is_confirmed and not is_cancelled:
            ready_count += 1

        manager = get_manager_from_name(display_name) or "Неизвестно"
        manager_counts[manager] = manager_counts.get(manager, 0) + 1

        technologist = technologist_from_folder(display_name) or "Неизвестно"
        tech_counts[technologist] = tech_counts.get(technologist, 0) + 1

    def format_top_counts(title: str, items: dict[str, int], limit: int = 5) -> list[str]:
        if not items:
            return [f"{title}: нет данных"]
        sorted_items = sorted(items.items(), key=lambda item: item[1], reverse=True)
        lines = [title + ":"]
        for name, count in sorted_items[:limit]:
            lines.append(f"• {name}: {count}")
        if len(sorted_items) > limit:
            lines.append(f"… ещё {len(sorted_items) - limit}")
        return lines

    lines = [
        "📊 Статус",
        f"Всего заказов: {total}",
        f"Готовых: {ready_count}",
        f"Обработанных технологом: {processed_count}",
        f"Подтверждённых: {confirmed_count}",
        "",
    ]
    lines.extend(format_top_counts("Менеджеры", manager_counts))
    lines.append("")
    lines.extend(format_top_counts("Технологи", tech_counts))
    return "\n".join(lines).strip()


def _build_new_orders_message(limit: int = MAX_NEW_ORDERS) -> str:
    orders = _fetch_order_times()
    status_map = load_status_map()
    lines = ["🆕 Новые заказы"]
    count = 0

    for row in orders:
        if count >= limit:
            break
        display_name = row.get("last_display_name") or ""
        order_key = row.get("order_key") or ""
        if not order_key:
            continue
        status = status_map.get(order_key)

        is_cancelled = bool(status and status.cancelled)
        is_confirmed = bool(status and status.approved and not is_cancelled)
        has_ready_marker = folder_has_ready_marker(display_name)

        if is_cancelled or is_confirmed or has_ready_marker:
            continue

        created_at = _format_datetime(row.get("created_at"))
        manager = get_manager_from_name(display_name) or "Неизвестно"
        lines.append(f"• {order_key} | {manager} | {created_at}")
        count += 1

    if count == 0:
        lines.append("Нет новых заказов.")

    return "\n".join(lines)


def _handle_command(text: str, chat_id: str) -> None:
    command = text.strip().split()[0] if text else ""
    if not command.startswith("/"):
        return
    if "@" in command:
        command = command.split("@", 1)[0]
    command = command.lower()

    if command == "/status":
        response = _build_status_message()
    elif command == "/new":
        response = _build_new_orders_message()
    else:
        return

    try:
        bot.send_message(chat_id=chat_id, text=response)
    except Exception as exc:
        logger.exception("[TG command] Ошибка отправки ответа (%s)", exc)


def _poll_updates() -> None:
    global _last_update_id
    while True:
        if not _telegram_configured():
            time.sleep(5)
            continue
        try:
            # Long polling, чтобы не перегружать API Telegram.
            updates = bot.get_updates(offset=_last_update_id, timeout=20)
        except Exception as exc:  # pragma: no cover - сеть/ошибки API
            logger.exception("[TG polling] Ошибка получения обновлений", exc_info=exc)
            time.sleep(5)
            continue

        for update in updates:
            _last_update_id = update.update_id + 1
            message = getattr(update, "message", None)
            if not message:
                continue
            text = getattr(message, "text", None)
            if not text:
                continue
            chat_id = str(getattr(message, "chat_id", "")).strip()
            if not chat_id:
                continue
            if chat_id not in _get_chat_ids():
                continue
            _handle_command(text, chat_id)


def start_polling_once() -> None:
    global _polling_started
    if _polling_started:
        return
    with _polling_lock:
        if _polling_started:
            return
        threading.Thread(target=_poll_updates, daemon=True).start()
        _polling_started = True


def init_bot(token: str) -> None:
    global bot, _disabled_warning_logged
    _disabled_warning_logged = False
    if token and _get_chat_ids():
        bot = _create_bot(token)
        start_worker_once()
        start_polling_once()
    else:
        bot = None


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
    chat_ids = _get_chat_ids()
    if not chat_ids:
        return

    new_entries: list[dict] = []
    for chat_id in chat_ids:
        try:
            sent = bot.send_message(chat_id=chat_id, text=msg)
        except Exception as exc:
            logger.exception("[Telegram Error] %s", exc)
            continue
        new_entries.append(
            {
                "folder": folder_name,
                "order_key": order_key_from_name(folder_name),
                "chat_id": chat_id,
                "message_id": sent.message_id,
            }
        )
        logger.info(
            "[TG] Сообщение отправлено и сохранено для %s -> id %s",
            folder_name,
            sent.message_id,
        )

    if not new_entries:
        return
    with messages_lock:
        messages.extend(new_entries)
        snapshot = list(messages)
    save_messages(snapshot)


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


def _find_message_entries(old_name: str, new_name: Optional[str] = None) -> List[dict]:
    keys = set()
    if old_name:
        keys.add(order_key_from_name(old_name))
    if new_name:
        keys.add(order_key_from_name(new_name))

    with messages_lock:
        return [
            entry
            for entry in messages
            if entry.get("folder") == old_name
            or (keys and entry.get("order_key") in keys)
        ]


def update_message_for_folder(old_name: str, new_name: str) -> bool:
    entries = _find_message_entries(old_name, new_name)
    if not entries:
        return False

    new_text = build_order_message(new_name)
    if not _telegram_configured():
        return False

    updated = False
    updated_entries: list[dict] = []
    for entry in entries:
        chat_id = entry.get("chat_id")
        message_id = entry.get("message_id")
        if chat_id is None or message_id is None:
            continue
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
            logger.info("[TG] Сообщение %s обновлено для %s", message_id, new_name)
            updated_entries.append(entry)
            updated = True
        except Exception as exc:
            logger.exception("[TG edit error] %s (folder=%s -> %s)", exc, old_name, new_name)

    with messages_lock:
        for entry in updated_entries:
            if entry in messages:
                entry["folder"] = new_name
                entry["order_key"] = order_key_from_name(new_name)
        snapshot = list(messages)

    save_messages(snapshot)
    return updated


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
