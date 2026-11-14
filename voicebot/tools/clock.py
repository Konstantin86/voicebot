"""World clock: the time and date anywhere, from the Pi's own clock and time zone data (no internet)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import Tool


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 2)


def world_time(timezone: str, place: str = "", now: datetime | None = None) -> dict:
    try:
        zone = ZoneInfo(timezone.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"Unknown time zone {timezone!r}. Use an IANA name such as 'Pacific/Auckland'."}
    here = (now or datetime.now()).astimezone()
    there = here.astimezone(zone)
    ahead = _hours(there.utcoffset() - here.utcoffset())
    return {
        "place": place or timezone,
        "time_there": there.strftime("%H:%M"),
        "date_there": there.strftime("%A, %d %B %Y"),
        "time_here": here.strftime("%H:%M, %A %d %B"),
        "hours_ahead_of_here": ahead,
        "note": "Say the time there, and the day if it differs from here.",
    }


def make_tool() -> Tool:
    return Tool(
        name="world_time",
        description=(
            "The current time and date in another city or country, and how many hours it is ahead of or behind "
            "home. Use it for any question about the time somewhere else; never web search for this."
        ),
        parameters={
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA time zone of the place, e.g. 'Pacific/Auckland' "
                             "for New Zealand, 'America/New_York', 'Asia/Tokyo'. For a country with several zones, "
                             "use its capital or largest city unless a city was named."},
                "place": {"type": "string", "description": "The place as the user named it."},
            },
            "required": ["timezone"],
        },
        handler=world_time,
    )
