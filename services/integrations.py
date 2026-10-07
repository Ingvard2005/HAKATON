"""Explicit, server-side Bitrix24 and Google Calendar synchronisation."""
import hashlib
import os
from datetime import timedelta
from urllib.parse import urlparse
from pathlib import Path

import httpx
from sqlalchemy import select
from dotenv import load_dotenv

from database import SessionLocal, DATA_DIR
from models import ExternalLink, AppSetting, IntegrationNotice, Agreement
from services.settings import get_settings
from services.dates import deadline_with_zone, local_now
from services.storage import get_agreements, enrich_agreements, get_call

load_dotenv()
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendars.readonly"]


class IntegrationError(ValueError):
    pass


class RemoteMissing(IntegrationError):
    remote_missing = True


def form_fields(value, prefix=""):
    """Classic Bitrix endpoints accept nested form fields, including OAuth auth."""
    if isinstance(value, dict):
        return {key: item for name, child in value.items() for key, item in form_fields(child, f"{prefix}[{name}]" if prefix else name).items()}
    if isinstance(value, (list, tuple)):
        return {key: item for n, child in enumerate(value) for key, item in form_fields(child, f"{prefix}[{n}]").items()}
    return {prefix: "" if value is None else value}


def installation_id():
    import uuid
    with SessionLocal() as db:
        row = db.get(AppSetting, "installation_id")
        if not row:
            row = AppSetting(key="installation_id", value=uuid.uuid4().hex)
            db.add(row)
            db.commit()
        return row.value


def connection_status():
    from services.connections import token_path
    return {
        "bitrix24": token_path("bitrix24").is_file() or bool(os.getenv("BITRIX24_WEBHOOK_URL") and os.getenv("BITRIX24_RESPONSIBLE_ID")),
        "google": Path(os.getenv("GOOGLE_TOKEN_FILE", str(DATA_DIR / "google-token.json"))).is_file(),
    }


