"""Linked Google events: three-way merge, conditional writes, no automatic creation."""
import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from database import SessionLocal
from models import CalendarBinding, ExternalLink, Agreement, AgreementDetails, AppSetting
from services.settings import get_settings
from services.dates import local_now, normalize_datetime
from services.crm import dump, merge_fields, journal
from services.integrations import IntegrationError, calendar_payload, installation_id

LOCK = threading.RLock()


def item_for(agreement_id):
    from services.workspace import tasks
    item = next((i for i in tasks() if i["id"] == agreement_id), None)
    if not item:
        raise ValueError("Договорённость не найдена")
    return item


def rows(agreement_id=None):
    with SessionLocal() as db:
        query = select(CalendarBinding, ExternalLink).join(ExternalLink, CalendarBinding.key == ExternalLink.key)
        if agreement_id is not None:
            query = query.where(ExternalLink.agreement_id == agreement_id)
        return [dict(key=b.key, calendar_id=b.calendar_id, baseline=json.loads(b.baseline),
                     conflicts=json.loads(b.conflicts), options=json.loads(b.local_options), etag=b.etag,
                     agreement_id=l.agreement_id, external_id=l.external_id, state=l.state,
                     message=l.message, url=l.url, updated_at=l.updated_at) for b, l in db.execute(query)]


def row_for(key):
    return next((r for r in rows() if r["key"] == key), None)


def mark(key, state, message=None, baseline=None, conflicts=None, etag=None, url=None):
    with SessionLocal() as db:
        link, binding = db.get(ExternalLink, key), db.get(CalendarBinding, key)
        link.state, link.message, link.updated_at = state, message, local_now(get_settings()["timezone"])
        if url:
            link.url = url
        if baseline is not None:
            binding.baseline = dump(baseline)
        if conflicts is not None:
            binding.conflicts = dump(conflicts)
        if etag:
            binding.etag = etag
        db.commit()


def local_snapshot(item, settings, options):
    due = item["deadline"]
    if due and item["date_only"]:
        due = due.replace(hour=0, minute=0, second=0, microsecond=0)
    reminder = options.get("reminders", {"useDefault": False, "overrides": [{"method": "popup", "minutes": settings["reminder_minutes"]}]})
    if item["status"] == "done":
        reminder = {"useDefault": False, "overrides": []}
    return dict(action=item["description"], deadline=due.isoformat() if due else None,
                date_only=item["date_only"], kind=item["kind"],
                duration_minutes=1440 if item["date_only"] else options.get("duration_minutes", settings["event_minutes"]) if item["kind"] == "meeting" else 1,
                reminders=reminder,
                calendar_state="cancelled" if options.get("cancelled") or not due or (item["status"] == "done" and item["kind"] == "task") else "active")


def remote_snapshot(raw, base, settings):
    if raw.get("status") == "cancelled":
        # Tombstones can contain only ID/status. Cancellation is never task completion.
        return {**base, "calendar_state": "cancelled"}
    if raw.get("recurrence") or raw.get("recurringEventId"):
        raise IntegrationError("Повторяющееся событие требует ручной проверки; CallMind переносит отдельные события.")
    kind = base["kind"]
    if raw.get("transparency", "opaque") != ("opaque" if kind == "meeting" else "transparent"):
        raise IntegrationError("Занятость события расходится с типом договорённости. Проверьте тип в CallMind и занятость в Google.")
    start, end = raw.get("start", {}), raw.get("end", {})
    date_only = "date" in start
    if date_only:
        due = datetime.fromisoformat(start["date"])
        finish = datetime.fromisoformat(end["date"])
    else:
        due = normalize_datetime(start["dateTime"], settings["timezone"])
        finish = normalize_datetime(end["dateTime"], settings["timezone"])
    seconds = (finish - due).total_seconds()
    if seconds <= 0 or seconds % 60 or (date_only and seconds != 86400):
        raise IntegrationError("Неподдерживаемая длительность события. Нужна проверка; данные не перезаписаны.")
    if kind == "task" and not date_only and seconds != 60:
        raise IntegrationError("Задача — минутное напоминание без занятости. Встречу нужно явно выбрать в CallMind.")
    title = raw.get("summary", "").strip()
    if kind == "task" and title.startswith("Напоминание: "):
        title = title[len("Напоминание: "):]
    if not title:
        raise IntegrationError("В Google отсутствует название. Уточните действие перед сверкой.")
    return dict(action=title, deadline=due.isoformat(), date_only=date_only,
                kind=kind, duration_minutes=int(seconds / 60),
                reminders=raw.get("reminders", {"useDefault": True}), calendar_state="active")


