import asyncio
import inspect
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import app as bazis_app
import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.order_manager_overrides import get_overrides_map
from app.services.order_times import (
    ensure_order_times_table,
    get_sqlite_connection,
    normalize_order_key,
)
from app.dal.database import load_messages as load_messages_from_db
from app.dal.database import replace_messages as replace_messages_in_db

bot: Optional[Bot] = None
messages: List[dict] = []
messages_lock = threading.Lock()
tg_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_disabled_warning_logged = False


def _telegram_configured() -> bool:
    return bot is not None and bool(app_config.CHAT_IDS)


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


def _resolve_awaitable(result):
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


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
_updates_started = False
_updates_lock = threading.Lock()
_last_update_id = 0

def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True).start()
        _worker_started = True


def start_updates_worker_once():
    global _updates_started
    if _updates_started:
        return
    with _updates_lock:
        if _updates_started:
            return
        threading.Thread(target=_updates_worker, daemon=True).start()
        _updates_started = True

def init_bot(token: str) -> None:
    global bot, _disabled_warning_logged
    _disabled_warning_logged = False
    if token and app_config.CHAT_IDS:
        bot = _create_bot(token)
        start_worker_once()
        start_updates_worker_once()
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


def _resolve_manager_name(folder_name: str) -> str:
    order_key = normalize_order_key(folder_name)
    override = get_overrides_map([order_key]).get(order_key) if order_key else None
    return override or get_manager_from_name(folder_name)


def build_order_message(folder_name: str) -> str:
    manager = _resolve_manager_name(folder_name)
    return f"📁 Новый заказ: {folder_name}\n👤 Менеджер: {manager}"


def _send_message_to_chat(chat_id: str, msg: str, folder_name: str) -> Optional[int]:
    global messages
    try:
        sent = _resolve_awaitable(bot.send_message(chat_id=chat_id, text=msg))
        entry = {
            "folder": folder_name,
            "order_key": order_key_from_name(folder_name),
            "chat_id": chat_id,
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
        return sent.message_id
    except Exception as exc:
        logger.exception("[Telegram Error] %s", exc)
        return None


def send_telegram_message(msg: str, folder_name: str) -> None:
    global messages
    if not _telegram_configured():
        _log_disabled_once()
        return
    for chat_id in list(app_config.CHAT_IDS):
        _send_message_to_chat(chat_id, msg, folder_name)


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
                _resolve_awaitable(bot.delete_message(chat_id=entry["chat_id"], message_id=entry["message_id"]))
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


def find_message_entries(old_name: str, new_name: Optional[str] = None) -> List[dict]:
    keys = set()
    if old_name:
        keys.add(order_key_from_name(old_name))
    if new_name:
        keys.add(order_key_from_name(new_name))

    matches = []
    with messages_lock:
        for entry in messages:
            if entry.get("folder") == old_name:
                matches.append(entry)
            elif keys and entry.get("order_key") in keys:
                matches.append(entry)
    return matches


def update_message_for_folder(old_name: str, new_name: str) -> bool:
    entries = find_message_entries(old_name, new_name)
    if not entries:
        return False

    new_text = build_order_message(new_name)
    if not _telegram_configured():
        return False

    updated_any = False
    for entry in entries:
        chat_id = entry.get("chat_id")
        message_id = entry.get("message_id")
        if chat_id is None or message_id is None:
            continue
        try:
            _resolve_awaitable(bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text))
            logger.info("[TG] Сообщение %s обновлено для %s", message_id, new_name)
            updated_any = True
        except Exception as exc:
            logger.exception("[TG edit error] %s (folder=%s -> %s)", exc, old_name, new_name)

    with messages_lock:
        for entry in entries:
            if entry in messages:
                entry["folder"] = new_name
                entry["order_key"] = order_key_from_name(new_name)
        snapshot = list(messages)

    save_messages(snapshot)
    return updated_any


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
    existing_entries = []
    with messages_lock:
        for entry in messages:
            if entry.get("order_key") == key:
                existing_entries.append(entry)

    if not existing_entries:
        send_telegram_message(build_order_message(folder_name), folder_name)
        return

    current_name = next((entry.get("folder") for entry in existing_entries if entry.get("folder")), None)
    if current_name and current_name != folder_name:
        update_message_for_folder(current_name, folder_name)

    existing_chat_ids = {entry.get("chat_id") for entry in existing_entries}
    for chat_id in app_config.CHAT_IDS:
        if chat_id in existing_chat_ids:
            continue
        _send_message_to_chat(chat_id, build_order_message(folder_name), folder_name)


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
                _resolve_awaitable(
                    bot.delete_message(chat_id=entry.get("chat_id"), message_id=entry.get("message_id"))
                )
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


def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Статус", callback_data="status"),
                InlineKeyboardButton("🆕 Новые", callback_data="new_orders"),
            ],
            [InlineKeyboardButton("⏳ В работе", callback_data="stuck")],
        ]
    )


def _is_allowed_chat(chat_id: int | str | None) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) in {str(item) for item in app_config.CHAT_IDS}


def _send_menu(chat_id: str) -> None:
    if not _telegram_configured():
        return
    _resolve_awaitable(
        bot.send_message(chat_id=chat_id, text="Меню Order Monitor:", reply_markup=_menu_markup())
    )


