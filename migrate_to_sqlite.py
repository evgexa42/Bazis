from pathlib import Path

from app.dal.database import replace_clients, replace_messages
from app.dal.db import init_db
from app.dal.json_store import load_json_file
from app.paths import get_base_dir

BASE_DIR = Path(get_base_dir())
CLIENTS_JSON = BASE_DIR / "clients.json"
MESSAGES_JSON = BASE_DIR / "messages.json"


def migrate():
    init_db()

    clients_data = load_json_file(str(CLIENTS_JSON)) if CLIENTS_JSON.exists() else {}
    if isinstance(clients_data, dict):
        replace_clients(clients_data)

    messages_data = load_json_file(str(MESSAGES_JSON)) if MESSAGES_JSON.exists() else []
    telegram_entries = []
    if isinstance(messages_data, list):
        for entry in messages_data:
            if isinstance(entry, dict):
                telegram_entries.append(entry)
    replace_messages(telegram_entries, level="telegram")

    print("Migration completed. Database populated from JSON files.")


if __name__ == "__main__":
    migrate()