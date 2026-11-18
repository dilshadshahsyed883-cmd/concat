"""Display-only helpers. Stored upload schedules remain UTC."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def schedule_pkt(value):
    if not value:
        return 'Next available'
    try:
        scheduled = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        return scheduled.astimezone(ZoneInfo('Asia/Karachi')).strftime('%d %b %Y · %H:%M')
    except (ValueError, TypeError):
        return 'Check schedule'