class Bitrix24:
    def __init__(self):
        from services.connections import token_path, bitrix_credentials
        self.auth = None
        self.crm_enabled = False
        if token_path("bitrix24").is_file():
            try:
                credentials = bitrix_credentials()
                self.origin = credentials["portal"]
                self.base = self.origin + "/rest/"
                self.responsible = int(os.getenv("BITRIX24_RESPONSIBLE_ID") or credentials["user_id"])
                if self.responsible <= 0:
                    raise ValueError("invalid user")
                self.auth = credentials["access_token"]
                self.crm_enabled = "crm" in credentials.get("scope", "").split(",")
                self.account = hashlib.sha256(f"{urlparse(self.origin).netloc}|{credentials['user_id']}|{self.responsible}".encode()).hexdigest()[:24]
                return
            except Exception:
                raise IntegrationError("Повторите подключение Bitrix24 в настройках") from None
        self.base = os.getenv("BITRIX24_WEBHOOK_URL", "").rstrip("/") + "/"
        parsed = urlparse(self.base)
        if parsed.scheme != "https" or not parsed.hostname or "/rest/" not in parsed.path:
            raise IntegrationError("Настройте входящий вебхук Bitrix24 на сервере")
        try:
            self.responsible = int(os.getenv("BITRIX24_RESPONSIBLE_ID", "0"))
        except ValueError as error:
            raise IntegrationError("Укажите ID ответственного в Bitrix24") from error
        if self.responsible <= 0:
            raise IntegrationError("Укажите ID ответственного в Bitrix24")
        self.account = hashlib.sha256(f"{parsed.netloc}|{parsed.path.split('/rest/')[1].split('/')[0]}|{self.responsible}".encode()).hexdigest()[:24]
        self.origin = f"https://{parsed.netloc}"

    def response(self, method, payload):
        # Never propagate raw HTTP exceptions: webhook URLs contain a secret.
        try:
            arguments = {"data": form_fields({**payload, "auth": self.auth})} if self.auth else {"json": payload}
            response = httpx.post(self.base + method + ".json", **arguments, timeout=20)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise IntegrationError("Bitrix24 не подтвердил запрос. Проверьте подключение и журнал портала.") from None
        if data.get("error"):
            if data["error"] in {"ERROR_NOT_FOUND", "TASK_NOT_FOUND", "NOT_FOUND"}:
                raise RemoteMissing("Запись удалена или больше недоступна в CRM. История CallMind сохранена.")
            raise IntegrationError("Bitrix24 отклонил запрос. Проверьте права вебхука и поля задачи.")
        if "result" not in data:
            raise IntegrationError("Bitrix24 не вернул результат запроса")
        return data

    def request(self, method, payload):
        return self.response(method, payload)["result"]

    def list_all(self, method, payload):
        result, start = [], 0
        for _ in range(100):
            reply = self.response(method, {**payload, "start": start})
            result.extend(reply["result"])
            if "next" not in reply:
                return result
            if int(reply["next"]) <= start:
                raise IntegrationError("CRM вернула некорректную страницу результатов")
            start = int(reply["next"])
        raise IntegrationError("Слишком большой список CRM. Уточните фильтр.")

    def prepare(self, item):
        if not self.crm_enabled:
            return
        from services.crm import ensure_contact, task_options, binding
        options = task_options(item["id"])
        if options["deal_id"]:
            deal = binding(self.origin, "deal", options["deal_id"])
            if not deal or not deal["external_id"]:
                raise IntegrationError("Выбранная сделка ещё не связана с текущим порталом CRM")
            item["crm_deal_id"] = deal["external_id"]
        item["crm_contact_id"] = ensure_contact(self, item["client_id"])
        item["crm_executor"] = options["responsible_id"] or self.responsible
        # A manually entered executor is checked before any task is created.
        if options["responsible_id"]:
            users = self.request("user.get", {"ID": options["responsible_id"]})
            if not users or str(users[0].get("ACTIVE")) not in {"True", "Y", "1"}:
                raise IntegrationError("Исполнитель CRM не найден или неактивен")

    def sync(self, item, external_id, settings):
        fields = {
            "TITLE": settings["task_template"].format(description=item["description"], client=item["client_name"]),
            "DESCRIPTION": f"Клиент: {item['client_name']}\nТелефон: {item.get('client_phone') or 'не указан'}\n"
                f"Сторона обязательства: {item['responsible']}\nПодтверждение: {item['evidence']}\nCallMind #{item['id']}",
            "RESPONSIBLE_ID": item.get("crm_executor", self.responsible),
            "PRIORITY": {"low": 0, "normal": 1, "high": 2}[item["priority"]],
            "DEADLINE": deadline_with_zone(item, settings["timezone"]).isoformat() if item["deadline"] else "",
        }
        # Optional explicit CRM binding; never guess a contact/deal from its name.
        binding = os.getenv("BITRIX24_CRM_BINDING")
        if binding:
            fields["UF_CRM_TASK"] = [binding]
        if item.get("crm_contact_id"):
            fields["UF_CRM_TASK"] = ["C_" + str(item["crm_contact_id"])]
            if item.get("crm_deal_id"):
                fields["UF_CRM_TASK"].append("D_" + str(item["crm_deal_id"]))
        if external_id:
            self.request("tasks.task.update", {"taskId": external_id, "fields": fields})
        else:
            result = self.request("tasks.task.add", {"fields": fields})
            external_id = str(result["task"]["id"])
            # Save the acknowledged ID before a verification request can time out.
            with SessionLocal() as db:
                link = db.get(ExternalLink, f"{item['id']}:bitrix24:{self.account}")
                if link:
                    link.external_id = external_id
                    db.commit()
            if getattr(self, "crm_enabled", False):
                from services.crm import remember_task
                remember_task(self, item, external_id)
        task = self.request("tasks.task.get", {"taskId": external_id})["task"]
        remote_done = str(task.get("status")) == "5"
        if item["status"] == "done" and not remote_done:
            self.request("tasks.task.complete", {"taskId": external_id})
        elif item["status"] == "pending" and remote_done:
            self.request("tasks.task.renew", {"taskId": external_id})
        if getattr(self, "crm_enabled", False):
            from services.crm import register_task
            register_task(self, item, external_id)
        return external_id, f"{self.origin}/company/personal/user/{self.responsible}/tasks/task/view/{external_id}/"


def google_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    path = Path(os.getenv("GOOGLE_TOKEN_FILE", str(DATA_DIR / "google-token.json")))
    if not path.is_file():
        raise IntegrationError("Сначала авторизуйте Google Calendar по инструкции в README")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), GOOGLE_SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            from services.connections import atomic_token
            atomic_token(path, credentials.to_json())
        if not credentials.valid:
            raise ValueError("invalid token")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except Exception:
        raise IntegrationError("Не удалось авторизовать Google Calendar. Повторите подключение.") from None


