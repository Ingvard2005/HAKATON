import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="callmind-tests-")
os.environ["CALLMIND_DATA_DIR"] = TEMP.name
os.environ["CALLMIND_API_KEY"] = "test-key"
os.environ["CALLMIND_TEST_MODE"] = "1"
os.environ["BITRIX24_TOKEN_FILE"] = str(Path(TEMP.name) / "absent-bitrix-token.json")
os.environ.pop("BITRIX24_WEBHOOK_URL", None)
os.environ.pop("BITRIX24_RESPONSIBLE_ID", None)
os.environ["GOOGLE_TOKEN_FILE"] = str(Path(TEMP.name) / "absent-token.json")
os.environ["GOOGLE_CREDENTIALS_FILE"] = str(Path(TEMP.name) / "absent-credentials.json")
os.environ["BITRIX24_CREDENTIALS_FILE"] = str(Path(TEMP.name) / "absent-bitrix-credentials.json")

from database import Base, engine, init_db, SessionLocal
from models import Client, Call, Agreement, ExternalLink
from services.storage import (get_or_create_client, get_agreements, get_call,
    update_agreement, update_call, set_agreement_status, enrich_agreements,
    get_revisions, parse_deadline, save_call)
from services.dates import local_now, is_overdue
from services.settings import get_settings, save_settings
from services.exports import calendar_ics, agreements_csv
from services.integrations import sync_agreement, calendar_payload, IntegrationError


def tearDownModule():
    engine.dispose()
    TEMP.cleanup()


class ProjectTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        init_db()
        now = local_now()
        with SessionLocal() as db:
            client = Client(name="Тестовый клиент", phone="+375291234567")
            db.add(client)
            db.flush()
            call = Call(client_id=client.id, call_datetime=now, transcript="Пришлю КП завтра",
                transcript_segments='[{"start": 1, "end": 3, "text": "Пришлю КП завтра"}]',
                summary="Обсудили КП", follow_up_required=True)
            db.add(call)
            db.flush()
            for description, responsible, deadline, status in [
                ("Отправить КП", "manager", now - timedelta(days=1), "pending"),
                ("Получить ответ", "client", now.replace(hour=0, minute=0, second=0, microsecond=0), "pending"),
                ("Выполнено", "manager", None, "done")]:
                db.add(Agreement(call_id=call.id, description=description, responsible=responsible,
                    deadline=deadline, status=status, evidence="Пришлю КП завтра"))
            db.commit()

    def test_same_name_different_phone_is_not_merged(self):
        with SessionLocal() as db:
            a = get_or_create_client(db, "Александр", "+375 (29) 111-11-11")
            b = get_or_create_client(db, "Александр", "+375291111112")
            c = get_or_create_client(db, "Александр", "+375291111111")
            self.assertNotEqual(a.id, b.id)
            self.assertEqual(a.id, c.id)
            d = get_or_create_client(db, "Без номера")
            e = get_or_create_client(db, "Без номера")
            self.assertNotEqual(d.id, e.id)

    def test_oauth_state_is_bound_to_provider_single_use_and_expiry(self):
        from services import connections as c
        import time
        with patch.dict(c.PENDING, {}, clear=True), patch.object(c, "finish") as finish:
            c.PENDING["secret-state"] = dict(provider="google", expires=time.time() + 30)
            c.remember("secret-state", c.PENDING["secret-state"])
            self.assertFalse(c.consume("bitrix24", "secret-state", {"code": ["fake"]})[0])
            finish.assert_not_called()
            self.assertTrue(c.consume("google", "secret-state", {"code": ["fake"]})[0])
            self.assertTrue(c.consume("google", "secret-state", {"code": ["fake"]})[0])
            self.assertEqual(finish.call_count, 1)
            c.PENDING["expired"] = dict(provider="google", expires=time.time() - 1)
            c.remember("expired", c.PENDING["expired"])
            self.assertFalse(c.consume("google", "expired", {"code": ["fake"]})[0])

    def test_oauth_cancel_has_no_token_and_preserves_other_service_message(self):
        from services import connections as c
        import time
        c.record("bitrix24", "connected", "Доступ подтверждён", "test portal")
        with patch.dict(c.PENDING, {}, clear=True), patch.object(c, "atomic_token") as write:
            c.PENDING["cancel"] = dict(provider="google", expires=time.time() + 30)
            c.remember("cancel", c.PENDING["cancel"])
            self.assertFalse(c.consume("google", "cancel", {"error": ["access_denied"]})[0])
            write.assert_not_called()
        self.assertEqual(c.details("google")["status"], "needs_check")
        self.assertEqual(c.details("bitrix24")["status"], "connected")

    def test_oauth_restores_pending_flow_after_process_restart(self):
        from services import connections as c
        import time
        with patch.dict(c.PENDING, {}, clear=True), patch.object(c, "finish") as finish:
            c.remember("persisted", dict(provider="bitrix24", portal="https://demo.bitrix24.ru", expires=time.time() + 600))
            self.assertTrue(c.consume("bitrix24", "persisted", {"code": ["fake"]})[0])
            self.assertTrue(c.consume("bitrix24", "persisted", {"code": ["fake"]})[0])
            finish.assert_called_once()
            self.assertFalse(c.consume("bitrix24", "persisted", {"code": ["other"]})[0])

    def test_oauth_clock_starts_on_click_and_each_click_is_a_new_flow(self):
        from services import connections as c
        from urllib.parse import parse_qs, urlparse
        import time
        with patch.object(c, "availability", return_value=None), patch.object(c, "ensure_listener"), \
                patch.object(c, "authorize_link", return_value=("https://provider.example/authorize", time.time() + 600)) as authorize:
            link, expiry = c.start_link("google")
            self.assertGreater(expiry, time.time() + 80000)
            authorize.assert_not_called()
            ticket = parse_qs(urlparse(link).query)["ticket"][0]
            self.assertEqual(c.start_authorization("google", ticket), "https://provider.example/authorize")
            c.start_authorization("google", ticket)
            self.assertEqual(authorize.call_count, 2)
            with self.assertRaises(ValueError):
                c.start_authorization("bitrix24", ticket)

    def test_oauth_restores_google_pkce_without_generating_another_verifier(self):
        from services import connections as c
        from google_auth_oauthlib.flow import Flow
        with patch.object(Flow, "from_client_secrets_file") as factory:
            transaction = c.restore(dict(provider="google", credentials_file="fake.json", code_verifier="original-pkce",
                                         redirect_uri="http://127.0.0.1:8766/oauth/google/callback"))
            self.assertEqual(factory.call_args.kwargs["code_verifier"], "original-pkce")
            self.assertFalse(factory.call_args.kwargs["autogenerate_code_verifier"])
            self.assertIs(transaction["flow"], factory.return_value)

    def test_oauth_google_flow_round_trip_keeps_original_pkce(self):
        from services import connections as c
        from google_auth_oauthlib.flow import Flow
        from services.integrations import GOOGLE_SCOPES
        import json, time
        path = Path(TEMP.name) / "fake-client.json"
        path.write_text(json.dumps({"installed": dict(client_id="fake-client", client_secret="fake-secret",
            auth_uri="https://accounts.google.com/o/oauth2/auth", token_uri="https://oauth2.googleapis.com/token")}), encoding="utf-8")
        with patch.dict(os.environ, {"GOOGLE_CREDENTIALS_FILE": str(path)}), patch.dict(c.PENDING, {}, clear=True), \
                patch.object(c, "finish") as finish:
            flow = Flow.from_client_secrets_file(str(path), scopes=GOOGLE_SCOPES,
                redirect_uri="http://127.0.0.1:8766/oauth/google/callback", autogenerate_code_verifier=True)
            flow.authorization_url(state="round-trip")
            c.remember("round-trip", dict(provider="google", flow=flow, calendar_id="primary", expires=time.time() + 600))
            self.assertTrue(c.consume("google", "round-trip", {"code": ["fake"]})[0])
            restored = finish.call_args.args[1]["flow"]
            self.assertEqual(restored.code_verifier, flow.code_verifier)
            self.assertEqual(restored.redirect_uri, flow.redirect_uri)

    def test_oauth_bitrix_verifies_portal_and_saves_without_transferring(self):
        from services import connections as c
        data = dict(access_token="fake-access", refresh_token="fake-refresh", expires_at=99999999999,
                    member_id="fake-member", scope="task,user", client_endpoint="https://demo.bitrix24.ru/rest/")
        response = MagicMock()
        response.json.return_value = {"result": {"ID": "17", "NAME": "Тест"}}
        scope_response = MagicMock()
        scope_response.json.return_value = {"result": ["task", "user", "crm"]}
        with patch.object(c, "bitrix_token", return_value=data), patch.object(c.httpx, "post", side_effect=[scope_response, response]) as post, \
                patch.object(c, "atomic_token") as write, patch("services.integrations.sync_agreement") as sync:
            c.finish("bitrix24", {"portal": "https://demo.bitrix24.ru"}, {"code": ["fake"], "domain": ["demo.bitrix24.ru"]})
            self.assertEqual(post.call_count, 2)
            self.assertTrue(post.call_args.args[0].endswith("/user.current.json"))
            self.assertEqual(write.call_count, 1)
            self.assertEqual(c.details("bitrix24")["status"], "connected")
            sync.assert_not_called()
            with self.assertRaises(ValueError):
                c.finish("bitrix24", {"portal": "https://demo.bitrix24.ru"}, {"code": ["fake"], "domain": ["other.bitrix24.ru"]})

    def test_oauth_google_checks_calendar_before_saving(self):
        from services import connections as c
        flow = MagicMock()
        flow.credentials.to_json.return_value = '{"fake": true}'
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {"id": "fake-calendar"}
        with patch("googleapiclient.discovery.build", return_value=service), patch.object(c, "atomic_token") as write, \
                patch("services.integrations.sync_agreement") as sync:
            c.finish("google", {"flow": flow, "calendar_id": "primary"}, {"code": ["fake"]})
            flow.fetch_token.assert_called_once_with(code="fake")
            self.assertEqual(write.call_count, 1)
            self.assertEqual(c.details("google")["account"], "fake-calendar")
            sync.assert_not_called()
            service.calendars.return_value.get.return_value.execute.side_effect = RuntimeError("secret-token")
            write.reset_mock()
            with self.assertRaises(RuntimeError):
                c.finish("google", {"flow": flow, "calendar_id": "primary"}, {"code": ["fake"]})
            write.assert_not_called()

    def test_bitrix_missing_token_scope_is_verified_with_portal(self):
        from services import connections as c
        data = dict(access_token="fake-access", refresh_token="fake-refresh", expires_at=99999999999,
                    member_id="fake-member", client_endpoint="https://demo.bitrix24.ru/rest/")
        scope_response, user_response = MagicMock(), MagicMock()
        scope_response.json.return_value = {"result": ["task", "user", "crm"]}
        user_response.json.return_value = {"result": {"ID": "17", "NAME": "Тест"}}
        with patch.object(c, "bitrix_token", return_value=data), \
            patch.object(c.httpx, "post", side_effect=[scope_response, user_response]) as post, \
            patch.object(c, "atomic_token") as save, patch.object(c, "record"):
            c.finish("bitrix24", {"portal": "https://demo.bitrix24.ru"}, {"code": ["fake"], "domain": ["demo.bitrix24.ru"], "scope": ["untrusted"]})
            self.assertTrue(post.call_args_list[0].args[0].endswith("/scope.json"))
            self.assertNotIn("full", post.call_args_list[0].kwargs["data"])
            self.assertEqual(data["scope"], "crm,task,user")
            save.assert_called_once()

    def test_bitrix_missing_permission_cannot_be_supplied_by_callback(self):
        from services import connections as c
        response = MagicMock()
        response.json.return_value = {"result": ["user", "crm"]}
        with patch.object(c, "bitrix_token", return_value=dict(access_token="fake", client_endpoint="https://demo.bitrix24.ru/rest/")), \
            patch.object(c.httpx, "post", return_value=response), patch.object(c, "atomic_token") as save:
            with self.assertRaises(ValueError):
                c.finish("bitrix24", {"portal": "https://demo.bitrix24.ru"}, {"code": ["fake"], "domain": ["demo.bitrix24.ru"], "scope": ["task,user,crm"]})
            save.assert_not_called()

    def test_oauth_portal_validation_and_authorization_link_excludes_secret(self):
        from services import connections as c
        from urllib.parse import urlparse, parse_qs
        for bad in ("http://demo.bitrix24.ru", "https://127.0.0.1", "https://demo.bitrix24.ru.evil.org",
                    "https://user:secret@demo.bitrix24.ru", "https://demo.bitrix24.ru/rest/secret"):
            with self.assertRaises(ValueError):
                c.portal_url(bad)
        with patch.dict(os.environ, {"BITRIX24_CLIENT_ID": "test-app", "BITRIX24_CLIENT_SECRET": "never-in-url",
                                    "CALLMIND_OAUTH_PUBLIC_BASE": "https://callmind.example"}), \
                patch.object(c, "SERVER", MagicMock()), patch.dict(c.PENDING, {}, clear=True):
            link, expiry = c.authorize_link("bitrix24", "https://demo.bitrix24.ru")
            self.assertNotIn("never-in-url", link)
            state = parse_qs(urlparse(link).query)["state"][0]
            self.assertEqual(c.PENDING[state]["provider"], "bitrix24")

    def test_bitrix_public_callback_preserves_google_local_callback(self):
        from services import connections as c
        with patch.dict(os.environ, {"BITRIX24_PUBLIC_BASE": "https://bitrix-callback.example",
                                    "CALLMIND_OAUTH_PUBLIC_BASE": "", "CALLMIND_OAUTH_PORT": "8770"}):
            self.assertEqual(c.callback_base("bitrix24"), "https://bitrix-callback.example")
            self.assertEqual(c.callback_base("google"), "http://127.0.0.1:8770")

    def test_bitrix_scopes_accept_oauth_spaces_and_comma_format(self):
        from services.connections import bitrix_scopes
        for value in ("task,user,crm", "task user crm", " task, user crm ", ["task", "user", "crm"]):
            self.assertEqual(bitrix_scopes(value), {"task", "user", "crm"})
        self.assertNotIn("task", bitrix_scopes("tasks_extended user crm"))
        self.assertEqual(bitrix_scopes(None), set())

    def test_settings_shows_two_separate_connection_buttons(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend" / "app.py")).run()
        app.radio(key="navigation").set_value("Настройки").run()
        self.assertFalse(app.exception)
        labels = [element.proto.label for element in app.get("link_button")]
        self.assertIn("Подключить Bitrix24", labels)
        self.assertIn("Войти через Google", labels)

    def test_connected_google_has_secondary_account_change_instead_of_login(self):
        from streamlit.testing.v1 import AppTest
        status = {"status": "connected", "message": "Доступ к календарю подтверждён",
                  "account": "test@example.test", "checked_at": "2026-10-06T22:16:01"}
        with patch("frontend.connections.details", side_effect=lambda provider: status if provider == "google" else {}), \
            patch("frontend.connections.connection_status", return_value={"google": True, "bitrix24": False}), \
            patch("frontend.connections.availability", return_value=None), \
            patch("frontend.connections.start_link", return_value=("http://127.0.0.1:8770/oauth/google/start?ticket=test", 9999999999)):
            app = AppTest.from_file(str(ROOT / "frontend" / "app.py")).run()
            app.radio(key="navigation").set_value("Настройки").run()
            self.assertFalse(app.exception)
            buttons = app.get("link_button")
            self.assertNotIn("Войти через Google", [element.proto.label for element in buttons])
            change = next(element for element in buttons if element.proto.label == "Сменить аккаунт")
            self.assertEqual(change.proto.type, "secondary")
            self.assertTrue(any("06.10.2026, 22:16" in caption.value for caption in app.caption))

    def test_bad_date_is_not_silently_lost(self):
        with self.assertRaises(ValueError):
            parse_deadline("не дата")
        self.assertEqual(parse_deadline("2026-10-06T14:00:00Z").hour, 17)

    def test_date_only_is_not_overdue_at_midnight(self):
        item = enrich_agreements(get_agreements())[1]
        item.update(deadline=local_now().replace(hour=0, minute=0), date_only=True, status="pending")
        self.assertFalse(is_overdue(item, local_now()))
        item["date_only"] = False
        self.assertTrue(is_overdue(item, local_now().replace(hour=12)))

    def test_edit_preserves_original_in_revision(self):
        update_agreement(1, description="Отправить новое КП", responsible="manager",
            deadline="2026-10-09", date_only=True, priority="high", kind="meeting")
        self.assertEqual(get_revisions("agreement", 1)[0]["before"]["description"], "Отправить КП")
        item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)
        self.assertEqual(item["priority"], "high")
        self.assertEqual(item["status"], "pending")

    def test_call_revision_preserves_transcript(self):
        update_call(1, summary="Исправлено", transcript="Текст исправлен", next_action="Позвонить", follow_up_required=True)
        self.assertEqual(get_revisions("call", 1)[0]["before"]["transcript"], "Пришлю КП завтра")
        self.assertEqual(get_call(1)["transcript"], "Текст исправлен")

    def test_status_completion_time_and_validation(self):
        set_agreement_status(1, "done")
        item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)
        self.assertIsNotNone(item["completed_at"])
        set_agreement_status(1, "pending")
        self.assertIsNone(next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)["completed_at"])
        with self.assertRaises(ValueError):
            set_agreement_status(1, "invalid")

    def test_settings_validation_and_persistence(self):
        save_settings(get_settings() | {"default_priority": "high", "task_template": "{client}: {description}"})
        self.assertEqual(get_settings()["default_priority"], "high")
        with self.assertRaises(ValueError):
            save_settings(get_settings() | {"task_template": "{client.__class__}"})

    def test_exports_and_calendar_all_day(self):
        item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 2)
        payload = calendar_payload(item, get_settings())
        self.assertIn("date", payload["start"])
        self.assertEqual((datetime.fromisoformat(payload["end"]["date"]) - datetime.fromisoformat(payload["start"]["date"])).days, 1)
        self.assertIn(b"DTSTART;VALUE=DATE", calendar_ics(item, get_settings()))
        item["description"] = "=HYPERLINK(1)"
        self.assertIn("'=HYPERLINK", agreements_csv([item]).decode("utf-8-sig"))

    def test_google_retries_use_stable_event_id(self):
        from googleapiclient.errors import HttpError
        from services.integrations import GoogleCalendar
        service = MagicMock()
        service.calendars().get().execute.return_value = {"id": "demo@example.com"}
        events = service.events()
        response = SimpleNamespace(status=404, reason="not found")
        events.get().execute.side_effect = [HttpError(response, b'{}'), {"id": "existing"}]
        events.insert().execute.return_value = {"id": "created", "htmlLink": "https://calendar.google.com"}
        events.update().execute.return_value = {"id": "updated", "htmlLink": "https://calendar.google.com"}
        with patch("services.integrations.google_service", return_value=service):
            adapter = GoogleCalendar(get_settings())
            item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 2)
            adapter.sync(item, None, get_settings())
            inserted_id = events.insert.call_args.kwargs["body"]["id"]
            adapter.sync(item, None, get_settings())
            self.assertEqual(events.update.call_args.kwargs["eventId"], inserted_id)
            self.assertEqual(len(inserted_id), 64)

    def test_invalid_ai_deadline_does_not_copy_audio(self):
        from database import AUDIO_DIR
        source = Path(TEMP.name) / "input.mp3"
        source.write_bytes(b"synthetic")
        analysis = SimpleNamespace(agreements=[SimpleNamespace(deadline="bad-date")])
        before = list(AUDIO_DIR.iterdir())
        with self.assertRaises(ValueError):
            save_call("Test", "+375291111111", local_now(), "input.mp3", str(source), {}, analysis)
        self.assertEqual(list(AUDIO_DIR.iterdir()), before)

    @patch.dict(os.environ, {"BITRIX24_WEBHOOK_URL": "https://demo.bitrix24.ru/rest/1/secret/", "BITRIX24_RESPONSIBLE_ID": "1"})
    def test_bitrix_retry_updates_existing_task(self):
        from services.workspace import confirm_task
        confirm_task(1)
        requests = []
        def request(_self, method, body):
            requests.append(method)
            if method == "tasks.task.add":
                return {"task": {"id": "123"}}
            if method == "tasks.task.get":
                return {"task": {"id": "123", "status": "2"}}
            return True
        with patch("services.integrations.Bitrix24.request", request):
            sync_agreement(1, "bitrix24")
            sync_agreement(1, "bitrix24")
        self.assertEqual(requests.count("tasks.task.add"), 1)
        self.assertEqual(requests.count("tasks.task.update"), 1)

    @patch.dict(os.environ, {"BITRIX24_WEBHOOK_URL": "https://demo.bitrix24.ru/rest/1/secret/", "BITRIX24_RESPONSIBLE_ID": "1"})
    def test_bitrix_ambiguous_create_is_not_repeated(self):
        from services.workspace import confirm_task
        confirm_task(1)
        with patch("services.integrations.Bitrix24.request", side_effect=IntegrationError("Не подтверждено")) as request:
            with self.assertRaises(IntegrationError):
                sync_agreement(1, "bitrix24")
            with self.assertRaises(IntegrationError):
                sync_agreement(1, "bitrix24")
            self.assertEqual(request.call_count, 1)

    def test_api_is_protected_and_edits_data(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as api:
            self.assertEqual(api.get("/agreements").status_code, 401)
            result = api.patch("/agreements/1/status", headers={"X-API-Key": "test-key"}, json={"status": "done"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(api.get("/calls/999", headers={"X-API-Key": "test-key"}).status_code, 404)

    def test_ui_all_pages_and_shared_task(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertEqual(len(app.tabs), 0)
        app.button(key="tasks_1_open").click().run()
        app.button(key="task_status").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(next(i for i in get_agreements() if i["id"] == 1)["status"], "done")
        for page in ["Звонки", "Клиенты", "Сделки", "Аналитика", "Настройки"]:
            app.radio(key="navigation").set_value(page).run()
            self.assertEqual(len(app.exception), 0, f"{page}: {list(app.exception)}")

    def test_ui_edit_and_settings_save(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        app.button(key="tasks_1_open").click().run()
        app.text_area(key="draft_1_description").set_value("Исправленное КП").run()
        app.button(key="save_task").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertEqual(next(i for i in get_agreements() if i["id"] == 1)["description"], "Исправленное КП")
        app.radio(key="navigation").set_value("Настройки").run()
        next(w for w in app.selectbox if w.label == "Приоритет новых задач").set_value("high")
        next(w for w in app.button if w.label == "Сохранить настройки").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertEqual(get_settings()["default_priority"], "high")

    def test_draft_survives_navigation_and_dynamic_deadline(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        app.button(key="tasks_1_open").click().run()
        app.text_area(key="draft_1_description").set_value("Несохранённая правка").run()
        app.checkbox(key="draft_1_has").uncheck().run()
        self.assertFalse(any(w.label == "Дата выполнения" for w in app.date_input))
        self.assertFalse(any(w.label == "Время выполнения" for w in app.time_input))
        app.radio(key="navigation").set_value("Клиенты").run()
        self.assertTrue(any("несохранённый" in w.value for w in app.warning))
        app.button(key="resume_1").click().run()
        self.assertEqual(app.text_area(key="draft_1_description").value, "Несохранённая правка")
        self.assertFalse(app.checkbox(key="draft_1_has").value)
        app.button(key="save_task").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        row = next(i for i in get_agreements() if i["id"] == 1)
        self.assertIsNone(row["deadline"])
        self.assertEqual(row["description"], "Несохранённая правка")

    def test_review_invalidates_after_edit_or_transcript_change(self):
        from services.workspace import tasks, confirm_task
        confirm_task(1)
        self.assertFalse(next(i for i in tasks() if i["id"] == 1)["review_reasons"])
        update_call(1, summary="Исправлено", transcript="Другой текст", next_action="", follow_up_required=False)
        self.assertTrue(next(i for i in tasks() if i["id"] == 1)["review_reasons"])
        with self.assertRaises(ValueError):
            confirm_task(1)

    def test_deal_results_do_not_follow_task_status_and_keep_history(self):
        from services.workspace import create_deal, record_outcome, deals, analytics, link_deal
        now = local_now()
        d = create_deal(1, "Договор", "100.10", "RUB", now - timedelta(days=5))
        link_deal("agreement", 1, d)
        set_agreement_status(1, "done")
        self.assertEqual(deals()[0]["outcome"], "open")
        with self.assertRaises(ValueError):
            record_outcome(d, "lost", now - timedelta(days=2), "", "Менеджер")
        record_outcome(d, "lost", now - timedelta(days=2), "Цена", "Менеджер")
        record_outcome(d, "open", now - timedelta(days=1), "", "Менеджер")
        self.assertEqual(deals()[0]["outcome"], "open")
        self.assertEqual(len(deals()[0]["events"]), 2)
        historic = analytics((now - timedelta(days=3)).date(), (now - timedelta(days=2)).date())
        self.assertEqual(len(historic["lost"]), 1)
        current = analytics((now - timedelta(days=3)).date(), now.date())
        self.assertEqual(len(current["lost"]), 0)
        with self.assertRaises(ValueError):
            create_deal(1, "Плохая сумма", "NaN", "RUB", now)

    def test_first_confirmed_deadline_retains_breach_and_reopen_events(self):
        from services.workspace import confirm_task, analytics
        now = local_now()
        old_due = now - timedelta(hours=1)
        update_agreement(1, description="Отправить КП", responsible="manager", deadline=old_due,
            date_only=False, priority="normal", kind="task")
        with patch("services.workspace.stamp", return_value=now - timedelta(hours=2)):
            confirm_task(1)
        update_agreement(1, description="Отправить КП", responsible="manager", deadline=now + timedelta(days=1),
            date_only=False, priority="normal", kind="task")
        confirm_task(1)
        # The original deadline can fall yesterday when the test runs after midnight.
        report = analytics(old_due.date(), now.date())
        self.assertEqual([i["id"] for i in report["eligible"]], [1])
        self.assertEqual(len(report["ontime"]), 0)
        set_agreement_status(1, "done")
        set_agreement_status(1, "pending")
        self.assertEqual(len(analytics(now.date(), now.date())["completed"]), 1)

    def test_calendar_task_and_meeting_semantics_match_export(self):
        item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)
        item["date_only"] = False
        item["kind"] = "task"
        task = calendar_payload(item, get_settings())
        self.assertEqual(task["transparency"], "transparent")
        self.assertTrue(task["summary"].startswith("Напоминание:"))
        self.assertIn(b"TRANSP:TRANSPARENT", calendar_ics(item, get_settings()))
        item.update(kind="meeting", status="done")
        meeting = calendar_payload(item, get_settings())
        self.assertEqual(meeting["transparency"], "opaque")
        self.assertEqual(meeting["status"], "confirmed")
        self.assertIn(b"TRANSP:OPAQUE", calendar_ics(item, get_settings()))

    def test_provider_messages_are_independent_and_no_network(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        from frontend.redesign import synchronize
        # Exercise the controller inside the app's session context through patched adapter.
        from services.workspace import confirm_task
        confirm_task(1)
        app.button(key="tasks_1_open").click().run()
        with patch("frontend.redesign.connection_status", return_value={"bitrix24": True, "google": True}), \
            patch("frontend.redesign.sync_agreement", side_effect=[IntegrationError("Тестовая ошибка Bitrix24"), "https://example.test/event"]):
            app.run()
            app.button(key="sync_bitrix24").click().run()
            app.button(key="sync_google").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertTrue(any("Тестовая ошибка Bitrix24" in e.value for e in app.error))
        self.assertTrue(any("Подтверждено сервисом" in e.value for e in app.success))

    def test_empty_ui_and_ai_error_do_not_invent_results(self):
        from streamlit.testing.v1 import AppTest
        import types
        module = types.ModuleType("services.llm")
        module.analyze_client_history = MagicMock(side_effect=RuntimeError("test AI error"))
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        app.radio(key="navigation").set_value("Клиенты").run()
        with patch.dict(sys.modules, {"services.llm": module}):
            next(b for b in app.button if b.label == "Проверить риски истории").click().run()
        self.assertEqual(len(app.exception), 0, list(app.exception))
        self.assertTrue(any("Не удалось проанализировать" in e.value for e in app.error))
        Base.metadata.drop_all(engine)
        init_db()
        app = AppTest.from_file(str(ROOT / "frontend/app.py")).run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("не найдены" in w.value for w in app.info))

    def test_sync_setup_failure_is_persisted_per_provider(self):
        from services.workspace import confirm_task
        confirm_task(1)
        for provider, adapter in [("bitrix24", "Bitrix24"), ("google", "GoogleCalendar")]:
            with patch("services.integrations." + adapter, side_effect=IntegrationError("Нет доступа: " + provider)):
                with self.assertRaises(IntegrationError):
                    sync_agreement(1, provider)
        row = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)
        self.assertEqual({n["provider"] for n in row["last_attempts"]}, {"bitrix24", "google"})
        self.assertTrue(all(not n["success"] for n in row["last_attempts"]))

    def test_removing_deadline_cancels_existing_calendar_record(self):
        from services.integrations import GoogleCalendar
        adapter = GoogleCalendar.__new__(GoogleCalendar)
        adapter.service = MagicMock()
        adapter.calendar_id = "test-calendar"
        adapter.service.events.return_value.patch.return_value.execute.return_value = {"id": "existing"}
        item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == 1)
        item["deadline"] = None
        self.assertEqual(adapter.sync(item, "existing", get_settings())[0], "existing")
        body = adapter.service.events.return_value.patch.call_args.kwargs["body"]
        self.assertEqual(body["status"], "cancelled")
        adapter.service.events.return_value.insert.assert_not_called()
        with self.assertRaises(IntegrationError):
            adapter.sync(item, None, get_settings())

    def test_invalid_editor_retains_draft_and_time_is_hidden(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
        app.button(key="tasks_1_open").click().run()
        app.checkbox(key="draft_1_date_only").check().run()
        self.assertFalse(any(w.label == "Время выполнения" for w in app.time_input))
        app.text_area(key="draft_1_description").set_value("").run()
        app.button(key="save_task").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Не сохранено" in e.value for e in app.error))
        self.assertEqual(next(i for i in get_agreements() if i["id"] == 1)["description"], "Отправить КП")
        self.assertEqual(app.text_area(key="draft_1_description").value, "")

    def test_additive_schema_and_new_api_keep_existing_records(self):
        before = get_call(1)
        init_db()
        self.assertEqual(get_call(1), before)
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as api:
            headers = {"X-API-Key": "test-key"}
            self.assertEqual(api.get("/deals").status_code, 401)
            result = api.post("/deals", headers=headers, json={"client_id": 1,
                "title": "Поставка", "created_at": (local_now() - timedelta(days=1)).isoformat()})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(api.post("/agreements/1/review", headers=headers).status_code, 200)
            report = api.get("/analytics", headers=headers, params={"start": local_now().date().isoformat(), "end": local_now().date().isoformat()})
            self.assertEqual(report.status_code, 200, report.text)
            self.assertEqual(get_call(1)["transcript"], before["transcript"])

    def test_first_deadline_can_be_confirmed_after_initial_review_without_date(self):
        from services.workspace import confirm_task
        from models import AgreementReview
        confirm_task(3)
        due = local_now() + timedelta(days=1)
        update_agreement(3, description="Следующий контакт", responsible="manager", deadline=due,
            date_only=False, priority="normal", kind="task")
        confirm_task(3)
        with SessionLocal() as db:
            self.assertEqual(db.get(AgreementReview, 3).confirmed_deadline, due)

    def test_no_implicit_transfer_even_with_legacy_auto_sync_enabled(self):
        import json
        from models import AppSetting
        from streamlit.testing.v1 import AppTest
        with SessionLocal() as db:
            db.merge(AppSetting(key="preferences", value=json.dumps({"auto_sync": True})))
            db.commit()
        self.assertFalse(get_settings()["auto_sync"])
        with patch("frontend.redesign.sync_agreement") as send:
            app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
            app.button(key="tasks_1_open").click().run()
            app.text_area(key="draft_1_description").set_value("Проверенное КП").run()
            app.button(key="save_task").click().run()
            app.button(key="confirm_task").click().run()
            app.button(key="task_status").click().run()
            self.assertEqual(len(app.exception), 0, list(app.exception))
            send.assert_not_called()
        save_settings({"auto_sync": True})
        self.assertFalse(get_settings()["auto_sync"])

    def test_transfer_requires_confirmation_before_any_adapter_or_api_send(self):
        with patch("services.integrations.Bitrix24") as adapter:
            with self.assertRaisesRegex(IntegrationError, "Подтвердить"):
                sync_agreement(1, "bitrix24")
            adapter.assert_not_called()
        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as api, patch("services.integrations.GoogleCalendar") as adapter:
            result = api.post("/agreements/1/sync/google", headers={"X-API-Key": "test-key"})
            self.assertEqual(result.status_code, 400)
            adapter.assert_not_called()

    def test_google_scope_superset_is_accepted_without_sending_events(self):
        from services.connections import finish
        from services.integrations import GOOGLE_SCOPES
        flow = MagicMock()
        warning = Warning("scope changed")
        warning.token = {"access_token": "synthetic-token"}
        warning.new_scope = list(GOOGLE_SCOPES) + ["openid"]
        flow.fetch_token.side_effect = warning
        flow.credentials.valid = True
        flow.credentials.refresh_token = "synthetic-refresh"
        flow.credentials.has_scopes.return_value = True
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {"id": "test-calendar"}
        with patch("googleapiclient.discovery.build", return_value=service), \
            patch("services.connections.atomic_token") as save, patch("services.connections.record"):
            finish("google", {"flow": flow, "calendar_id": "primary"}, {"code": ["synthetic-code"]})
            save.assert_called_once()
            service.events.assert_not_called()
            self.assertEqual(flow.oauth2session.token, warning.token)

    def test_google_missing_scope_is_rejected_without_saving_token(self):
        from services.connections import finish
        flow = MagicMock()
        warning = Warning("scope changed")
        warning.token = {"access_token": "synthetic-token"}
        warning.new_scope = ["openid"]
        flow.fetch_token.side_effect = warning
        with patch("googleapiclient.discovery.build") as build, \
            patch("services.connections.atomic_token") as save:
            with self.assertRaisesRegex(ValueError, "missing_calendar_permissions"):
                finish("google", {"flow": flow, "calendar_id": "primary"}, {"code": ["synthetic-code"]})
            save.assert_not_called()
            build.assert_not_called()

    def test_three_destinations_and_partial_failure(self):
        from streamlit.testing.v1 import AppTest
        with patch("frontend.redesign.connection_status", return_value={"bitrix24": True, "google": True}), \
            patch("frontend.redesign.sync_agreement") as send:
            app = AppTest.from_file(str(ROOT / "frontend/app.py"), default_timeout=20).run()
            app.button(key="tasks_1_open").click().run()
            for key in ("sync_both", "sync_bitrix24", "sync_google"):
                self.assertTrue(app.button(key=key).disabled)
            app.button(key="confirm_task").click().run()
            send.assert_not_called()
            app.button(key="sync_bitrix24").click().run()
            self.assertEqual(send.call_args_list[-1].args, (1, "bitrix24"))
            send.reset_mock()
            app.button(key="sync_google").click().run()
            self.assertEqual(send.call_args_list[-1].args, (1, "google"))
            send.reset_mock()
            send.side_effect = [IntegrationError("Тестовая ошибка CRM"), "calendar-url"]
            app.button(key="sync_both").click().run()
            self.assertEqual([call.args for call in send.call_args_list], [(1, "bitrix24"), (1, "google")])
            self.assertTrue(any("Тестовая ошибка CRM" in e.value for e in app.error))
            self.assertTrue(any("Подтверждено сервисом" in e.value for e in app.success))
            self.assertEqual(len(app.exception), 0, list(app.exception))


if __name__ == "__main__":
    unittest.main()
