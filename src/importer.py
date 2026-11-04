"""
Bulk import of posts from a CSV file (plan a month in a spreadsheet, import it).

Columns (header row required, order free, extra columns ignored):
    topic, caption, hashtags, scheduled_time, platforms, media_type, image_url, video_url
scheduled_time: "2026-11-20 09:30" (optional). platforms: "facebook,instagram" (optional).
Rows with a scheduled_time are created as 'scheduled' (already approved); the
rest are drafts for the approval queue.
"""

import csv
import io

from src.web import queries

COLUMNS = ("topic", "caption", "hashtags", "scheduled_time", "platforms", "media_type", "image_url", "video_url")


def import_csv(text: str, user_id: int, page_id: int | None = None, approve_scheduled: bool = True) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {"created": 0, "errors": ["The file is empty."]}
    fields = {f.strip().lower(): f for f in reader.fieldnames}
    if "topic" not in fields and "caption" not in fields:
        return {"created": 0, "errors": ["Need at least a 'topic' or 'caption' column."]}
    created, errors = [], []
    for n, row in enumerate(reader, start=2):
        get = lambda k: (row.get(fields.get(k, ""), "") or "").strip()   # noqa: E731
        topic, caption = get("topic"), get("caption")
        if not topic and not caption:
            continue
        media_type = get("media_type").lower() or "image"
        if media_type not in ("image", "video"):
            errors.append(f"Row {n}: media_type must be image or video.")
            continue
        try:
            pid = queries.create_draft(topic or caption[:60], caption, get("hashtags"), media_type=media_type,
                                       user_id=user_id, platforms=get("platforms") or None, page_id=page_id)
            extra = {}
            if get("image_url"):
                extra["image_url"] = get("image_url")
            if get("video_url"):
                extra["video_url"] = get("video_url")
            if extra:
                queries.update_post(pid, **extra)
            when = get("scheduled_time")
            if when:
                if approve_scheduled:
                    queries.set_status(pid, "approved", when, platforms=get("platforms") or None)
                else:
                    queries.update_post(pid, scheduled_time=when)
            created.append(pid)
        except ValueError as exc:
            errors.append(f"Row {n}: {exc}")
    return {"created": len(created), "ids": created, "errors": errors}


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerow(["Sunset drinks on the harbour", "Golden hour, cold drinks, no rush.", "#rhodes #sunset",
                "2026-11-20 18:30", "facebook,instagram", "image", "", ""])
    w.writerow(["Weekend brunch", "Brunch every Saturday and Sunday, 10 to 2.", "#brunch #rhodes", "", "", "image", "", ""])
    return buf.getvalue()
