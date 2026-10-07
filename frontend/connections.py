"""Two independent OAuth entry points; secrets never enter Streamlit widgets."""
import time
from datetime import datetime

import streamlit as st

from services.connections import availability, start_link, details, portal_url, bitrix_app_config
from services.integrations import connection_status


def connection_state(provider):
    # Poll local state only: never contact providers or send agreements on a timer.
    status = details(provider)
    configured = connection_status()[provider]
    if status.get("status") == "connected" and configured:
        st.success(status["message"])
        st.write(status.get("account") or "Аккаунт подтверждён")
        checked_at = datetime.fromisoformat(status["checked_at"])
        st.caption(f"Проверено: {checked_at:%d.%m.%Y, %H:%M}")
    elif status.get("status") == "needs_check":
        st.warning(status["message"])
    elif configured:
        st.info("Подключение настроено ранее. Повторите вход, чтобы подтвердить доступ.")
    else:
        st.write("Не подключено")


@st.fragment(run_every=3)
def connection_panel():
    st.html('''<style>
    [data-testid="stAppViewContainer"]:has(.st-key-connection_bitrix24){background:#f5f6f8}
    div[class*="st-key-connection_"]{background:#fff;border:1px solid #e2e6ec!important;border-radius:14px;padding:20px!important;gap:14px!important;min-height:470px}
    div[class*="st-key-connection_"] [data-testid="stElementContainer"]:has(>[data-testid="stAlert"]){width:100%!important}
    div[class*="st-key-connection_"] [data-testid="stAlert"]{width:100%}
    div[class*="st-key-connection_"] [data-testid="stAlert"]>div{width:100%;min-height:80px;display:flex;align-items:center;box-sizing:border-box}
    div[class*="st-key-connection_"] [data-testid="stExpander"] summary{min-height:44px}
    div[class*="st-key-connection_"] [data-testid="stLinkButton"] a{min-height:44px}
    div[class*="st-key-connection_"] [data-testid="stCaptionContainer"] p{font-size:14px;line-height:1.6}
    </style>''')
    st.subheader("Подключения")
    st.caption("Войдите в каждый сервис и разрешите доступ. Подключение ничего не отправляет: перенос доступен после подтверждения договорённости.")
    columns = st.columns(2)
    for column, (provider, label) in zip(columns, [("bitrix24", "Bitrix24"), ("google", "Google Calendar")]):
        with column.container(border=True, key=f"connection_{provider}"):
            st.subheader(label)
            connection_state(provider)
            connected = details(provider).get("status") == "connected" and connection_status()[provider]
            portal = ""
            if provider == "bitrix24":
                with st.expander("Портал Bitrix24", expanded=not connected):
                    portal = st.text_input("Адрес вашего Bitrix24", value=bitrix_app_config().get("portal_url", ""), placeholder="https://company.bitrix24.ru", key="bitrix_portal")
            else:
                with st.expander("Календарь для переноса"):
                    from services.settings import get_settings
                    calendar_id = get_settings()["calendar_id"]
                    st.write("Основной календарь" if calendar_id == "primary" else calendar_id)
                    st.caption("Календарь можно изменить ниже в разделе «Правила задач и напоминаний».")
            reason = availability(provider)
            setup_unavailable = bool(reason)
            link = None
            if not reason and (provider == "google" or portal):
                key = f"oauth_link_{provider}"
                cached = st.session_state.get(key)
                if not cached or cached[1] <= time.time() or cached[2] != portal:
                    try:
                        if provider == "bitrix24":
                            portal_url(portal)
                    except ValueError as error:
                        reason = str(error)
                    try:
                        if reason:
                            raise RuntimeError("invalid portal")
                        url, expires = start_link(provider, portal)
                        cached = (url, expires, portal)
                        st.session_state[key] = cached
                    except Exception:
                        reason = reason or "Не удалось подготовить авторизацию. Проверьте настройки приложения и свободный порт обработчика."
                if not reason:
                    link = cached[0]
            if provider == "bitrix24" and not portal and not reason:
                reason = "Сначала укажите адрес портала."
            button_label = "Войти через Google" if provider == "google" else "Подключить Bitrix24"
            if connected:
                button_label = "Сменить аккаунт" if provider == "google" else "Подключить другой портал"
            st.link_button(button_label, link or "#", type="secondary" if connected else "primary", disabled=not link, width="stretch")
            if provider == "google":
                st.caption("Подключён один аккаунт Google. Календарь для переноса выбирается в поле «Google Calendar ID» ниже. Смена аккаунта заменит текущее подключение после успешного входа." if connected else "Вход подключает Google Calendar к CallMind. Вы сами выбираете аккаунт и разрешаете доступ на странице Google.")
            if reason:
                st.caption("Подключение временно недоступно: CallMind ещё не настроен для этого сервиса. Пользователю не нужно создавать приложение или вводить ключи API." if setup_unavailable else reason)
            elif provider == "bitrix24":
                st.caption("CallMind должен быть установлен на вашем портале. Доступ определяется вашими правами в Bitrix24.")
    if st.button("Обновить состояние подключений", key="refresh_connections"):
        for provider in ("bitrix24", "google"):
            st.session_state.pop(f"oauth_link_{provider}", None)
        st.rerun()
