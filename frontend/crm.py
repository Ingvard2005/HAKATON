"""CRM editors and conflict resolution use durable server-side bindings."""
import os
import streamlit as st
from services import crm
from services.integrations import Bitrix24, connection_status
from services.workspace import deals
from services.connections import connected_bitrix_portal

LABELS = {"name": "Имя", "phone": "Телефон", "email": "Email", "company_id": "Компания CRM",
          "manager_id": "Менеджер CRM", "title": "Название задачи", "deadline": "Срок",
          "status": "Статус", "priority": "Приоритет", "executor": "Исполнитель CRM", "crm_links": "Связанные контакт и сделка"}
STATES = {"synced": "Синхронизировано", "new": "Ещё не перенесено", "outdated": "Ожидает сверки",
          "error": "Ошибка — данные сохранены локально", "conflict": "Нужен выбор значения",
          "uncertain": "Создание не подтверждено", "creating": "Создаётся", "syncing": "Синхронизируется",
          "needs_review": "Нужно подтвердить правки", "deleted": "Удалено или недоступно в CRM"}


def render_bindings(kind, entity_id):
    rows = crm.bindings(kind, entity_id)
    for row in rows:
        st.write(f"**{STATES.get(row['state'], row['state'])}** · {row['portal']}")
        if row["external_id"]:
            st.caption(f"ID Bitrix24: {row['external_id']} · Проверено: {row['updated_at']:%d.%m.%Y, %H:%M}")
        if row["message"]:
            st.info(row["message"])
        for field, values in row["conflicts"].items():
            st.warning(f"{LABELS.get(field, field)}: изменено в обеих системах")
            st.write(f"CallMind: {values['local'] if values['local'] is not None else 'Не задано'}")
            st.write(f"Bitrix24: {values['remote'] if values['remote'] is not None else 'Не задано'}")
            a, b = st.columns(2)
            for column, choice, label in ((a, "local", "Оставить CallMind"), (b, "remote", "Принять CRM")):
                if column.button(label, key=f"resolve_{row['key']}_{field}_{choice}", use_container_width=True):
                    try:
                        crm.resolve_conflict(Bitrix24(), row["key"], field, choice)
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
        if row["state"] not in {"conflict", "creating", "syncing", "uncertain", "deleted"} and kind != "deal":
            if st.button("Сверить с CRM", key=f"reconcile_{row['key']}"):
                try:
                    crm.reconcile(Bitrix24(), row["key"])
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))


def client_panel(client_id):
    with st.expander("Данные клиента и связь с CRM", expanded=True):
        values = crm.client_data(client_id)
        with st.form(f"client_editor_{client_id}"):
            name = st.text_input("Имя клиента", values["name"])
            a, b = st.columns(2)
            phone = a.text_input("Телефон клиента", values["phone"])
            email = b.text_input("Email клиента", values["email"])
            company = a.text_input("ID компании в Bitrix24", values["company_id"])
            manager = b.number_input("ID менеджера контакта в CRM (0 — не задан)", min_value=0, value=values["manager_id"] or 0)
            if st.form_submit_button("Сохранить клиента"):
                try:
                    crm.save_client(client_id, dict(name=name, phone=phone, email=email, company_id=company, manager_id=int(manager) or None))
                    st.success("Сохранено в CallMind. Состояние CRM показано ниже.")
                except ValueError as error:
                    st.error(str(error))
        render_bindings("contact", client_id)
        with st.expander("Выбрать существующий контакт CRM / подтвердить найденный контакт"):
            st.caption("ID берётся из карточки контакта Bitrix24. Выбор проверяет запись, не создаёт новый контакт. Неоднозначные совпадения не объединяются автоматически.")
            contact_id = st.number_input("ID контакта CRM", min_value=1, key=f"contact_id_{client_id}")
            if st.button("Связать контакт", key=f"bind_contact_{client_id}", disabled=not connection_status()["bitrix24"]):
                try:
                    crm.bind_contact(Bitrix24(), client_id, int(contact_id))
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
        if st.button("Получить сделки клиента из CRM", key=f"import_deals_{client_id}", disabled=not connection_status()["bitrix24"]):
            try:
                count = crm.import_deals(Bitrix24(), client_id)
                st.success(f"Получено сделок: {count}. Данные не отправлялись в CRM.")
            except ValueError as error:
                st.error(str(error))
        st.caption("Семейное имя и другие поля существующего контакта CRM не перезаписываются. CallMind синхронизирует поле имени NAME, рабочие телефон/email, компанию по ID и менеджера.")


def task_panel(item):
    options = crm.task_options(item["id"])
    with st.form(f"crm_options_{item['id']}"):
        executor = st.number_input("ID исполнителя задачи в CRM (0 — вошедший пользователь)", min_value=0, value=options["responsible_id"] or 0)
        portal = connected_bitrix_portal()
        available = [d for d in deals() if d["client_id"] == item["client_id"] and portal and crm.binding(portal, "deal", d["id"])]
        ids = [None] + [d["id"] for d in available]
        selected = st.selectbox("Сделка CRM для задачи", ids, index=ids.index(options["deal_id"]) if options["deal_id"] in ids else 0,
            format_func=lambda n: "Без сделки — задача связана с контактом" if n is None else next(d["title"] for d in available if d["id"] == n))
        if st.form_submit_button("Сохранить назначение CRM"):
            try:
                crm.save_task_options(item["id"], int(executor) or None, selected)
                st.success("Назначение сохранено в CallMind. Для первого переноса подтвердите договорённость.")
            except ValueError as error:
                st.error(str(error))
    st.caption("Сделки загружаются в разделе «Клиенты». Новая сделка автоматически из обещания не создаётся.")
    render_bindings("task", item["id"])


def settings_status():
    if os.getenv("CALLMIND_TEST_MODE") == "1":
        st.info("Тестовый режим: фоновые сетевые запросы выключены. Первый перенос остаётся явным действием.")
    else:
        st.caption("При включённой автосинхронизации связанные записи сверяются каждые 30 секунд, пока CallMind запущен. Для работы при закрытом приложении нужен отдельный CRM worker на сервере.")
    from database import SessionLocal
    from models import AppSetting
    with SessionLocal() as db:
        last, error = db.get(AppSetting, "crm_worker_last_run"), db.get(AppSetting, "crm_worker_error")
        if last:
            st.caption("Последняя сверка: " + last.value.replace("T", " ").split(".")[0])
        if error and error.value:
            st.warning(error.value)
    if st.button("Сверить все связанные записи сейчас", key="crm_sync_now", disabled=not connection_status()["bitrix24"]):
        try:
            crm.sync_cycle(force=True)
            st.success("Сверка завершена. Ошибки и конфликты показаны у соответствующих записей.")
        except ValueError as error:
            st.error(str(error))