def payload(values, item, settings):
    copy = {**item, "description": values["action"], "deadline": normalize_datetime(values["deadline"], settings["timezone"]) if values["deadline"] else None,
            "date_only": values["date_only"], "kind": values["kind"]}
    if not copy["deadline"]:
        return {"status": "cancelled", "reminders": {"useDefault": False, "overrides": []}}
    result = calendar_payload(copy, {**settings, "event_minutes": values["duration_minutes"]})
    result["status"] = "cancelled" if values["calendar_state"] == "cancelled" else "confirmed"
    result["reminders"] = values["reminders"] if values["calendar_state"] == "active" and item["status"] != "done" else {"useDefault": False, "overrides": []}
    return result


def read(adapter, event_id):
    return adapter.service.events().get(calendarId=adapter.calendar_id, eventId=event_id).execute()


def status_code(error):
    return getattr(getattr(error, "resp", None), "status", None)


def conditional_patch(adapter, event_id, raw, body):
    if not raw.get("etag"):
        raise IntegrationError("Google не вернул версию события; безопасное обновление приостановлено.")
    request = adapter.service.events().patch(calendarId=adapter.calendar_id, eventId=event_id, body=body, sendUpdates="none")
    request.headers["If-Match"] = raw["etag"]
    return request.execute()


def apply_local(row, expected, values):
    with SessionLocal() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        binding = db.get(CalendarBinding, row["key"])
        current_options = json.loads(binding.local_options)
        current = local_snapshot(item_for(row["agreement_id"]), get_settings(), current_options)
        if current != expected:
            raise IntegrationError("В CallMind появились новые правки во время сверки. Повторите проверку.")
        agreement = db.get(Agreement, row["agreement_id"])
        details = db.get(AgreementDetails, agreement.id) or AgreementDetails(agreement_id=agreement.id, priority="normal")
        agreement.description = values["action"]
        agreement.deadline = normalize_datetime(values["deadline"], get_settings()["timezone"]) if values["deadline"] else None
        details.date_only, details.kind = values["date_only"], values["kind"]
        db.add(details)
        options = dict(current_options)
        if values["duration_minutes"] != expected["duration_minutes"]:
            options["duration_minutes"] = values["duration_minutes"]
        if values["reminders"] != expected["reminders"]:
            options["reminders"] = values["reminders"]
        if values["calendar_state"] != expected["calendar_state"]:
            options["cancelled"] = values["calendar_state"] == "cancelled"
        binding.local_options = dump(options)
        if values != expected:
            journal(db, "agreement", agreement.id, expected, values, "Google Calendar / синхронизация")
            for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == agreement.id, ExternalLink.provider == "bitrix24")):
                if link.state == "synced":
                    link.state = "outdated"
        db.commit()


def reconcile(adapter, key):
    with LOCK:
        row = row_for(key)
        if not row or row["calendar_id"] != adapter.calendar_id:
            raise IntegrationError("Связь с выбранным календарём не найдена")
        if row["state"] in {"conflict", "deleted", "creating"}:
            return row
        if row["state"] == "syncing" and row["updated_at"] > local_now(get_settings()["timezone"]) - timedelta(minutes=3):
            return row
        with SessionLocal() as db:
            claim = db.execute(update(ExternalLink).where(ExternalLink.key == key, ExternalLink.state == row["state"]).values(state="syncing", updated_at=local_now(get_settings()["timezone"])))
            if claim.rowcount != 1:
                raise IntegrationError("Сверка уже выполняется")
            db.commit()
        try:
            item, settings = item_for(row["agreement_id"]), get_settings()
            local = local_snapshot(item, settings, row["options"])
            raw = read(adapter, row["external_id"])
            # A changed local kind is explicit; remote transparency alone never changes kind.
            base = row["baseline"]
            remote = remote_snapshot(raw, base, settings)
            if local["kind"] != base["kind"] and remote["kind"] == base["kind"]:
                remote["kind"] = base["kind"]
            merged, conflicts = merge_fields(base, local, remote)
            if conflicts:
                mark(key, "conflict", "Поле изменено в CallMind и Google. Выберите значение.", conflicts=conflicts, etag=raw.get("etag"))
                return row_for(key)
            changed = {f for f in merged if merged[f] != remote[f]}
            if changed and item["review_reasons"]:
                mark(key, "needs_review", "Сохраните правки и подтвердите договорённость перед отправкой в Google")
                return row_for(key)
            if changed:
                desired = payload(merged, item, settings)
                mapping = {"action": {"summary"}, "deadline": {"start", "end", "status", "reminders"},
                           "date_only": {"start", "end"}, "kind": {"summary", "transparency", "start", "end"},
                           "duration_minutes": {"end"}, "reminders": {"reminders"}, "calendar_state": {"status", "reminders"}}
                names = set().union(*(mapping[f] for f in changed))
                body = {f: desired[f] for f in names if f in desired}
                conditional_patch(adapter, row["external_id"], raw, body)
                raw = read(adapter, row["external_id"])
                remote = remote_snapshot(raw, merged, settings)
                if any(remote[f] != merged[f] for f in changed):
                    raise IntegrationError("Google не подтвердил новые значения. Требуется повторная сверка.")
            apply_local(row, local, merged)
            mark(key, "synced", "Событие отменено; договорённость остаётся в истории" if merged["calendar_state"] == "cancelled" else None,
                 baseline=merged, conflicts={}, etag=raw.get("etag"), url=raw.get("htmlLink"))
            return row_for(key)
        except Exception as error:
            code = status_code(error)
            state = "deleted" if code in {404, 410} else "outdated" if code == 412 else "error"
            message = "Событие удалено или недоступно. Договорённость сохранена; автоматическое воссоздание отключено." if state == "deleted" else \
                      "Google изменился во время записи. Данные не перезаписаны; нужна новая сверка." if code == 412 else \
                      str(error) if isinstance(error, ValueError) else "Google не подтвердил сверку. Локальные данные сохранены."
            mark(key, state, message)
            raise IntegrationError(message) from None


