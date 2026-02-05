import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from telegram import Bot

import app as bazis_app
import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.database import load_messages as load_messages_from_db
from app.dal.database import replace_messages as replace_messages_in_db

bot: Optional[Bot] = None
messages: List[dict] = []
messages_lock = threading.Lock()
tg_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_disabled_warning_logged = False
_command_worker_started = False
_command_lock = threading.Lock()
_last_update_id: Optional[int] = None
_command_allowed_updates = ["message"]
_tz_local = ZoneInfo("Europe/Chisinau")


def _get_chat_ids() -> list[str]:
    chat_ids = [str(item).strip() for item in app_config.TELEGRAM_CHAT_IDS if str(item).strip()]
    if not chat_ids and app_config.CHAT_ID:
        chat_ids = [str(app_config.CHAT_ID).strip()]
    seen = set()
    unique = []
    for item in chat_ids:
        if item in seen:
            continue
        unique.append(item)
        seen.add(item)
    return unique


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


def start_command_worker_once():
    global _command_worker_started
    if _command_worker_started:
        return
    with _command_lock:
        if _command_worker_started:
            return
        threading.Thread(target=_poll_updates_loop, daemon=True).start()
        _command_worker_started = True


def init_bot(token: str) -> None:
    global bot, _disabled_warning_logged
    _disabled_warning_logged = False
    if token and _get_chat_ids():
        bot = _create_bot(token)
        start_worker_once()
        start_command_worker_once()
    else:
        bot = None


def _parse_iso_to_local(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_tz_local)


def _format_timestamp(value: Optional[str], fallback_ts: Optional[float]) -> str:
    dt = _parse_iso_to_local(value) if value else None
    if not dt and fallback_ts is not None:
        try:
            dt = datetime.fromtimestamp(fallback_ts, tz=_tz_local)
        except (OSError, ValueError):
            dt = None
    return dt.strftime("%d.%m.%Y %H:%M") if dt else ""


def _format_counts(title: str, stats: dict, limit: int = 6) -> list[str]:
    items = [(name, count) for name, count in stats.items() if count]
    items.sort(key=lambda item: (-item[1], item[0]))
    lines = [title]
    if not items:
        lines.append("• Нет данных")
        return lines
    for name, count in items[:limit]:
        lines.append(f"• {name}: {count}")
    if limit and len(items) > limit:
        remainder = sum(count for _, count in items[limit:])
        lines.append(f"• Остальные: {remainder}")
    return lines


def _build_status_message() -> str:
    from app.services import snapshot as snapshot_service

    payload = snapshot_service.build_orders_payload()
    folders = payload.get("orders") or []

    total = payload.get("total", len(folders))
    done_count = sum(1 for item in folders if item.get("status") in {"Готов", "Подтвержден"})
    processed_count = sum(
        1 for item in folders if item.get("is_processed") or item.get("has_technologist")
    )
    confirmed_count = sum(
        1 for item in folders if item.get("is_approved") or item.get("status") == "Подтвержден"
    )
    new_count = sum(1 for item in folders if item.get("status") == "Новый")

    lines = [
        "📊 Статус",
        f"Всего заказов: {total}",
        f"Готовых: {done_count}",
        f"Обработано технологом: {processed_count}",
        f"Подтверждено: {confirmed_count}",
        f"Новых: {new_count}",
        "",
    ]
    lines.extend(_format_counts("👤 Менеджеры:", payload.get("managers") or {}))
    lines.append("")
    lines.extend(_format_counts("🛠 Технологи:", payload.get("technologists") or {}))
    return "\n".join(lines).strip()


def _build_new_orders_message(limit: int = 30) -> str:
    from app.services import snapshot as snapshot_service

    payload = snapshot_service.build_orders_payload()
    folders = payload.get("orders") or []
    new_orders = [item for item in folders if item.get("status") == "Новый"]

    def sort_key(item: dict) -> float:
        created_at = item.get("created_at")
        parsed = _parse_iso_to_local(created_at) if created_at else None
        if parsed:
            return parsed.timestamp()
        return float(item.get("mtime_ts") or 0.0)

    new_orders.sort(key=sort_key, reverse=True)

    total_new = len(new_orders)
    limited = new_orders[:max(1, min(50, limit))]

    lines = [f"🆕 Новые заказы: {total_new}"]
    if not limited:
        lines.append("Нет новых заказов.")
        return "\n".join(lines)

    for item in limited:
        name = item.get("display_name") or item.get("name") or ""
        manager = item.get("manager") or "Неизвестно"
        created = _format_timestamp(item.get("created_at"), item.get("mtime_ts"))
        created_part = f" | {created}" if created else ""
        lines.append(f"• {name} | {manager}{created_part}")

    if total_new > len(limited):
        lines.append(f"…ещё {total_new - len(limited)}")
    return "\n".join(lines)


