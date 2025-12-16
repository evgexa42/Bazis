import atexit
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import app as bazis_app
import app.config as app_config
from app import heartbeat, measure_time
from app.services import telegram as telegram_service
from app.services.snapshot import remove_order, upsert_order

logger = bazis_app.logger

known_folders = set()
known_folders_lock = threading.Lock()
observer = None
observer_started = False
observer_lock = threading.Lock()
observer_stop_event = threading.Event()
observer_thread: threading.Thread | None = None
programmatic_renames: dict[tuple[str, str], float] = {}
programmatic_renames_lock = threading.Lock()


def _norm_real(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))

def _norm_abs(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))

def _get_watch_roots() -> set[str]:
    # корень берём тот же, на котором висит observer (читаем из config модуля)
    root = app_config.WATCHED_PATH or app_config.FOLDER_PATH
    if not root:
        return set()
    return {_norm_real(root), _norm_abs(root)}

WATCH_ROOTS = _get_watch_roots()

def in_watch_dir(path: str) -> bool:
    if not path or not WATCH_ROOTS:
        return False

    p_real = _norm_real(path)
    p_abs  = _norm_abs(path)

    for root in WATCH_ROOTS:
        try:
            if os.path.commonpath([p_real, root]) == root:
                return True
        except ValueError:
            pass

        try:
            if os.path.commonpath([p_abs, root]) == root:
                return True
        except ValueError:
            pass

    return False


class OrderFolderHandler(FileSystemEventHandler):
    @measure_time("observer_on_created")
    def on_created(self, event):
        if not event.is_directory:
            return
        if not in_watch_dir(event.src_path):
            return
        folder_name = os.path.basename(event.src_path)
        logger.info("[observer] Папка создана: %s", folder_name)
        if register_known_folder(folder_name):
            return
        upsert_order(folder_name)
        if telegram_service.should_notify(folder_name):
            telegram_service.enqueue(
                telegram_service.send_telegram_message,
                telegram_service.build_order_message(folder_name),
                folder_name,
            )

    @measure_time("observer_on_moved")
    def on_moved(self, event):
        if not event.is_directory:
            return

        src_name = os.path.basename(event.src_path)
        dest_name = os.path.basename(event.dest_path)

        if _consume_programmatic_move(event.src_path, event.dest_path):
            logger.debug("[observer] Пропуск служебного переименования: %s -> %s", src_name, dest_name)
            return

        src_in_watch = in_watch_dir(event.src_path)
        dest_in_watch = in_watch_dir(event.dest_path)

        if src_in_watch and not dest_in_watch:
            unregister_known_folder(src_name)
            telegram_service.enqueue(telegram_service.delete_telegram_message, src_name)
            logger.info("[observer] Папка перемещена из каталога: %s", src_name)
            remove_order(src_name)
            return

        if dest_in_watch and not src_in_watch:
            if register_known_folder(dest_name):
                return
            upsert_order(dest_name)
            if telegram_service.should_notify(dest_name):
                telegram_service.enqueue(
                    telegram_service.send_telegram_message,
                    telegram_service.build_order_message(dest_name),
                    dest_name,
                )
            logger.info(
                "[observer] Папка перемещена в каталог или создана: %s -> %s",
                src_name,
                dest_name,
            )
            return

        already_known = move_known_folder(src_name, dest_name)
        logger.info("[observer] Папка переименована: %s -> %s", src_name, dest_name)
        remove_order(src_name)
        upsert_order(dest_name)
        telegram_service.enqueue(
            telegram_service.handle_moved_notification, src_name, dest_name, already_known
        )

    @measure_time("observer_on_deleted")
    def on_deleted(self, event):
        if not event.is_directory:
            return
        if not in_watch_dir(event.src_path):
            return
        folder_name = os.path.basename(event.src_path)
        logger.info("[observer] Папка удалена: %s", folder_name)
        unregister_known_folder(folder_name)
        telegram_service.enqueue(telegram_service.delete_telegram_message, folder_name)
        remove_order(folder_name)


