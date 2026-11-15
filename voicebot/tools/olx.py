"""olx tool: search OLX.ua listings and read one listing's details.

Uses the JSON feed the OLX website itself loads (/api/v1/offers/). It is not a documented API,
so it can change; robots.txt allows these paths (it blocks /search/ pages). Requests are few
and only made when the user asks.
"""

import datetime
import html
import logging
import re
from typing import Any

import aiohttp

from ..links import LINKS
from . import Tool
from .matching import key_words, matches
from .web_search import DailyLimit

log = logging.getLogger(__name__)

BASE = "https://www.olx.ua/api/v1/offers/"
TIMEOUT_SECONDS = 8
FETCH = 50
SHOW = 5
DAILY_REQUESTS = 100
DESCRIPTION_CHARS = 1200
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) voicebot", "Accept": "application/json"}

# OLX city ids (the feed has no lookup), with English, Ukrainian and Russian names.
CITY_IDS = {
    280: ("kharkiv", "kharkov", "харків", "харьков"),
    268: ("kyiv", "kiev", "київ", "киев"),
    176: ("lviv", "lvov", "львів", "львов"),
    62: ("odesa", "odessa", "одеса", "одесса"),
    121: ("dnipro", "dnepr", "дніпро", "днепр"),
    194: ("zaporizhzhia", "zaporozhye", "запоріжжя", "запорожье"),
    313: ("vinnytsia", "vinnitsa", "вінниця", "винница"),
    284: ("poltava", "полтава"),
}
MIN_FOR_TYPICAL_PRICE = 4
ACCESSORY_SHARE = 0.3
COMPUTER_CATEGORIES = {78, 80}  # desktop PCs, laptops
# Accessories sold "for" the item; ignored unless the user asks for one.
ACCESSORY_TITLE = re.compile(
    r"коробк|коробоч|упаковк|\bbox\b|чохол|чехол|\bcase\b|скло|стекл|плівк|пленк|кабель|переходник|перехідник|"
    r"зарядк|шолом|шлем|кронштейн|підставк|подставк|наклейк",
    re.IGNORECASE,
)
# Model variants that change the product: "RTX 5070" must not match "RTX 5070 Ti".
VARIANT_WORDS = {"ti", "super", "max", "plus", "mini"}

DESCRIPTION = (
    "Search OLX.ua (Ukrainian classifieds, mostly used items) and read listing details. "
    "search returns the cheapest matching listings with price, condition and city; "
    "details returns one listing's description, parameters and seller info."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "details"]},
        "query": {"type": "string", "description": "What to search for, e.g. 'RTX 5070' or 'дитячий велосипед'."},
        "city": {"type": "string", "description": "City name; empty = home city; 'all' = all of Ukraine."},
        "min_price": {"type": "integer", "description": "Lowest price in UAH."},
        "max_price": {"type": "integer", "description": "Highest price in UAH."},
        "condition": {"type": "string", "enum": ["any", "new", "used"]},
        "part_only": {"type": "boolean", "description": "True when the user wants a component (e.g. a graphics card), not a whole PC or laptop."},
        "id": {"type": "integer", "description": "Listing id from search results, for details."},
    },
    "required": ["action"],
}


def city_id(name: str) -> int | None:
    name = name.strip().lower()
    for cid, names in CITY_IDS.items():
        if any(name == n or name.startswith(n) for n in names):
            return cid
    return None


def clean_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def price_of(offer: dict) -> dict[str, Any] | None:
    for p in offer.get("params", []):
        if p.get("key") == "price":
            v = p.get("value") or {}
            amount, currency = v.get("value"), v.get("currency")
            if currency != "UAH" and v.get("converted_currency") == "UAH":
                amount, currency = v.get("converted_value"), "UAH"
            if amount and currency == "UAH":
                return {"uah": int(amount), "negotiable": bool(v.get("negotiable"))}
    return None


def condition_of(offer: dict) -> str:
    for p in offer.get("params", []):
        if p.get("key") == "state":
            return (p.get("value") or {}).get("key") or ""
    return ""


def is_variant_mismatch(title: str, query: str) -> bool:
    wanted = set(re.findall(r"\w+", query.lower()))
    have = set(re.findall(r"\w+", (title or "").lower()))
    return any((v in have) != (v in wanted) for v in VARIANT_WORDS)


def summarize(offer: dict) -> dict[str, Any]:
    price = price_of(offer) or {}
    location = offer.get("location") or {}
    return {
        "id": offer.get("id"),
        "title": offer.get("title"),
        "price_uah": price.get("uah"),
        "negotiable": price.get("negotiable"),
        "condition": condition_of(offer) or None,
        "city": (location.get("city") or {}).get("name"),
        "posted": (offer.get("created_time") or "")[:10],
        "shop": bool(offer.get("business")),
        "link": LINKS.add(offer.get("url"), offer.get("title")),
    }


