import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel

from database import init_db
from services.storage import (get_call, get_recent_calls, get_clients, get_agreements,
    enrich_agreements, update_agreement, update_call, set_agreement_status)
from services.settings import get_settings, save_settings
from services.integrations import sync_agreement, IntegrationError
from services.workspace import deals, create_deal, record_outcome, link_deal, confirm_task, analytics
from datetime import date


def authenticate(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("CALLMIND_API_KEY", "")
    if not expected:
        raise HTTPException(503, "Настройте CALLMIND_API_KEY на сервере")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Нет доступа")


@asynccontextmanager
async def lifespan(app):
    init_db()
    from services.crm import start_worker
    start_worker()
    from services.calendar_sync import start_worker as start_calendar_worker
    start_calendar_worker()
    yield


class CrmClientUpdate(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    company_id: str = ""
    manager_id: int | None = None


class CrmTaskUpdate(BaseModel):
    responsible_id: int | None = None
    deal_id: int | None = None


class CalendarOptionsUpdate(BaseModel):
    duration_minutes: int
    reminder_minutes: int
    reminders_enabled: bool
    restore: bool = False


app = FastAPI(title="CallMind API", version="0.1.0", lifespan=lifespan,
    dependencies=[Depends(authenticate)], docs_url=None, redoc_url=None, openapi_url=None)


class AgreementUpdate(BaseModel):
    description: str
    responsible: Literal["manager", "client", "unknown"]
    deadline: datetime | None = None
    date_only: bool = True
    priority: Literal["low", "normal", "high"] = "normal"
    kind: Literal["task", "meeting"] = "task"


class CallUpdate(BaseModel):
    summary: str
    transcript: str
    next_action: str = ""
    follow_up_required: bool = False


class StatusUpdate(BaseModel):
    status: Literal["pending", "done"]


def apply_change(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.patch("/clients/{client_id}")
def edit_client(client_id: int, body: CrmClientUpdate):
    from services.crm import save_client
    apply_change(save_client, client_id, body.model_dump())
    return {"saved": "CallMind"}


@app.put("/agreements/{agreement_id}/crm")
def edit_crm_options(agreement_id: int, body: CrmTaskUpdate):
    from services.crm import save_task_options
    apply_change(save_task_options, agreement_id, **body.model_dump())
    return {"saved": "CallMind"}


@app.get("/crm/bindings")
def list_crm_bindings():
    from services.crm import bindings
    return bindings()


@app.get("/calendar/bindings")
def calendar_bindings():
    from services.calendar_sync import rows
    return rows()


@app.post("/calendar/reconcile")
def reconcile_calendar():
    from services.calendar_sync import sync_cycle
    apply_change(sync_cycle, force=True)
    return {"checked": True}


@app.put("/calendar/bindings/{key}/options")
def calendar_options(key: str, body: CalendarOptionsUpdate):
    from services.calendar_sync import save_options
    apply_change(save_options, key, **body.model_dump())
    return {"saved": "CallMind"}


@app.post("/calendar/conflicts/{key}/{field}/{choice}")
def calendar_choice(key: str, field: str, choice: Literal["local", "remote"]):
    from services.calendar_sync import resolve
    from services.integrations import GoogleCalendar
    apply_change(lambda: resolve(GoogleCalendar(get_settings()), key, field, choice))
    return {"saved": "CallMind"}


@app.post("/crm/reconcile")
def reconcile_crm():
    from services.crm import sync_cycle
    apply_change(sync_cycle, force=True)
    return {"checked": True}


@app.post("/clients/{client_id}/crm/contact/{external_id}")
def select_crm_contact(client_id: int, external_id: int):
    from services.crm import bind_contact
    from services.integrations import Bitrix24
    return {"id": apply_change(lambda: bind_contact(Bitrix24(), client_id, external_id))}


@app.post("/clients/{client_id}/crm/deals")
def fetch_crm_deals(client_id: int):
    from services.crm import import_deals
    from services.integrations import Bitrix24
    return {"count": apply_change(lambda: import_deals(Bitrix24(), client_id))}


@app.post("/crm/conflicts/{key}/{field}/{choice}")
def choose_crm_value(key: str, field: str, choice: Literal["local", "remote"]):
    from services.crm import resolve_conflict
    from services.integrations import Bitrix24
    apply_change(lambda: resolve_conflict(Bitrix24(), key, field, choice))
    return {"saved": True}


@app.get("/calls")
def calls(limit: int = 100):
    if not 1 <= limit <= 1000:
        raise HTTPException(400, "limit: от 1 до 1000")
    return get_recent_calls(limit)


@app.get("/calls/{call_id}")
def call(call_id: int):
    result = get_call(call_id)
    if result is None:
        raise HTTPException(404, "Звонок не найден")
    result["agreements"] = enrich_agreements(result["agreements"])
    return result


@app.patch("/calls/{call_id}")
def edit_call(call_id: int, body: CallUpdate):
    apply_change(update_call, call_id, **body.model_dump())
    return call(call_id)


@app.get("/clients")
def clients():
    from services.crm import client_data
    return [{**c, **client_data(c["id"])} for c in get_clients()]


@app.get("/agreements")
def agreements():
    return enrich_agreements(get_agreements())


@app.patch("/agreements/{agreement_id}")
def edit_agreement(agreement_id: int, body: AgreementUpdate):
    apply_change(update_agreement, agreement_id, **body.model_dump())
    return {"saved": True}


@app.patch("/agreements/{agreement_id}/status")
def edit_status(agreement_id: int, body: StatusUpdate):
    if not set_agreement_status(agreement_id, body.status):
        raise HTTPException(404, "Договорённость не найдена")
    return {"saved": True}


@app.post("/agreements/{agreement_id}/sync/{provider}")
def synchronize(agreement_id: int, provider: Literal["bitrix24", "google"]):
    return {"url": apply_change(sync_agreement, agreement_id, provider)}


@app.get("/settings")
def settings():
    return get_settings()


@app.put("/settings")
def edit_settings(body: dict):
    apply_change(save_settings, body)
    return get_settings()


class DealCreate(BaseModel):
    client_id: int
    title: str
    amount: str = ""
    currency: Literal["RUB", "BYN", "USD", "EUR"] = "RUB"
    created_at: datetime


class OutcomeUpdate(BaseModel):
    outcome: Literal["open", "won", "lost"]
    occurred_at: datetime
    reason: str = ""
    actor: str


@app.get("/deals")
def list_deals():
    return deals()


@app.post("/deals")
def add_deal(body: DealCreate):
    return {"id": apply_change(create_deal, **body.model_dump())}


@app.post("/deals/{deal_id}/outcomes")
def confirm_outcome(deal_id: int, body: OutcomeUpdate):
    apply_change(record_outcome, deal_id, **body.model_dump())
    return {"saved": True, "source": "manual"}


@app.post("/deals/{deal_id}/links/{entity_type}/{entity_id}")
def add_link(deal_id: int, entity_type: Literal["call", "agreement"], entity_id: int):
    apply_change(link_deal, entity_type, entity_id, deal_id)
    return {"saved": True}


@app.post("/agreements/{agreement_id}/review")
def review_agreement(agreement_id: int):
    apply_change(confirm_task, agreement_id)
    return {"saved": True}


@app.get("/analytics")
def get_analytics(start: date, end: date):
    return apply_change(analytics, start, end)
