"""Reusable Streamlit workspace components; all actions use persisted services."""
import os
import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
import streamlit as st
from services.storage import (get_call, update_agreement, set_agreement_status, get_revisions,
    get_clients)
from services.workspace import tasks, confirm_task, deals, create_deal, record_outcome, link_deal, analytics
from services.integrations import connection_status, sync_agreement, IntegrationError, reconcile_bitrix
from services.exports import calendar_ics, agreements_csv
from services.settings import get_settings
from services.dates import local_now, is_overdue

RESP = {"manager": "Менеджер", "client": "Клиент", "unknown": "Нужна проверка"}
PRIORITY = {"low": "Низкий", "normal": "Обычный", "high": "Высокий"}
KIND = {"task": "Задача", "meeting": "Встреча"}
OUTCOME = {"open": "В работе", "won": "Выиграна", "lost": "Проиграна"}
PROVIDERS = {"bitrix24": "Bitrix24", "google": "Google Calendar"}
STATES = {"synced": "Подтверждено сервисом", "outdated": "Нужна повторная проверка",
    "error": "Ошибка — нужна повторная проверка", "uncertain": "Результат не подтверждён",
    "syncing": "Синхронизируется", "new": "Настроено, ещё не проверено",
    "conflict": "Нужен выбор значения", "creating": "Создаётся", "needs_review": "Подтвердите правки",
    "deleted": "Удалено или недоступно — история сохранена"}


def fmt(value, date_only=False):
    return value.strftime("%d.%m.%Y" if date_only else "%d.%m.%Y · %H:%M") if value else "Без срока"


def goto(page, **values):
    st.session_state["navigation"] = page
    st.session_state.update(values)


def open_task(item_id):
    goto("Обзор", selected_task=item_id)


def invalidate():
    for key in list(st.session_state):
        if key.startswith("risk_"):
            del st.session_state[key]


def synchronize(item, provider):
    notices = st.session_state.setdefault("sync_messages", {})
    try:
        sync_agreement(item["id"], provider)
        notices[(item["id"], provider)] = (True, "Подтверждено сервисом")
    except IntegrationError as error:
        notices[(item["id"], provider)] = (False, str(error))


def transfer(item, providers):
    """Explicit user action; one failed provider does not hide the other result."""
    for provider in providers:
        synchronize(item, provider)


def status(item, now):
    if item["status"] == "done":
        return "✓ Выполнено"
    if is_overdue(item, now):
        return "! Просрочено"
    if item["deadline"] and item["deadline"].date() == now.date():
        return "◷ Сегодня"
    return "В работе"


def sort_tasks(items, now):
    return sorted(items, key=lambda i: (i["status"] == "done", not is_overdue(i, now),
        not bool(i["deadline"] and i["deadline"].date() == now.date()),
        not bool(i["review_reasons"]), i["deadline"] or datetime.max,
        i["priority"] != "high", i["id"]))


def render_agreement(item, context):
    # Compact shared representation for call and client pages.
    st.write(item["description"])
    st.caption(f"{RESP[item['responsible']]} · {fmt(item['deadline'], item['date_only'])}")
    st.button("Открыть договорённость", key=f"{context}_{item['id']}_open",
        on_click=open_task, args=(item["id"],))


def render_list(items, context="tasks"):
    now = local_now(get_settings()["timezone"])
    if not items:
        st.info("Договорённости не найдены. Измените фильтры или добавьте звонок.")
        return
    st.caption(f"Найдено: {len(items)} · сначала срочные")
    for item in sort_tasks(items, now):
        with st.container(border=True, key=f"taskrow_{context}_{item['id']}"):
            if context.startswith("analytics_"):
                text = st.container()
                due, state = st.columns(2)
                action = st.container()
            else:
                text, due, state, action = st.columns([5, 2, 2, 2], vertical_alignment="center")
            text.write(f"**{item['description']}**")
            text.caption(f"{item['client_name']} · {RESP[item['responsible']]}")
            from services import crm
            from frontend.crm import STATES
            options = crm.task_options(item["id"])
            links = crm.bindings("task", item["id"])
            if links or options["responsible_id"] or options["deal_id"]:
                text.caption(f"Исполнитель CRM: {options['responsible_id'] or 'вошедший пользователь'} · Сделка: {options['deal_id'] or 'без сделки'}")
                state.caption(" · ".join(STATES.get(r["state"], r["state"]) for r in links) or "Не перенесено")
            due.write(fmt(item["deadline"], item["date_only"]))
            label = status(item, now)
            tone = "late" if is_overdue(item, now) else "done" if item["status"] == "done" else "active"
            state.markdown(f'<span class="task-state {tone}">{label}</span>', unsafe_allow_html=True)
            if item["review_reasons"]:
                state.caption("⚠ Требует проверки")
            action.button("Проверить" if item["review_reasons"] else "Открыть", key=f"{context}_{item['id']}_open",
                on_click=open_task, args=(item["id"],), use_container_width=True)