def pick_offers(offers: list[dict], query: str, city: int | None, city_name: str,
                min_price: int | None, max_price: int | None, condition: str, part_only: bool = False) -> list[dict]:
    """Real sale listings matching the query, cheapest first."""
    words = key_words(query)
    wants_accessory = bool(ACCESSORY_TITLE.search(query))
    picked = []
    for o in offers:
        category = o.get("category") or {}
        if o.get("offer_type", "offer") != "offer" or category.get("type") == "services":
            continue
        if (not wants_accessory and ACCESSORY_TITLE.search(o.get("title") or "")) or (
            part_only and category.get("id") in COMPUTER_CATEGORIES
        ):
            continue
        if not matches(o.get("title"), words) or is_variant_mismatch(o.get("title"), query):
            continue
        price = price_of(o)
        if not price:
            continue
        if (min_price and price["uah"] < min_price) or (max_price and price["uah"] > max_price):
            continue
        if condition in ("new", "used") and condition_of(o) != condition:
            continue
        loc = (o.get("location") or {}).get("city") or {}
        if city is None and city_name and city_name.lower() not in (loc.get("name") or "").lower():
            continue
        picked.append(o)
    picked.sort(key=lambda o: price_of(o)["uah"])
    return drop_accessories(picked)


def drop_accessories(offers: list[dict]) -> list[dict]:
    """Listings far below the typical price are almost always accessories or parts of the item."""
    if len(offers) < MIN_FOR_TYPICAL_PRICE:
        return offers
    prices = sorted(price_of(o)["uah"] for o in offers)
    typical = prices[len(prices) // 2]
    return [o for o in offers if price_of(o)["uah"] >= typical * ACCESSORY_SHARE]


def make_tool(home_city: str = "", limiter: DailyLimit | None = None) -> Tool:
    limiter = limiter or DailyLimit(DAILY_REQUESTS)

    async def get(http: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict:
        if not limiter.take():
            raise RuntimeError("daily OLX request limit reached")
        async with http.get(url, params=params, headers=HEADERS) as resp:
            if resp.status == 404:
                return {}
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.json()

    async def search(http, query: str, city: str, min_price, max_price, condition: str,
                     part_only: bool) -> dict[str, Any]:
        city = (city or home_city).strip()
        everywhere = city.lower() in ("all", "ukraine", "україна", "украина", "")
        cid = None if everywhere else city_id(city)
        # The most relevant listings, sorted by price here: sorting by price on OLX's side brings up
        # cables, boxes and other accessories first.
        params: dict[str, Any] = {"offset": 0, "limit": FETCH, "query": query,
                                  "filter_float_price:from": min_price or 100}
        if max_price:
            params["filter_float_price:to"] = max_price
        if cid:
            params["city_id"] = cid
        data = await get(http, BASE, params)
        offers = pick_offers(data.get("data", []), query, cid, "" if everywhere or cid else city,
                             min_price, max_price, condition, part_only)
        log.info("olx %r in %s: %d listings, %d match; cheapest %s", query, city or "Ukraine",
                 (data.get("metadata") or {}).get("total_elements", 0), len(offers),
                 ", ".join(f"{price_of(o)['uah']} {o.get('title', '')[:40]}" for o in offers[:3]) or "-")
        result: dict[str, Any] = {"city": "all of Ukraine" if everywhere else city,
                                  "matching_listings": len(offers),
                                  "cheapest": [summarize(o) for o in offers[:SHOW]]}
        if offers:
            prices = [price_of(o)["uah"] for o in offers]
            result["price_range_uah"] = [min(prices), max(prices)]
            result["note"] = ("Listings are from private sellers and shops; prices are what sellers ask. "
                              "Use details with an id to check a listing.")
        else:
            result["note"] = "No matching listings with a price. Say so; offer to search all of Ukraine."
        return result

    async def details(http, offer_id: int) -> dict[str, Any]:
        data = (await get(http, f"{BASE}{int(offer_id)}/")).get("data")
        if not data:
            return {"error": "That listing no longer exists."}
        user = data.get("user") or {}
        location = data.get("location") or {}
        params = {p.get("name"): (p.get("value") or {}).get("label")
                  for p in data.get("params", []) if p.get("key") != "price" and (p.get("value") or {}).get("label")}
        created = (user.get("created") or "")[:4]
        log.info("olx details %s: %s", offer_id, data.get("title"))
        return {
            **summarize(data),
            "status": data.get("status"),
            "refreshed": (data.get("last_refresh_time") or "")[:10],
            "district": (location.get("district") or {}).get("name"),
            "description": clean_text(data.get("description"))[:DESCRIPTION_CHARS],
            "parameters": params,
            "seller": {"name": user.get("name"), "on_olx_since": created or None,
                       "last_seen": (user.get("last_seen") or "")[:10] or None},
            "today": datetime.date.today().isoformat(),
        }

    async def olx(action: str, query: str = "", city: str = "", min_price: int | None = None,
                  max_price: int | None = None, condition: str = "any", id: int | None = None,
                  part_only: bool = False) -> dict[str, Any]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
                if action == "details":
                    if not id:
                        return {"error": "id is required for details"}
                    return await details(http, id)
                if not query:
                    return {"error": "query is required for search"}
                return await search(http, query, city, min_price, max_price, condition, bool(part_only))
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("olx: %s", e)
            return {"error": "OLX did not answer. Tell the user."}

    return Tool(name="olx", description=DESCRIPTION, parameters=PARAMETERS, handler=olx)
