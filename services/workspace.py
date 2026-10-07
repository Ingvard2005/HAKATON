"""Human review, explicit deal results and auditable analytics. No network calls."""
import hashlib
import json
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from database import SessionLocal
from models import Agreement, AgreementReview, Call, Client, Deal, DealOutcomeEvent, DealLink, EditRevision
from services.storage import get_agreements, enrich_agreements, get_revisions
from services.dates import local_now, is_overdue, normalize_datetime
from services.settings import get_settings


def stamp():
    return local_now(get_settings()["timezone"])


def fingerprint(item, transcript):
    values = [item[k] for k in ("description", "responsible", "deadline", "date_only", "kind", "evidence")]
    return hashlib.sha256(json.dumps([values, transcript], default=str, ensure_ascii=False).encode()).hexdigest()


def tasks():
    items = enrich_agreements(get_agreements())
    with SessionLocal() as db:
        for item in items:
            call = db.get(Call, item["call_id"])
            review = db.get(AgreementReview, item["id"])
            reasons = []
            if item["responsible"] == "unknown":
                reasons.append("Не определена сторона обязательства")
            if not item["evidence"] or item["evidence"] not in call.transcript:
                reasons.append("Цитата не найдена в текущей транскрипции")
            if not review or review.fingerprint != fingerprint(item, call.transcript):
                reasons.append("Договорённость или её правки ещё не проверены человеком")
            item["review_reasons"] = reasons
            item["reviewed_at"] = review.reviewed_at if review else None
            item["created_at"] = db.get(Agreement, item["id"]).created_at
            item["deal_ids"] = list(db.scalars(select(DealLink.deal_id).where(
                DealLink.entity_type == "agreement", DealLink.entity_id == item["id"])))
    return items


def confirm_task(agreement_id):
    item = next((i for i in tasks() if i["id"] == agreement_id), None)
    if not item:
        raise ValueError("Договорённость не найдена")
    with SessionLocal() as db:
        call = db.get(Call, item["call_id"])
        if item["responsible"] == "unknown" or not item["evidence"] or item["evidence"] not in call.transcript:
            raise ValueError("Сначала уточните сторону и проверьте цитату в транскрипции")
        row = db.get(AgreementReview, agreement_id)
        if not row:
            row = AgreementReview(agreement_id=agreement_id, confirmed_deadline=item["deadline"],
                date_only=item["date_only"], reviewed_at=stamp())
            db.add(row)
        elif row.confirmed_deadline is None and item["deadline"] is not None:
            row.confirmed_deadline, row.date_only, row.reviewed_at = item["deadline"], item["date_only"], stamp()
        # First confirmed deadline is retained: moving it never erases an earlier breach.
        row.fingerprint = fingerprint(item, call.transcript)
        row.reviewed_at = stamp() if not row.reviewed_at else row.reviewed_at
        db.add(EditRevision(entity_type="agreement", entity_id=agreement_id,
            before_json=json.dumps({"review": "Требует проверки" if item["review_reasons"] else "Проверено"}, ensure_ascii=False),
            after_json=json.dumps({"review": "Проверено человеком", "reviewed_at": str(stamp())}, ensure_ascii=False),
            created_at=stamp()))
        db.commit()


def create_deal(client_id, title, amount, currency, created_at):
    created_at = normalize_datetime(created_at, get_settings()["timezone"])
    if not title.strip():
        raise ValueError("Введите название сделки")
    if currency not in {"RUB", "BYN", "USD", "EUR"}:
        raise ValueError("Выберите валюту")
    value = None
    if amount.strip():
        try:
            number = Decimal(amount.replace(",", "."))
            if not number.is_finite() or number < 0:
                raise InvalidOperation
            value = str(number.quantize(Decimal("0.01")))
        except InvalidOperation:
            raise ValueError("Сумма должна быть неотрицательным числом") from None
    if created_at > stamp():
        raise ValueError("Дата создания не может быть в будущем")
    with SessionLocal() as db:
        if not db.get(Client, client_id):
            raise ValueError("Клиент не найден")
        row = Deal(client_id=client_id, title=title.strip(), amount=value,
            currency=currency, created_at=created_at)
        db.add(row)
        db.commit()
        return row.id


def record_outcome(deal_id, outcome, occurred_at, reason, actor):
    occurred_at = normalize_datetime(occurred_at, get_settings()["timezone"])
    if outcome not in {"open", "won", "lost"} or not actor.strip():
        raise ValueError("Укажите результат и имя подтверждающего")
    if outcome == "lost" and not reason.strip():
        raise ValueError("Укажите подтверждённую причину проигрыша")
    with SessionLocal() as db:
        row = db.get(Deal, deal_id)
        if not row:
            raise ValueError("Сделка не найдена")
        if occurred_at < row.created_at or occurred_at > stamp():
            raise ValueError("Дата результата должна быть между созданием сделки и текущим временем")
        latest = db.scalar(select(DealOutcomeEvent).where(DealOutcomeEvent.deal_id == deal_id)
            .order_by(DealOutcomeEvent.occurred_at.desc(), DealOutcomeEvent.id.desc()))
        if latest and occurred_at < latest.occurred_at:
            raise ValueError("Новое событие не может быть раньше последнего результата")
        db.add(DealOutcomeEvent(deal_id=deal_id, outcome=outcome, occurred_at=occurred_at,
            recorded_at=stamp(), reason=reason.strip() if outcome == "lost" else None,
            actor=actor.strip(), source="manual"))
        db.commit()


