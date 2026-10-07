"""Repeat only the explicitly requested synthetic phone notification check."""
import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Explicit --live required")
    os.environ["CALLMIND_DATA_DIR"] = str(ROOT / ".test-data/calendar-live")
    os.environ["CALLMIND_TEST_MODE"] = "1"
    os.environ["GOOGLE_TOKEN_FILE"] = str(Path(args.token_file).resolve())
    from database import init_db, SessionLocal
    from models import Client, Call, Agreement, AgreementDetails
    from services.dates import local_now
    from services.workspace import confirm_task
    from services.settings import get_settings
    from services.integrations import GoogleCalendar, sync_agreement
    from services import calendar_sync as calendar
    init_db()
    due = local_now().replace(second=0, microsecond=0) + timedelta(minutes=4)
    evidence = "Проверим тестовое уведомление без данных клиентов."
    with SessionLocal() as db:
        client = Client(name="Тест уведомления — без данных клиента")
        db.add(client)
        db.flush()
        call = Call(client_id=client.id, call_datetime=local_now(), transcript=evidence, transcript_segments="[]", summary="Повторная техническая проверка")
        db.add(call)
        db.flush()
        item = Agreement(call_id=call.id, description="CallMind · ТЕСТ: повторное уведомление на телефоне", responsible="manager", deadline=due, evidence=evidence, status="pending")
        db.add(item)
        db.flush()
        db.add(AgreementDetails(agreement_id=item.id, date_only=False, kind="meeting", priority="normal"))
        db.commit()
        agreement_id = item.id
    confirm_task(agreement_id)
    sync_agreement(agreement_id, "google")
    row = calendar.rows(agreement_id)[0]
    calendar.save_options(row["key"], 5, 1, True)
    sync_agreement(agreement_id, "google")
    adapter = GoogleCalendar(get_settings())
    raw = calendar.read(adapter, row["external_id"])
    report = dict(agreement_id=agreement_id, event_id=row["external_id"], event_at=due.isoformat(),
                  notification_at=(due-timedelta(minutes=1)).isoformat(),
                  reminders_confirmed=raw["reminders"] == {"useDefault":False,"overrides":[{"method":"popup","minutes":1}]},
                  phone_delivery="Ожидает проверки пользователем")
    (ROOT / ".test-data/calendar-live/notification-repeat.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