def transfer(adapter, item, settings):
    """Called only after server-side human review validation."""
    key = f"{item['id']}:google:{adapter.account}"
    existing = row_for(key)
    if existing:
        retry = existing["state"] == "uncertain" or (existing["state"] == "creating" and existing["updated_at"] < local_now(settings["timezone"]) - timedelta(minutes=3))
        if retry:
            # Explicit retry only, with the same event ID. Background workers never create.
            try:
                read(adapter, existing["external_id"])
            except Exception as error:
                if status_code(error) != 404:
                    raise IntegrationError("Google не подтвердил проверку события. Повторите позже.") from None
                values = local_snapshot(item, settings, existing["options"])
                if not values["deadline"]:
                    raise IntegrationError("Укажите срок перед повтором первого переноса")
                try:
                    adapter.service.events().insert(calendarId=adapter.calendar_id, body={**payload(values, item, settings), "id": existing["external_id"]}, sendUpdates="none").execute()
                except Exception as create_error:
                    if status_code(create_error) != 409:
                        raise IntegrationError("Google не подтвердил повтор. Сохранён прежний ID события.") from None
                mark(key, "outdated", baseline=values)
            else:
                mark(key, "outdated")
        current = reconcile(adapter, key)
        if current["state"] != "synced":
            raise IntegrationError(current["message"] or "Сначала разрешите состояние календаря")
        return current["external_id"], current["url"]
    if not item["deadline"]:
        raise IntegrationError("Для первого переноса в календарь укажите срок")
    expected = local_snapshot(item, settings, {})
    with SessionLocal() as db:
        link = db.get(ExternalLink, key)
        event_id = link.external_id if link and link.external_id else hashlib.sha256(f"{installation_id()}:{item['id']}:{adapter.account}".encode()).hexdigest()
        if not link:
            link = ExternalLink(key=key, agreement_id=item["id"], provider="google", external_id=event_id, state="creating")
            db.add(link)
        else:
            # Legacy event: capture differences before sending any overwrite.
            link.state = "creating"
        db.add(CalendarBinding(key=key, calendar_id=adapter.calendar_id, baseline=dump(expected)))
        try:
            db.commit()
        except Exception:
            raise IntegrationError("Перенос уже выполняется; обновите состояние") from None
    try:
        try:
            raw = read(adapter, event_id)
        except Exception as error:
            if status_code(error) != 404 or (link and link.external_id and link.external_id != hashlib.sha256(f"{installation_id()}:{item['id']}:{adapter.account}".encode()).hexdigest()):
                raise
            body = {**payload(expected, item, settings), "id": event_id}
            try:
                adapter.service.events().insert(calendarId=adapter.calendar_id, body=body, sendUpdates="none").execute()
            except Exception as create_error:
                if status_code(create_error) != 409:
                    raise
            raw = read(adapter, event_id)
        remote = remote_snapshot(raw, expected, settings)
        conflicts = {f: dict(base=None, local=expected[f], remote=remote[f]) for f in expected if expected[f] != remote[f]}
        mark(key, "conflict" if conflicts else "synced", "Google содержит другие значения. Проверьте поля перед обновлением." if conflicts else None,
             baseline=expected, conflicts=conflicts, etag=raw.get("etag"), url=raw.get("htmlLink"))
        if conflicts:
            raise IntegrationError("Требуется выбрать значения календаря; событие не перезаписано")
        return event_id, raw.get("htmlLink")
    except Exception as error:
        if row_for(key)["state"] != "conflict":
            mark(key, "uncertain", "Результат переноса не подтверждён. ID сохранён; повторная проверка не создаёт дубль.")
        raise IntegrationError(str(error) if isinstance(error, ValueError) else "Google не подтвердил перенос. Повторите проверку.") from None


