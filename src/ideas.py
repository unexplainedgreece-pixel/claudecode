"""
Content ideas: upcoming Greek public holidays, hospitality-relevant international
days, and AI topic suggestions in the brand's voice.

The dates below are fixed by law or by the Orthodox calendar for 2026 and 2027.
Movable feasts (Clean Monday, Easter, Whit Monday, Carnival) are computed from
Orthodox Easter so later years keep working.
"""

import json
from datetime import date, timedelta

from src.ai import providers
from src.security.secrets import ConfigError


def orthodox_easter(year: int) -> date:
    """Meeus Julian algorithm, converted to the Gregorian calendar (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def holidays(year: int) -> list[dict]:
    easter = orthodox_easter(year)
    fixed = [
        (1, 1, "New Year's Day", "holiday"), (1, 6, "Epiphany (Theophania)", "holiday"),
        (3, 25, "Greek Independence Day", "holiday"), (5, 1, "Labour Day", "holiday"),
        (8, 15, "Assumption of Mary (Dekapentavgoustos)", "holiday"), (10, 28, "Ochi Day", "holiday"),
        (12, 24, "Christmas Eve", "season"), (12, 25, "Christmas Day", "holiday"),
        (12, 26, "Boxing Day (Synaxis of the Theotokos)", "holiday"), (12, 31, "New Year's Eve", "season"),
        (2, 14, "Valentine's Day", "international"), (3, 8, "International Women's Day", "international"),
        (4, 22, "Earth Day", "international"), (6, 21, "Summer solstice, longest day", "season"),
        (8, 19, "World Photography Day", "international"), (9, 27, "World Tourism Day", "international"),
        (10, 1, "International Coffee Day", "international"), (10, 16, "World Food Day", "international"),
        (10, 31, "Halloween", "international"), (11, 1, "World Vegan Day", "international"),
    ]
    out = [{"date": date(year, m, d), "name": n, "kind": k} for m, d, n, k in fixed]
    out += [
        {"date": easter - timedelta(days=48), "name": "Clean Monday (Kathara Deftera), kites and Lenten food", "kind": "holiday"},
        {"date": easter - timedelta(days=59), "name": "Tsiknopempti, grilled-meat Thursday of Carnival", "kind": "season"},
        {"date": easter - timedelta(days=49), "name": "Carnival Sunday (Apokries)", "kind": "season"},
        {"date": easter - timedelta(days=2), "name": "Orthodox Good Friday", "kind": "holiday"},
        {"date": easter, "name": "Orthodox Easter Sunday, lamb on the spit", "kind": "holiday"},
        {"date": easter + timedelta(days=1), "name": "Easter Monday", "kind": "holiday"},
        {"date": easter + timedelta(days=50), "name": "Whit Monday (Agiou Pnevmatos), long weekend", "kind": "holiday"},
        {"date": nth_weekday(year, 5, 6, 2), "name": "Mother's Day (Greece)", "kind": "international"},
        {"date": nth_weekday(year, 6, 6, 3), "name": "Father's Day", "kind": "international"},
        {"date": nth_weekday(year, 11, 3, 4) + timedelta(days=1), "name": "Black Friday", "kind": "international"},
    ]
    return sorted(out, key=lambda h: h["date"])


def upcoming(days: int = 45, today: date | None = None) -> list[dict]:
    today = today or date.today()
    found = []
    for year in (today.year, today.year + 1):
        for h in holidays(year):
            delta = (h["date"] - today).days
            if 0 <= delta <= days:
                found.append(dict(h, in_days=delta))
    return sorted(found, key=lambda h: h["date"])


def suggest_topics(brand: dict, user_id: int | None, count: int = 6, today: date | None = None) -> list[dict]:
    """AI post ideas for the coming two weeks, aware of what is coming up on the calendar."""
    today = today or date.today()
    events = ", ".join(f"{h['name']} on {h['date'].strftime('%d %b')}" for h in upcoming(21, today)) or "nothing special"
    name = brand.get("brand_name") or brand.get("display_name") or "the business"
    brief = (brand.get("brand_brief") or "").strip() or "A local business."
    pillars = (brand.get("content_pillars") or "").strip()
    language = brand.get("brand_language") or "the language of the brief, otherwise English"
    prompt = f"""You plan social media for {name}.
About the business and how it should sound: {brief}
{('Content pillars: ' + pillars) if pillars else ''}
Today is {today.strftime('%A %d %B %Y')}. Coming up: {events}.

Suggest {count} post ideas for the next two weeks. Mix practical (offers, hours, dishes, rooms),
seasonal (the dates above when relevant) and human (staff, guests, behind the scenes).
Answer ONLY with JSON: {{"ideas": [{{"topic": "short title in {language}", "why": "one sentence, in {language}",
"best_day": "YYYY-MM-DD", "format": "image or video"}}]}}"""
    raw = providers.text(prompt, user_id, json_mode=True)
    start, end = raw.find("{"), raw.rfind("}")
    try:
        ideas = json.loads(raw[start:end + 1]).get("ideas") or []
    except (ValueError, AttributeError):
        return []
    clean = []
    for i in ideas[:count]:
        if not isinstance(i, dict) or not i.get("topic"):
            continue
        clean.append({"topic": str(i["topic"])[:120], "why": str(i.get("why", ""))[:300],
                      "best_day": str(i.get("best_day", ""))[:10],
                      "format": "video" if str(i.get("format", "")).lower().startswith("v") else "image"})
    return clean
