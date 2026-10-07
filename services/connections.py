"""OAuth for the single-account demo. Connecting never transfers agreements."""
import json
import hashlib
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from database import DATA_DIR, SessionLocal
from models import AppSetting, OAuthAttempt
from sqlalchemy import update, delete

LOCK = threading.RLock()
PENDING = {}
SERVER = None
TOKEN_ENDPOINT = "https://oauth.bitrix.info/oauth/token/"


def bitrix_scopes(value):
    # OAuth scope strings can use spaces; Bitrix examples also use commas.
    if isinstance(value, str):
        return set(filter(None, re.split(r"[\s,]+", value)))
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def remember(state, transaction, status="ready"):
    payload = {key: value for key, value in transaction.items() if key != "flow"}
    if "flow" in transaction:
        flow = transaction["flow"]
        payload.update(code_verifier=flow.code_verifier, redirect_uri=flow.redirect_uri,
                       credentials_file=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials-google.json"))
    with SessionLocal() as db:
        db.execute(delete(OAuthAttempt).where(OAuthAttempt.expires < time.time() - 86400))
        db.add(OAuthAttempt(key=digest(state), provider=transaction["provider"], status=status,
                            payload=json.dumps(payload), expires=transaction["expires"]))
        db.commit()


def restore(transaction):
    if transaction["provider"] == "google" and "flow" not in transaction and "credentials_file" in transaction:
        from google_auth_oauthlib.flow import Flow
        from services.integrations import GOOGLE_SCOPES
        transaction["flow"] = Flow.from_client_secrets_file(transaction["credentials_file"],
            scopes=GOOGLE_SCOPES, redirect_uri=transaction["redirect_uri"],
            code_verifier=transaction["code_verifier"], autogenerate_code_verifier=False)
    return transaction


def ensure_listener():
    global SERVER
    with LOCK:
        if SERVER is None:
            SERVER = ThreadingHTTPServer(("127.0.0.1", int(os.getenv("CALLMIND_OAUTH_PORT", "8766"))), CallbackHandler)
            threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def start_link(provider, portal=""):
    reason = availability(provider)
    if reason:
        raise ValueError(reason)
    transaction = dict(provider=provider, expires=time.time() + 86400)
    if provider == "bitrix24":
        transaction["portal"] = portal_url(portal)
    ensure_listener()
    ticket = secrets.token_urlsafe(32)
    remember(ticket, transaction, status="start")
    return callback_base() + f"/oauth/{provider}/start?" + urlencode(dict(ticket=ticket)), transaction["expires"]


def start_authorization(provider, ticket):
    with SessionLocal() as db:
        row = db.get(OAuthAttempt, digest(ticket))
        if not row or row.provider != provider or row.status != "start" or row.expires <= time.time():
            raise ValueError("Ссылка входа устарела. Вернитесь в настройки CallMind и обновите ссылки.")
        transaction = json.loads(row.payload)
    return authorize_link(provider, transaction.get("portal", ""))[0]


def token_path(provider):
    variable = "GOOGLE_TOKEN_FILE" if provider == "google" else "BITRIX24_TOKEN_FILE"
    return Path(os.getenv(variable, str(DATA_DIR / f"{provider}-token.json")))


def atomic_token(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with LOCK:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
        os.chmod(temporary, 0o600)
        temporary.replace(path)


def record(provider, status, message, account=None, diagnostic=None):
    from services.dates import local_now
    value = json.dumps(dict(status=status, message=message, account=account, diagnostic=diagnostic,
                           checked_at=local_now().isoformat()), ensure_ascii=False)
    with SessionLocal() as db:
        db.merge(AppSetting(key=f"oauth_{provider}", value=value))
        db.commit()


def details(provider):
    with SessionLocal() as db:
        row = db.get(AppSetting, f"oauth_{provider}")
        return json.loads(row.value) if row else {}


def portal_url(value):
    parsed = urlparse(value.strip())
    # Cloud portals only: prevents private-network endpoints and arbitrary callback hosts.
    host = parsed.hostname or ""
    suffixes = (".bitrix24.ru", ".bitrix24.com", ".bitrix24.by", ".bitrix24.eu",
                ".bitrix24.de", ".bitrix24.kz", ".bitrix24.ua")
    if (parsed.scheme != "https" or not host.endswith(suffixes) or parsed.username
            or parsed.password or parsed.port or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Укажите HTTPS-адрес облачного портала Bitrix24, например https://company.bitrix24.ru")
    return "https://" + host


def bitrix_app_config():
    path = Path(os.getenv("BITRIX24_CREDENTIALS_FILE", "credentials-bitrix24.json"))
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    for key in ("client_id", "client_secret", "portal_url", "public_base"):
        value = os.getenv("BITRIX24_" + key.upper())
        if value:
            data[key] = value
    return data


def connected_bitrix_portal():
    """Non-secret portal metadata, without a token refresh or network request."""
    path = token_path("bitrix24")
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("portal") if path.is_file() else None
    except (OSError, ValueError):
        return None


def callback_base(provider=None):
    if provider == "bitrix24":
        address = bitrix_app_config().get("public_base")
        if address:
            return address.rstrip("/")
    return os.getenv("CALLMIND_OAUTH_PUBLIC_BASE", "").rstrip("/") or f"http://127.0.0.1:{int(os.getenv('CALLMIND_OAUTH_PORT', '8766'))}"


def availability(provider):
    if provider == "bitrix24":
        config = bitrix_app_config()
        if not config.get("client_id") or not config.get("client_secret"):
            return "Администратору нужно зарегистрировать приложение Bitrix24 и задать его ключи на сервере."
        if not callback_base(provider).startswith("https://"):
            return "Для возврата из Bitrix24 нужен зарегистрированный HTTPS-адрес CallMind и прокси к обработчику авторизации."
    elif provider == "google":
        path = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials-google.json"))
        if not path.is_file():
            return "Администратору нужно включить Calendar API и добавить файл ключей OAuth-приложения Google."
    else:
        raise ValueError("Неизвестный сервис")
    return None


def bitrix_token(grant, **values):
    config = bitrix_app_config()
    response = httpx.get(TOKEN_ENDPOINT, params=dict(grant_type=grant,
        client_id=config["client_id"], client_secret=config["client_secret"], **values), timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("error") or not data.get("access_token") or not data.get("refresh_token"):
        raise ValueError("Bitrix24 не выдал доступ")
    data["expires_at"] = time.time() + int(data["expires_in"])
    return data


def bitrix_credentials():
    with LOCK:
        path = token_path("bitrix24")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["expires_at"] <= time.time() + 60:
            refreshed = bitrix_token("refresh_token", refresh_token=data["refresh_token"])
            if refreshed.get("member_id") != data.get("member_id"):
                raise ValueError("Изменился аккаунт Bitrix24")
            if refreshed.get("client_endpoint", "").rstrip("/") != data["portal"] + "/rest":
                raise ValueError("Изменился адрес портала Bitrix24")
            data.update(refreshed)
            atomic_token(path, json.dumps(data))
        return data


def finish(provider, transaction, parameters):
    if parameters.get("error") or not parameters.get("code"):
        raise ValueError("Доступ не разрешён")
    code = parameters["code"][0]
    if provider == "google":
        from googleapiclient.discovery import build
        flow = transaction["flow"]
        transaction["phase"] = "token_exchange"
        try:
            flow.fetch_token(code=code)
        except Warning as warning:
            # OAuthlib rejects ANY scope change, including a harmless superset.
            # Accept only a token that still grants every required permission.
            from services.integrations import GOOGLE_SCOPES
            token = getattr(warning, "token", None)
            granted = set(getattr(warning, "new_scope", []) or [])
            if not token or not set(GOOGLE_SCOPES).issubset(granted):
                raise ValueError("missing_calendar_permissions") from None
            flow.oauth2session.token = dict(token)
        transaction["phase"] = "credentials_validation"
        credentials = flow.credentials
        from services.integrations import GOOGLE_SCOPES
        if not credentials.valid or not credentials.refresh_token or not credentials.has_scopes(GOOGLE_SCOPES):
            raise ValueError("missing_calendar_permissions")
        transaction["phase"] = "calendar_check"
        calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False).calendars().get(
            calendarId=transaction["calendar_id"]).execute()
        atomic_token(token_path(provider), credentials.to_json())
        record(provider, "connected", "Доступ к календарю подтверждён", calendar["id"])
    else:
        transaction["phase"] = "portal_validation"
        portal = transaction["portal"]
        if parameters.get("domain", [""])[0].lower() != urlparse(portal).hostname:
            raise ValueError("Ответ пришёл от другого портала")
        transaction["phase"] = "token_exchange"
        data = bitrix_token("authorization_code", code=code)
        transaction["phase"] = "token_portal_validation"
        if data.get("client_endpoint", "").rstrip("/") != portal + "/rest":
            raise ValueError("Не совпадает адрес портала")
        transaction["phase"] = "scope_validation"
        # Verify actual permissions with the portal, independently of token metadata
        # and untrusted callback parameters. Do not request all portal scopes.
        response = httpx.post(portal + "/rest/scope.json", data={"auth": data["access_token"]}, timeout=20)
        response.raise_for_status()
        scope_reply = response.json()
        scope_error = scope_reply.get("error")
        if scope_error:
            transaction["scope_error"] = str(scope_error) if re.fullmatch(r"[A-Za-z_]{1,64}", str(scope_error)) else "provider_error"
        granted = bitrix_scopes(scope_reply.get("result"))
        transaction["granted_scopes"] = sorted(granted & {"task", "tasks", "user", "crm"})
        if "task" not in granted:
            raise ValueError("Нет разрешения на задачи")
        data["scope"] = ",".join(sorted(granted))
        transaction["phase"] = "user_check"
        response = httpx.post(portal + "/rest/user.current.json", data={"auth": data["access_token"]}, timeout=20)
        response.raise_for_status()
        user = response.json()["result"]
        data.update(portal=portal, user_id=int(user["ID"]))
        atomic_token(token_path(provider), json.dumps(data))
        record(provider, "connected", "Авторизация и доступ к задачам предоставлены; создание проверяется при переносе",
               f"{portal} · {user.get('NAME', '')} {user.get('LAST_NAME', '')}")


def consume(provider, state, parameters):
    with LOCK:
        code_hash = digest(parameters.get("code", [""])[0])
        with SessionLocal() as db:
            row = db.get(OAuthAttempt, digest(state))
            if not row or row.provider != provider:
                return False, "Этот ответ не относится к текущему входу. Начните вход из настроек CallMind."
            if row.status in ("succeeded", "failed") and row.code_hash == code_hash:
                return row.status == "succeeded", row.result
            if row.expires <= time.time():
                return False, "Время входа истекло. Нажмите «Войти через Google» в настройках CallMind ещё раз." if provider == "google" else "Время входа истекло. Начните подключение заново."
            claim = db.execute(update(OAuthAttempt).where(OAuthAttempt.key == row.key,
                OAuthAttempt.status == "ready").values(status="processing", code_hash=code_hash))
            if claim.rowcount != 1:
                return False, "Этот ответ уже обрабатывается или был использован. Проверьте состояние в настройках CallMind."
            transaction = PENDING.pop(state, None) or json.loads(row.payload)
            db.commit()
    try:
        transaction = restore(transaction)
        finish(provider, transaction, parameters)
        success = True
        message = "Подключение подтверждено. Вернитесь в CallMind — состояние обновится автоматически. Договорённости не отправлялись."
    except Exception as error:
        # Never disclose provider exceptions: they may contain tokens or authorization codes.
        message = "Подключение не подтверждено. Повторите вход и разрешите доступ к сервису. Если ошибка повторяется, подключение нужно проверить на стороне CallMind."
        if isinstance(error, ValueError) and str(error) == "missing_calendar_permissions":
            message = "Google не предоставил оба разрешения календаря. При повторном входе отметьте управление событиями и просмотр сведений о календарях."
        elif type(error).__name__ == "SSLError":
            message = "CallMind не смог установить защищённое соединение с Google. Требуется проверка соединения на стороне приложения."
        elif type(error).__name__ in ("ConnectionError", "ConnectError", "Timeout", "ConnectTimeout", "ReadTimeout", "TransportError"):
            message = "CallMind не смог связаться с сервисом. Проверьте доступ приложения к интернету и начните новый вход из настроек."
        elif getattr(error, "error", None) == "invalid_client":
            message = "Google отклонил ключи приложения CallMind. Требуется исправить настройку приложения."
        elif getattr(error, "error", None) == "invalid_grant":
            message = "Google отклонил одноразовый код входа. Начните новый вход из настроек CallMind."
        diagnostic = {"phase":transaction.get("phase", "restore"), "type":type(error).__name__}
        if "granted_scopes" in transaction:
            diagnostic["granted_scopes"] = transaction["granted_scopes"]
        if "scope_error" in transaction:
            diagnostic["scope_error"] = transaction["scope_error"]
        record(provider, "needs_check", message, diagnostic=diagnostic)
        success = False
    with SessionLocal() as db:
        db.execute(update(OAuthAttempt).where(OAuthAttempt.key == digest(state)).values(
            status="succeeded" if success else "failed", result=message, payload="{}"))
        db.commit()
    return success, message


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Query strings contain one-time authorization codes.

    def do_GET(self):
        parsed = urlparse(self.path)
        provider = {"/oauth/google/callback": "google", "/oauth/bitrix24/callback": "bitrix24"}.get(parsed.path)
        parameters = parse_qs(parsed.query)
        start_provider = {"/oauth/google/start": "google", "/oauth/bitrix24/start": "bitrix24"}.get(parsed.path)
        if start_provider:
            try:
                location = start_authorization(start_provider, parameters.get("ticket", [""])[0])
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            except Exception:
                success, message = False, "Не удалось начать вход. Вернитесь в настройки CallMind и обновите ссылки подключения."
        else:
            success, message = consume(provider, parameters.get("state", [""])[0], parameters) if provider else (False, "Неизвестный адрес")
        body = ("<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<title>CallMind · Подключение</title><h1>CallMind</h1><p>" + message + "</p></html>").encode()
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def authorize_link(provider, portal=""):
    global SERVER
    reason = availability(provider)
    if reason:
        raise ValueError(reason)
    transaction = dict(provider=provider, expires=time.time() + 600)
    state = secrets.token_urlsafe(32)
    redirect = callback_base(provider) + f"/oauth/{provider}/callback"
    if provider == "google":
        from google_auth_oauthlib.flow import Flow
        from services.integrations import GOOGLE_SCOPES
        from services.settings import get_settings
        flow = Flow.from_client_secrets_file(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials-google.json"),
            scopes=GOOGLE_SCOPES, redirect_uri=redirect, autogenerate_code_verifier=True)
        url, _ = flow.authorization_url(state=state, access_type="offline", prompt="consent select_account")
        transaction.update(flow=flow, calendar_id=get_settings()["calendar_id"])
    else:
        transaction["portal"] = portal_url(portal)
        url = transaction["portal"] + "/oauth/authorize/?" + urlencode(dict(client_id=bitrix_app_config()["client_id"], state=state))
    with LOCK:
        ensure_listener()
        for key in list(PENDING):
            if PENDING[key]["expires"] <= time.time():
                del PENDING[key]
        if len(PENDING) >= 100:
            raise ValueError("Слишком много незавершённых подключений. Повторите через 10 минут.")
        PENDING[state] = transaction
        remember(state, transaction)
    return url, transaction["expires"]
