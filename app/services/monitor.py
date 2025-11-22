import atexit
import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import app as bazis_app
from app.services import telegram as telegram_service

logger = bazis_app.logger

known_folders = set()
known_folders_lock = threading.Lock()
observer = None
observer_started = False


class OrderFolderHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            return
        parent = os.path.normcase(os.path.abspath(os.path.dirname(event.src_path)))
        if parent != bazis_app.WATCHED_PATH_NORM:
            return
        folder_name = os.path.basename(event.src_path)
        logger.info("[observer] Папка создана: %s", folder_name)
        if register_known_folder(folder_name):
            return
        if telegram_service.should_notify(folder_name):
            telegram_service.send_telegram_message(
                telegram_service.build_order_message(folder_name), folder_name
            )

    def on_moved(self, event):
        if not event.is_directory:
            return

        src_name = os.path.basename(event.src_path)
        dest_name = os.path.basename(event.dest_path)

        src_in_watch = (
            os.path.normcase(os.path.abspath(os.path.dirname(event.src_path)))
            == bazis_app.WATCHED_PATH_NORM
        )
        dest_in_watch = (
            os.path.normcase(os.path.abspath(os.path.dirname(event.dest_path)))
            == bazis_app.WATCHED_PATH_NORM
        )

        if src_in_watch and not dest_in_watch:
            unregister_known_folder(src_name)
            telegram_service.delete_telegram_message(src_name)
            logger.info("[observer] Папка перемещена из каталога: %s", src_name)
            return

        if dest_in_watch and not src_in_watch:
            if register_known_folder(dest_name):
                return
            if telegram_service.should_notify(dest_name):
                telegram_service.send_telegram_message(
                    telegram_service.build_order_message(dest_name), dest_name
                )
            logger.info(
                "[observer] Папка перемещена в каталог или создана: %s -> %s",
                src_name,
                dest_name,
            )
            return

        already_known = move_known_folder(src_name, dest_name)
        logger.info("[observer] Папка переименована: %s -> %s", src_name, dest_name)

        if telegram_service.folder_has_ready_marker(dest_name):
            telegram_service.delete_telegram_message(dest_name)
            return

        if telegram_service.update_message_for_folder(src_name, dest_name):
            return

        if not already_known and telegram_service.should_notify(dest_name):
            telegram_service.send_telegram_message(
                telegram_service.build_order_message(dest_name), dest_name
            )

    def on_deleted(self, event):
        if not event.is_directory:
            return
        parent = os.path.normcase(os.path.abspath(os.path.dirname(event.src_path)))
        if parent != bazis_app.WATCHED_PATH_NORM:
            return
        folder_name = os.path.basename(event.src_path)
        logger.info("[observer] Папка удалена: %s", folder_name)
        unregister_known_folder(folder_name)
        telegram_service.delete_telegram_message(folder_name)


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


def start_observer_once():
    global observer_started, observer
    if observer_started:
        return
    observer_started = True

    if not bazis_app.FOLDER_PATH:
        logger.warning("[observer] Путь к папке заказов не настроен. Мониторинг не запущен.")
        return

    logger.info("[observer] Запуск мониторинга папки заказов: %s", bazis_app.FOLDER_PATH)
    handler = OrderFolderHandler()
    observer = Observer()
    observer.schedule(handler, bazis_app.FOLDER_PATH, recursive=False)
    observer.start()

    def stop_observer():
        if observer is not None:
            logger.info("[observer] Остановка мониторинга")
            observer.stop()
            observer.join(timeout=5)

    atexit.register(stop_observer)


def initialize_known_state():
    from app.services.telegram import cleanup_missing_messages, ensure_message_for_folder
    from app.services.telegram import delete_telegram_message, order_key_from_name, should_notify

    if not bazis_app.FOLDER_PATH or not os.path.isdir(bazis_app.FOLDER_PATH):
        logger.warning("[init] Путь не найден: %s", bazis_app.FOLDER_PATH)
        return

    actual_folders = []
    try:
        with os.scandir(bazis_app.FOLDER_PATH) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                name = entry.name
                if name in telegram_service.IGNORED_FOLDERS:
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