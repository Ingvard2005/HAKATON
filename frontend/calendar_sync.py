"""Per-event controls and honest Google states, backed by persisted reconciliation."""
import json
import os
import streamlit as st
from services import calendar_sync as calendar
from services.integrations import GoogleCalendar, connection_status
from services.settings import get_settings
from frontend.crm import STATES

LABELS = {"action": "Действие", "deadline": "Срок", "date_only": "Без времени", "kind": "Тип",
          "duration_minutes": "Длительность, минут", "reminders": "Напоминания", "calendar_state": "Состояние события"}


def display(value):
    if isinstance(value, dict):
        if value.get("useDefault"):
            return "По настройкам Google Calendar"
        overrides = value.get("overrides", [])
        return "; ".join(f"{r['method']}: за {r['minutes']} мин." for r in overrides) or "Без напоминаний"
    return {"active": "Активно", "cancelled": "Отменено", "task": "Задача", "meeting": "Встреча", "True": "Да", "False": "Нет", "None": "Не задано"}.get(str(value), str(value))


def event_panel(item, dirty=False):
    values = calendar.rows(item["id"])
    for row in values:
        with st.expander("Сверка события Google Calendar", expanded=bool(row["conflicts"])):
            st.caption(f"Календарь: {row['calendar_id']} · {STATES.get(row['state'], row['state'])}")
            if row["message"]:
                st.info(row["message"])
            for field, conflict in row["conflicts"].items():
                st.warning(f"{LABELS[field]}: правки в обеих системах")
                st.write("CallMind: " + display(conflict["local"]))
                st.write("Google: " + display(conflict["remote"]))
                a, b = st.columns(2)
                for column, choice, text in ((a, "local", "Оставить CallMind"), (b, "remote", "Принять Google")):
                    if column.button(text, key=f"gconflict_{row['key']}_{field}_{choice}", disabled=dirty):
                        try:
                            calendar.resolve(GoogleCalendar(get_settings()), row["key"], field, choice)
                            st.rerun()
                        except ValueError as error:
                            st.error(str(error))
            reminders = row["options"].get("reminders", row["baseline"].get("reminders", {}))
            st.caption("Параметры напоминаний: " + display(reminders))
            if row["baseline"].get("calendar_state") == "cancelled" or item["status"] == "done":
                st.caption("Сейчас уведомления выключены. Сохранённые параметры применятся после восстановления события или возврата задачи в работу.")
            overrides = reminders.get("overrides", [])
            default_minutes = next((r["minutes"] for r in overrides if r["method"] == "popup"), get_settings()["reminder_minutes"])
            with st.form(f"calendar_options_{row['key']}"):
                duration = st.number_input("Длительность этой встречи, минут", min_value=1, max_value=1440,
                    value=row["options"].get("duration_minutes", get_settings()["event_minutes"]) if item["kind"] == "meeting" else 1,
                    disabled=item["date_only"] or item["kind"] != "meeting")
                enabled = st.checkbox("Уведомлять о событии", value=bool(overrides or reminders.get("useDefault")))
                minutes = st.number_input("Уведомить за, минут", min_value=0, max_value=40320, value=default_minutes)
                restore = st.checkbox("Восстановить отменённое событие", disabled=row["state"] == "deleted")
                if st.form_submit_button("Сохранить настройки события", disabled=dirty or row["state"] in {"deleted", "creating", "syncing"}):
                    try:
                        calendar.save_options(row["key"], duration, minutes, enabled, restore)
                        st.session_state.saved_notice = "Настройки события сохранены в CallMind; перенос вручную или при включённой автосинхронизации."
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
            if row["state"] == "uncertain":
                st.caption("Для безопасного повтора сохраните и подтвердите правки, затем нажмите «Перенос в календарь». Используется прежний ID события.")
            if st.button("Сверить с Google", key=f"gcheck_{row['key']}", disabled=dirty or row["state"] in {"conflict", "deleted", "creating", "syncing", "uncertain"}):
                try:
                    calendar.reconcile(GoogleCalendar(get_settings()), row["key"])
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
            if row["state"] == "deleted" and st.button("Проверить восстановление в Google", key=f"grestore_check_{row['key']}", disabled=dirty):
                try:
                    adapter = GoogleCalendar(get_settings())
                    if adapter.calendar_id != row["calendar_id"]:
                        raise ValueError("Выберите прежний календарь в настройках")
                    raw = calendar.read(adapter, row["external_id"])
                    if raw.get("status") == "cancelled":
                        raise ValueError("Сначала восстановите событие в Google Calendar")
                    calendar.mark(row["key"], "outdated")
                    calendar.reconcile(adapter, row["key"])
                    st.rerun()
                except Exception:
                    st.error("Восстановление не подтверждено. Проверьте событие в Google Calendar; локальная история сохранена.")
            st.caption("Отмена события не означает выполнение задачи. Правки действия и срока из Google требуют повторного подтверждения перед следующей отправкой. Повторяющиеся и многодневные события требуют ручной проверки.")


def settings_status():
    if os.getenv("CALLMIND_TEST_MODE") == "1":
        st.info("Календарь: в тестовом режиме фоновая сеть выключена. Явный перенос и сверка доступны.")
    else:
        st.caption("При включённой автосинхронизации связанные события сверяются каждые 30 секунд, пока приложение или отдельный worker работает.")
    from database import SessionLocal
    from models import AppSetting
    with SessionLocal() as db:
        last, error = db.get(AppSetting, "calendar_worker_last_run"), db.get(AppSetting, "calendar_worker_error")
        if last:
            st.caption("Последняя сверка Google: " + last.value.replace("T", " ").split(".")[0])
        if error and error.value:
            st.warning(error.value)
    if st.button("Сверить связанные события Google сейчас", disabled=not connection_status()["google"]):
        try:
            calendar.sync_cycle(force=True)
            st.success("Сверка завершена. Состояние и конфликты показаны в договорённостях.")
        except ValueError as error:
            st.error(str(error))
