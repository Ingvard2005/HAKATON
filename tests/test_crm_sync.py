"""Synthetic SQLite and an in-memory CRM. Any real HTTP request fails the test."""
import copy
import json
import unittest
from unittest.mock import patch
from tests.test_ui_integrations import ProjectTests, SessionLocal, get_settings, local_now
from models import CrmBinding, Agreement, AgreementDetails, DealOutcomeEvent, Deal
from services import crm
from services.integrations import IntegrationError, RemoteMissing, form_fields
from services.workspace import tasks, confirm_task
seed_database = ProjectTests.setUp
del ProjectTests  # Do not rediscover the imported suite in this module.


def tearDownModule():
    from tests.test_ui_integrations import engine
    engine.dispose()  # Release Windows SQLite handles before temporary-directory cleanup.


class FakeCrm:
    origin = "https://synthetic.bitrix24.ru"
    responsible = 1
    crm_enabled = True
    account = "synthetic"

    def __init__(self):
        self.contacts, self.tasks, self.calls = {}, {}, []
        self.matches, self.fail_create = [], False

    def request(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == "crm.duplicate.findbycomm":
            return {"CONTACT": self.matches}
        if method == "crm.contact.add":
            if self.fail_create:
                raise IntegrationError("Timeout")
            self.contacts["11"] = copy.deepcopy(payload["fields"])
            return 11
        if method == "crm.contact.get":
            if str(payload["id"]) not in self.contacts:
                raise RemoteMissing("Удалено")
            return copy.deepcopy(self.contacts[str(payload["id"])])
        if method == "crm.contact.update":
            self.contacts[str(payload["id"])].update(copy.deepcopy(payload["fields"]))
            return True
        if method == "tasks.task.get":
            return {"task": copy.deepcopy(self.tasks[str(payload["taskId"])])}
        if method == "tasks.task.update":
            mapping = {"TITLE": "title", "DEADLINE": "deadline", "PRIORITY": "priority", "RESPONSIBLE_ID": "responsibleId", "UF_CRM_TASK": "ufCrmTask"}
            self.tasks[str(payload["taskId"])].update({mapping[k]: v for k, v in payload["fields"].items()})
            return True
        if method in {"tasks.task.complete", "tasks.task.renew"}:
            self.tasks[str(payload["taskId"])]["status"] = "5" if method.endswith("complete") else "2"
            return True
        raise AssertionError(f"Unexpected CRM method: {method}")

    def prepare(self, item):
        crm.ensure_contact(self, item["client_id"])


class CrmTests(unittest.TestCase):
    def setUp(self):
        seed_database(self)
        self.network = patch("httpx.post", side_effect=AssertionError("Real network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.adapter = FakeCrm()

    def contact(self):
        crm.ensure_contact(self.adapter, 1)
        return crm.binding(self.adapter.origin, "contact", 1)

    def task(self):
        self.contact()
        confirm_task(1)
        item = next(i for i in tasks() if i["id"] == 1)
        value = crm.task_snapshot(item, 1, get_settings(), self.adapter.origin)
        self.adapter.tasks["21"] = dict(title=value["title"], deadline=value["deadline"], status="2", priority=value["priority"], responsibleId=1, ufCrmTask=value["crm_links"])
        crm.register_task(self.adapter, item, "21")
        return crm.binding(self.adapter.origin, "task", 1)

    def test_field_merge_and_conflict(self):
        merged, conflict = crm.merge_fields({"name": "A", "phone": "1"}, {"name": "B", "phone": "1"}, {"name": "A", "phone": "2"})
        self.assertEqual(merged, {"name": "B", "phone": "2"})
        self.assertFalse(conflict)
        _, conflict = crm.merge_fields({"name": "A"}, {"name": "B"}, {"name": "C"})
        self.assertIn("name", conflict)

    def test_contact_creation_reuses_id(self):
        self.contact()
        crm.ensure_contact(self.adapter, 1)
        self.assertEqual(sum(m == "crm.contact.add" for m, _ in self.adapter.calls), 1)
        self.assertEqual(crm.client_data(1)["manager_id"], 1)

    def test_ambiguous_contacts_do_not_create(self):
        self.adapter.matches = [11, 12]
        with self.assertRaisesRegex(IntegrationError, "несколько"):
            self.contact()
        self.assertFalse(any(m.endswith(".add") for m, _ in self.adapter.calls))

    def test_uncertain_create_cannot_repeat(self):
        self.adapter.fail_create = True
        with self.assertRaises(IntegrationError):
            self.contact()
        with self.assertRaisesRegex(IntegrationError, "повтор заблокирован"):
            self.contact()
        self.assertEqual(sum(m == "crm.contact.add" for m, _ in self.adapter.calls), 1)

    def test_contact_bidirectional_merge_preserves_other_phone(self):
        row = self.contact()
        self.adapter.contacts["11"]["PHONE"].append(dict(ID="2", VALUE="999", VALUE_TYPE="HOME"))
        self.adapter.contacts["11"]["EMAIL"] = [dict(VALUE="crm@example.com", VALUE_TYPE="WORK")]
        crm.save_client(1, {**crm.client_data(1), "name": "Новое имя"})
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(crm.client_data(1)["email"], "crm@example.com")
        self.assertEqual(self.adapter.contacts["11"]["NAME"], "Новое имя")
        self.assertEqual(self.adapter.contacts["11"]["PHONE"][1]["VALUE"], "999")

    def test_conflict_requires_human_and_detects_stale_choice(self):
        row = self.contact()
        crm.save_client(1, {**crm.client_data(1), "name": "Локально"})
        self.adapter.contacts["11"]["NAME"] = "В CRM"
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(crm.binding(self.adapter.origin, "contact", 1)["state"], "conflict")
        self.adapter.contacts["11"]["NAME"] = "Еще правка"
        with self.assertRaisesRegex(ValueError, "изменились"):
            crm.resolve_conflict(self.adapter, row["key"], "name", "local")
        self.assertEqual(self.adapter.contacts["11"]["NAME"], "Еще правка")

    def test_human_choice_is_applied_on_next_reconcile(self):
        row = self.contact()
        crm.save_client(1, {**crm.client_data(1), "name": "Локально"})
        self.adapter.contacts["11"]["NAME"] = "В CRM"
        crm.reconcile(self.adapter, row["key"])
        crm.resolve_conflict(self.adapter, row["key"], "name", "remote")
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(crm.client_data(1)["name"], "В CRM")

    def test_inbound_completion_does_not_invent_time_or_deal_result(self):
        row = self.task()
        self.adapter.tasks["21"]["status"] = "5"
        crm.reconcile(self.adapter, row["key"])
        with SessionLocal() as db:
            self.assertEqual(db.get(Agreement, 1).status, "done")
            self.assertIsNone(db.get(AgreementDetails, 1).completed_at)
            self.assertEqual(db.query(DealOutcomeEvent).count(), 0)

    def test_unconfirmed_action_change_is_not_sent(self):
        row = self.task()
        with SessionLocal() as db:
            db.get(Agreement, 1).description = "Изменено"
            db.commit()
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(crm.binding(self.adapter.origin, "task", 1)["state"], "needs_review")
        self.assertNotEqual(self.adapter.tasks["21"]["title"], "Изменено")
        confirm_task(1)
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.tasks["21"]["title"], "Изменено")

    def test_deleted_contact_preserves_local_data(self):
        row = self.contact()
        self.adapter.contacts.clear()
        with self.assertRaises(IntegrationError):
            crm.reconcile(self.adapter, row["key"])
        self.assertEqual(crm.binding(self.adapter.origin, "contact", 1)["state"], "deleted")
        self.assertEqual(crm.client_data(1)["name"], "Тестовый клиент")

    def test_disabled_sync_does_not_connect_or_create(self):
        with patch("services.integrations.Bitrix24", side_effect=AssertionError("Should not connect")):
            crm.sync_cycle()
        self.assertFalse(self.adapter.calls)

    def test_selected_deal_updates_crm_links_preserving_unrelated_links(self):
        row = self.task()
        now = local_now().isoformat()
        crm.import_deal(self.adapter, 1, dict(ID="31", TITLE="Сделка", OPPORTUNITY="100", CURRENCY_ID="BYN", DATE_CREATE=now, DATE_MODIFY=now, STAGE_SEMANTIC_ID="P", STAGE_ID="NEW"))
        with SessionLocal() as db:
            deal_id = db.query(Deal).one().id
        crm.save_task_options(1, None, deal_id)
        self.adapter.tasks["21"]["ufCrmTask"].append("CO_91")
        crm.reconcile(self.adapter, row["key"])
        self.assertEqual(self.adapter.tasks["21"]["ufCrmTask"], ["C_11", "D_31", "CO_91"])

    def test_form_encoding_retains_nested_fields(self):
        self.assertEqual(form_fields({"fields": {"UF_CRM_TASK": ["C_11", "D_31"]}}), {"fields[UF_CRM_TASK][0]": "C_11", "fields[UF_CRM_TASK][1]": "D_31"})

    def test_inbound_completion_uses_actual_crm_timestamp(self):
        row = self.task()
        stamp = local_now().replace(microsecond=0)
        self.adapter.tasks["21"].update(status="5", closedDate=stamp.isoformat())
        crm.reconcile(self.adapter, row["key"])
        with SessionLocal() as db:
            self.assertEqual(db.get(AgreementDetails, 1).completed_at, stamp)

    def test_unverified_create_id_is_durable_and_not_synced(self):
        self.contact()
        item = next(i for i in tasks() if i["id"] == 1)
        crm.remember_task(self.adapter, item, "21")
        row = crm.binding(self.adapter.origin, "task", 1)
        self.assertEqual(row["external_id"], "21")
        self.assertEqual(row["state"], "creating")

    def test_initial_verification_difference_requires_choice(self):
        self.contact()
        item = next(i for i in tasks() if i["id"] == 1)
        self.adapter.tasks["21"] = dict(title="Другое название", deadline=None, status="2", priority=1, responsibleId=1, ufCrmTask=["C_11"])
        with self.assertRaisesRegex(ValueError, "разрешите"):
            crm.register_task(self.adapter, item, "21")
        row = crm.binding(self.adapter.origin, "task", 1)
        self.assertEqual(row["state"], "conflict")
        self.assertEqual(row["external_id"], "21")

    def test_portal_binding_cannot_be_updated_by_another_portal(self):
        row = self.contact()
        self.adapter.origin = "https://other.bitrix24.ru"
        self.adapter.calls.clear()
        with self.assertRaises(IntegrationError):
            crm.reconcile(self.adapter, row["key"])
        self.assertFalse(self.adapter.calls)

    def test_crm_result_is_deduplicated_and_has_no_invented_reason(self):
        stamp = local_now().isoformat()
        raw = dict(ID="31", TITLE="Сделка", OPPORTUNITY="100", CURRENCY_ID="BYN", DATE_CREATE=stamp, DATE_MODIFY=stamp, MOVED_TIME=stamp, STAGE_SEMANTIC_ID="F", STAGE_ID="LOSE")
        crm.import_deal(self.adapter, 1, raw)
        crm.import_deal(self.adapter, 1, raw)
        with SessionLocal() as db:
            event = db.query(DealOutcomeEvent).one()
            self.assertEqual(event.outcome, "lost")
            self.assertEqual(event.source, "crm")
            self.assertIsNone(event.reason)

    def test_api_requires_auth_and_reports_unconnected_crm(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        client = TestClient(app)
        self.assertEqual(client.get("/crm/bindings").status_code, 401)
        self.assertEqual(client.post("/clients/1/crm/deals", headers={"X-API-Key": "test-key"}).status_code, 400)

    def test_explicit_repeat_reconciles_client_without_recreating_task(self):
        self.task()
        crm.save_client(1, {**crm.client_data(1), "email": "updated@example.com"})
        from services.integrations import sync_agreement
        with patch("services.integrations.Bitrix24", return_value=self.adapter):
            sync_agreement(1, "bitrix24")
        self.assertEqual(self.adapter.contacts["11"]["EMAIL"][0]["VALUE"], "updated@example.com")
        self.assertFalse(any(m == "tasks.task.add" for m, _ in self.adapter.calls))


if __name__ == "__main__":
    unittest.main()
