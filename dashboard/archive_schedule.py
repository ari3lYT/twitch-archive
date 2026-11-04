"""One daily download window, shared by guards, the dashboard and ETA calculations."""
import datetime as dt
import os
from zoneinfo import ZoneInfo


def configuration():
    zone = ZoneInfo(os.environ.get('ARCHIVE_TIMEZONE', 'Europe/Moscow'))
    start = dt.time.fromisoformat(os.environ.get('DOWNLOAD_START', '06:30'))
    end = dt.time.fromisoformat(os.environ.get('DOWNLOAD_END', '23:30'))
    if not start < end:
        raise ValueError('DOWNLOAD_START must precede DOWNLOAD_END within one day')
    return zone, start, end


def bounds(now=None):
    zone, start, end = configuration()
    now = now or dt.datetime.now(zone)
    now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    return now, dt.datetime.combine(now.date(), start, zone), dt.datetime.combine(now.date(), end, zone)


def allowed(now=None):
    now, start, end = bounds(now)
    return start <= now < end


def snapshot():
    now, start, end = bounds()
    active = start <= now < end
    boundary = end if active else (start if now < start else start + dt.timedelta(days=1))
    return {'allowed': active, 'now': now.isoformat(), 'next_change': boundary.isoformat(),
            'download_start': start.strftime('%H:%M'), 'download_end': end.strftime('%H:%M'),
            'nas_start': os.environ.get('NAS_START', '06:00'),
            'nas_end': os.environ.get('NAS_END', '00:00'),
            'timezone': str(now.tzinfo)}


def finish_time(now, remaining):
    zone, _, _ = configuration()
    current = dt.datetime.fromtimestamp(now, zone)
    remaining = max(0, float(remaining))
    for _ in range(370):
        current, start, end = bounds(current)
        if current < start:
            current = start
        elif current >= end:
            current = start + dt.timedelta(days=1)
            continue
        usable = (end - current).total_seconds()
        if remaining <= usable:
            return (current + dt.timedelta(seconds=remaining)).timestamp()
        remaining -= usable
        current = start + dt.timedelta(days=1)
    return None
