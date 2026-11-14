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
from .matching import key_words, matches, prices_uah

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
    query = re.sub(r"\b[\w-]+\.(?:ua|com|net|org)\b", " ", query.lower())
    words = re.findall(r"\w+", query)
    return frozenset(w for w in words if w not in IGNORED_WORDS and not w.isdigit())


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= SIMILARITY


# Ukrainian shops and price comparison sites, preferred (not required) for shopping searches.
SHOP_SITES = [
    "hotline.ua", "ek.ua", "rozetka.com.ua", "comfy.ua", "foxtrot.com.ua", "allo.ua",
    "telemart.ua", "brain.com.ua", "moyo.ua", "citrus.com.ua", "prom.ua", "elmir.ua",
]


def clean_site(site: str | None) -> str:
    """'https://www.Elmir.ua/x' -> 'elmir.ua'; '' when not a plausible domain."""
    site = (site or "").strip().lower()
    if "//" in site:
        site = urlparse(site).netloc
    site = site.split("/")[0].removeprefix("www.")
    return site if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", site) else ""


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
        "site": {
            "type": "string",
            "description": "Search only this website, e.g. 'elmir.ua', when the user names a site.",
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


def cheapest_offers(query: str, results: list[dict], limit: int = 5) -> list[dict[str, Any]]:
    """Lowest UAH price per result whose title matches the query's key words, cheapest first."""
    words = key_words(query)
    offers = []
    for r in results:
        if not matches(r.get("title"), words):
            continue
        prices = prices_uah(r.get("content") or "") + prices_uah(r.get("title") or "")
        if prices:
            offers.append({"site": site_name(r.get("url")), "title": r.get("title"), "price_uah": min(prices)})
    offers.sort(key=lambda o: o["price_uah"])
    return offers[:limit]


def make_tool(
    api_key: str, daily_limit: int, depth: str = "basic", country: str = "", limiter: DailyLimit | None = None
) -> Tool:
    limiter = limiter or DailyLimit(daily_limit)
    recent: list[tuple[float, tuple[str, bool, str], frozenset[str], asyncio.Task]] = []

    async def web_search(
        query: str, topic: str = "general", shopping: bool = False, site: str = ""
    ) -> dict[str, Any]:
        """Reuses a running or recent search for a near-identical query (the model sometimes asks twice)."""
        topic = topic if topic in ("general", "news") else "general"
        site = clean_site(site)
        kind = (topic, bool(shopping), site)
        now = time.monotonic()
        recent[:] = [r for r in recent if now - r[0] < REUSE_SECONDS]
        words = query_words(query)
        for _, old_kind, old_words, task in recent:
            if old_kind == kind and similar(words, old_words):
                log.info("web_search %r: reusing a recent search", query)
                return await asyncio.shield(task)
        task = asyncio.ensure_future(search(query, topic, bool(shopping), site))
        recent.append((now, kind, words, task))
        return await asyncio.shield(task)

    async def search(query: str, topic: str, shopping: bool, site: str) -> dict[str, Any]:
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
        if site:
            body["include_domains"] = [site]
            body["include_domains_mode"] = "restrict"
        elif shopping:
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
            "web_search %r (%s%s%s): %d results in %.1f s, %d searches today, sites: %s",
            query, topic, ", shopping" if shopping else "", f", site {site}" if site else "", len(results), time.monotonic() - started,
            limiter.count, ", ".join(filter(None, (site_name(r.get("url")) for r in results))),
        )
        output: dict[str, Any] = {
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
        if shopping:
            words = key_words(query)
            for r in results:
                log.info(
                    "  result %s | %s | match=%s | prices %s",
                    site_name(r.get("url")), (r.get("title") or "")[:70], matches(r.get("title"), words),
                    prices_uah((r.get("content") or "") + " " + (r.get("title") or "")) or "-",
                )
            offers = cheapest_offers(query, results)
            if not offers:
                output["note"] = (
                    "No result for this exact product had a readable price. Tell the user you could not "
                    "find a reliable price for this exact model. Do not quote prices from the snippets."
                )
            if offers:
                output["cheapest_offers"] = offers
                output["note"] = (
                    "cheapest_offers lists the lowest UAH price mentioned in each matching result, "
                    "cheapest first. Use it for the cheapest option."
                )
                log.info("web_search cheapest: %s", ", ".join(f"{o['site']} {o['price_uah']}" for o in offers))
        return output

    return Tool(name="web_search", description=DESCRIPTION, parameters=PARAMETERS, handler=web_search)


YOUTUBE_ID = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})")


def make_video_finder(api_key: str, limiter: DailyLimit):
    """Returns find_video(query) -> {"id", "title"} using one search limited to youtube.com."""

    async def find_video(query: str = "", channel: str = "", latest: bool = False) -> dict[str, Any]:
        query = " ".join(filter(None, [channel, query, "latest" if latest else ""]))
        if not limiter.take():
            return {"error": "Daily search limit reached."}
        body = {"query": query, "max_results": 5, "include_domains": ["youtube.com", "youtu.be"]}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                async with http.post(URL, json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                    if resp.status != 200:
                        log.warning("video search: HTTP %d", resp.status)
                        return {"error": "Video search failed."}
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("video search: %s", e)
            return {"error": "Video search did not respond."}
        for r in data.get("results", []):
            match = YOUTUBE_ID.search(r.get("url") or "")
            if match:
                log.info("video search %r: %s %s", query, match.group(1), r.get("title"))
                return {"id": match.group(1), "title": r.get("title")}
        log.info("video search %r: no video", query)
        return {}

    return find_video


def from_env() -> tuple[list[Tool], Any]:
    """web_search and price_lookup sharing one daily search limit, plus a YouTube video finder
    using the same limit. ([], None) without TAVILY_API_KEY."""
    from . import price_lookup

    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return [], None
    daily_limit = int(os.environ.get("SEARCH_DAILY_LIMIT", "50"))
    limiter = DailyLimit(daily_limit)
    search = make_tool(
        key,
        daily_limit=daily_limit,
        depth=os.environ.get("TAVILY_SEARCH_DEPTH", "basic"),
        country=os.environ.get("SEARCH_COUNTRY", "ukraine").strip(),
        limiter=limiter,
    )
    return [search, price_lookup.make_tool(key, limiter)], make_video_finder(key, limiter)
