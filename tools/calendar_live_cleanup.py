"""Cancel only event IDs recorded by CallMind's synthetic live checks."""
import argparse
import json
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--phone-confirmed", action="store_true", help="Only after a human confirms delivery")
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Explicit --live required")
    data = ROOT / ".test-data/calendar-live"
    os.environ["CALLMIND_DATA_DIR"] = str(data)
    os.environ["CALLMIND_TEST_MODE"] = "1"
    os.environ["GOOGLE_TOKEN_FILE"] = str(Path(args.token_file).resolve())
    from database import init_db
    from services.integrations import GoogleCalendar
    from services.settings import get_settings
    from services.dates import local_now
    from services import calendar_sync as calendar
    init_db()
    reports = [json.loads((data/name).read_text(encoding="utf-8")) for name in ("result.json", "notification-repeat.json")]
    ids = set(reports[0]["event_ids"] + [reports[1]["event_id"]])
    adapter = GoogleCalendar(get_settings())
    count = 0
    for row in calendar.rows():
        if row["external_id"] not in ids or row["calendar_id"] != adapter.calendar_id:
            continue
        raw = calendar.read(adapter, row["external_id"])
        if raw.get("status") != "cancelled":
            if not raw.get("summary", "").startswith("CallMind · ТЕСТ"):
                raise SystemExit("Test title changed; cleanup requires manual review")
            calendar.conditional_patch(adapter, row["external_id"], raw, {"status":"cancelled", "reminders":{"useDefault":False,"overrides":[]}})
            calendar.reconcile(adapter, row["key"])
        if calendar.read(adapter, row["external_id"]).get("status") != "cancelled":
            raise SystemExit("Google did not confirm test cancellation")
        count += 1
    if args.phone_confirmed:
        reports[1]["phone_delivery"] = "Подтверждено пользователем: уведомление пришло на телефон"
        reports[1]["confirmed_at"] = local_now().isoformat()
        (data/"notification-repeat.json").write_text(json.dumps(reports[1],ensure_ascii=False,indent=2),encoding="utf-8")
    result = {"cancelled_test_events":count,"expected_events":len(ids),"phone_confirmed":args.phone_confirmed,"checked_at":local_now().isoformat()}
    (data/"cleanup.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result))
    return 0 if count == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