def deals(at=None):
    with SessionLocal() as db:
        result = []
        for row in db.scalars(select(Deal).order_by(Deal.id.desc())):
            events = list(db.scalars(select(DealOutcomeEvent).where(DealOutcomeEvent.deal_id == row.id)
                .order_by(DealOutcomeEvent.occurred_at, DealOutcomeEvent.id)))
            eligible = [e for e in events if at is None or e.occurred_at <= at]
            last = eligible[-1] if eligible else None
            result.append(dict(id=row.id, title=row.title, client_id=row.client_id,
                client_name=db.get(Client, row.client_id).name, created_at=row.created_at,
                amount=row.amount, currency=row.currency, outcome=last.outcome if last else "open",
                closed_at=last.occurred_at if last and last.outcome != "open" else None,
                reason=last.reason if last else None, source=last.source if last else None,
                actor=last.actor if last else None, recorded_at=last.recorded_at if last else None,
                events=[dict(outcome=e.outcome, occurred_at=e.occurred_at, recorded_at=e.recorded_at,
                    reason=e.reason, actor=e.actor, source=e.source) for e in events],
                links=[dict(entity_type=l.entity_type, entity_id=l.entity_id) for l in db.scalars(
                    select(DealLink).where(DealLink.deal_id == row.id))]))
        return result


def link_deal(entity_type, entity_id, deal_id):
    if entity_type not in {"agreement", "call"}:
        raise ValueError("Недопустимая связь")
    with SessionLocal() as db:
        entity = db.get(Agreement if entity_type == "agreement" else Call, entity_id)
        deal = db.get(Deal, deal_id)
        call = db.get(Call, entity.call_id) if entity_type == "agreement" and entity else entity
        if not deal or not call or call.client_id != deal.client_id:
            raise ValueError("Связывать можно только записи одного клиента")
        key = (entity_type, entity_id, deal_id)
        if not db.get(DealLink, key):
            db.add(DealLink(entity_type=entity_type, entity_id=entity_id, deal_id=deal_id))
            db.commit()


def analytics(start, end, client_id=None, priority=None, kind=None):
    left, right = datetime.combine(start, time.min), min(datetime.combine(end, time.max), stamp())
    if left > right:
        raise ValueError("Выберите корректный период без будущих дат")
    items = [i for i in tasks() if (client_id is None or i["client_id"] == client_id)
        and (priority is None or i["priority"] == priority) and (kind is None or i["kind"] == kind)]
    eligible, ontime, excluded = [], [], []
    completed = []
    with SessionLocal() as db:
        for item in items:
            revisions = sorted(get_revisions("agreement", item["id"]), key=lambda r: r["created_at"])
            events = [r for r in revisions if "status" in r["after"] and
                r["before"].get("status") != r["after"]["status"]]
            if any(left <= r["created_at"] <= right and r["after"]["status"] == "done" for r in events):
                completed.append(item)
            review = db.get(AgreementReview, item["id"])
            if not review or not review.confirmed_deadline:
                excluded.append(item)
                continue
            due = datetime.combine(review.confirmed_deadline.date(), time.max) if review.date_only else review.confirmed_deadline
            if not left <= due <= right:
                continue
            # A confirmation after the deadline cannot retrospectively validate the deadline.
            if review.reviewed_at > due:
                excluded.append(item)
                continue
            eligible.append(item)
            last = next((r for r in reversed(events) if r["created_at"] <= due), None)
            if last and last["after"]["status"] == "done":
                ontime.append(item)
    results = [d for d in deals(right) if d["closed_at"] and left <= d["closed_at"] <= right
        and (client_id is None or d["client_id"] == client_id)]
    return dict(new=[i for i in items if left <= i["created_at"] <= right], completed=completed,
        overdue=[i for i in items if is_overdue(i, stamp()) and not i["review_reasons"]],
        today=[i for i in items if i["status"] != "done" and i["deadline"] and i["deadline"].date() == stamp().date()],
        no_deadline=[i for i in items if i["status"] != "done" and not i["deadline"]],
        review=[i for i in items if i["status"] != "done" and i["review_reasons"]],
        sync_errors=[i for i in items if any(l["state"] in {"error", "uncertain", "outdated", "syncing"} for l in i["integrations"])],
        eligible=eligible, ontime=ontime,
        excluded=excluded, won=[d for d in results if d["outcome"] == "won"],
        lost=[d for d in results if d["outcome"] == "lost"], end=right)
