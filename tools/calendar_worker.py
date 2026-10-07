"""Persistent polling of linked Google events with the same data/config as CallMind."""
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import init_db
from services.calendar_sync import sync_cycle

if __name__ == "__main__":
    if os.getenv("CALLMIND_TEST_MODE") == "1":
        raise SystemExit("Тестовый режим: фоновая сеть отключена")
    init_db()
    while True:
        try:
            sync_cycle()
        except Exception:
            pass  # Never expose provider exceptions containing credentials.
        time.sleep(30)
