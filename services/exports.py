import csv
import io
from datetime import timedelta, datetime, timezone
from services.dates import deadline_with_zone


def agreements_csv(items):
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Клиент", "Телефон", "Договорённость", "Ответственный", "Срок", "Статус", "Приоритет"])
    def safe(value):
        text = str(value or "")
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
    for item in items:
        writer.writerow([safe(item.get(k)) for k in ("id", "client_name", "client_phone", "description", "responsible", "deadline", "status", "priority")])
    return output.getvalue().encode("utf-8-sig")


def calendar_ics(item, settings):
    if not item["deadline"]:
        raise ValueError("Укажите срок для календаря")
    def escape(value):
        return str(value).replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CallMind//RU", "BEGIN:VEVENT",
        f"UID:agreement-{item['id']}@callmind.local", f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"]
    if item["date_only"]:
        lines += [f"DTSTART;VALUE=DATE:{item['deadline']:%Y%m%d}", f"DTEND;VALUE=DATE:{item['deadline'] + timedelta(days=1):%Y%m%d}"]
    else:
        value = deadline_with_zone(item, settings["timezone"]).astimezone(timezone.utc)
        duration = settings['event_minutes'] if item.get('kind') == 'meeting' else 1
        lines += [f"DTSTART:{value:%Y%m%dT%H%M%SZ}", f"DTEND:{value + timedelta(minutes=duration):%Y%m%dT%H%M%SZ}"]
    meeting = item.get('kind') == 'meeting'
    lines += [f"SUMMARY:{escape(item['description'] if meeting else 'Напоминание: ' + item['description'])}",
        f"TRANSP:{'OPAQUE' if meeting else 'TRANSPARENT'}",
        f"STATUS:{'CANCELLED' if settings.get('calendar_cancelled') or (item['status'] == 'done' and not meeting) else 'CONFIRMED'}",
        f"DESCRIPTION:{escape(item['evidence'])}"]
    reminders = settings.get("calendar_reminders") or {"overrides": [{"method": "popup", "minutes": settings["reminder_minutes"]}]}
    if item['status'] != 'done' and not settings.get('calendar_cancelled'):
        for reminder in reminders.get("overrides", []):
            if reminder["method"] == "popup":
                lines += ['BEGIN:VALARM', 'ACTION:DISPLAY', 'DESCRIPTION:Напоминание CallMind',
                    f"TRIGGER:-PT{reminder['minutes']}M", 'END:VALARM']
    lines += ['END:VEVENT', 'END:VCALENDAR']
    # RFC 5545 folds by octets, without cutting a multibyte character.
    folded = []
    for line in lines:
        chunk = ""
        for char in line:
            if len((chunk + char).encode("utf-8")) > 73:
                folded.append(chunk)
                chunk = " "
            chunk += char
        folded.append(chunk)
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
