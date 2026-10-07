from datetime import datetime, time
from zoneinfo import ZoneInfo


def local_now(tz="Europe/Minsk"):
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def normalize_datetime(value, tz="Europe/Minsk"):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Некорректная дата. Используйте ISO-формат.") from error
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return value


def is_overdue(item, now):
    if item["status"] != "pending" or not item["deadline"]:
        return False
    if item.get("date_only", False):
        return item["deadline"].date() < now.date()
    return item["deadline"] < now


def deadline_with_zone(item, tz):
    value = item["deadline"]
    if value and item.get("date_only"):
        value = datetime.combine(value.date(), time(23, 59))
    return value.replace(tzinfo=ZoneInfo(tz)) if value else None
