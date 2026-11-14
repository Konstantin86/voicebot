"""Video search with the YouTube Data API v3 (free quota: 10,000 units a day; one search = 100 units)."""

import datetime
import html
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
TIMEOUT_SECONDS = 8
SEARCH_UNITS = 100
DAILY_UNITS = 9000  # stay under Google's 10,000


class Quota:
    def __init__(self, limit: int = DAILY_UNITS):
        self.limit = limit
        self.day = datetime.date.today()
        self.used = 0

    def take(self, units: int) -> bool:
        today = datetime.date.today()
        if today != self.day:
            self.day, self.used = today, 0
        if self.used + units > self.limit:
            return False
        self.used += units
        return True


def make_video_finder(api_key: str, region: str = "UA", quota: Quota | None = None):
    quota = quota or Quota()

    async def search(http: aiohttp.ClientSession, params: dict[str, Any]) -> list[dict]:
        if not quota.take(SEARCH_UNITS):
            raise RuntimeError("daily YouTube quota reached")
        params = {"part": "snippet", "maxResults": 5, "regionCode": region, "key": api_key, **params}
        async with http.get(SEARCH_URL, params=params) as resp:
            if resp.status != 200:
                text = (await resp.text())[:200].replace(api_key, "***")
                raise RuntimeError(f"HTTP {resp.status}: {text}")
            return (await resp.json()).get("items", [])

    async def find_video(query: str = "", channel: str = "", latest: bool = False) -> dict[str, Any]:
        """Finds one video. With channel, searches only that channel; latest sorts by upload date."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                params: dict[str, Any] = {"type": "video"}
                if channel:
                    found = await search(http, {"type": "channel", "q": channel, "maxResults": 1})
                    if not found:
                        return {"error": f"No YouTube channel called {channel!r}."}
                    params["channelId"] = found[0]["snippet"]["channelId"]
                    channel = found[0]["snippet"].get("channelTitle", channel)
                if query:
                    params["q"] = query
                if latest:
                    params["order"] = "date"
                items = await search(http, params)
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as e:
            log.warning("youtube search: %s", e)
            return {"error": "YouTube search failed."}
        for item in items:
            video_id = item.get("id", {}).get("videoId")
            if video_id:
                snippet = item.get("snippet", {})
                for key in ("title", "channelTitle"):
                    snippet[key] = html.unescape(snippet.get(key) or "")
                log.info(
                    "youtube search %r%s%s: %s %s (%s, %s), quota used today %d",
                    query, f" channel {channel}" if channel else "", " latest" if latest else "",
                    video_id, snippet.get("title"), snippet.get("channelTitle"),
                    (snippet.get("publishedAt") or "")[:10], quota.used,
                )
                return {
                    "id": video_id,
                    "title": snippet.get("title"),
                    "channel": snippet.get("channelTitle"),
                    "published": (snippet.get("publishedAt") or "")[:10],
                }
        log.info("youtube search %r: no video", query)
        return {}

    return find_video
