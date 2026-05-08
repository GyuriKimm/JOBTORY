from datetime import date
from zoneinfo import ZoneInfo

from django.utils import timezone


def _to_kst_iso(dt):
    if not dt:
        return None
    tz = ZoneInfo("Asia/Seoul")
    try:
        if timezone.is_naive(dt):
            aware = timezone.make_aware(dt, timezone=ZoneInfo("UTC"))
        else:
            aware = dt
        aware = aware.astimezone(tz)
    except Exception:
        return dt.isoformat()
    return aware.isoformat()


def _format_birthdate(value):
    """birthdate가 문자열이든 date 객체든 안전하게 변환"""
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, date):
        return value.isoformat()
    return None