def _handle_command(command: str) -> Optional[str]:
    command = (command or "").strip().lower()
    if command == "/status":
        return _build_status_message()
    if command == "/new":
        return _build_new_orders_message()
    return None


def _poll_updates_loop() -> None:
    global _last_update_id

    while True:
        if bot is None or not _get_chat_ids():
            time.sleep(2.0)
            continue
        try:
            updates = bot.get_updates(
                offset=_last_update_id,
                timeout=30,
                allowed_updates=_command_allowed_updates,
            )
        except Exception as exc:  # pragma: no cover - защитное логирование
            logger.warning("[TG] Ошибка получения обновлений", exc_info=exc)
            time.sleep(2.0)
            continue

        for update in updates:
            try:
                _last_update_id = (update.update_id or 0) + 1
                message = getattr(update, "message", None)
                if not message:
                    continue
                chat_id = getattr(message, "chat_id", None)
                text = (getattr(message, "text", "") or "").strip()
                if not chat_id or not text:
                    continue
                if str(chat_id) not in _get_chat_ids():
                    continue
                command = text.split()[0]
                if "@" in command:
                    command = command.split("@", 1)[0]
                response = _handle_command(command)
                if response:
                    bot.send_message(chat_id=chat_id, text=response)
            except Exception as exc:  # pragma: no cover - защитная логика
                logger.warning("[TG] Ошибка обработки команды", exc_info=exc)
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
        _log_disabled_once()
        return
    try:
        entries = []
        for chat_id in chat_ids:
            try:
                sent = bot.send_message(chat_id=chat_id, text=msg)
            except Exception as exc:
                logger.exception("[Telegram Error] %s", exc)
                continue
            entry = {
                "folder": folder_name,
                "order_key": order_key_from_name(folder_name),
                "chat_id": chat_id,
                "message_id": sent.message_id,
            }
            entries.append(entry)
            logger.info(
                "[TG] Сообщение отправлено и сохранено для %s -> id %s",
                folder_name,
                sent.message_id,
            )

        if not entries:
            return
        with messages_lock:
            messages.extend(entries)
            snapshot = list(messages)
        save_messages(snapshot)
    except Exception as exc:
        logger.exception("[Telegram Error] %s", exc)


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


def find_message_entries(old_name: str, new_name: Optional[str] = None) -> list[dict]:
    keys = set()
    if old_name:
        keys.add(order_key_from_name(old_name))
    if new_name:
        keys.add(order_key_from_name(new_name))

    matched = []
    with messages_lock:
        for entry in messages:
            if entry.get("folder") == old_name:
                matched.append(entry)
                continue
            if keys and entry.get("order_key") in keys:
                matched.append(entry)
    return matched


def update_message_for_folder(old_name: str, new_name: str) -> bool:
    new_text = build_order_message(new_name)
    if not _telegram_configured():
        return False

    entries = find_message_entries(old_name, new_name)
    if not entries:
        return False

    updated = False
    for entry in entries:
        chat_id = entry.get("chat_id")
        message_id = entry.get("message_id")
        if chat_id is None or message_id is None:
            continue
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
            logger.info("[TG] Сообщение %s обновлено для %s", message_id, new_name)
            updated = True
        except Exception as exc:
            logger.exception("[TG edit error] %s (folder=%s -> %s)", exc, old_name, new_name)

    with messages_lock:
        for entry in entries:
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
        entries = [entry for entry in messages if entry.get("order_key") == key]
        current_name = entries[0].get("folder") if entries else None

    if not entries:
        send_telegram_message(build_order_message(folder_name), folder_name)
        return

    if current_name != folder_name:
        update_message_for_folder(current_name or folder_name, folder_name)
        return

    existing_chat_ids = {str(entry.get("chat_id")) for entry in entries if entry.get("chat_id")}
    missing_chat_ids = [chat_id for chat_id in _get_chat_ids() if chat_id not in existing_chat_ids]
    if not missing_chat_ids:
        return

    for chat_id in missing_chat_ids:
        try:
            sent = bot.send_message(chat_id=chat_id, text=build_order_message(folder_name))
        except Exception as exc:
            logger.exception("[Telegram Error] %s", exc)
            continue
        entry = {
            "folder": folder_name,
            "order_key": key,
            "chat_id": chat_id,
            "message_id": sent.message_id,
        }
        with messages_lock:
            messages.append(entry)
            snapshot = list(messages)
        save_messages(snapshot)


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