def calendar_payload(item, settings):
    if not item["deadline"]:
        raise IntegrationError("Для события сначала укажите дату")
    if item["date_only"]:
        start = {"date": item["deadline"].date().isoformat()}
        end = {"date": (item["deadline"].date() + timedelta(days=1)).isoformat()}
    else:
        value = deadline_with_zone(item, settings["timezone"])
        start = {"dateTime": value.isoformat(), "timeZone": settings["timezone"]}
        end = {"dateTime": (value + timedelta(minutes=settings["event_minutes"] if item.get("kind") == "meeting" else 1)).isoformat(), "timeZone": settings["timezone"]}
    return {
        "summary": item["description"] if item.get("kind") == "meeting" else "Напоминание: " + item["description"],
        "description": f"Клиент: {item['client_name']}\nПодтверждение: {item['evidence']}\nCallMind #{item['id']}",
        "start": start, "end": end,
        "status": "cancelled" if item["status"] == "done" and item.get("kind") != "meeting" else "confirmed",
        "transparency": "opaque" if item.get("kind") == "meeting" else "transparent",
        "visibility": "private",
        "reminders": {"useDefault": False, "overrides": [] if item["status"] == "done" else [
            {"method": "popup", "minutes": settings["reminder_minutes"]}]},
    }


class GoogleCalendar:
    supports_reconciliation = True
    def __init__(self, settings):
        self.service = google_service()
        self.calendar_id = settings["calendar_id"]
        # Resolve primary to an actual calendar ID, stable across OAuth reconnects.
        try:
            self.calendar_id = self.service.calendars().get(calendarId=self.calendar_id).execute()["id"]
        except Exception:
            raise IntegrationError("Нет доступа к календарю. Проверьте ID и повторите подключение Google.") from None
        self.account = hashlib.sha256(self.calendar_id.encode()).hexdigest()[:24]

    def sync(self, item, external_id, settings):
        from googleapiclient.errors import HttpError
        events = self.service.events()
        if not item["deadline"]:
            if not external_id:
                raise IntegrationError("Без срока нельзя создать напоминание; ранее отправленное событие не найдено")
            try:
                result = events.patch(calendarId=self.calendar_id, eventId=external_id,
                    body={"status": "cancelled", "reminders": {"useDefault": False, "overrides": []}}, sendUpdates="none").execute()
                return result["id"], result.get("htmlLink")
            except Exception:
                raise IntegrationError("Google Calendar не подтвердил отмену события после удаления срока") from None
        body = calendar_payload(item, settings)
        event_id = external_id or hashlib.sha256(f"{installation_id()}:{item['id']}:{self.account}".encode()).hexdigest()
        try:
            try:
                events.get(calendarId=self.calendar_id, eventId=event_id).execute()
                result = events.update(calendarId=self.calendar_id, eventId=event_id, body=body, sendUpdates="none").execute()
            except HttpError as error:
                if error.resp.status != 404:
                    raise
                body["id"] = event_id
                result = events.insert(calendarId=self.calendar_id, body=body, sendUpdates="none").execute()
            return result["id"], result.get("htmlLink")
        except Exception:
            raise IntegrationError("Google Calendar не подтвердил синхронизацию. Проверьте доступ и повторите.") from None


def sync_agreement(agreement_id, provider):
    """Keep independent durable messages, including failures before adapter creation."""
    try:
        result = _sync_agreement(agreement_id, provider)
    except IntegrationError as error:
        _record_notice(agreement_id, provider, False, str(error))
        raise
    _record_notice(agreement_id, provider, True, "Подтверждено сервисом")
    return result


def _record_notice(agreement_id, provider, success, message):
    if provider not in {"bitrix24", "google"}:
        return
    with SessionLocal() as db:
        if not db.get(Agreement, agreement_id):
            return
        row = db.get(IntegrationNotice, (agreement_id, provider))
        if not row:
            row = IntegrationNotice(agreement_id=agreement_id, provider=provider)
            db.add(row)
        row.success, row.message, row.recorded_at = success, message, local_now(get_settings()["timezone"])
        if not success:
            for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == agreement_id, ExternalLink.provider == provider)):
                if link.state == "synced":
                    link.state = "outdated"
        db.commit()


