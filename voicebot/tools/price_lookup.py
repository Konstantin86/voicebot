"""price_lookup tool: product price ranges from Price Navigator (pn.com.ua).

Product pages are found with a Tavily search limited to pn.com.ua (the site's own /search/
is disallowed for bots in its robots.txt), then each product page's structured data is read.
"""

import asyncio
import html
import json
import logging
import re
import time
from typing import Any

import aiohttp

from . import Tool
from .matching import key_words, matches
from .web_search import URL as TAVILY_URL, DailyLimit

log = logging.getLogger(__name__)

SITE = "pn.com.ua"
PRODUCT_URL = re.compile(r"^https?://(?:www\.)?pn\.com\.ua/md/(\d+)/?")
MAX_PAGES = 3
TIMEOUT_SECONDS = 8
USER_AGENT = "Mozilla/5.0 (compatible; home-voicebot/1.0)"

DESCRIPTION = (
    "Look up current prices of a product in Ukrainian shops on Price Navigator (pn.com.ua), "
    "a price comparison site. Use for product price questions in Ukraine or when the user mentions "
    "Price Navigator. Returns price ranges and number of shops, including shops in Kharkiv."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "product": {
            "type": "string",
            "description": (
                "Brand and model as shops list it, in Latin letters, e.g. 'Apple MacBook Pro 14 M5 Pro', "
                "'RTX 5070', 'iPhone 16 128GB'. No descriptive words."
            ),
        },
    },
    "required": ["product"],
}


def product_page_urls(results: list[dict]) -> list[str]:
    """Unique product page URLs (https://pn.com.ua/md/<id>/), without query strings."""
    urls = []
    for r in results:
        m = PRODUCT_URL.match(r.get("url") or "")
        if m:
            url = f"https://pn.com.ua/md/{m.group(1)}/"
            if url not in urls:
                urls.append(url)
    return urls[:MAX_PAGES]


def parse_product_page(page: str) -> dict[str, Any] | None:
    product = None
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            product = data
            break
    if not product:
        return None
    offers = product.get("offers") or {}
    result: dict[str, Any] = {
        "name": product.get("name"),
        "price_from": offers.get("lowPrice"),
        "price_to": offers.get("highPrice"),
        "currency": offers.get("priceCurrency"),
        "shops": offers.get("offerCount"),
    }
    # "Продается в 20 магазинах (13 не из Харькова)": the site's default region is Kharkiv.
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))
    m = re.search(r"магазин\w*\s*\((\d+)\s+не\s+(?:из|з)\s+Хар\w+\)", text)
    if m and isinstance(result["shops"], int):
        result["shops_in_kharkiv"] = result["shops"] - int(m.group(1))
    return result


def make_tool(api_key: str, limiter: DailyLimit) -> Tool:
    async def price_lookup(product: str) -> dict[str, Any]:
        if not limiter.take():
            return {"error": "Daily search limit reached. Tell the user you cannot search any more today."}
        started = time.monotonic()
        body = {
            "query": product,
            "search_depth": "basic",
            "max_results": 8,
            "include_domains": [SITE],
            "include_domains_mode": "restrict",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                async with http.post(TAVILY_URL, json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                    if resp.status != 200:
                        log.warning("price_lookup: Tavily HTTP %d", resp.status)
                        return {"error": f"Search failed (HTTP {resp.status})."}
                    found = await resp.json()
                urls = product_page_urls(found.get("results", []))
                if not urls:
                    log.info("price_lookup %r: no product pages found", product)
                    return {"error": f"No product page for {product!r} found on Price Navigator."}
                pages = await asyncio.gather(*(fetch(http, u) for u in urls), return_exceptions=True)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("price_lookup: %s", e)
            return {"error": "Price Navigator did not respond."}
        words = key_words(product)
        products = []
        for url, page in zip(urls, pages):
            info = parse_product_page(page) if isinstance(page, str) else None
            if not info:
                log.info("price_lookup: could not read %s (%r)", url, page if not isinstance(page, str) else "no data")
            elif info["price_from"] is None:
                log.info("price_lookup: %s has no offers", info["name"])
            elif not matches(info["name"], words):
                log.info("price_lookup: %s does not match %s", info["name"], words)
            else:
                products.append(info)
        log.info(
            "price_lookup %r: %d of %d pages read in %.1f s: %s",
            product, len(products), len(urls), time.monotonic() - started,
            "; ".join(f"{p['name']} {p['price_from']}-{p['price_to']}" for p in products),
        )
        if not products:
            return {
                "error": f"No matching product with prices found on Price Navigator for {product!r}.",
                "next_step": "Call web_search with shopping=true for the same product now.",
            }
        products.sort(key=lambda p: p["price_from"] if isinstance(p["price_from"], (int, float)) else float("inf"))
        return {
            "source": "Price Navigator (pn.com.ua), Kharkiv region",
            "note": "Products sorted from cheapest. price_from is the cheapest offer.",
            "products": products,
        }

    async def fetch(http: aiohttp.ClientSession, url: str) -> str:
        async with http.get(url, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            return await resp.text()

    return Tool(name="price_lookup", description=DESCRIPTION, parameters=PARAMETERS, handler=price_lookup)