def overview():
    items = tasks()
    now = local_now(get_settings()["timezone"])
    if st.session_state.get("selected_task"):
        item = next((i for i in items if i["id"] == st.session_state.selected_task), None)
        if item:
            detail(item)
            return
        st.session_state.selected_task = None
    st.subheader("Договорённости")
    with st.container(key="urgency"):
        c1, c2, c3 = st.columns(3)
    for col, label, group, values in [
        (c1, "Просрочено", [i for i in items if is_overdue(i, now)], {"due_filter": "Просрочено", "status_filter": "В работе", "review_filter": False}),
        (c2, "Сегодня", [i for i in items if i["status"] != "done" and i["deadline"] and i["deadline"].date() == now.date()], {"due_filter": "Сегодня", "status_filter": "В работе", "review_filter": False}),
        (c3, "Требует проверки", [i for i in items if i["review_reasons"] and i["status"] != "done"], {"review_filter": True, "due_filter": "Все сроки", "status_filter": "В работе"})]:
        col.button(f"{label} · {len(group)}", use_container_width=True, on_click=lambda v=values: st.session_state.update(v))
    query = st.text_input("Поиск по клиенту или договорённости", key="task_search", placeholder="Имя, телефон или действие").strip().casefold()
    with st.expander("Фильтры: статус, срок, сторона"):
        a, b, c = st.columns(3)
        state = a.selectbox("Статус", ["В работе", "Все статусы", "Выполнено"], key="status_filter")
        due = b.selectbox("Срок", ["Все сроки", "Просрочено", "Сегодня", "Предстоящие", "Без срока"], key="due_filter")
        side = c.selectbox("Сторона обязательства", ["Все стороны", *RESP.values()], key="side_filter")
        check = st.checkbox("Только требует проверки", key="review_filter")
    st.caption(" · ".join([state, due, side] + (["Требует проверки"] if check else [])))
    filtered = [i for i in items if query in f"{i['client_name']} {i['client_phone'] or ''} {i['description']}".casefold()
        and (state == "Все статусы" or (i["status"] == "done") == (state == "Выполнено"))
        and (side == "Все стороны" or RESP[i["responsible"]] == side)
        and (not check or i["review_reasons"])
        and (due == "Все сроки" or (due == "Просрочено" and is_overdue(i, now))
            or (due == "Сегодня" and i["deadline"] and i["deadline"].date() == now.date())
            or (due == "Предстоящие" and i["deadline"] and i["deadline"].date() > now.date())
            or (due == "Без срока" and not i["deadline"]))]
    render_list(filtered)
    with st.expander("Экспорт текущего списка"):
        with st.container(key="export_actions"):
            csv_column, pdf_column, _ = st.columns([1.2, 1.2, 7.6])
        csv_column.download_button("Скачать CSV", agreements_csv(filtered), "callmind-tasks.csv", "text/csv")
        from services.pdf_export import agreements_pdf
        export_filters = " · ".join([state, due, side] + (["Требует проверки"] if check else []) + ([f"Поиск: {query}"] if query else []))
        try:
            pdf = agreements_pdf(sort_tasks(filtered, now), get_settings(), filters=export_filters, generated_at=now)
            pdf_column.download_button("Скачать PDF", pdf, "callmind-tasks.pdf", "application/pdf")
        except (ImportError, OSError):
            pdf_column.error("PDF недоступен: проверьте установку ReportLab и файла шрифта. CSV остаётся доступным.")


def draft_values(item):
    return dict(description=item["description"], responsible=item["responsible"],
        has=bool(item["deadline"]), day=item["deadline"].date() if item["deadline"] else local_now(get_settings()["timezone"]).date(),
        date_only=item["date_only"], clock=item["deadline"].time().replace(second=0, microsecond=0) if item["deadline"] else time(17),
        priority=item["priority"], kind=item["kind"])


def discard_draft(item_id):
    prefix = f"draft_{item_id}_"
    for key in list(st.session_state):
        if key.startswith(prefix):
            del st.session_state[key]
    st.session_state.setdefault("dirty_tasks", set()).discard(item_id)
    st.session_state.setdefault("task_drafts", {}).pop(item_id, None)


def remember_draft(item_id):
    draft = st.session_state.setdefault("task_drafts", {}).setdefault(item_id, {})
    prefix = f"draft_{item_id}_"
    for field in ("description", "responsible", "has", "day", "date_only", "clock", "priority", "kind"):
        if prefix + field in st.session_state:
            draft[field] = st.session_state[prefix + field]


