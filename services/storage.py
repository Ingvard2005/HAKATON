import json
import shutil
import uuid
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import (
    SessionLocal,
    AUDIO_DIR
)

from models import (
    Client,
    Call,
    Agreement
)
from models import AgreementDetails, EditRevision, ExternalLink, IntegrationNotice
from services.dates import normalize_datetime, local_now
from services.settings import get_settings


def parse_deadline(value):
    if not value:
        return None

    return normalize_datetime(value, get_settings()["timezone"])


def get_or_create_client(
    db,
    name,
    phone=None
):
    name = name.strip()

    if phone:
        phone = re.sub(r"[^\d+]", "", phone)

        client = db.scalar(
            select(Client).where(Client.phone == phone)
        )

        if client:
            if name:
                client.name = name

            return client

    client = Client(
        name=name or "Неизвестный клиент",
        phone=phone or None
    )

    db.add(client)
    db.flush()

    return client


def save_audio(
    source_path,
    original_filename
):
    suffix = Path(
        original_filename
    ).suffix.lower()

    filename = (
        f"{uuid.uuid4().hex}{suffix}"
    )

    destination = (
        AUDIO_DIR / filename
    )

    shutil.copy2(
        source_path,
        destination
    )

    return str(destination)


def save_call(client_name, client_phone, call_datetime, original_filename,
              source_audio_path, transcription, analysis):
    settings = get_settings()
    deadlines = [parse_deadline(item.deadline) for item in analysis.agreements]
    normalized_call = normalize_datetime(call_datetime, settings["timezone"])
    audio_path = None
    try:
        with SessionLocal() as db:
            client = get_or_create_client(db, client_name, client_phone)
            audio_path = save_audio(source_audio_path, original_filename)
            call = Call(client_id=client.id, call_datetime=normalized_call,
                original_filename=original_filename, audio_path=audio_path,
                transcript=transcription["text"],
                transcript_segments=json.dumps(transcription["segments"], ensure_ascii=False),
                summary=analysis.summary, next_action=analysis.next_action,
                follow_up_required=analysis.follow_up_required, created_at=local_now(settings["timezone"]))
            db.add(call)
            db.flush()
            for item, deadline in zip(analysis.agreements, deadlines):
                agreement = Agreement(call_id=call.id, description=item.description,
                    responsible=item.responsible, deadline=deadline,
                    deadline_original=item.deadline_original, evidence=item.evidence, status="pending",
                    created_at=local_now(settings["timezone"]))
                db.add(agreement)
                db.flush()
                db.add(AgreementDetails(agreement_id=agreement.id,
                    priority=settings["default_priority"],
                    date_only=bool(item.deadline and len(item.deadline) == 10), kind="task"))
            db.commit()
            return call.id
    except Exception:
        if audio_path:
            Path(audio_path).unlink(missing_ok=True)
        raise


def call_to_dict(call):
    return {
        "id": call.id,

        "client_id": call.client.id,
        "client_name": call.client.name,
        "client_phone": call.client.phone,

        "call_datetime": call.call_datetime,

        "original_filename": (
            call.original_filename
        ),

        "audio_path": call.audio_path,

        "transcript": call.transcript,

        "transcript_segments": json.loads(
            call.transcript_segments
        ),

        "summary": call.summary,

        "next_action": call.next_action,

        "follow_up_required": (
            call.follow_up_required
        ),

        "agreements": [
            {
                "id": item.id,

                "description": (
                    item.description
                ),

                "responsible": (
                    item.responsible
                ),

                "deadline": item.deadline,

                "deadline_original": (
                    item.deadline_original
                ),

                "evidence": item.evidence,

                "status": item.status
            }

            for item in call.agreements
        ]
    }


def get_call(call_id):
    with SessionLocal() as db:

        query = (
            select(Call)
            .options(
                selectinload(Call.client),
                selectinload(Call.agreements)
            )
            .where(
                Call.id == call_id
            )
        )

        call = db.scalar(query)

        if not call:
            return None

        return call_to_dict(call)


def get_recent_calls(limit=100):
    with SessionLocal() as db:

        query = (
            select(Call)
            .options(
                selectinload(Call.client),
                selectinload(Call.agreements)
            )
            .order_by(
                Call.call_datetime.desc()
            )
            .limit(limit)
        )

        calls = db.scalars(
            query
        ).all()

        return [
            call_to_dict(call)
            for call in calls
        ]


def get_agreements():
    with SessionLocal() as db:

        query = (
            select(Agreement)
            .options(
                selectinload(
                    Agreement.call
                ).selectinload(
                    Call.client
                )
            )
            .order_by(
                Agreement.deadline.asc()
            )
        )

        agreements = db.scalars(
            query
        ).all()

        return [
            {
                "id": item.id,

                "description": (
                    item.description
                ),

                "responsible": (
                    item.responsible
                ),

                "deadline": item.deadline,

                "deadline_original": (
                    item.deadline_original
                ),

                "evidence": item.evidence,

                "status": item.status,

                "call_id": item.call.id,

                "call_datetime": (
                    item.call.call_datetime
                ),

                "client_id": (
                    item.call.client.id
                ),

                "client_name": (
                    item.call.client.name
                ),

                "client_phone": (
                    item.call.client.phone
                )
            }

            for item in agreements
        ]