def register_known_folder(folder_name):
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


def register_programmatic_move(src_path: str, dest_path: str, ttl: float = 5.0) -> None:
    """Фиксируем служебное переименование, чтобы игнорировать один соответствующий эвент."""
    now = time.monotonic()
    key = (_norm_real(src_path), _norm_real(dest_path))
    with programmatic_renames_lock:
        programmatic_renames[key] = now + max(1.0, ttl)


def discard_programmatic_move(src_path: str, dest_path: str) -> None:
    key = (_norm_real(src_path), _norm_real(dest_path))
    with programmatic_renames_lock:
        programmatic_renames.pop(key, None)


def _consume_programmatic_move(src_path: str, dest_path: str) -> bool:
    now = time.monotonic()
    key = (_norm_real(src_path), _norm_real(dest_path))

    with programmatic_renames_lock:
        expired = [item for item, exp in programmatic_renames.items() if exp < now]
        for item in expired:
            programmatic_renames.pop(item, None)

        expiry = programmatic_renames.get(key)
        if expiry and expiry >= now:
            programmatic_renames.pop(key, None)
            return True
    return False


def _stop_observer_instance():
    """Останавливает активный observer безопасно."""

    global observer
    with observer_lock:
        current = observer
        observer = None

    if current is None:
        return

    try:
        current.stop()
    except Exception:
        logger.warning("[observer] Ошибка при остановке наблюдателя", exc_info=True)

    try:
        current.join(timeout=5)
    except Exception:
        logger.warning("[observer] Ошибка при ожидании завершения наблюдателя", exc_info=True)


def start_observer_once():
    global observer_started, observer_thread, observer
    if observer_started:
        return
    observer_started = True

    observer_stop_event.clear()

    def stop_observer():
        global observer_started
        observer_started = False
        observer_stop_event.set()
        _stop_observer_instance()

    def observer_worker():
        global observer_started
        backoff = 1.0

        while observer_started and not observer_stop_event.is_set():
            if not app_config.FOLDER_PATH:
                logger.warning("[observer] Путь к папке заказов не настроен. Мониторинг не запущен.")
                break

            try:
                handler = OrderFolderHandler()
                local_observer = Observer()

                # пересчёт корней (на случай если конфиг загрузился позже)
                global WATCH_ROOTS
                WATCH_ROOTS = _get_watch_roots()

                local_observer.schedule(handler, app_config.FOLDER_PATH, recursive=False)

                with observer_lock:
                    observer = local_observer

                local_observer.start()
                logger.info("[observer] Мониторинг запущен: %s", app_config.FOLDER_PATH)
                backoff = 1.0

                while observer_started and not observer_stop_event.is_set():
                    if not local_observer.is_alive():
                        raise RuntimeError("observer thread stopped")
                    heartbeat("watchdog")
                    local_observer.join(timeout=1.5)

            except Exception as exc:
                if not observer_started or observer_stop_event.is_set():
                    break
                logger.warning(
                    "[observer] Мониторинг остановлен, перезапуск через %.1f сек", backoff, exc_info=exc
                )
                _stop_observer_instance()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            _stop_observer_instance()

        observer_started = False
        logger.info("[observer] Мониторинг остановлен")

    observer_thread = threading.Thread(target=observer_worker, daemon=True)
    observer_thread.start()

    atexit.register(stop_observer)


def initialize_known_state():
    from app.services.telegram import cleanup_missing_messages, ensure_message_for_folder
    from app.services.telegram import delete_telegram_message, order_key_from_name, should_notify

    if not app_config.FOLDER_PATH or not os.path.isdir(app_config.FOLDER_PATH):
        logger.warning("[init] Путь не найден: %s", app_config.FOLDER_PATH)
        return

    actual_folders = []
    try:
        with os.scandir(app_config.FOLDER_PATH) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                name = entry.name
                if telegram_service.is_folder_ignored(name):
                    continue
                actual_folders.append(name)
    except Exception as exc:
        logger.exception("[init] Не удалось прочитать каталог", exc_info=exc)
        return

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