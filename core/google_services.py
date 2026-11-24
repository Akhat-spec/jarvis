"""Google для Джарвиса: Gmail, Календарь, Карты.

Доступ — официальный OAuth: пользователь один раз разрешает доступ в браузере, ключ доступа
хранится только на этом компьютере (google_token.json). Нужен «ключ приложения» из Google Cloud —
google_client_secret.json рядом с run.py (как его получить — в README, раздел «Google»).
Карты работают без ключей: открывают Google Карты в браузере.
"""
import base64
import datetime
import json
import re
import threading
import urllib.parse
import webbrowser
from email.mime.text import MIMEText
from email.utils import parseaddr

from .config import ROOT

CLIENT_FILE = ROOT / "google_client_secret.json"
TOKEN_FILE = ROOT / "google_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "openid", "https://www.googleapis.com/auth/userinfo.email",
]

_lock = threading.Lock()
_creds = None


# ── доступ ──
def _load_creds():
    global _creds
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    with _lock:
        if _creds is None and TOKEN_FILE.exists():
            _creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if _creds and not _creds.valid and _creds.refresh_token:
            try:
                _creds.refresh(Request())
            except RefreshError:
                # доступ отозван или истёк (в режиме «Testing» — через 7 дней): ключ больше не годится,
                # убираем его, чтобы HUD предложил подключиться заново, а не застрял на «Ошибке»
                _creds = None
                TOKEN_FILE.unlink(missing_ok=True)
                return None
            data = json.loads(_creds.to_json())
            try:  # адрес аккаунта из прежнего файла не теряем
                data["account"] = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("account", "")
            except Exception:
                pass
            TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
        return _creds if _creds and _creds.valid else None


def status() -> dict:
    if not CLIENT_FILE.exists():
        return {"state": "no_client"}
    try:
        c = _load_creds()
    except Exception as e:
        return {"state": "error", "error": str(e)[:200]}
    if not c:
        return {"state": "not_connected"}
    email = ""
    try:
        info = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        email = info.get("account", "") or ""
    except Exception:
        pass
    return {"state": "connected", "email": email}


def connect() -> dict:
    """Открывает браузер со страницей согласия Google; ждёт ответа (до 5 минут)."""
    global _creds
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_FILE.exists():
        raise FileNotFoundError("нет google_client_secret.json — см. README, раздел «Google»")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, timeout_seconds=300,
                                  authorization_prompt_message="", success_message=(
                                      "Готово: Джарвис подключён к Google. Это окно можно закрыть."))
    data = json.loads(creds.to_json())
    try:  # адрес аккаунта — только чтобы показать в настройках, к какому он подключён
        from googleapiclient.discovery import build
        data["account"] = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute().get("email", "")
    except Exception:
        pass
    TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    with _lock:
        _creds = None
    return status()


def disconnect() -> dict:
    global _creds
    with _lock:
        _creds = None
    TOKEN_FILE.unlink(missing_ok=True)
    return status()


def _svc(name, version):
    from googleapiclient.discovery import build
    c = _load_creds()
    if not c:
        raise PermissionError("Google не подключён: Настройки → Google → «Подключить».")
    return build(name, version, credentials=c, cache_discovery=False)


# ── Gmail ──
def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(payload) -> str:
    """Текст письма: text/plain, иначе text/html без тегов."""
    def walk(p):
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt in ("text/plain", "text/html"):
            yield mt, base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for sub in p.get("parts", []) or []:
            yield from walk(sub)
    parts = list(walk(payload))
    for mt, text in parts:
        if mt == "text/plain":
            return text
    for mt, text in parts:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return ""


def gmail_list(query: str = "is:unread in:inbox", max_results: int = 10) -> list[dict]:
    g = _svc("gmail", "v1")
    ids = g.users().messages().list(userId="me", q=query, maxResults=max(1, min(25, max_results))).execute()
    out = []
    for m in ids.get("messages", []):
        msg = g.users().messages().get(userId="me", id=m["id"], format="metadata",
                                       metadataHeaders=["From", "Subject", "Date"]).execute()
        out.append({"id": m["id"], "from": _header(msg, "From"), "subject": _header(msg, "Subject"),
                    "date": _header(msg, "Date"), "snippet": msg.get("snippet", ""),
                    "unread": "UNREAD" in msg.get("labelIds", [])})
    return out


def gmail_read(msg_id: str) -> dict:
    g = _svc("gmail", "v1")
    msg = g.users().messages().get(userId="me", id=msg_id, format="full").execute()
    text = _body_text(msg.get("payload", {}))
    return {"from": _header(msg, "From"), "to": _header(msg, "To"), "subject": _header(msg, "Subject"),
            "date": _header(msg, "Date"), "text": text[:8000] + ("…[обрезано]" if len(text) > 8000 else "")}


def gmail_send(to: str, subject: str, body: str) -> str:
    if "@" not in parseaddr(to)[1]:
        raise ValueError(f"Непонятный адрес получателя: {to}")
    mime = MIMEText(body, "plain", "utf-8")
    mime["to"], mime["subject"] = to, subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    sent = _svc("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Письмо отправлено (id {sent.get('id')})"


def gmail_unread_count() -> int:
    return _svc("gmail", "v1").users().labels().get(userId="me", id="INBOX").execute().get("messagesUnread", 0)


# ── Календарь ──
def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def calendar_list(days: int = 1) -> list[dict]:
    now = datetime.datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) if days <= 1 else now
    end = start + datetime.timedelta(days=max(1, min(31, days)))
    res = _svc("calendar", "v3").events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=50).execute()
    out = []
    for e in res.get("items", []):
        s, en = e.get("start", {}), e.get("end", {})
        out.append({"title": e.get("summary", "(без названия)"), "start": s.get("dateTime") or s.get("date"),
                    "end": en.get("dateTime") or en.get("date"), "all_day": "date" in s,
                    "location": e.get("location", "")})
    return out


def _parse_dt(text: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=_local_tz())


def calendar_add(title: str, start: str, end: str = "", location: str = "", description: str = "") -> str:
    s = _parse_dt(start)
    e = _parse_dt(end) if end else s + datetime.timedelta(hours=1)
    if e <= s:
        raise ValueError("Конец события раньше начала")
    body = {"summary": title, "start": {"dateTime": s.isoformat()}, "end": {"dateTime": e.isoformat()}}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    ev = _svc("calendar", "v3").events().insert(calendarId="primary", body=body).execute()
    return f"Событие «{title}» добавлено на {s:%d.%m.%Y %H:%M} ({ev.get('htmlLink', '')})"


def next_event() -> dict | None:
    for e in calendar_list(days=2):
        if e["all_day"]:
            continue
        if _parse_dt(e["end"]) > datetime.datetime.now().astimezone():
            return e
    return None


# ── Карты (без ключей) ──
MODES = {"car": "driving", "driving": "driving", "walk": "walking", "walking": "walking",
         "transit": "transit", "bike": "bicycling", "bicycling": "bicycling"}


def maps_search(query: str) -> str:
    webbrowser.open("https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query))
    return f"Открыл Google Карты: {query}"


def maps_route(destination: str, origin: str = "", mode: str = "driving") -> str:
    q = {"api": "1", "destination": destination, "travelmode": MODES.get(mode, "driving")}
    if origin:
        q["origin"] = origin
    webbrowser.open("https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q))
    return f"Открыл маршрут в Google Картах до «{destination}»"
