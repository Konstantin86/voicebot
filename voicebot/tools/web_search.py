"""web_search tool using the Tavily API (1 credit per basic search)."""

import asyncio
import datetime
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp

from . import Tool

log = logging.getLogger(__name__)

URL = "https://api.tavily.com/search"
TIMEOUT_SECONDS = 8
MAX_SNIPPET_CHARS = 600
CHUNKS_PER_RESULT = 2
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


# Ukrainian shops and price comparison sites, preferred (not required) for shopping searches.
SHOP_SITES = [
    "hotline.ua", "ek.ua", "rozetka.com.ua", "comfy.ua", "foxtrot.com.ua", "allo.ua",
    "telemart.ua", "brain.com.ua", "moyo.ua", "citrus.com.ua", "prom.ua",
]


def site_name(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.") or None


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
        "shopping": {
            "type": "boolean",
            "description": "True for where-to-buy, shop, availability or product price questions.",
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


def make_tool(api_key: str, daily_limit: int, depth: str = "basic", country: str = "") -> Tool:
    limiter = DailyLimit(daily_limit)
    recent: list[tuple[float, tuple[str, bool], frozenset[str], asyncio.Task]] = []

    async def web_search(query: str, topic: str = "general", shopping: bool = False) -> dict[str, Any]:
        """Reuses a running or recent search for a near-identical query (the model sometimes asks twice)."""
        topic = topic if topic in ("general", "news") else "general"
        kind = (topic, bool(shopping))
        now = time.monotonic()
        recent[:] = [r for r in recent if now - r[0] < REUSE_SECONDS]
        words = query_words(query)
        for _, old_kind, old_words, task in recent:
            if old_kind == kind and similar(words, old_words):
                log.info("web_search %r: reusing a recent search", query)
                return await asyncio.shield(task)
        task = asyncio.ensure_future(search(query, topic, bool(shopping)))
        recent.append((now, kind, words, task))
        return await asyncio.shield(task)

    async def search(query: str, topic: str, shopping: bool) -> dict[str, Any]:
        if not limiter.take():
            log.warning("web_search: daily limit of %d reached", daily_limit)
            return {"error": "Daily search limit reached. Tell the user you cannot search any more today."}
        body: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "search_depth": depth,
            "max_results": 5,
            "chunks_per_source": CHUNKS_PER_RESULT,
            "include_answer": "basic",
        }
        if country and topic == "general":
            body["country"] = country
        if shopping:
            body["include_domains"] = SHOP_SITES
            body["include_domains_mode"] = "prefer"
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
        results = data.get("results", [])
        log.info(
            "web_search %r (%s%s): %d results in %.1f s, %d searches today, sites: %s",
            query, topic, ", shopping" if shopping else "", len(results), time.monotonic() - started,
            limiter.count, ", ".join(filter(None, (site_name(r.get("url")) for r in results))),
        )
        return {
            "answer": data.get("answer"),
            "results": [
                {
                    "site": site_name(r.get("url")),
                    "title": r.get("title"),
                    "date": r.get("published_date"),
                    "snippet": (r.get("content") or "")[:MAX_SNIPPET_CHARS],
                }
                for r in results
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
        country=os.environ.get("SEARCH_COUNTRY", "ukraine").strip(),
    )
