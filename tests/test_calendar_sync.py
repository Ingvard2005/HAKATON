"""Isolated calendar reconciliation: simulated server, no real client/event data."""
import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from googleapiclient.errors import HttpError
from tests.test_ui_integrations import ProjectTests, SessionLocal, get_settings, local_now
from models import Agreement, AgreementDetails, ExternalLink
from services.workspace import confirm_task
from services.storage import update_agreement, set_agreement_status
from services import calendar_sync as calendar
from services.integrations import IntegrationError
seed_database = ProjectTests.setUp
del ProjectTests


def tearDownModule():
    from tests.test_ui_integrations import engine
    engine.dispose()


def http_error(code):
    return HttpError(SimpleNamespace(status=code, reason="Synthetic error"), b'{}')


class Request:
    def __init__(self, action):
        self.action, self.headers = action, {}

    def execute(self):
        return self.action(self.headers)


class Events:
    def __init__(self):
        self.records, self.calls = {}, []
        self.version = 0
        self.fail_after_insert = False
        self.race = False

    def versioned(self, raw):
        self.version += 1
        raw["etag"] = f'"{self.version}"'
        raw["htmlLink"] = "https://calendar.google.com/synthetic"
        return raw

    def get(self, calendarId, eventId):
        def action(headers):
            self.calls.append(("get", eventId))
            if eventId not in self.records:
                raise http_error(404)
            return copy.deepcopy(self.records[eventId])
        return Request(action)

    def insert(self, calendarId, body, sendUpdates):
        def action(headers):
            self.calls.append(("insert", body["id"]))
            if body["id"] in self.records:
                raise http_error(409)
            self.records[body["id"]] = self.versioned(copy.deepcopy(body))
            if self.fail_after_insert:
                raise RuntimeError("Network response lost after server commit")
            return copy.deepcopy(self.records[body["id"]])
        return Request(action)

    def patch(self, calendarId, eventId, body, sendUpdates):
        def action(headers):
            self.calls.append(("patch", eventId))
            raw = self.records[eventId]
            if self.race:
                raw["summary"] = "Concurrent Google change"
                self.versioned(raw)
                self.race = False
            if headers.get("If-Match") != raw["etag"]:
                raise http_error(412)
            raw.update(copy.deepcopy(body))
            return copy.deepcopy(self.versioned(raw))
        return Request(action)