def detail(item):
    settings = get_settings()
    st.button("← К списку", on_click=lambda: st.session_state.update(selected_task=None))
    st.subheader(item["description"])
    st.caption(f"{item['client_name']} · звонок #{item['call_id']} · {fmt(item['call_datetime'])}")
    st.write(f"**{status(item, local_now(settings['timezone']))}** · {KIND[item['kind']]} · {RESP[item['responsible']]}")
    if item["review_reasons"]:
        st.warning("Нужна проверка: " + "; ".join(item["review_reasons"]))
    st.write("**Цитата из разговора**")
    st.write(item["evidence"] or "Цитата отсутствует")
    if item["deadline_original"]:
        st.caption("Срок в разговоре: «" + item["deadline_original"] + "»")
    call = get_call(item["call_id"])
    segment = next((s for s in call["transcript_segments"] if item["evidence"] and item["evidence"] in s["text"]), None)
    if segment and item["evidence"] in call["transcript"] and call["audio_path"] and Path(call["audio_path"]).is_file():
        st.audio(call["audio_path"], start_time=int(segment["start"]))
        st.caption(f"Запись с подтверждённой отметки {segment['start']:.1f} сек.")
    else:
        st.caption("Переход к цитате недоступен: нет подходящей отметки или доступной записи.")
    a, b = st.columns(2)
    a.button("Открыть звонок", on_click=goto, args=("Звонки",), kwargs={"focused_call": item["call_id"], "upload": False})
    b.button("Открыть клиента", on_click=goto, args=("Клиенты",), kwargs={"focused_client": item["client_id"]})
    base = draft_values(item)
    prefix = f"draft_{item['id']}_"
    for field, value in base.items():
        st.session_state.setdefault(prefix + field, st.session_state.get("task_drafts", {}).get(item["id"], {}).get(field, value))
    st.write("**Исправить договорённость**")
    st.caption("Первый перенос — после подтверждения и явного действия. Автосинхронизация CRM и Google включается отдельно в настройках и касается только связанных записей. Изменённые действие и срок перед отправкой нужно подтвердить.")
    changes = dict(on_change=remember_draft, args=(item["id"],))
    description = st.text_area("Действие", key=prefix + "description", height=90, **changes)
    a, b = st.columns(2)
    responsible = a.selectbox("Чья договорённость", list(RESP), format_func=RESP.get, key=prefix + "responsible", **changes)
    kind = b.selectbox("Тип", list(KIND), format_func=KIND.get, key=prefix + "kind", **changes)
    if kind == "meeting":
        st.caption(f"Дата и время задают начало встречи. Для новых событий длительность — {settings['event_minutes']} мин.; для связанных событий её можно изменить ниже.")
    has = st.checkbox("Есть срок", key=prefix + "has", **changes)
    if has:
        day = st.date_input("Дата выполнения", key=prefix + "day", **changes)
        date_only = st.checkbox("Без точного времени", key=prefix + "date_only", **changes)
        clock = st.time_input("Время выполнения", key=prefix + "clock", **changes) if not date_only else time.min
    else:
        day, date_only, clock = st.session_state[prefix + "day"], True, time.min
    priority = st.selectbox("Приоритет", list(PRIORITY), format_func=PRIORITY.get, key=prefix + "priority", **changes)
    dirty = any(st.session_state[prefix + k] != v for k, v in base.items())
    if dirty:
        st.session_state.setdefault("dirty_tasks", set()).add(item["id"])
        st.warning("Есть несохранённые изменения. Они сохраняются как черновик в этой сессии.")
    else:
        st.session_state.setdefault("dirty_tasks", set()).discard(item["id"])
    a, b = st.columns(2)
    if a.button("Сохранить изменения", type="primary", key="save_task", disabled=not dirty):
        try:
            update_agreement(item["id"], description=description, responsible=responsible,
                deadline=datetime.combine(day, time.min if date_only else clock) if has else None,
                date_only=date_only, priority=priority, kind=kind)
            invalidate()
            discard_draft(item["id"])
            st.session_state["saved_notice"] = "Сохранено в CallMind. Статусы внешних сервисов указаны отдельно ниже."
            st.rerun()
        except ValueError as error:
            st.error("Не сохранено: " + str(error))
    b.button("Отменить правки", disabled=not dirty, on_click=discard_draft, args=(item["id"],))
    if st.session_state.get("saved_notice"):
        st.success(st.session_state.pop("saved_notice"))
    a, b = st.columns(2)
    if a.button("Подтвердить", disabled=dirty, key="confirm_task", help="Подтверждает проверку в CallMind. Отправка — отдельной кнопкой переноса."):
        try:
            confirm_task(item["id"])
            st.rerun()
        except ValueError as error:
            st.error(str(error))
    if b.button("Вернуть в работу" if item["status"] == "done" else "Отметить выполненным", key="task_status", disabled=dirty):
        set_agreement_status(item["id"], "pending" if item["status"] == "done" else "done")
        invalidate()
        st.rerun()
    with st.expander("CRM и календарь", expanded=True):
        executor = os.getenv("BITRIX24_RESPONSIBLE_ID")
        st.caption(f"Исполнитель по умолчанию: ID {executor}" if executor else "Исполнитель по умолчанию — пользователь, подключивший Bitrix24. Можно выбрать другой ID ниже.")
        st.caption("Сторона обязательства и исполнитель CRM — разные поля. Клиентская договорённость становится задачей менеджера на контроль.")
        from frontend.crm import task_panel
        task_panel(item)
        st.caption("Встреча → событие с занятостью. Задача → напоминание по сроку, без занятости; без времени — на весь день. При ручном переносе: без срока новое событие не создаётся, удаление срока отменяет прежнее событие, выполненная задача отменяет напоминание. Состоявшаяся встреча остаётся в истории.")
        st.caption("После «Подтвердить» выберите направление: «Перенос» — в Bitrix24 и Google Calendar; две другие кнопки — только в указанный сервис. Передаются данные клиента и цитата, запись разговора не передаётся. Повторный перенос обновляет ранее созданную запись.")
        configured_services = connection_status()
        google_links = [l for l in item["integrations"] if l["provider"] == "google"]
        ready = not dirty and not item["review_reasons"]
        crm_ready = ready and configured_services["bitrix24"]
        calendar_ready = ready and configured_services["google"] and bool(item["deadline"] or google_links)
        if not ready:
            st.info("Сначала сохраните правки и нажмите «Подтвердить». При неопределённой стороне или отсутствующей цитате исправьте причину проверки.")
        if not all(configured_services.values()):
            st.caption("Кнопки неподключённых сервисов недоступны. Подключения настраиваются в разделе «Настройки».")
        if not item["deadline"] and not google_links:
            st.caption("Для переноса в календарь укажите срок. Перенос только в CRM доступен без срока.")
        a, b, c = st.columns(3)
        for col, label, key, providers, enabled in [
            (a, "Перенос", "sync_both", ("bitrix24", "google"), crm_ready and calendar_ready),
            (b, "Перенос в CRM", "sync_bitrix24", ("bitrix24",), crm_ready),
            (c, "Перенос в календарь", "sync_google", ("google",), calendar_ready)]:
            if col.button(label, key=key, disabled=not enabled, type="primary" if key == "sync_both" else "secondary",
                help="Bitrix24 и Google Calendar" if key == "sync_both" else None, use_container_width=True):
                transfer(item, providers)
                st.rerun()
        for provider, configured in configured_services.items():
            st.write(f"**{PROVIDERS[provider]}**")
            links = [l for l in item["integrations"] if l["provider"] == provider]
            if not configured:
                st.caption("Не подключено")
            elif not links:
                st.caption("Настроено, ещё не проверено")
            for link in links:
                st.caption(STATES.get(link["state"], link["state"]) + " · " + fmt(link.get("updated_at")))
                if link["message"]:
                    st.warning(link["message"])
                if link["url"]:
                    st.link_button("Открыть в " + PROVIDERS[provider], link["url"])
            for attempt in item.get("last_attempts", []):
                if attempt["provider"] == provider:
                    st.caption("Последняя попытка · " + fmt(attempt["recorded_at"]))
                    (st.success if attempt["success"] else st.error)(attempt["message"])
            notice = st.session_state.get("sync_messages", {}).get((item["id"], provider))
            if notice:
                (st.success if notice[0] else st.error)(notice[1])
        if any(l["state"] in {"uncertain", "syncing"} and l["provider"] == "bitrix24" for l in item["integrations"]):
            remote_id = st.text_input("ID уже созданной задачи Bitrix24")
            if st.button("Привязать без повторного создания"):
                try:
                    reconcile_bitrix(item["id"], remote_id)
                    st.rerun()
                except IntegrationError as error:
                    st.error(str(error))
        if item["deadline"]:
            from services.calendar_sync import export_settings
            export_config = export_settings(item["id"], settings)
            st.download_button("Скачать .ics", calendar_ics(item, export_config), f"agreement-{item['id']}.ics", "text/calendar")
            if export_config.get("calendar_reminders", {}).get("useDefault"):
                st.caption("В ICS не включены неизвестные стандартные уведомления Google; настройте их при импорте.")
        from frontend.calendar_sync import event_panel
        event_panel(item, dirty)
    with st.expander("Связанные сделки"):
        available = [d for d in deals() if d["client_id"] == item["client_id"]]
        for deal in available:
            if deal["id"] in item["deal_ids"]:
                st.button(deal["title"], key=f"deal_link_{deal['id']}", on_click=goto,
                    args=("Сделки",), kwargs={"focused_deal": deal["id"]})
        if available:
            deal_id = st.selectbox("Связать со сделкой клиента", [d["id"] for d in available],
                format_func=lambda n: next(d["title"] for d in available if d["id"] == n))
            if st.button("Добавить связь"):
                link_deal("agreement", item["id"], deal_id)
                st.rerun()
        else:
            st.caption("Нет сделок этого клиента. Создайте сделку в разделе «Сделки».")
    with st.expander("История изменений"):
        render_history("agreement", item["id"])


