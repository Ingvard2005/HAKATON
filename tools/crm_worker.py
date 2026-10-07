"""Run independently of Streamlit for continuous polling of existing CRM links."""
import sys
import time
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import init_db
from services.crm import sync_cycle

if __name__ == "__main__":
    if os.getenv("CALLMIND_TEST_MODE") == "1":
        raise SystemExit("Тестовый режим: фоновая сеть отключена")
    init_db()
    while True:
        try:
            sync_cycle()
        except Exception:
            pass  # Per-record messages are durable; provider exceptions can contain secrets.
        time.sleep(30)
