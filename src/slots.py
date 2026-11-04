"""
Posting queue with time slots, like Buffer's queue.

A user defines slots such as "Mon,Tue,Wed,Thu,Fri 09:00,18:00; Sat,Sun 11:00".
"Approve to next slot" gives a post the next free slot that no other
approved/scheduled post of this user already occupies.

Defaults (when the user has not set any) lean on what the scheduling tools
publish as decent local-business times: weekdays 09:00 and 18:30,
weekends 11:00.
"""

from datetime import datetime, timedelta

from config.db import get_connection

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SLOTS = "Mon,Tue,Wed,Thu,Fri 09:00,18:30; Sat,Sun 11:00"


class SlotsError(ValueError):
    pass


def parse_slots(text: str | None) -> list[tuple[int, int, int]]:
    """'Mon,Tue 09:00,18:00; Sat 11:00' -> [(weekday, hour, minute), ...] sorted."""
    text = (text or "").strip() or DEFAULT_SLOTS
    out = set()
    for group in text.split(";"):
        group = group.strip()
        if not group:
            continue
        parts = group.split()
        if len(parts) != 2:
            raise SlotsError(f"Cannot read '{group}'. Use the form: Mon,Tue 09:00,18:00")
        days, times = parts
        for day in days.split(","):
            key = day.strip().lower()[:3]
            if key not in DAYS:
                raise SlotsError(f"Unknown day '{day}'. Use Mon, Tue, Wed, Thu, Fri, Sat, Sun.")
            for t in times.split(","):
                try:
                    hour, minute = (int(x) for x in t.strip().split(":"))
                    if not (0 <= hour < 24 and 0 <= minute < 60):
                        raise ValueError
                except ValueError:
                    raise SlotsError(f"Unknown time '{t}'. Use HH:MM, for example 18:30.")
                out.add((DAYS.index(key), hour, minute))
    if not out:
        raise SlotsError("No slots given.")
    return sorted(out)


def upcoming(slots: list[tuple[int, int, int]], start: datetime, count: int = 50) -> list[datetime]:
    """The next `count` slot datetimes strictly after `start`."""
    found = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(60):
        for weekday, hour, minute in slots:
            if day.weekday() != weekday:
                continue
            when = day.replace(hour=hour, minute=minute)
            if when > start:
                found.append(when)
        if len(found) >= count:
            break
        day += timedelta(days=1)
    return sorted(found)[:count]


def taken_times(user_id: int) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT scheduled_time FROM posts WHERE user_id = ? AND status IN ('scheduled', 'approved') "
            "AND scheduled_time IS NOT NULL", (user_id,)).fetchall()
    return {r["scheduled_time"][:16] for r in rows}


def next_free_slot(user_id: int, slots_text: str | None, now: datetime | None = None) -> str:
    """The next slot with no post in it, as 'YYYY-MM-DD HH:MM'."""
    now = now or datetime.now()
    busy = taken_times(user_id)
    for when in upcoming(parse_slots(slots_text), now, 200):
        key = when.strftime("%Y-%m-%d %H:%M")
        if key not in busy:
            return key
    raise SlotsError("No free slot in the next two months. Add more slots.")


def preview(slots_text: str | None, n: int = 5, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    return [w.strftime("%a %d %b %H:%M") for w in upcoming(parse_slots(slots_text), now, n)]