def render_history(entity_type, entity_id):
    labels = {"description": "Действие", "responsible": "Сторона", "deadline": "Срок",
        "date_only": "Без времени", "priority": "Приоритет", "kind": "Тип", "status": "Статус",
        "summary": "Резюме", "transcript": "Транскрипция", "next_action": "Следующий шаг", "follow_up_required": "Последующий контакт",
        "review": "Проверка", "reviewed_at": "Время подтверждения"}
    values = {**RESP, **PRIORITY, **KIND, "pending": "В работе", "done": "Выполнено", "None": "Не указано"}
    revisions = get_revisions(entity_type, entity_id)
    if not revisions:
        st.caption("Правок пока нет")
    for revision in revisions:
        st.caption(fmt(revision["created_at"]) + " · " + revision["after"].get("источник", "Ручная правка / API; автор старых правок не записан"))
        for field, value in revision["after"].items():
            before = revision["before"].get(field)
            if before != value:
                st.write(f"{labels.get(field, field)}: {values.get(str(before), str(before))} → {values.get(str(value), str(value))}")


def deal_page():
    st.subheader("Сделки")
    st.caption("Результат поступает из Bitrix24 или подтверждается человеком. Выполнение договорённости и прогноз AI не закрывают сделку.")
    st.info("Получите сделки в карточке связанного клиента. Стадии и результаты импортируются из CRM; название, сумма и стадия сделки обратно в CRM пока не отправляются.")
    clients = get_clients()
    with st.expander("Создать сделку"):
        if not clients:
            st.caption("Сначала добавьте звонок клиента.")
        else:
            with st.form("create_deal"):
                client_id = st.selectbox("Клиент сделки", [c["id"] for c in clients], format_func=lambda n: next(c["name"] for c in clients if c["id"] == n))
                title = st.text_input("Название сделки")
                amount = st.text_input("Сумма, если известна")
                currency = st.selectbox("Валюта", ["RUB", "BYN", "USD", "EUR"])
                day = st.date_input("Фактическая дата создания", local_now(get_settings()["timezone"]).date())
                if st.form_submit_button("Создать", type="primary"):
                    try:
                        st.session_state.focused_deal = create_deal(client_id, title, amount, currency, datetime.combine(day, time.min))
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
    rows = deals()
    query = st.text_input("Поиск сделки или клиента").casefold()
    rows = [d for d in rows if query in f"{d['title']} {d['client_name']}".casefold()]
    if not rows:
        st.info("Сделки не найдены. Недостаточно данных для результатов продаж.")
        return
    ids = [d["id"] for d in rows]
    focused = st.session_state.get("focused_deal")
    selected = st.selectbox("Сделка", ids, index=ids.index(focused) if focused in ids else 0,
        format_func=lambda n: next(f"{d['title']} · {d['client_name']} · {OUTCOME[d['outcome']]}" for d in rows if d["id"] == n))
    deal = next(d for d in rows if d["id"] == selected)
    from frontend.crm import render_bindings
    render_bindings("deal", selected)
    from services import crm
    for link in crm.bindings("deal", selected):
        baseline = json.loads(crm.binding(link["portal"], "deal", selected)["baseline"])
        st.caption(f"Стадия CRM: {baseline.get('stage') or 'не задана'} · Менеджер CRM, ID: {baseline.get('manager_id') or 'не задан'}")
    st.write(f"**{OUTCOME[deal['outcome']]}** · {deal['amount'] or 'Сумма неизвестна'} {deal['currency'] if deal['amount'] else ''}")
    if deal["closed_at"]:
        source = "CRM: время смены стадии (при отсутствии — время обновления записи)" if deal["source"] == "crm" else "Подтверждение человеком"
        st.caption(f"Результат: {fmt(deal['closed_at'])} · {source}: {deal['actor']} · получено {fmt(deal['recorded_at'])}")
    with st.form(f"outcome_{selected}"):
        outcome = st.selectbox("Подтверждённый результат", list(OUTCOME), index=list(OUTCOME).index(deal["outcome"]), format_func=OUTCOME.get)
        day = st.date_input("Фактическая дата результата", local_now(get_settings()["timezone"]).date())
        clock = st.time_input("Фактическое время результата", local_now(get_settings()["timezone"]).time().replace(second=0, microsecond=0))
        reason = st.text_input("Причина проигрыша (обязательна для проигранной сделки)")
        actor = st.text_input("Кто подтверждает", help="Имя для журнала; это не система корпоративных прав доступа")
        if st.form_submit_button("Подтвердить результат", type="primary"):
            try:
                record_outcome(selected, outcome, datetime.combine(day, clock), reason, actor)
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    st.write("**Связанные записи**")
    for link in deal["links"]:
        if link["entity_type"] == "agreement":
            st.button(f"Договорённость #{link['entity_id']}", key=f"dl_{link['entity_id']}", on_click=open_task, args=(link["entity_id"],))
        else:
            st.button(f"Звонок #{link['entity_id']}", key=f"cl_{link['entity_id']}", on_click=goto, args=("Звонки",), kwargs={"focused_call": link["entity_id"], "upload": False})
    calls = __import__("services.storage", fromlist=["get_client_calls"]).get_client_calls(deal["client_id"])
    if calls:
        cid = st.selectbox("Связать звонок", [c["id"] for c in calls], format_func=lambda n: f"Звонок #{n}")
        if st.button("Добавить связь со звонком"):
            link_deal("call", cid, selected)
            st.rerun()
    with st.expander("Журнал результатов"):
        for event in reversed(deal["events"]):
            st.write(f"{OUTCOME[event['outcome']]} · {fmt(event['occurred_at'])}")
            st.caption(f"Источник: человек · {event['actor']} · записано {fmt(event['recorded_at'])}")
            if event["reason"]:
                st.write(event["reason"])