def _format_dt(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "—"
    return parsed.astimezone(timezone.utc).strftime("%d.%m %H:%M")


def _fetch_counts() -> dict:
    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        total = conn.execute("SELECT COUNT(*) FROM order_times").fetchone()[0] or 0
        processed = (
            conn.execute("SELECT COUNT(*) FROM order_times WHERE processed_at IS NOT NULL").fetchone()[0]
            or 0
        )
        confirmed = (
            conn.execute("SELECT COUNT(*) FROM order_events WHERE action = ?", ("manager_confirmed",)).fetchone()[0]
            or 0
        )
    finally:
        conn.close()
    return {"total": total, "processed": processed, "confirmed": confirmed}


def _fetch_grouped(action: str, limit: int = 6) -> list[tuple[str, int]]:
    conn = get_sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT manager, COUNT(*) as cnt
            FROM order_events
            WHERE action = ?
            GROUP BY manager
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (action, limit),
        ).fetchall()
        return [(row[0] or "Неизвестно", int(row[1] or 0)) for row in rows]
    finally:
        conn.close()


def _fetch_recent_orders(limit: int = 25) -> list[dict]:
    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        rows = conn.execute(
            """
            SELECT order_key, created_at, last_display_name
            FROM order_times
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {"order_key": row[0], "created_at": row[1], "display_name": row[2]}
            for row in rows
        ]
    finally:
        conn.close()


def _fetch_stuck_orders(limit: int = 25, days: int = 2) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        rows = conn.execute(
            """
            SELECT order_key, created_at, last_display_name
            FROM order_times
            WHERE processed_at IS NULL AND created_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [
            {"order_key": row[0], "created_at": row[1], "display_name": row[2]}
            for row in rows
        ]
    finally:
        conn.close()


def _build_status_text() -> str:
    counts = _fetch_counts()
    managers = _fetch_grouped("manager_confirmed")
    technologists = _fetch_grouped("technologist_processed")

    manager_lines = "\n".join(f"- {name}: {count}" for name, count in managers) or "—"
    tech_lines = "\n".join(f"- {name}: {count}" for name, count in technologists) or "—"

    return (
        "📊 Статус\n"
        f"Всего заказов: {counts['total']}\n"
        f"Готовых: {counts['processed']}\n"
        f"Обработано технологом: {counts['processed']}\n"
        f"Подтверждено: {counts['confirmed']}\n\n"
        "По менеджерам:\n"
        f"{manager_lines}\n\n"
        "По технологам:\n"
        f"{tech_lines}"
    )


def _build_new_orders_text() -> str:
    items = _fetch_recent_orders()
    if not items:
        return "🆕 Новые\nНет новых заказов."

    keys = [item["order_key"] for item in items if item.get("order_key")]
    overrides = get_overrides_map(keys)
    lines = []
    for item in items:
        order_key = item.get("order_key") or "—"
        display_name = item.get("display_name") or ""
        manager = overrides.get(order_key) or get_manager_from_name(display_name or order_key)
        created = _format_dt(item.get("created_at") or "")
        lines.append(f"{order_key} · {manager} · {created}")
    return "🆕 Новые\n" + "\n".join(lines)


def _build_stuck_orders_text() -> str:
    items = _fetch_stuck_orders()
    if not items:
        return "⏳ В работе\nНет зависших заказов."

    lines = []
    for item in items:
        order_key = item.get("order_key") or "—"
        created = _format_dt(item.get("created_at") or "")
        lines.append(f"{order_key} · {created}")
    return "⏳ В работе\n" + "\n".join(lines)


def _handle_update(update) -> None:
    if update.message and update.message.text:
        chat_id = update.message.chat_id
        if not _is_allowed_chat(chat_id):
            return
        text = update.message.text.strip().lower()
        if text in {"/start", "/menu", "menu"}:
            _send_menu(str(chat_id))
            return

    if update.callback_query:
        chat_id = update.callback_query.message.chat_id if update.callback_query.message else None
        if not _is_allowed_chat(chat_id):
            return
        data = (update.callback_query.data or "").strip().lower()
        try:
            _resolve_awaitable(bot.answer_callback_query(update.callback_query.id))
        except Exception:
            logger.debug("[TG] Не удалось ответить на callback", exc_info=True)
        if data == "status":
            _resolve_awaitable(bot.send_message(chat_id=chat_id, text=_build_status_text()))
        elif data == "new_orders":
            _resolve_awaitable(bot.send_message(chat_id=chat_id, text=_build_new_orders_text()))
        elif data == "stuck":
            _resolve_awaitable(bot.send_message(chat_id=chat_id, text=_build_stuck_orders_text()))


def _updates_worker():
    global _last_update_id
    while True:
        if not _telegram_configured():
            time.sleep(5)
            continue
        try:
            updates = _resolve_awaitable(bot.get_updates(offset=_last_update_id + 1, timeout=20))
            for update in updates:
                _last_update_id = max(_last_update_id, update.update_id)
                _handle_update(update)
        except Exception as exc:  # pragma: no cover - защитное логирование
            logger.warning("[TG] Ошибка получения обновлений", exc_info=exc)
            time.sleep(3)