class FakeCalendar:
    calendar_id = "synthetic-calendar"
    account = hashlib.sha256(calendar_id.encode()).hexdigest()[:24]
    supports_reconciliation = True

    def __init__(self):
        self.events = Events()
        self.service = SimpleNamespace(events=lambda: self.events)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        seed_database(self)
        self.network = patch("googleapiclient.discovery.build", side_effect=AssertionError("Real network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.adapter = FakeCalendar()
        self.edit("Встреча", local_now().replace(microsecond=0), date_only=False, kind="meeting")

    def edit(self, description, deadline, date_only=False, kind="meeting"):
        update_agreement(1, description=description, responsible="manager", deadline=deadline, date_only=date_only, priority="normal", kind=kind)
        confirm_task(1)

    def first(self):
        item = calendar.item_for(1)
        calendar.transfer(self.adapter, item, get_settings())
        return calendar.rows(1)[0]

    def test_initial_creation_readback_and_repeat_no_duplicate(self):
        row = self.first()
        calendar.transfer(self.adapter, calendar.item_for(1), get_settings())
        self.assertEqual(sum(m == "insert" for m, _ in self.adapter.events.calls), 1)
        self.assertEqual(len(row["external_id"]), 64)
        self.assertEqual(row["state"], "synced")

    def test_inbound_title_and_time_require_review(self):
        row = self.first()
        raw = self.adapter.events.records[row["external_id"]]
        raw["summary"] = "Из Google"
        raw["start"]["dateTime"] = "2026-11-06T10:00:00+03:00"
        raw["end"]["dateTime"] = "2026-11-06T10:45:00+03:00"
        self.adapter.events.versioned(raw)
        calendar.reconcile(self.adapter, row["key"])
        item = calendar.item_for(1)
        self.assertEqual(item["description"], "Из Google")
        self.assertEqual(item["deadline"].hour, 10)
        self.assertTrue(item["review_reasons"])
        self.assertEqual(calendar.rows(1)[0]["options"]["duration_minutes"], 45)

    def test_unconfirmed_outbound_is_blocked(self):
        row = self.first()
        with SessionLocal() as db:
            db.get(Agreement, 1).description = "Новая правка"
            db.commit()
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(calendar.rows(1)[0]["state"], "needs_review")
        self.assertEqual(self.adapter.events.records[row["external_id"]]["summary"], "Встреча")
        confirm_task(1)
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.events.records[row["external_id"]]["summary"], "Новая правка")

    def test_independent_edits_merge_preserving_google_notes_and_guests(self):
        row = self.first()
        raw = self.adapter.events.records[row["external_id"]]
        raw.update(description="Google notes", attendees=[{"email": "synthetic@example.invalid"}])
        raw["reminders"] = {"useDefault": False, "overrides": []}
        self.adapter.events.versioned(raw)
        self.edit("Новое действие", calendar.item_for(1)["deadline"])
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(raw["summary"], "Новое действие")
        self.assertEqual(raw["description"], "Google notes")
        self.assertEqual(len(raw["attendees"]), 1)
        self.assertEqual(calendar.rows(1)[0]["options"]["reminders"]["overrides"], [])

    def test_same_field_conflict_requires_choice(self):
        row = self.first()
        self.edit("Локально", calendar.item_for(1)["deadline"])
        self.adapter.events.records[row["external_id"]]["summary"] = "Google"
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(calendar.rows(1)[0]["state"], "conflict")
        calendar.resolve(self.adapter, row["key"], "action", "remote")
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(calendar.item_for(1)["description"], "Google")

    def test_etag_prevents_lost_google_edit(self):
        row = self.first()
        self.edit("Локально", calendar.item_for(1)["deadline"])
        self.adapter.events.race = True
        with self.assertRaisesRegex(IntegrationError, "не перезаписаны"):
            calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.events.records[row["external_id"]]["summary"], "Concurrent Google change")
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(calendar.rows(1)[0]["state"], "conflict")

    def test_cancelled_google_event_does_not_complete_task(self):
        row = self.first()
        self.adapter.events.records[row["external_id"]] = dict(id=row["external_id"], status="cancelled", etag='"cancelled"')
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(calendar.item_for(1)["status"], "pending")
        self.assertTrue(calendar.rows(1)[0]["options"]["cancelled"])

    def test_removed_deadline_cancels_without_deleting_local_history(self):
        row = self.first()
        self.edit("Встреча", None)
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.events.records[row["external_id"]]["status"], "cancelled")
        self.assertIsNone(calendar.item_for(1)["deadline"])

    def test_completed_task_cancels_but_completed_meeting_remains(self):
        self.edit("Задача", calendar.item_for(1)["deadline"], kind="task")
        row = self.first()
        set_agreement_status(1, "done")
        calendar.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.events.records[row["external_id"]]["status"], "cancelled")

    def test_completed_meeting_keeps_history_and_disables_reminders(self):
        row = self.first()
        set_agreement_status(1, "done")
        calendar.reconcile(self.adapter, row["key"])
        raw = self.adapter.events.records[row["external_id"]]
        self.assertEqual(raw["status"], "confirmed")
        self.assertFalse(raw["reminders"]["overrides"])

    def test_lost_insert_response_recovers_by_same_id(self):
        self.adapter.events.fail_after_insert = True
        with self.assertRaises(IntegrationError):
            self.first()
        row = calendar.rows(1)[0]
        self.assertEqual(row["state"], "uncertain")
        self.adapter.events.fail_after_insert = False
        calendar.transfer(self.adapter, calendar.item_for(1), get_settings())
        self.assertEqual(sum(m == "insert" for m, _ in self.adapter.events.calls), 1)
        self.assertEqual(calendar.rows(1)[0]["state"], "synced")

    def test_deleted_event_not_automatically_recreated(self):
        row = self.first()
        self.adapter.events.records.clear()
        with self.assertRaises(IntegrationError):
            calendar.reconcile(self.adapter, row["key"])
        calendar.sync_cycle(self.adapter, force=True)
        self.assertEqual(sum(m == "insert" for m, _ in self.adapter.events.calls), 1)
        self.assertEqual(calendar.rows(1)[0]["state"], "deleted")

    def test_default_off_and_test_mode_do_not_connect(self):
        with patch("services.integrations.GoogleCalendar", side_effect=AssertionError("Should not connect")):
            calendar.sync_cycle()
            calendar.start_worker()

    def test_all_day_event_does_not_shift_date(self):
        self.edit("Встреча", local_now().replace(hour=0, minute=0, second=0, microsecond=0), date_only=True)
        row = self.first()
        self.assertIn("date", self.adapter.events.records[row["external_id"]]["start"])
        calendar.reconcile(self.adapter, row["key"])
        self.assertTrue(calendar.item_for(1)["date_only"])

    def test_recurrence_and_multiday_are_not_overwritten(self):
        row = self.first()
        self.adapter.events.records[row["external_id"]]["recurrence"] = ["RRULE:FREQ=DAILY"]
        with self.assertRaisesRegex(IntegrationError, "Повторяющееся"):
            calendar.reconcile(self.adapter, row["key"])
        self.assertFalse(any(m == "patch" for m, _ in self.adapter.events.calls))

    def test_event_options_are_reflected_in_ics(self):
        row = self.first()
        calendar.save_options(row["key"], 45, 2, False)
        from services.exports import calendar_ics
        config = calendar.export_settings(1, get_settings())
        self.assertEqual(config["event_minutes"], 45)
        content = calendar_ics(calendar.item_for(1), config)
        self.assertNotIn(b"BEGIN:VALARM", content)

    def test_bound_event_form_renders_without_network(self):
        self.first()
        from streamlit.testing.v1 import AppTest
        from tests.test_ui_integrations import ROOT
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        app.button(key="tasks_1_open").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertTrue(any(b.label == "Сверить с Google" for b in app.button))

    def test_enabled_background_cycle_updates_only_previously_linked_events(self):
        row = self.first()
        self.edit("Подтверждённая правка", calendar.item_for(1)["deadline"])
        confirm_task(2)  # Confirmed but never transferred; worker must not create it.
        from services.settings import save_settings
        save_settings({**get_settings(), "calendar_auto_sync": True})
        with patch("services.integrations.GoogleCalendar", return_value=self.adapter):
            calendar.sync_cycle()
        self.assertEqual(self.adapter.events.records[row["external_id"]]["summary"], "Подтверждённая правка")
        self.assertEqual(sum(m == "insert" for m, _ in self.adapter.events.calls), 1)
        self.assertFalse(calendar.rows(2))


if __name__ == "__main__":
    unittest.main()