@contextmanager
def analytics_card(column, label, value, key, tone="neutral"):
    """A real metric with its definition and the records used to calculate it."""
    with column:
        with st.container(border=True, key=f"analytics_card_{tone}_{key}"):
            st.metric(label, "—" if value == "Недостаточно данных" else value)
            if value == "Недостаточно данных":
                st.caption("Недостаточно данных")
            else:
                st.caption("Состояние сейчас" if key in {"overdue", "today", "no_deadline", "review", "sync_errors"} else "За выбранный период")
            with st.expander("Записи и расчёт"):
                yield


def analytics_page():
    st.html('''<style>
    [data-testid="stAppViewContainer"]:has(div[class*="st-key-analytics_card_"]){background:#f5f6f8}
    div[class*="st-key-analytics_card_"]{background:#fff;border-radius:14px;padding:18px!important;border:1px solid #e2e6ec!important;gap:12px!important}
    div[class*="st-key-analytics_card_"] [data-testid="stMetricLabel"]{color:#485566;white-space:normal;min-height:40px}
    div[class*="st-key-analytics_card_"] [data-testid="stMetricLabel"] p{white-space:normal!important;overflow:visible!important;text-overflow:clip!important}
    [data-testid="stAppViewContainer"]:has(div[class*="st-key-analytics_card_"]) [data-testid="stDateInput"] input{background:#fff}
    div[class*="st-key-analytics_card_"] [data-testid="stMetricValue"]{font-size:34px;font-weight:700;line-height:1.2;color:#182230}
    div[class*="st-key-analytics_card_danger_"] [data-testid="stMetricValue"]{color:#b5202b}
    div[class*="st-key-analytics_card_success_"] [data-testid="stMetricValue"]{color:#21643d}
    div[class*="st-key-analytics_card_attention_"] [data-testid="stMetricValue"]{color:#8a5700}
    div[class*="st-key-analytics_card_"] [data-testid="stExpander"]{background:#fafbfc;border-radius:8px}
    div[class*="st-key-analytics_card_"] [data-testid="stExpander"] summary{min-height:44px}
    .st-key-analytics_summary [data-testid="stHorizontalBlock"]:has(details[open]){display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}
    .st-key-analytics_summary [data-testid="stHorizontalBlock"]:has(details[open])>[data-testid="stColumn"]{width:100%!important;min-width:0!important}
    .st-key-analytics_summary [data-testid="stColumn"]:has(details[open]){grid-column:1 / -1;min-width:0!important}
    @media(max-width:1000px){.st-key-analytics_summary [data-testid="stHorizontalBlock"]{flex-wrap:wrap}
    .st-key-analytics_summary [data-testid="stColumn"]{flex:1 1 45%!important;width:calc(50% - .5rem)!important}
    .st-key-analytics_summary [data-testid="stHorizontalBlock"]:has(details[open]){grid-template-columns:repeat(2,minmax(0,1fr))}
    .st-key-analytics_summary [data-testid="stColumn"]:has(details[open]){flex-basis:100%!important;width:100%!important}
    div[class*="st-key-analytics_card_"]{padding:14px!important}
    div[class*="st-key-analytics_card_"] [data-testid="stMetricValue"]{font-size:28px}
    div[class*="st-key-analytics_card_"] [data-testid="stExpander"] summary{font-size:13px}}
    </style>''')
    st.title("Аналитика")
    st.caption("Договорённости и результаты продаж — два отдельных среза работы.")
    now = local_now(get_settings()["timezone"])
    a, b = st.columns(2)
    start = a.date_input("Начало периода", now.date() - timedelta(days=29))
    end = b.date_input("Конец периода", now.date())
    with st.expander("Фильтры аналитики"):
        clients = get_clients()
        client_id = st.selectbox("Клиент", [None, *[c["id"] for c in clients]], format_func=lambda n: "Все клиенты" if n is None else next(c["name"] for c in clients if c["id"] == n))
        priority = st.selectbox("Приоритет договорённостей", [None, *PRIORITY], format_func=lambda n: "Все" if n is None else PRIORITY[n])
        kind = st.selectbox("Тип договорённостей", [None, *KIND], format_func=lambda n: "Все" if n is None else KIND[n])
        st.caption("Приоритет и тип относятся к договорённостям; результаты сделок фильтруются по клиенту и периоду.")
    try:
        report = analytics(start, end, client_id, priority, kind)
    except ValueError as error:
        st.error(str(error))
        return
    st.caption(f"Период {fmt(datetime.combine(start, time.min), True)} — {fmt(report['end'], True)} · {get_settings()['timezone']}")
    st.subheader("Соблюдение договорённостей")
    st.caption("Создание и выполнение — за выбранный период. Просрочки, проверка и сервисы — состояние сейчас.")
    task_summary = st.container(key="analytics_summary")
    with task_summary:
        task_rows = [st.columns(4), st.columns(4)]
    for index, (key, label, definition) in enumerate([("new", "Новые", "Созданы в выбранном периоде."),
        ("completed", "Выполненные", "Хотя бы один переход в «Выполнено» за период; каждая задача считается один раз."),
        ("overdue", "Подтверждённые просрочки сейчас", "Текущий снимок, независимо от периода; только проверенные договорённости."),
        ("today", "На сегодня", "Текущие активные договорённости со сроком сегодня, включая требующие проверки."),
        ("no_deadline", "Без срока", "Текущие активные договорённости без даты; отсутствие срока само по себе не ошибка."),
        ("review", "Требуют проверки", "Текущие активные договорённости с причинами проверки."),
        ("sync_errors", "Сервисы требуют внимания", "Текущие ошибки, неподтверждённые запросы, изменения после отправки или незавершённая синхронизация.")]):
        tone = {"completed": "success", "overdue": "danger", "review": "attention", "sync_errors": "attention"}.get(key, "neutral")
        with analytics_card(task_rows[index // 4][index % 4], label, len(report[key]), key, tone):
            st.caption(definition)
            render_list(report[key], "analytics_" + key)
    total = len(report["eligible"])
    ratio = f"{len(report['ontime']) / total:.0%}" if total else "Недостаточно данных"
    with analytics_card(task_rows[1][3], "Соблюдение сроков", ratio, "deadline"):
        st.caption("Выполнены к первому подтверждённому сроку / все проверенные задачи со сроком, наступившим в периоде. Перенос срока не стирает нарушение. Подтверждение после срока не участвует. Старые записи без подтверждения не восстанавливаются догадками.")
        st.caption(f"База: {total}; вовремя: {len(report['ontime'])}; без подходящего подтверждения: {len(report['excluded'])}.")
        render_list(report["eligible"], "analytics_due")
        if st.checkbox("Показать записи без подходящего подтверждения"):
            render_list(report["excluded"], "analytics_excluded")
    from frontend.analytics_charts import donut
    ontime_ids = {item["id"] for item in report["ontime"]}
    donut([
        ("Выполнены вовремя", report["ontime"], "#21643d"),
        ("Не выполнены вовремя", [item for item in report["eligible"] if item["id"] not in ontime_ids], "#b5202b"),
    ], "Соблюдение сроков", "Только проверенные задачи с первым подтверждённым сроком в выбранном периоде. Непроверенные записи исключены; перенос срока не стирает нарушение.",
        "deadlines", lambda items, context: render_list(items, context))
    st.subheader("Результаты сделок")
    st.caption("Только подтверждённые выигрыши и проигрыши. Выполнение задачи не означает выигрыш сделки.")
    count = len(report["won"]) + len(report["lost"])
    deal_columns = st.columns(3)
    for index, (key, label) in enumerate([("won", "Выигранные"), ("lost", "Проигранные")]):
        with analytics_card(deal_columns[index], label, len(report[key]), key, "success" if key == "won" else "danger"):
            st.caption("Фактическая дата результата в периоде; последнее состояние на конец периода. Источник — ручное подтверждение человека.")
            for deal in report[key]:
                st.button(f"{deal['title']} · {deal['client_name']} · {fmt(deal['closed_at'])}", key=f"analytic_deal_{deal['id']}", on_click=goto, args=("Сделки",), kwargs={"focused_deal": deal["id"]})
            if not report[key]:
                st.caption("Нет подтверждённых результатов")
    with analytics_card(deal_columns[2], "Доля выигранных", f"{len(report['won']) / count:.1%}" if count else "Недостаточно данных", "win_rate"):
        st.caption(f"Выиграно {len(report['won'])} из {count} закрытых сделок.")
        st.caption("Выигранные / (выигранные + проигранные); сделки в работе не входят. Выполненная задача и прогноз риска AI не являются результатом сделки.")
        for d in report["won"] + report["lost"]:
            st.button(f"{d['title']} · {OUTCOME[d['outcome']]}", key=f"rate_{d['id']}", on_click=goto, args=("Сделки",), kwargs={"focused_deal": d["id"]})
        if not count:
            st.caption("Подтвердите результаты сделок с фактической датой.")
    def chart_deals(items, context):
        if not items:
            st.caption("Нет подтверждённых результатов")
        for deal in items:
            st.button(f"{deal['title']} · {deal['client_name']}", key=f"{context}_{deal['id']}",
                on_click=goto, args=("Сделки",), kwargs={"focused_deal": deal["id"]})
    donut([
        ("Выигранные", report["won"], "#21643d"),
        ("Проигранные", report["lost"], "#b5202b"),
    ], "Результаты закрытых сделок", "Подтверждённые выигрыши и проигрыши за выбранный период. Сделки в работе и прогнозы AI в расчёт не входят.",
        "deals", chart_deals)
    currencies = sorted({d["currency"] for d in report["won"] if d["amount"] is not None})
    finance_columns = st.columns(2)
    from decimal import Decimal
    for currency in currencies:
        rows = [d for d in report["won"] if d["currency"] == currency and d["amount"] is not None]
        total_amount = sum(Decimal(d['amount']) for d in rows)
        with analytics_card(finance_columns[0], f"Сумма выигранных · {currency}", f"{total_amount:,.2f}", f"money_{currency}"):
            st.write(f"Средняя сумма: {total_amount / len(rows):,.2f} {currency}")
            st.caption("Сумма сделок, не полученная выручка. Валюты не складываются. Неизвестные суммы исключены.")
            st.caption(f"Средняя: сумма / {len(rows)} выигранных сделок с известной суммой в этой валюте.")
            for d in rows:
                st.button(d["title"], key=f"money_{d['id']}", on_click=goto, args=("Сделки",), kwargs={"focused_deal": d["id"]})
    if not currencies:
        with analytics_card(finance_columns[0], "Сумма выигранных", "Недостаточно данных", "money_empty"):
            st.caption("Нужны подтверждённые выигрыши с суммой и валютой.")
    closed = report["won"] + report["lost"]
    from statistics import median
    intervals = [(d["closed_at"] - d["created_at"]).total_seconds() / 86400 for d in closed if d["closed_at"] >= d["created_at"]]
    with analytics_card(finance_columns[1], "Время до закрытия · медиана", f"{median(intervals):.1f} дней" if intervals else "Недостаточно данных", "duration"):
        st.caption("Медиана фактическое закрытие − создание; только подтверждённые закрытия с корректными датами.")
        for d in closed:
            st.button(d["title"], key=f"duration_{d['id']}", on_click=goto, args=("Сделки",), kwargs={"focused_deal": d["id"]})
    st.caption(f"Сумма известна у {sum(d['amount'] is not None for d in report['won'])} из {len(report['won'])} выигранных сделок. Сумма сделок не равна полученной выручке.")
    with st.expander("Подтверждённые причины проигрыша"):
        if not report["lost"]:
            st.caption("Недостаточно данных — нужны проигранные сделки с подтверждённой причиной.")
        from collections import Counter
        for reason, count in Counter(d["reason"] or "Не указано" for d in report["lost"]).items():
            st.write(f"{reason} · {count}")
            for deal in report["lost"]:
                if (deal["reason"] or "Не указано") == reason:
                    st.button(deal["title"], key=f"reason_{deal['id']}", on_click=goto, args=("Сделки",), kwargs={"focused_deal": deal["id"]})
    st.caption("История закрытий и повторных открытий сохраняется. Эти данные не доказывают причины потерь или влияние CallMind на продажи.")
