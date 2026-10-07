"""Create a separate, synthetic UI preview. Never touches the working database."""
import os
import sys
import json
from datetime import timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
target = ROOT / ".test-data" / "redesign"
os.environ["CALLMIND_DATA_DIR"] = str(target)
from database import init_db, SessionLocal
from models import Client, Call, Agreement, AgreementDetails
from services.dates import local_now
from services.settings import save_settings
from services.workspace import confirm_task, create_deal, record_outcome, link_deal
init_db()
with SessionLocal() as db:
    if db.query(Client).count():
        print("Synthetic preview already exists")
        sys.exit(0)
    now = local_now("Europe/Minsk")
    client = Client(name="Тестовый клиент · Северные технологии и инженерные системы", phone=None)
    db.add(client)
    db.flush()
    call = Call(client_id=client.id, call_datetime=now - timedelta(days=2),
        transcript="Пришлю коммерческое предложение. Созвонимся для обсуждения. Срок уточним позже.",
        transcript_segments=json.dumps([dict(start=12.0, end=18.0, text="Пришлю коммерческое предложение.")]),
        summary="Тестовый разговор для проверки интерфейса", follow_up_required=True)
    db.add(call)
    db.flush()
    for index, description, side, due, evidence in [
        (1, "Отправить коммерческое предложение с расчётом стоимости", "manager", now - timedelta(days=1), "Пришлю коммерческое предложение."),
        (2, "Связаться с клиентом и согласовать следующий шаг", "client", now.replace(hour=0, minute=0, second=0, microsecond=0), "Созвонимся для обсуждения."),
        (3, "Уточнить состав участников и условия поставки по проекту внедрения новой системы", "unknown", None, "Срок уточним позже.")]:
        a = Agreement(call_id=call.id, description=description, responsible=side, deadline=due, evidence=evidence, status="pending")
        db.add(a)
        db.flush()
        db.add(AgreementDetails(agreement_id=a.id, date_only=True, priority="high" if index == 1 else "normal", kind="task"))
    db.commit()
    client_id, call_id = client.id, call.id
save_settings({"auto_sync": False})
confirm_task(1)
deal_id = create_deal(client_id, "Тестовая поставка оборудования", "120000", "RUB", now - timedelta(days=20))
record_outcome(deal_id, "won", now - timedelta(days=1), "", "Тестовый менеджер")
link_deal("agreement", 1, deal_id)
link_deal("call", call_id, deal_id)
print("Synthetic preview prepared")
