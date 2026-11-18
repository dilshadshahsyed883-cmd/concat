"""Schedule public YouTube releases without deferring the manual upload queue."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def concat_publish_at(local_value: str, timezone_name: str, visibility: str, *, now=None) -> str:
    if not str(local_value or "").strip():
        return ""
    if str(visibility).strip().lower() != "public":
        raise ValueError("A scheduled YouTube release must use Public visibility")
    try:
        zone = ZoneInfo(str(timezone_name or "Asia/Karachi"))
        local = datetime.fromisoformat(str(local_value).strip().replace("Z", "+00:00"))
        if local.tzinfo is None:
            local = local.replace(tzinfo=zone)
        # Reject nonexistent DST wall times (silently normalizing changes the
        # user's chosen date) and ambiguous local times without an offset.
        if local.utcoffset() is None or (local.tzinfo is zone and
                local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None)):
            raise ValueError("Nonexistent local time")
        if local.tzinfo is zone and zone.utcoffset(local.replace(fold=0)) != zone.utcoffset(local.replace(fold=1)):
            raise ValueError("Ambiguous local time: choose an unambiguous clock time")
        utc = local.astimezone(timezone.utc)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Invalid time or timezone for scheduled YouTube release") from exc
    clock = now or datetime.now(timezone.utc)
    if utc <= clock.astimezone(timezone.utc) + timedelta(minutes=10):
        raise ValueError("Scheduled YouTube release must be more than 10 minutes in the future")
    return utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
