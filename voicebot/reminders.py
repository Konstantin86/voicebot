"""Timers, reminders and alarms: kept in a file on the Pi, checked by a loop in the app, spoken when due.

The spoken sentence is made once, when the reminder is set (Azure voice, ~$0.0005), and saved next to the
list, so a reminder still speaks if the internet is down when it is due.
"""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

KINDS = ("timer", "reminder", "alarm")
REPEATS = ("", "daily", "weekdays", "weekends")
LATE_LIMIT = timedelta(minutes=30)  # a one-off missed by more than this (Pi was off) is dropped, not spoken
FILE_MODE = 0o640


@dataclass
class Reminder:
    id: int
    kind: str
    due: float  # epoch seconds
    text: str  # the sentence to say, in the user's language
    label: str = ""
    person: str | None = None
    repeat: str = ""


def _fits(day: datetime, repeat: str) -> bool:
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekends":
        return day.weekday() >= 5
    return True


def next_time(due: datetime, repeat: str) -> datetime:
    """The next day (same time) that fits the repeat rule."""
    day = due + timedelta(days=1)
    while not _fits(day, repeat):
        day += timedelta(days=1)
    return day


def parse_due(at: str = "", in_minutes: float | None = None, repeat: str = "",
              now: datetime | None = None) -> datetime:
    """'in_minutes' from now, or 'at' as 'HH:MM' (next such time) or 'YYYY-MM-DD HH:MM' (local time)."""
    now = now or datetime.now()
    if in_minutes is not None and not at:
        if in_minutes <= 0:
            raise ValueError("in_minutes must be more than 0")
        return now + timedelta(minutes=float(in_minutes))
    at = (at or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            due = datetime.strptime(at, fmt)
            break
        except ValueError:
            continue
    else:
        try:
            clock = datetime.strptime(at, "%H:%M")
        except ValueError:
            raise ValueError("give 'at' as HH:MM or YYYY-MM-DD HH:MM, or give in_minutes") from None
        due = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
    if due <= now:
        raise ValueError(f"{due:%Y-%m-%d %H:%M} is in the past")
    while repeat and not _fits(due, repeat):
        due += timedelta(days=1)
    return due


def describe(due: float, now: datetime | None = None) -> str:
    now = now or datetime.now()
    when = datetime.fromtimestamp(due)
    left = when - now
    hours, rest = divmod(max(0, round(left.total_seconds())), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        span = f"{hours} h {minutes} min"
    elif minutes:
        span = f"{minutes} min {seconds} s" if minutes < 5 and seconds else f"{minutes} min"
    else:
        span = f"{seconds} s"
    day = "today" if when.date() == now.date() else "tomorrow" if when.date() == (now + timedelta(days=1)).date() \
        else when.strftime("%A %d %B")
    return f"{day} at {when:%H:%M} (in {span})"


class Reminders:
    def __init__(self, path: str):
        self.path = path
        self.audio_dir = os.path.join(os.path.dirname(path), "reminders")
        self.items: list[Reminder] = self._load()

    def _load(self) -> list[Reminder]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return [Reminder(**r) for r in json.load(f)]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as e:
            log.warning("reminders: cannot read %s: %s", self.path, e)
            return []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self.items], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, kind: str, due: datetime, text: str, label: str = "", person: str | None = None,
            repeat: str = "") -> Reminder:
        item = Reminder(id=max((r.id for r in self.items), default=0) + 1, kind=kind, due=due.timestamp(),
                        text=text.strip(), label=label.strip(), person=person or None, repeat=repeat)
        self.items.append(item)
        self._save()
        log.info("reminders: set %s #%d %s: %s", kind, item.id, describe(item.due), item.text)
        return item

    def cancel(self, item_id: int) -> Reminder | None:
        for item in self.items:
            if item.id == item_id:
                self.items.remove(item)
                self._save()
                self._drop_audio(item)
                log.info("reminders: cancelled %s #%d", item.kind, item.id)
                return item
        return None

    def upcoming(self) -> list[Reminder]:
        return sorted(self.items, key=lambda r: r.due)

    def due_now(self, now: float) -> list[Reminder]:
        return [r for r in self.upcoming() if r.due <= now]

    def finished(self, item: Reminder) -> None:
        """After it went off: a repeating one moves to its next day, a one-off is removed."""
        if item.repeat:
            nxt = next_time(datetime.fromtimestamp(item.due), item.repeat)
            while nxt <= datetime.now():
                nxt = next_time(nxt, item.repeat)
            item.due = nxt.timestamp()
            log.info("reminders: %s #%d next %s", item.kind, item.id, describe(item.due))
        elif item in self.items:
            self.items.remove(item)
            self._drop_audio(item)
        self._save()

    def is_stale(self, item: Reminder, now: float) -> bool:
        return now - item.due > LATE_LIMIT.total_seconds()

    def audio_path(self, item: Reminder) -> str:
        return os.path.join(self.audio_dir, f"{item.id}.pcm")

    def save_audio(self, item: Reminder, pcm: bytes) -> None:
        os.makedirs(self.audio_dir, mode=0o750, exist_ok=True)
        fd = os.open(self.audio_path(item), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(pcm)

    def audio(self, item: Reminder) -> bytes | None:
        try:
            with open(self.audio_path(item), "rb") as f:
                return f.read()
        except OSError:
            return None

    def _drop_audio(self, item: Reminder) -> None:
        try:
            os.remove(self.audio_path(item))
        except OSError:
            pass


def make_tool(reminders: Reminders, make_audio: Callable[[Reminder], Awaitable[None]] | None = None):
    """Reminders are shared by the household: no names."""
    from .tools import Tool

    background: set[asyncio.Task] = set()

    async def handle(action: str, kind: str = "reminder", text: str = "", label: str = "", at: str = "",
                     in_minutes: float | None = None, repeat: str = "", id: int | None = None) -> dict:
        if action == "list":
            items = reminders.upcoming()
            if not items:
                return {"items": [], "note": "Nothing is set."}
            return {"items": [{"id": r.id, "kind": r.kind, "when": describe(r.due), "says": r.text,
                               "label": r.label, "repeat": r.repeat or "once"} for r in items]}
        if action == "cancel":
            if id is None:
                return {"error": "Give the id (call list first if needed)."}
            item = reminders.cancel(int(id))
            return {"status": "cancelled", "kind": item.kind, "was": item.text} if item else \
                {"error": f"Nothing with id {id}."}
        if action != "add":
            return {"error": "action must be add, list or cancel"}
        if kind not in KINDS:
            return {"error": f"kind must be one of {', '.join(KINDS)}"}
        if repeat not in REPEATS:
            return {"error": "repeat must be daily, weekdays, weekends or empty"}
        if not text.strip():
            return {"error": "text (the sentence to say when it is due) is needed"}
        try:
            due = parse_due(at, in_minutes, repeat)
        except ValueError as e:
            return {"error": str(e)}
        item = reminders.add(kind, due, text, label, None, repeat)
        if make_audio:
            task = asyncio.create_task(make_audio(item))
            background.add(task)
            task.add_done_callback(background.discard)
        return {"status": "set", "id": item.id, "kind": kind, "when": describe(item.due),
                "repeat": repeat or "once",
                "note": "Confirm in a few words with the time (or how long), in the user's language."}

    return Tool(
        name="reminders",
        description=(
            "Timers, reminders and alarms said aloud at home when due. "
            "add: a timer ('timer for 10 minutes' -> in_minutes 10), a reminder ('remind me at 6 to call mom'), "
            "or an alarm that rings until stopped ('wake me up at 7'). list: what is set. cancel: by id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "cancel"]},
                "kind": {"type": "string", "enum": list(KINDS)},
                "text": {"type": "string", "description": "For add: the short sentence to say when it is due, "
                         "in the user's language, without anyone's name, e.g. 'Пора позвонить маме.' "
                         "or 'Таймер на 10 минут: макароны готовы.' or 'Доброе утро, пора вставать.'"},
                "label": {"type": "string", "description": "For add: a few words for the list, e.g. 'pasta'."},
                "at": {"type": "string", "description": "For add: local time 'HH:MM' (the next such time) "
                       "or 'YYYY-MM-DD HH:MM' for another day."},
                "in_minutes": {"type": "number", "description": "For add: minutes from now (timers, 'in an hour')."},
                "repeat": {"type": "string", "enum": list(REPEATS), "description": "For add: '' for once."},
                "id": {"type": "integer", "description": "For cancel: the id from add or list."},
            },
            "required": ["action"],
        },
        handler=handle,
    )