def set_agreement_status(
    agreement_id,
    status
):
    if status not in {"pending", "done"}:
        raise ValueError("Недопустимый статус")
    with SessionLocal() as db:

        agreement = db.get(
            Agreement,
            agreement_id
        )

        if not agreement:
            return False

        details = db.get(AgreementDetails, agreement_id)
        if details is None:
            details = AgreementDetails(agreement_id=agreement_id,
                date_only=bool(agreement.deadline and agreement.deadline.time() == datetime.min.time()))
            db.add(details)
        before = {"status": agreement.status}
        if agreement.status != status:
            details.completed_at = local_now(get_settings()["timezone"]) if status == "done" else None
        agreement.status = status
        for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == agreement_id)):
            if link.state == "synced":
                link.state = "outdated"
        db.add(EditRevision(entity_type="agreement", entity_id=agreement_id,
            before_json=json.dumps(before), after_json=json.dumps({"status": status}),
            created_at=local_now(get_settings()["timezone"])))

        db.commit()

        return True

def get_clients():
    with SessionLocal() as db:

        query = (
            select(Client)
            .options(
                selectinload(Client.calls)
            )
            .order_by(
                Client.name.asc()
            )
        )

        clients = db.scalars(
            query
        ).all()

        return [
            {
                "id": client.id,
                "name": client.name,
                "phone": client.phone,
                "calls_count": len(client.calls)
            }
            for client in clients
        ]


def get_client_calls(client_id):
    with SessionLocal() as db:

        query = (
            select(Call)
            .options(
                selectinload(Call.client),
                selectinload(Call.agreements)
            )
            .where(
                Call.client_id == client_id
            )
            .order_by(
                Call.call_datetime.asc()
            )
        )

        calls = db.scalars(
            query
        ).all()

        return [
            call_to_dict(call)
            for call in calls
        ]


def enrich_agreements(items):
    """Metadata remains in additive tables, leaving legacy records intact."""
    with SessionLocal() as db:
        for item in items:
            details = db.get(AgreementDetails, item["id"])
            item.update(
                priority=details.priority if details else "normal",
                kind=details.kind if details else "task",
                date_only=details.date_only if details else bool(item["deadline"] and item["deadline"].time() == datetime.min.time()),
                completed_at=details.completed_at if details else None,
            )
            item["integrations"] = [{"provider": link.provider, "state": link.state,
                "url": link.url, "message": link.message, "updated_at": link.updated_at} for link in db.scalars(
                select(ExternalLink).where(ExternalLink.agreement_id == item["id"])).all()]
            item["last_attempts"] = [{"provider": n.provider, "success": n.success,
                "message": n.message, "recorded_at": n.recorded_at} for n in db.scalars(
                select(IntegrationNotice).where(IntegrationNotice.agreement_id == item["id"]))]
    return items


def update_agreement(agreement_id, *, description, responsible, deadline,
                     date_only, priority, kind):
    if not description.strip():
        raise ValueError("Введите описание договорённости")
    if responsible not in {"manager", "client", "unknown"}:
        raise ValueError("Некорректный ответственный")
    if priority not in {"low", "normal", "high"} or kind not in {"task", "meeting"}:
        raise ValueError("Некорректный тип или приоритет")
    deadline = parse_deadline(deadline) if deadline else None
    if deadline and date_only:
        deadline = deadline.replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        row = db.get(Agreement, agreement_id)
        if row is None:
            raise ValueError("Договорённость не найдена")
        details = db.get(AgreementDetails, agreement_id)
        if details is None:
            details = AgreementDetails(agreement_id=agreement_id, priority="normal", kind="task",
                date_only=bool(row.deadline and row.deadline.time() == datetime.min.time()))
            db.add(details)
        before = {"description": row.description, "responsible": row.responsible,
            "deadline": str(row.deadline), "priority": details.priority, "kind": details.kind,
            "date_only": details.date_only}
        row.description, row.responsible, row.deadline = description.strip(), responsible, deadline
        details.date_only, details.priority, details.kind = date_only, priority, kind
        after = {"description": row.description, "responsible": responsible,
            "deadline": str(deadline), "date_only": date_only, "priority": priority, "kind": kind}
        db.add(EditRevision(entity_type="agreement", entity_id=agreement_id,
            before_json=json.dumps(before, ensure_ascii=False), after_json=json.dumps(after, ensure_ascii=False),
            created_at=local_now(get_settings()["timezone"])))
        for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id == agreement_id)):
            if link.state == "synced":
                link.state = "outdated"
        db.commit()


def update_call(call_id, *, summary, transcript, next_action, follow_up_required):
    if not summary.strip() or not transcript.strip():
        raise ValueError("Резюме и транскрипция не должны быть пустыми")
    with SessionLocal() as db:
        row = db.get(Call, call_id)
        if row is None:
            raise ValueError("Звонок не найден")
        fields = ("summary", "transcript", "next_action", "follow_up_required")
        before = {field: getattr(row, field) for field in fields}
        after = dict(summary=summary.strip(), transcript=transcript.strip(),
            next_action=next_action.strip() or None, follow_up_required=follow_up_required)
        for field, value in after.items():
            setattr(row, field, value)
        if before["transcript"] != after["transcript"]:
            ids = list(db.scalars(select(Agreement.id).where(Agreement.call_id == call_id)))
            for link in db.scalars(select(ExternalLink).where(ExternalLink.agreement_id.in_(ids))):
                if link.state == "synced":
                    link.state = "outdated"
        db.add(EditRevision(entity_type="call", entity_id=call_id,
            before_json=json.dumps(before, ensure_ascii=False), after_json=json.dumps(after, ensure_ascii=False),
            created_at=local_now(get_settings()["timezone"])))
        db.commit()


def get_revisions(entity_type, entity_id):
    with SessionLocal() as db:
        return [{"created_at": r.created_at, "before": json.loads(r.before_json),
            "after": json.loads(r.after_json)} for r in db.scalars(select(EditRevision)
            .where(EditRevision.entity_type == entity_type, EditRevision.entity_id == entity_id)
            .order_by(EditRevision.id.desc())).all()]