def resolve(adapter, key, field, choice):
    with LOCK:
        row = row_for(key)
        if not row or row["calendar_id"] != adapter.calendar_id or field not in row["conflicts"]:
            raise IntegrationError("Конфликт больше не актуален")
        local = local_snapshot(item_for(row["agreement_id"]), get_settings(), row["options"])
        raw = read(adapter, row["external_id"])
        remote = remote_snapshot(raw, row["baseline"], get_settings())
        conflict = row["conflicts"][field]
        if local[field] != conflict["local"] or remote[field] != conflict["remote"]:
            mark(key, "outdated", "Значения изменились. Выполните новую сверку.", conflicts={})
            raise IntegrationError("Значения конфликта устарели")
        if choice == "remote":
            apply_local(row, local, {**local, field: remote[field]})
        elif choice != "local":
            raise IntegrationError("Выберите CallMind или Google")
        baseline, conflicts = dict(row["baseline"]), dict(row["conflicts"])
        baseline[field] = remote[field]
        conflicts.pop(field)
        with SessionLocal() as db:
            journal(db, "agreement", row["agreement_id"], {field: conflict}, {field: local[field] if choice == "local" else remote[field]}, "Человек разрешил конфликт Google")
            db.commit()
        mark(key, "conflict" if conflicts else "outdated", baseline=baseline, conflicts=conflicts, etag=raw.get("etag"))


def save_options(key, duration_minutes, reminder_minutes, reminders_enabled, restore=False):
    row = row_for(key)
    if not row:
        raise IntegrationError("Сначала перенесите договорённость в календарь")
    if not 1 <= duration_minutes <= 1440 or not 0 <= reminder_minutes <= 40320:
        raise IntegrationError("Проверьте длительность и время напоминания")
    with SessionLocal() as db:
        binding = db.get(CalendarBinding, key)
        old = json.loads(binding.local_options)
        values = {**old, "duration_minutes": duration_minutes,
                  "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}] if reminders_enabled else []}}
        if restore:
            values["cancelled"] = False
        binding.local_options = dump(values)
        journal(db, "agreement", row["agreement_id"], old, values, "Человек: настройки события Google")
        db.commit()


def export_settings(agreement_id, settings):
    """Use the selected calendar's local event preferences for the ICS download."""
    from services.connections import details
    selected = settings["calendar_id"]
    if selected == "primary":
        selected = details("google").get("account")
    candidates = rows(agreement_id)
    matching = [r for r in candidates if r["calendar_id"] == selected]
    row = matching[0] if len(matching) == 1 else candidates[0] if not selected and len(candidates) == 1 else None
    if not row:
        return settings
    options = row["options"]
    return {**settings, "event_minutes": options.get("duration_minutes", settings["event_minutes"]),
            "calendar_reminders": options.get("reminders", row["baseline"].get("reminders")),
            "calendar_cancelled": bool(options.get("cancelled"))}


def sync_cycle(adapter=None, force=False):
    if not force and not get_settings()["calendar_auto_sync"]:
        return
    from services.integrations import GoogleCalendar
    adapter = adapter or GoogleCalendar(get_settings())
    for row in rows():
        if row["calendar_id"] != adapter.calendar_id or row["state"] in {"conflict", "deleted", "creating", "uncertain"}:
            continue
        try:
            reconcile(adapter, row["key"])
        except ValueError:
            pass  # A failed event does not block other events. No provider secrets logged.
    with SessionLocal() as db:
        db.merge(AppSetting(key="calendar_worker_last_run", value=local_now(get_settings()["timezone"]).isoformat()))
        db.merge(AppSetting(key="calendar_worker_error", value=""))
        db.commit()


def start_worker():
    if os.getenv("CALLMIND_TEST_MODE") == "1":
        return
    with LOCK:
        if getattr(start_worker, "thread", None) and start_worker.thread.is_alive():
            return
        def run():
            stop = threading.Event()
            while not stop.wait(30):
                try:
                    sync_cycle()
                except Exception:
                    with SessionLocal() as db:
                        db.merge(AppSetting(key="calendar_worker_error", value="Проверьте подключение Google; сверка не запущена."))
                        db.commit()
        start_worker.thread = threading.Thread(target=run, name="callmind-calendar", daemon=True)
        start_worker.thread.start()
