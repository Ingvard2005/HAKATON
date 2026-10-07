"""Portal-scoped CRM bindings, three-way reconciliation and a polling worker.

Only an explicit transfer creates contacts/tasks. The worker handles existing
bindings only; calendar transfers and AI extraction are never invoked here.
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta

from sqlalchemy import select, update
from database import SessionLocal
from models import (Client, ClientDetails, CrmBinding, CrmTaskOptions, Agreement,
                    AgreementDetails, AgreementReview, EditRevision, ExternalLink,
                    Deal, DealOutcomeEvent, DealLink, AppSetting)
from services.dates import local_now, normalize_datetime, deadline_with_zone
from services.settings import get_settings

LOCK = threading.RLock()


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def key_for(portal, kind, entity_id):
    return f"{hashlib.sha256(portal.encode()).hexdigest()[:24]}:{kind}:{entity_id}"


def binding(portal, kind, entity_id):
    with SessionLocal() as db:
        row = db.get(CrmBinding, key_for(portal, kind, entity_id))
        return {column.name: getattr(row, column.name) for column in row.__table__.columns} if row else None


def bindings(kind=None, entity_id=None):
    with SessionLocal() as db:
        query = select(CrmBinding)
        if kind:
            query = query.where(CrmBinding.entity_type == kind)
        if entity_id is not None:
            query = query.where(CrmBinding.entity_id == entity_id)
        return [dict(key=r.key, portal=r.portal, entity_type=r.entity_type, entity_id=r.entity_id,
                     external_id=r.external_id, state=r.state, message=r.message,
                     updated_at=r.updated_at, conflicts=json.loads(r.conflicts)) for r in db.scalars(query)]


def journal(db, kind, entity_id, before, after, source):
    db.add(EditRevision(entity_type=kind, entity_id=entity_id,
        before_json=dump(before), after_json=dump({**after, "источник": source}),
        created_at=local_now(get_settings()["timezone"])))


def client_data(client_id):
    with SessionLocal() as db:
        row, extra = db.get(Client, client_id), db.get(ClientDetails, client_id)
        if not row:
            raise ValueError("Клиент не найден")
        return dict(name=row.name, phone=row.phone or "", email=extra.email or "" if extra else "",
                    company_id=extra.company_id or "" if extra else "",
                    manager_id=extra.crm_manager_id if extra else None)


def save_client(client_id, values):
    name = str(values.get("name", "")).strip()
    email = str(values.get("email", "")).strip()
    if not name:
        raise ValueError("Введите имя клиента")
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Проверьте email")
    company = str(values.get("company_id", "")).strip()
    manager = values.get("manager_id") or None
    if company and (not company.isdigit() or int(company) <= 0):
        raise ValueError("Компания задаётся её ID в Bitrix24")
    if manager is not None and (not isinstance(manager, int) or manager <= 0):
        raise ValueError("Укажите положительный ID менеджера CRM")
    before = client_data(client_id)
    with SessionLocal() as db:
        row = db.get(Client, client_id)
        row.name, row.phone = name, str(values.get("phone", "")).strip() or None
        extra = db.get(ClientDetails, client_id) or ClientDetails(client_id=client_id)
        extra.email, extra.company_id, extra.crm_manager_id = email or None, company or None, manager
        db.add(extra)
        journal(db, "client", client_id, before, values, "Человек в CallMind")
        db.commit()


def task_options(agreement_id):
    with SessionLocal() as db:
        row = db.get(CrmTaskOptions, agreement_id)
        return {"responsible_id": row.responsible_id, "deal_id": row.deal_id} if row else {"responsible_id": None, "deal_id": None}


def save_task_options(agreement_id, responsible_id=None, deal_id=None):
    if responsible_id is not None and (not isinstance(responsible_id, int) or responsible_id <= 0):
        raise ValueError("Укажите положительный ID исполнителя CRM")
    with SessionLocal() as db:
        row = db.get(Agreement, agreement_id)
        if not row:
            raise ValueError("Договорённость не найдена")
        if deal_id:
            deal = db.get(Deal, deal_id)
            if not deal or deal.client_id != row.call.client_id:
                raise ValueError("Сделка должна принадлежать клиенту звонка")
        before = task_options(agreement_id)
        if before["deal_id"] and before["deal_id"] != deal_id:
            old_link = db.get(DealLink, ("agreement", agreement_id, before["deal_id"]))
            if old_link:
                db.delete(old_link)
        if deal_id:
            db.merge(DealLink(entity_type="agreement", entity_id=agreement_id, deal_id=deal_id))
        db.merge(CrmTaskOptions(agreement_id=agreement_id, responsible_id=responsible_id, deal_id=deal_id))
        journal(db, "agreement", agreement_id, before,
                dict(responsible_id=responsible_id, deal_id=deal_id), "Человек в CallMind")
        db.commit()


def merge_fields(base, local, remote):
    """Independent field edits merge; divergent edits of one field never overwrite."""
    merged, conflicts = {}, {}
    for field in base:
        a, b, old = local.get(field), remote.get(field), base[field]
        if a == b:
            merged[field] = a
        elif a == old:
            merged[field] = b
        elif b == old:
            merged[field] = a
        else:
            conflicts[field] = dict(base=old, local=a, remote=b)
    return merged, conflicts


def phone(value):
    return re.sub(r"\D", "", value or "")


def canonical_contact(values):
    return {**values, "phone": phone(values.get("phone")), "email": (values.get("email") or "").casefold()}


def contact_snapshot(raw):
    def first(field):
        values = raw.get(field) or []
        return next((v.get("VALUE", "") for v in values if v.get("VALUE_TYPE") == "WORK"),
                    values[0].get("VALUE", "") if values else "")
    return canonical_contact(dict(name=raw.get("NAME") or "", phone=first("PHONE"), email=first("EMAIL"),
        company_id=str(raw.get("COMPANY_ID") or ""), manager_id=int(raw.get("ASSIGNED_BY_ID") or 0) or None))


def contact_patch(values, raw, changed):
    fields = {}
    for field, crm_field in {"name": "NAME", "company_id": "COMPANY_ID", "manager_id": "ASSIGNED_BY_ID"}.items():
        if field in changed:
            fields[crm_field] = values[field] or (0 if field == "company_id" else "")
    for field in ("phone", "email"):
        if field not in changed:
            continue
        # Preserve other phone/email entries and their IDs.
        entries = [dict(v) for v in raw.get(field.upper(), [])]
        index = next((n for n, v in enumerate(entries) if v.get("VALUE_TYPE") == "WORK"), 0)
        if entries:
            entries[index]["VALUE"] = values[field]
        elif values[field]:
            entries.append(dict(VALUE=values[field], VALUE_TYPE="WORK"))
        fields[field.upper()] = entries
    return fields


def mark(key, state, message=None, conflicts=None, baseline=None):
    with SessionLocal() as db:
        row = db.get(CrmBinding, key)
        row.state, row.message, row.updated_at = state, message, local_now(get_settings()["timezone"])
        if conflicts is not None:
            row.conflicts = dump(conflicts)
        if baseline is not None:
            row.baseline = dump(baseline)
        db.commit()


def bind_contact(adapter, client_id, external_id):
    raw = adapter.request("crm.contact.get", {"id": int(external_id)})
    remote = contact_snapshot(raw)
    local = canonical_contact(client_data(client_id))
    # An explicit selection establishes the existing CRM values as the baseline.
    # Differences are presented before any overwrite or task creation.
    conflicts = {f: dict(base=None, local=local[f], remote=remote[f])
                 for f in local if local[f] and remote[f] and local[f] != remote[f]}
    base = {f: local[f] if not local[f] else remote[f] for f in local}
    with SessionLocal() as db:
        old = db.get(CrmBinding, key_for(adapter.origin, "contact", client_id))
        if old and old.external_id and str(old.external_id) != str(external_id):
            raise ValueError("Контакт уже связан. Смена связи требует отдельного решения.")
        row = old or CrmBinding(key=key_for(adapter.origin, "contact", client_id), portal=adapter.origin,
                               entity_type="contact", entity_id=client_id)
        row.external_id, row.baseline, row.conflicts = str(external_id), dump(base), dump(conflicts)
        row.state = "conflict" if conflicts else "outdated"
        db.add(row)
        journal(db, "client", client_id, {}, {"Контакт Bitrix24": str(external_id)}, "Привязка CRM")
        db.commit()
    return str(external_id)


def ensure_contact(adapter, client_id):
    from services.integrations import IntegrationError
    row = binding(adapter.origin, "contact", client_id)
    if row and row["external_id"]:
        reconcile(adapter, row["key"])
        current = binding(adapter.origin, "contact", client_id)
        if current["state"] != "synced":
            raise IntegrationError(current["message"] or "Сначала разрешите различия в карточке клиента")
        return row["external_id"]
    if row and row["state"] in {"creating", "uncertain"}:
        raise IntegrationError("Создание контакта не подтверждено. Найдите его в CRM и укажите ID; повтор заблокирован.")
    local = canonical_contact(client_data(client_id))
    ids = set()
    for field, kind in (("phone", "PHONE"), ("email", "EMAIL")):
        if local[field]:
            result = adapter.request("crm.duplicate.findbycomm", {"entity_type": "CONTACT", "type": kind, "values": [local[field]]})
            ids.update(str(i) for i in result.get("CONTACT", []))
    if len(ids) > 1:
        raise IntegrationError("Найдено несколько контактов CRM. В карточке клиента выберите контакт по ID: " + ", ".join(sorted(ids)))
    if ids:
        bind_contact(adapter, client_id, next(iter(ids)))
        return ensure_contact(adapter, client_id)
    key = key_for(adapter.origin, "contact", client_id)
    with SessionLocal() as db:
        if db.get(CrmBinding, key):
            raise IntegrationError("Создание контакта уже начато")
        db.add(CrmBinding(key=key, portal=adapter.origin, entity_type="contact", entity_id=client_id, state="creating"))
        try:
            db.commit()
        except Exception:
            raise IntegrationError("Создание контакта уже начато") from None
    fields = contact_patch(local, {}, set(local))
    fields["ASSIGNED_BY_ID"] = local["manager_id"] or adapter.responsible
    fields["COMMENTS"] = f"Контакт перенесён из CallMind, клиент #{client_id}"
    try:
        external_id = str(adapter.request("crm.contact.add", {"fields": fields}))
        # Persist the ID before verification: a read timeout must not create a duplicate.
        with SessionLocal() as db:
            row = db.get(CrmBinding, key)
            row.external_id, row.state = external_id, "outdated"
            db.commit()
        remote = contact_snapshot(adapter.request("crm.contact.get", {"id": external_id}))
        apply_local("contact", client_id, local, remote)
        mark(key, "synced", baseline=remote)
        return external_id
    except Exception:
        current = binding(adapter.origin, "contact", client_id)
        mark(key, "error" if current["external_id"] else "uncertain", "Контакт не подтверждён CRM. Проверьте портал перед повтором.")
        raise IntegrationError("Контакт не подтверждён CRM. Проверьте карточку клиента.") from None


def task_snapshot(item, executor, settings, portal=None):
    deadline = deadline_with_zone(item, settings["timezone"])
    result = dict(title=settings["task_template"].format(description=item["description"], client=item["client_name"]),
        deadline=deadline.isoformat() if deadline else None, status=item["status"],
        priority={"low": 0, "normal": 1, "high": 2}[item["priority"]], executor=int(executor))
    if portal:
        contact = binding(portal, "contact", item["client_id"])
        selected = task_options(item["id"])["deal_id"]
        deal = binding(portal, "deal", selected) if selected else None
        if selected and (not deal or not deal["external_id"]):
            raise ValueError("Выбранная сделка не связана с текущим порталом CRM")
        result["crm_links"] = sorted((["C_" + contact["external_id"]] if contact and contact["external_id"] else []) +
                                      (["D_" + deal["external_id"]] if deal else []))
    return result


def remote_task(raw):
    due = raw.get("deadline")
    if due:
        due = normalize_datetime(due, get_settings()["timezone"])
        from zoneinfo import ZoneInfo
        due = due.replace(tzinfo=ZoneInfo(get_settings()["timezone"])).isoformat()
    return dict(title=raw.get("title", ""), deadline=due or None,
        status="done" if str(raw.get("status")) == "5" else "pending",
        priority=int(raw.get("priority") if raw.get("priority") is not None else 1), executor=int(raw["responsibleId"]),
        crm_links=sorted(v for v in raw.get("ufCrmTask", []) if str(v).startswith(("C_", "D_"))))


def remember_task(adapter, item, external_id):
    """Acknowledged create ID survives a subsequent read timeout."""
    values = task_snapshot(item, task_options(item["id"])["responsible_id"] or adapter.responsible, get_settings(), adapter.origin)
    with SessionLocal() as db:
        key = key_for(adapter.origin, "task", item["id"])
        row = db.get(CrmBinding, key)
        if not row:
            db.add(CrmBinding(key=key, portal=adapter.origin, entity_type="task", entity_id=item["id"],
                              external_id=str(external_id), baseline=dump(values), state="creating"))
            journal(db, "agreement", item["id"], {}, {"Задача Bitrix24": str(external_id)}, "Перенос в CRM")
        else:
            row.external_id = str(external_id)
        db.commit()


def register_task(adapter, item, external_id):
    remember_task(adapter, item, external_id)
    values = task_snapshot(item, task_options(item["id"])["responsible_id"] or adapter.responsible, get_settings(), adapter.origin)
    checked = remote_task(adapter.request("tasks.task.get", {"taskId": external_id, "select": ["*", "UF_CRM_TASK"]})["task"])
    conflicts = {f: dict(base=None, local=values[f], remote=checked.get(f)) for f in values if checked.get(f) != values[f]}
    mark(key_for(adapter.origin, "task", item["id"]), "conflict" if conflicts else "synced",
         "CRM вернула другие значения. Проверьте поля задачи." if conflicts else None, conflicts, values)
    if conflicts:
        raise ValueError("CRM не подтвердила поля задачи. ID сохранён; разрешите различия.")


def local_snapshot(row, adapter):
    if row["entity_type"] == "contact":
        return canonical_contact(client_data(row["entity_id"]))
    from services.workspace import tasks
    item = next(i for i in tasks() if i["id"] == row["entity_id"])
    return task_snapshot(item, task_options(item["id"])["responsible_id"] or adapter.responsible, get_settings(), adapter.origin)


def fetch_remote(row, adapter):
    if row["entity_type"] == "contact":
        raw = adapter.request("crm.contact.get", {"id": row["external_id"]})
        return contact_snapshot(raw), raw
    raw = adapter.request("tasks.task.get", {"taskId": row["external_id"], "select": ["*", "UF_CRM_TASK"]})["task"]
    return remote_task(raw), raw


def apply_local(kind, entity_id, expected, values, portal=None, closed_at=None):
    """Optimistic local check and update in one transaction; no lost UI edits."""
    from services.storage import enrich_agreements, get_agreements
    with SessionLocal() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if kind == "contact":
            if canonical_contact(client_data(entity_id)) != expected:
                raise ValueError("Клиент изменён во время синхронизации. Повторите проверку.")
            row = db.get(Client, entity_id)
            extra = db.get(ClientDetails, entity_id) or ClientDetails(client_id=entity_id)
            row.name, row.phone = values["name"], values["phone"] or None
            extra.email, extra.company_id, extra.crm_manager_id = values["email"] or None, values["company_id"] or None, values["manager_id"]
            db.add(extra)
            entity = "client"
        else:
            item = next(i for i in enrich_agreements(get_agreements()) if i["id"] == entity_id)
            # Executor is validated separately against task options in reconcile.
            current = task_snapshot(item, expected["executor"], get_settings(), portal)
            options_now = db.get(CrmTaskOptions, entity_id)
            if options_now and options_now.responsible_id and options_now.responsible_id != expected["executor"]:
                raise ValueError("Назначение CRM изменилось во время синхронизации")
            if current != expected:
                raise ValueError("Задача изменена во время синхронизации. Повторите проверку.")
            row = db.get(Agreement, entity_id)
            extra = db.get(AgreementDetails, entity_id) or AgreementDetails(agreement_id=entity_id, priority="normal", kind="task", date_only=True)
            if values["title"] != expected["title"]:
                if get_settings()["task_template"] != "{description}":
                    raise ValueError("Для обратной синхронизации названия используйте шаблон {description} в настройках")
                row.description = values["title"]
            if values["deadline"] != expected["deadline"]:
                due = normalize_datetime(values["deadline"], get_settings()["timezone"]) if values["deadline"] else None
                # Preserve date-only only when CRM retained the end-of-day convention.
                extra.date_only = bool(due and extra.date_only and due.hour == 23 and due.minute == 59)
                row.deadline = due.replace(hour=0, minute=0) if due and extra.date_only else due
            row.status = values["status"]
            extra.priority = {0: "low", 1: "normal", 2: "high"}.get(values["priority"], "normal")
            if row.status != "done":
                extra.completed_at = None
            elif expected["status"] != "done":
                extra.completed_at = normalize_datetime(closed_at, get_settings()["timezone"]) if closed_at else None
            db.add(extra)
            options = db.get(CrmTaskOptions, entity_id) or CrmTaskOptions(agreement_id=entity_id)
            options.responsible_id = values["executor"]
            if "crm_links" in values and values["crm_links"] != expected["crm_links"]:
                contacts = [v[2:] for v in values["crm_links"] if v.startswith("C_")]
                selected = [v[2:] for v in values["crm_links"] if v.startswith("D_")]
                contact = binding(portal, "contact", item["client_id"])
                if contacts != ([contact["external_id"]] if contact else []) or len(selected) > 1:
                    raise ValueError("Связь задачи с клиентом изменилась в CRM. Проверьте контакт вручную.")
                linked = db.scalar(select(CrmBinding).where(CrmBinding.portal == portal, CrmBinding.entity_type == "deal", CrmBinding.external_id == selected[0])) if selected else None
                if selected and (not linked or db.get(Deal, linked.entity_id).client_id != item["client_id"]):
                    raise ValueError("Сначала получите выбранную в CRM сделку в карточке клиента")
                if options.deal_id:
                    old_link = db.get(DealLink, ("agreement", entity_id, options.deal_id))
                    if old_link:
                        db.delete(old_link)
                options.deal_id = linked.entity_id if linked else None
                if options.deal_id:
                    db.merge(DealLink(entity_type="agreement", entity_id=entity_id, deal_id=options.deal_id))
            db.add(options)
            for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == entity_id)):
                if link.provider == "google" and link.state == "synced" and values != expected:
                    link.state = "outdated"
            entity = "agreement"
        if values != expected:
            journal(db, entity, entity_id, expected, values, "Bitrix24 / синхронизация")
        db.commit()


def reconcile(adapter, key):
    from services.integrations import IntegrationError
    with LOCK:
        with SessionLocal() as db:
            entity = db.get(CrmBinding, key)
            if not entity or entity.portal != adapter.origin or not entity.external_id:
                raise IntegrationError("Связь с текущим порталом не найдена")
            if entity.state in {"conflict", "deleted", "creating", "syncing"}:
                if entity.state != "syncing" or entity.updated_at > local_now(get_settings()["timezone"]) - timedelta(minutes=3):
                    return
            prior_state = entity.state
            claimed = db.execute(update(CrmBinding).where(CrmBinding.key == key, CrmBinding.state == prior_state).values(state="syncing", updated_at=local_now(get_settings()["timezone"])))
            if claimed.rowcount != 1:
                return
            db.commit()
            row = {c.name: getattr(entity, c.name) for c in entity.__table__.columns}
        try:
            local = local_snapshot(row, adapter)
            remote, raw = fetch_remote(row, adapter)
            base = json.loads(row["baseline"])
            merged, conflicts = merge_fields(base, local, remote)
            if conflicts:
                mark(key, "conflict", "Одно поле изменено в CallMind и CRM. Выберите значение.", conflicts)
                return
            changed = {f for f in merged if merged[f] != remote.get(f)}
            if changed and row["entity_type"] == "task":
                from services.workspace import tasks
                item = next(i for i in tasks() if i["id"] == row["entity_id"])
                if item["review_reasons"]:
                    mark(key, "needs_review", "Сохраните и подтвердите правки договорённости перед отправкой")
                    return
            # Re-read immediately before sending a patch. Only changed fields are sent.
            if changed and fetch_remote(row, adapter)[0] != remote:
                mark(key, "outdated", "CRM изменилась во время проверки; будет повторная сверка")
                return
            if changed:
                if row["entity_type"] == "contact":
                    adapter.request("crm.contact.update", {"id": row["external_id"], "fields": contact_patch(merged, raw, changed)})
                else:
                    field_map = {"title": "TITLE", "deadline": "DEADLINE", "priority": "PRIORITY", "executor": "RESPONSIBLE_ID"}
                    fields = {field_map[f]: merged[f] or "" for f in changed if f in field_map}
                    if "crm_links" in changed:
                        fields["UF_CRM_TASK"] = merged["crm_links"] + [v for v in raw.get("ufCrmTask", []) if not str(v).startswith(("C_", "D_"))]
                    if fields:
                        adapter.request("tasks.task.update", {"taskId": row["external_id"], "fields": fields})
                    if "status" in changed:
                        adapter.request("tasks.task.complete" if merged["status"] == "done" else "tasks.task.renew", {"taskId": row["external_id"]})
                checked, _ = fetch_remote(row, adapter)
                if any(checked.get(f) != merged[f] for f in changed):
                    raise IntegrationError("CRM не подтвердила новые значения; требуется повторная сверка")
            # No baseline advance until both sides reflect the reconciled values.
            apply_local(row["entity_type"], row["entity_id"], local, merged, adapter.origin, raw.get("closedDate"))
            mark(key, "synced", conflicts={}, baseline=merged)
            if row["entity_type"] == "task":
                with SessionLocal() as db:
                    for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == row["entity_id"], ExternalLink.provider == "bitrix24")):
                        if link.url and link.url.startswith(adapter.origin + "/"):
                            link.state, link.message, link.updated_at = "synced", None, local_now(get_settings()["timezone"])
                    db.commit()
        except Exception as error:
            state = "deleted" if getattr(error, "remote_missing", False) else "error"
            message = str(error) if isinstance(error, (IntegrationError, ValueError)) else "Ошибка CRM. Данные сохранены локально; повторная сверка позже."
            mark(key, state, message)
            raise IntegrationError(message) from None


def resolve_conflict(adapter, key, field, choice):
    with LOCK:
        with SessionLocal() as db:
            entity = db.get(CrmBinding, key)
            if not entity or entity.portal != adapter.origin or entity.state != "conflict":
                raise ValueError("Конфликт больше не актуален")
            row = {c.name: getattr(entity, c.name) for c in entity.__table__.columns}
        conflicts, base = json.loads(row["conflicts"]), json.loads(row["baseline"])
        if field not in conflicts:
            raise ValueError("Поле конфликта не найдено")
        conflict = conflicts[field]
        local, remote = local_snapshot(row, adapter), fetch_remote(row, adapter)[0]
        if local[field] != conflict["local"] or remote[field] != conflict["remote"]:
            mark(key, "outdated", "Данные изменились: требуется новая сверка", {})
            raise ValueError("Значения изменились после появления конфликта. Повторите сверку.")
        if choice == "remote":
            apply_local(row["entity_type"], row["entity_id"], local, {**local, field: remote[field]}, adapter.origin)
        elif choice != "local":
            raise ValueError("Выберите CallMind или CRM")
        base[field] = remote[field]
        conflicts.pop(field)
        with SessionLocal() as db:
            journal(db, "client" if row["entity_type"] == "contact" else "agreement", row["entity_id"],
                    {field: conflict}, {field: local[field] if choice == "local" else remote[field]}, "Человек разрешил конфликт")
            db.commit()
        mark(key, "conflict" if conflicts else "outdated", conflicts=conflicts, baseline=base)


def import_deals(adapter, client_id):
    contact = binding(adapter.origin, "contact", client_id)
    if not contact or not contact["external_id"]:
        raise ValueError("Сначала свяжите клиента с контактом Bitrix24")
    rows = adapter.list_all("crm.deal.list", {"filter": {"CONTACT_ID": contact["external_id"]},
                            "select": ["ID", "TITLE", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY", "MOVED_TIME", "STAGE_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID"]})
    for raw in rows:
        import_deal(adapter, client_id, raw)
    return len(rows)


def import_deal(adapter, client_id, raw):
    portal, external = adapter.origin, str(raw["ID"])
    contact = binding(portal, "contact", client_id)
    if contact and "CONTACT_ID" in raw and str(raw["CONTACT_ID"]) != contact["external_id"]:
        raise ValueError("Контакт сделки изменился в CRM; проверьте связь с клиентом")
    with SessionLocal() as db:
        link = db.scalar(select(CrmBinding).where(CrmBinding.portal == portal, CrmBinding.entity_type == "deal", CrmBinding.external_id == external))
        values = dict(title=raw["TITLE"], amount=str(raw["OPPORTUNITY"]) if raw.get("OPPORTUNITY") is not None else None,
                      currency=raw.get("CURRENCY_ID"), stage=raw.get("STAGE_ID"), manager_id=raw.get("ASSIGNED_BY_ID"))
        if not values["currency"]:
            raise ValueError("CRM не вернула валюту сделки")
        if link:
            deal = db.get(Deal, link.entity_id)
            if deal.client_id != client_id:
                raise ValueError("Связь сделки с клиентом изменилась в CRM; требуется проверка")
        else:
            deal = Deal(client_id=client_id, title=values["title"], amount=values["amount"], currency=values["currency"],
                        created_at=normalize_datetime(raw["DATE_CREATE"], get_settings()["timezone"]))
            db.add(deal)
            db.flush()
            link = CrmBinding(key=key_for(portal, "deal", deal.id), portal=portal, entity_type="deal", entity_id=deal.id, external_id=external)
            db.add(link)
        old = json.loads(link.baseline or "{}")
        if old != values:
            deal.title, deal.amount, deal.currency = values["title"], values["amount"], values["currency"]
            journal(db, "deal", deal.id, old, values, "Bitrix24")
        outcome = {"P": "open", "S": "won", "F": "lost"}.get(raw.get("STAGE_SEMANTIC_ID"))
        latest = db.scalar(select(DealOutcomeEvent).where(DealOutcomeEvent.deal_id == deal.id).order_by(DealOutcomeEvent.occurred_at.desc(), DealOutcomeEvent.id.desc()))
        if outcome and (not latest or latest.outcome != outcome):
            occurred = normalize_datetime(raw.get("MOVED_TIME") or raw["DATE_MODIFY"], get_settings()["timezone"])
            if occurred < deal.created_at or occurred > local_now(get_settings()["timezone"]):
                raise ValueError("Проверьте время результата сделки в CRM")
            if latest and latest.occurred_at >= occurred:
                raise ValueError("Результат CRM расходится с более новым подтверждением в CallMind. Нужна проверка.")
            db.add(DealOutcomeEvent(deal_id=deal.id, outcome=outcome, occurred_at=occurred,
                recorded_at=local_now(get_settings()["timezone"]), source="crm", actor="Bitrix24", reason=None))
        link.baseline, link.state, link.updated_at = dump(values), "synced", local_now(get_settings()["timezone"])
        db.commit()


def sync_cycle(adapter=None, force=False):
    if not force and not get_settings().get("crm_auto_sync"):
        return
    from services.integrations import Bitrix24
    adapter = adapter or Bitrix24()
    for row in bindings():
        if row["portal"] != adapter.origin or not row["external_id"] or row["state"] in {"deleted", "creating", "uncertain", "conflict"}:
            continue
        try:
            if row["entity_type"] == "deal":
                raw = adapter.request("crm.deal.get", {"id": row["external_id"]})
                with SessionLocal() as db:
                    client_id = db.get(Deal, row["entity_id"]).client_id
                import_deal(adapter, client_id, raw)
            else:
                reconcile(adapter, row["key"])
        except Exception as error:
            # Each binding retains its own error; one outage must not abort the rest.
            if row["entity_type"] == "deal":
                mark(row["key"], "deleted" if getattr(error, "remote_missing", False) else "error", str(error) if isinstance(error, ValueError) else "Не удалось обновить сделку из CRM. Предыдущие данные сохранены.")
    with SessionLocal() as db:
        db.merge(AppSetting(key="crm_worker_last_run", value=local_now(get_settings()["timezone"]).isoformat()))
        db.merge(AppSetting(key="crm_worker_error", value=""))
        db.commit()


def start_worker():
    """Single embedded worker; no networking in test mode or while setting is off."""
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
                        db.merge(AppSetting(key="crm_worker_error", value="Сверка не запущена. Проверьте подключение Bitrix24."))
                        db.commit()  # Never log OAuth tokens/provider exceptions.
        start_worker.thread = threading.Thread(target=run, name="callmind-crm", daemon=True)
        start_worker.thread.start()
