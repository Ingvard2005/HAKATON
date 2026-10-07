import json
from string import Formatter
from zoneinfo import ZoneInfo
from sqlalchemy import select, func

from database import SessionLocal
from models import AppSetting, Call

DEFAULTS = {
    "timezone": "Europe/Minsk",
    "default_priority": "normal",
    "task_template": "{description}",
    "auto_sync": False,
    "crm_auto_sync": False,
    "calendar_auto_sync": False,
    "calendar_id": "primary",
    "reminder_minutes": 30,
    "event_minutes": 30,
}


def get_settings():
    with SessionLocal() as db:
        row = db.get(AppSetting, "preferences")
        # Kept for compatibility with old preferences/API clients, always disabled.
        return DEFAULTS | (json.loads(row.value) if row else {}) | {"auto_sync": False}


def save_settings(values):
    values = DEFAULTS | {k: v for k, v in values.items() if k in DEFAULTS}
    values["auto_sync"] = False
    if not isinstance(values["crm_auto_sync"], bool):
        raise ValueError("Автосинхронизация CRM задаётся переключателем")
    if not isinstance(values["calendar_auto_sync"], bool):
        raise ValueError("Автосинхронизация календаря задаётся переключателем")
    try:
        ZoneInfo(values["timezone"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Укажите существующий часовой пояс") from None
    if not isinstance(values["task_template"], str) or not isinstance(values["calendar_id"], str):
        raise ValueError("Шаблон и календарь должны быть текстом")
    if values["default_priority"] not in {"low", "normal", "high"}:
        raise ValueError("Некорректный приоритет")
    if not values["calendar_id"].strip():
        raise ValueError("Укажите календарь")
    for name in ("reminder_minutes", "event_minutes"):
        if not isinstance(values[name], int) or not 1 <= values[name] <= 1440:
            raise ValueError("Интервал должен быть от 1 до 1440 минут")
    for _, field, spec, conversion in Formatter().parse(values["task_template"]):
        if field is not None and (field not in {"description", "client"} or spec or conversion):
            raise ValueError("В шаблоне доступны только {description} и {client}")
    if not values["task_template"].strip():
        raise ValueError("Шаблон не должен быть пустым")
    with SessionLocal() as db:
        if values["timezone"] != get_settings()["timezone"] and db.scalar(select(func.count()).select_from(Call)):
            raise ValueError("После сохранения звонков смена часового пояса требует миграции дат. Оставьте текущий пояс.")
        db.merge(AppSetting(key="preferences", value=json.dumps(values, ensure_ascii=False)))
        db.commit()
