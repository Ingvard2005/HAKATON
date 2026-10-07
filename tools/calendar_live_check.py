"""Explicit live check, synthetic records only, in a separate local database.

No existing agreement/customer/calendar event is selected or modified.
The reminder probe remains for the user to verify phone delivery.
"""
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
    parser.add_argument("--live", action="store_true", help="Explicit authorization for synthetic Google events")
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Live check requires --live; no network request sent")
    os.environ["CALLMIND_DATA_DIR"] = str(ROOT / ".test-data" / "calendar-live")
    os.environ["CALLMIND_TEST_MODE"] = "1"
    os.environ["GOOGLE_TOKEN_FILE"] = str(Path(args.token_file).resolve())
    from database import init_db, SessionLocal
    from models import Client, Call, Agreement, AgreementDetails
    from services.integrations import GoogleCalendar, sync_agreement
    from services.settings import get_settings
    from services.workspace import confirm_task
    from services.storage import update_agreement, set_agreement_status
    from services.dates import local_now
    from services import calendar_sync as calendar
    init_db()
    adapter = GoogleCalendar(get_settings())
    results, event_ids = [], []
    prefix = "CallMind · ТЕСТ календаря"
    evidence = "Проведём техническую проверку календаря без данных клиентов."

    def check(name, okay):
        results.append({"check": name, "passed": bool(okay)})
        if not okay:
            raise RuntimeError("Live check assertion failed: " + name)

    def fixture(suffix, kind="meeting", due=None, date_only=False):
        with SessionLocal() as db:
            client = Client(name="Тест интеграции — без данных клиента")
            db.add(client)
            db.flush()
            call = Call(client_id=client.id, call_datetime=local_now(), transcript=evidence, transcript_segments="[]", summary="Синтетическая проверка")
            db.add(call)
            db.flush()
            agreement = Agreement(call_id=call.id, description=f"{prefix}: {suffix}", responsible="manager", deadline=due, evidence=evidence, status="pending")
            db.add(agreement)
            db.flush()
            db.add(AgreementDetails(agreement_id=agreement.id, kind=kind, priority="normal", date_only=date_only))
            db.commit()
            agreement_id = agreement.id
        confirm_task(agreement_id)
        return agreement_id

    def send(agreement_id):
        sync_agreement(agreement_id, "google")
        row = calendar.rows(agreement_id)[0]
        if row["external_id"] not in event_ids:
            event_ids.append(row["external_id"])
        return row, calendar.read(adapter, row["external_id"])

    def edit(agreement_id, title, due, kind="meeting", date_only=False):
        update_agreement(agreement_id, description=title, responsible="manager", deadline=due, date_only=date_only, priority="normal", kind=kind)
        confirm_task(agreement_id)

    notification = None
    try:
        due = local_now().replace(second=0, microsecond=0) + timedelta(days=1)
        aid = fixture("создание и сверка", due=due)
        row, raw = send(aid)
        check("Реальное создание встречи", raw["status"] == "confirmed" and raw.get("transparency", "opaque") == "opaque")
        original_id = row["external_id"]
        edit(aid, f"{prefix}: изменённый срок", due + timedelta(hours=2))
        row, raw = send(aid)
        check("Повторный перенос обновляет прежний ID", row["external_id"] == original_id and raw["summary"].endswith("изменённый срок"))
        remote_due = due + timedelta(hours=3)
        request = adapter.service.events().patch(calendarId=adapter.calendar_id, eventId=original_id,
            body={"summary": f"{prefix}: правка в Google", "start": {"dateTime": remote_due.replace(tzinfo=__import__('zoneinfo').ZoneInfo(get_settings()["timezone"])).isoformat()},
                  "end": {"dateTime": (remote_due + timedelta(minutes=45)).replace(tzinfo=__import__('zoneinfo').ZoneInfo(get_settings()["timezone"])).isoformat()}}, sendUpdates="none")
        request.headers["If-Match"] = raw["etag"]
        request.execute()
        calendar.reconcile(adapter, row["key"])
        item = calendar.item_for(aid)
        check("Правки Google приходят в CallMind", item["description"].endswith("правка в Google") and item["deadline"] == remote_due)
        check("Входящие правки требуют подтверждения", bool(item["review_reasons"]))
        confirm_task(aid)
        edit(aid, f"{prefix}: локальная правка", remote_due)
        latest = calendar.read(adapter, original_id)
        request = adapter.service.events().patch(calendarId=adapter.calendar_id, eventId=original_id, body={"summary": f"{prefix}: другая правка Google"}, sendUpdates="none")
        request.headers["If-Match"] = latest["etag"]
        request.execute()
        calendar.reconcile(adapter, row["key"])
        check("Реальный конфликт не перезаписывает событие", calendar.row_for(row["key"])["state"] == "conflict")
        calendar.resolve(adapter, row["key"], "action", "local")
        send(aid)
        check("Выбор пользователя применён", calendar.read(adapter, original_id)["summary"].endswith("локальная правка"))
        edit(aid, f"{prefix}: локальная правка", None)
        row, raw = send(aid)
        check("Удаление срока отменяет событие", raw["status"] == "cancelled")

        task_id = fixture("напоминание о задаче", kind="task", due=due)
        task_row, raw = send(task_id)
        check("Задача не занимает время", raw["transparency"] == "transparent")
        set_agreement_status(task_id, "done")
        task_row, raw = send(task_id)
        check("Выполнение отменяет напоминание", raw["status"] == "cancelled")
        set_agreement_status(task_id, "pending")
        task_row, raw = send(task_id)
        check("Возврат в работу восстанавливает прежнее событие", raw["status"] == "confirmed")
        set_agreement_status(task_id, "done")
        send(task_id)

        day = due.replace(hour=0, minute=0, second=0, microsecond=0)
        day_id = fixture("задача без времени", kind="task", due=day, date_only=True)
        day_row, raw = send(day_id)
        check("Срок без времени сохраняет дату", raw["start"]["date"] == day.date().isoformat())
        set_agreement_status(day_id, "done")
        send(day_id)

        notify_at = local_now().replace(microsecond=0) + timedelta(minutes=6)
        notify_id = fixture("проверка уведомления на телефоне", due=notify_at)
        notify_row, raw = send(notify_id)
        calendar.save_options(notify_row["key"], 5, 1, True)
        notify_row, raw = send(notify_id)
        check("Google сохранил popup за минуту", raw["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1}]})
        notification = {"agreement_id": notify_id, "event_id": notify_row["external_id"], "event_at": notify_at.isoformat(), "notification_at": (notify_at - timedelta(minutes=1)).isoformat(), "phone_delivery": "Ожидает проверки пользователем"}
    except Exception as error:
        results.append({"check": "Непрерывный live-сценарий", "passed": False, "error_type": type(error).__name__,
                        "message": str(error) if isinstance(error, ValueError) else "Проверка прервана; подробности ответа провайдера не выводятся"})
    report = {"checked_at": local_now().isoformat(), "synthetic_only": True, "checks": results, "notification": notification, "event_ids": event_ids}
    target = ROOT / ".test-data" / "calendar-live" / "result.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if results and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
