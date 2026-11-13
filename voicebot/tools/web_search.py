"""web_search tool using the Tavily API (1 credit per basic search)."""

import asyncio
import datetime
import logging
import os
import re
import time
from typing import Any

import aiohttp

from . import Tool

log = logging.getLogger(__name__)

URL = "https://api.tavily.com/search"
TIMEOUT_SECONDS = 8
MAX_SNIPPET_CHARS = 400
REUSE_SECONDS = 30
SIMILARITY = 0.6

# Words that do not change what is being searched for.
IGNORED_WORDS = {
    "a", "an", "the", "of", "on", "in", "at", "for", "to", "and", "is", "are", "what", "how", "much",
    "latest", "current", "recent", "today", "now", "news", "price", "prices", "cost", "costs",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def query_words(query: str) -> frozenset[str]:
    words = re.findall(r"\w+", query.lower())
    return frozenset(w for w in words if w not in IGNORED_WORDS and not w.isdigit())


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= SIMILARITY

DESCRIPTION = (
    "Search the web for current or factual information: news, recent events, results, prices, "
    "schedules, or anything you are not sure about. Returns a short answer and a few snippets."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query. English often finds the best results."},
        "topic": {
            "type": "string",
            "enum": ["general", "news"],
            "description": "Use news for recent events and headlines.",
        },
    },
    "required": ["query"],
}


class DailyLimit:
    def __init__(self, limit: int):
        self.limit = limit
        self.day = datetime.date.today()
        self.count = 0

    def take(self) -> bool:
        today = datetime.date.today()
        if today != self.day:
            self.day, self.count = today, 0
        if self.count >= self.limit:
            return False
        self.count += 1
        return True


def make_tool(api_key: str, daily_limit: int, depth: str = "basic") -> Tool:
    limiter = DailyLimit(daily_limit)
    recent: list[tuple[float, str, frozenset[str], asyncio.Task]] = []

    async def web_search(query: str, topic: str = "general") -> dict[str, Any]:
        """Reuses a running or recent search for a near-identical query (the model sometimes asks twice)."""
        now = time.monotonic()
        recent[:] = [r for r in recent if now - r[0] < REUSE_SECONDS]
        words = query_words(query)
        for _, old_topic, old_words, task in recent:
            if old_topic == topic and similar(words, old_words):
                log.info("web_search %r: reusing a recent search", query)
                return await asyncio.shield(task)
        task = asyncio.ensure_future(search(query, topic))
        recent.append((now, topic, words, task))
        return await asyncio.shield(task)

    async def search(query: str, topic: str) -> dict[str, Any]:
        if not limiter.take():
            log.warning("web_search: daily limit of %d reached", daily_limit)
            return {"error": "Daily search limit reached. Tell the user you cannot search any more today."}
        body = {
            "query": query,
            "topic": topic if topic in ("general", "news") else "general",
            "search_depth": depth,
            "max_results": 5,
            "chunks_per_source": 1,
            "include_answer": "basic",
        }
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                async with http.post(URL, json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                    if resp.status != 200:
                        text = (await resp.text())[:200]
                        log.warning("web_search: HTTP %d: %s", resp.status, text)
                        return {"error": f"Search failed (HTTP {resp.status})."}
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("web_search: %s", e)
            return {"error": "Search service did not respond."}
        log.info(
            "web_search %r (%s): %d results in %.1f s, %d searches today",
            query, topic, len(data.get("results", [])), time.monotonic() - started, limiter.count,
        )
        return {
            "answer": data.get("answer"),
            "results": [
                {
                    "title": r.get("title"),
                    "date": r.get("published_date"),
                    "snippet": (r.get("content") or "")[:MAX_SNIPPET_CHARS],
                }
                for r in data.get("results", [])
            ],
        }

    return Tool(name="web_search", description=DESCRIPTION, parameters=PARAMETERS, handler=web_search)


def from_env() -> Tool | None:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return None
    return make_tool(
        key,
        daily_limit=int(os.environ.get("SEARCH_DAILY_LIMIT", "50")),
        depth=os.environ.get("TAVILY_SEARCH_DEPTH", "basic"),
    )