def _sync_agreement(agreement_id, provider):
    if provider not in {"bitrix24", "google"}:
        raise IntegrationError("Неизвестная интеграция")
    items = enrich_agreements(get_agreements())
    item = next((i for i in items if i["id"] == agreement_id), None)
    if not item:
        raise IntegrationError("Договорённость не найдена")
    from services.workspace import tasks
    reviewed = next(i for i in tasks() if i["id"] == agreement_id)
    if reviewed["review_reasons"]:
        raise IntegrationError("Сначала проверьте договорённость и нажмите «Подтвердить» в CallMind")
    if item["responsible"] == "unknown":
        raise IntegrationError("Сначала уточните ответственного")
    transcript = get_call(item["call_id"])["transcript"]
    if not item["evidence"] or item["evidence"] not in transcript:
        raise IntegrationError("Цитата не найдена в транскрипции. Проверьте и исправьте текст перед синхронизацией.")
    settings = get_settings()
    adapter = Bitrix24() if provider == "bitrix24" else GoogleCalendar(settings)
    if provider == "google" and getattr(adapter, "supports_reconciliation", False) is True:
        from services.calendar_sync import transfer
        _, url = transfer(adapter, item, settings)
        return url
    if provider == "bitrix24" and getattr(adapter, "crm_enabled", False) is True:
        from services.crm import binding as crm_binding, reconcile
        task_binding = crm_binding(adapter.origin, "task", agreement_id)
        if task_binding and task_binding["external_id"]:
            adapter.prepare(item)  # Explicit repeat also reconciles the linked client.
            reconcile(adapter, task_binding["key"])
            current = crm_binding(adapter.origin, "task", agreement_id)
            if current["state"] != "synced":
                raise IntegrationError(current["message"] or "Сначала разрешите конфликт CRM")
            return f"{adapter.origin}/company/personal/user/{adapter.responsible}/tasks/task/view/{task_binding['external_id']}/"
        adapter.prepare(item)
    key = f"{agreement_id}:{provider}:{adapter.account}"
    # Durable claim serialises workers; ambiguous Bitrix creates require reconciliation.
    with SessionLocal() as db:
        from sqlalchemy import update
        link = db.get(ExternalLink, key)
        if link is None:
            link = ExternalLink(key=key, agreement_id=agreement_id, provider=provider, state="new")
            db.add(link)
            try:
                db.commit()
            except Exception:
                raise IntegrationError("Синхронизация уже запущена. Обновите страницу.") from None
        if link.state == "syncing" or (link.state == "uncertain" and provider == "bitrix24" and not link.external_id):
            raise IntegrationError("Предыдущий запрос не подтверждён. Проверьте портал и привяжите ID созданной задачи; автоматический повтор заблокирован.")
        external_id = link.external_id
        result = db.execute(update(ExternalLink).where(ExternalLink.key == key,
            ExternalLink.state == link.state).values(state="syncing", message=None, updated_at=local_now(settings["timezone"])))
        if result.rowcount != 1:
            raise IntegrationError("Синхронизация уже запущена")
        db.commit()
    try:
        external_id, url = adapter.sync(item, external_id, settings)
    except Exception as error:
        if provider == "bitrix24" and getattr(adapter, "crm_enabled", False) is True:
            from services.crm import binding, mark
            task_binding = binding(adapter.origin, "task", agreement_id)
            if task_binding and task_binding["state"] == "creating":
                mark(task_binding["key"], "error", "ID созданной задачи сохранён. Требуется повторная сверка.")
        with SessionLocal() as db:
            link = db.get(ExternalLink, key)
            link.state = "uncertain" if provider == "bitrix24" and not external_id else "error"
            link.message = str(error) if isinstance(error, IntegrationError) else "Не удалось синхронизировать"
            link.updated_at = local_now(settings["timezone"])
            db.commit()
        raise IntegrationError(link.message) from None
    with SessionLocal() as db:
        link = db.get(ExternalLink, key)
        link.external_id, link.url, link.state, link.message = external_id, url, "synced", None
        link.updated_at = local_now(settings["timezone"])
        db.commit()
    return url


def reconcile_bitrix(agreement_id, external_id):
    if not str(external_id).isdigit() or int(external_id) <= 0:
        raise IntegrationError("Укажите числовой ID задачи")
    adapter = Bitrix24()
    task = adapter.request("tasks.task.get", {"taskId": external_id})["task"]
    if f"CallMind #{agreement_id}" not in task.get("description", ""):
        raise IntegrationError("Задача не содержит идентификатор этой договорённости")
    key = f"{agreement_id}:bitrix24:{adapter.account}"
    with SessionLocal() as db:
        link = db.get(ExternalLink, key)
        if not link:
            raise IntegrationError("Нет неподтверждённой синхронизации")
        link.external_id, link.state = str(external_id), "outdated"
        db.commit()
