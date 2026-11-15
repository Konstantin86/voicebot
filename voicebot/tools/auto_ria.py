"""auto_ria tool: used cars on AUTO.RIA (official API, developers.ria.com, needs AUTO_RIA_API_KEY).

One question costs about 5 API requests: makes and models are looked up once and kept, then one
search and up to SHOW listing details. Searches and listings are kept for 30 minutes, so "more options"
and repeated questions cost little. After an hourly-limit answer (HTTP 429) the tool waits 15 minutes. The average-price API is not used: on the free
plan it has only 30 requests.
Run as the bot's user to check it: python -m voicebot.tools.auto_ria Tesla "Model 3"
"""

import asyncio
import datetime
import logging
import os
import re
import sys
import time
from typing import Any

import aiohttp

from ..links import LINKS
from . import Tool
from .web_search import DailyLimit

log = logging.getLogger(__name__)

BASE = "https://developers.ria.com/auto"
TIMEOUT_SECONDS = 10
SHOW = 4
CACHE_SECONDS = 30 * 60
BLOCK_SECONDS = 15 * 60
LIMIT_NOTE = ("AUTO.RIA's hourly request limit is used up. Tell the user it will work again within the hour; "
              "do not guess listings and do not say there are none.")
FETCH_IDS = 30
DAILY_REQUESTS = 100
CARS = 1  # AUTO.RIA category: passenger cars
USD = 1

DESCRIPTION = (
    "Search used cars for sale in Ukraine on AUTO.RIA. Returns how many match "
    "and a few listings with price in USD, year, mileage, fuel, gearbox and city."
)
PARAMETERS = {
    "type": "object",
    "properties": {
        "make": {"type": "string", "description": "Car make in English, e.g. 'Tesla', 'Toyota'."},
        "model": {"type": "string", "description": "Model in English, e.g. 'Model 3', 'RAV4'. Empty = any model."},
        "year_from": {"type": "integer"},
        "year_to": {"type": "integer"},
        "price_min_usd": {"type": "integer"},
        "price_max_usd": {"type": "integer"},
        "sort": {"type": "string", "enum": ["default", "cheapest"],
                 "description": "cheapest = lowest price first; default = AUTO.RIA's usual order."},
        "offset": {"type": "integer", "description": "For more options from the same search: next_offset from the last result."},
        "no_damage": {"type": "boolean", "description": "True to skip cars the seller marked as damaged (after an accident or for repair)."},
    },
    "required": ["make"],
}


def simplify(name: str) -> str:
    return re.sub(r"[^a-z0-9а-яіїєё]+", "", (name or "").lower())


def find_id(name: str, items: list[dict]) -> tuple[int, str] | None:
    """items are [{"name": ..., "value": id}]; exact match first, then starts-with, then contains."""
    wanted = simplify(name)
    if not wanted:
        return None
    for test in (lambda n: n == wanted, lambda n: n.startswith(wanted), lambda n: wanted in n):
        for item in items:
            if test(simplify(item.get("name", ""))):
                return int(item["value"]), item["name"]
    return None


# Flags the seller sets on AUTO.RIA (autoInfoBar), in plain words.
SELLER_MARKS = {
    "damage": "damaged (after an accident)",
    "onRepairParts": "for repair or parts",
    "abroad": "currently abroad",
    "custom": "customs not cleared",
    "underCredit": "on credit",
    "confiscatedCar": "confiscated",
}


DAMAGE_MARKS = {SELLER_MARKS["damage"], SELLER_MARKS["onRepairParts"]}


def seller_marks(ad: dict) -> list[str] | None:
    """What the seller marked; [] = nothing marked; None = the listing has no such data."""
    bar = ad.get("autoInfoBar")
    if not isinstance(bar, dict):
        return None
    return [text for key, text in SELLER_MARKS.items() if bar.get(key)]


# Checks and options shown on the listing (only true ones are passed on).
LISTING_CHECKS = {
    "verifiedByInspectionCenter": "checked at an inspection centre",
    "technicalChecked": "AUTO.RIA technical check",
    "haveInfotechReport": "vehicle history report attached",
    "exchangePossible": "trade-in possible",
    "auctionPossible": "price negotiable",
}


def listing_checks(ad: dict) -> list[str]:
    checks = [text for key, text in LISTING_CHECKS.items() if ad.get(key) in (True, 1, "1")]
    if ad.get("dealer"):
        checks.append("sold by a dealer")
    return checks


def summarize_ad(ad: dict) -> dict[str, Any]:
    data = ad.get("autoData") or {}
    state = ad.get("stateData") or {}
    race = data.get("raceInt")
    title = " ".join(filter(None, [ad.get("markName"), ad.get("modelName"), str(data.get("year") or "")]))
    page = ad.get("linkToView")
    return {
        "title": title,
        "link": LINKS.add(f"https://auto.ria.com/uk{page}" if page else None, title),
        "price_usd": ad.get("USD"),
        "mileage_km": race * 1000 if isinstance(race, int) else None,
        "fuel": data.get("fuelName"),
        "gearbox": data.get("gearboxName"),
        "drive": data.get("driveName"),
        "city": state.get("name"),
        "added": ad.get("addDate", "")[:10] or None,
        "vin_checked": bool(ad.get("checkedVin")),
        "seller_marks": seller_marks(ad),
        "listing_checks": listing_checks(ad),
        "description": re.sub(r"\s+", " ", data.get("description") or "")[:300] or None,
    }


class LimitReached(RuntimeError):
    pass


class AutoRia:
    def __init__(self, api_key: str, limiter: DailyLimit | None = None):
        self.key = api_key
        self.limiter = limiter or DailyLimit(DAILY_REQUESTS)
        self.marks: list[dict] | None = None
        self.models: dict[int, list[dict]] = {}
        self.searches: dict[tuple, tuple[float, list, int]] = {}
        self.ads: dict[str, tuple[float, dict]] = {}
        self.blocked_until = 0.0

    async def _get(self, http: aiohttp.ClientSession, path: str, params: dict | list | None = None) -> Any:
        if time.monotonic() < self.blocked_until:
            raise LimitReached("hourly limit (waiting)")
        if not self.limiter.take():
            raise LimitReached("daily limit")
        query = [("api_key", self.key)] + (list(params.items()) if isinstance(params, dict) else (params or []))
        async with http.get(f"{BASE}{path}", params=query) as resp:
            if resp.status == 429:
                self.blocked_until = time.monotonic() + BLOCK_SECONDS
                raise LimitReached(f"{path}: hourly limit")
            if resp.status != 200:
                text = (await resp.text())[:200].replace(self.key, "***")
                raise RuntimeError(f"{path}: HTTP {resp.status}: {text}")
            return await resp.json(content_type=None)

    async def _ad(self, http: aiohttp.ClientSession, ad_id: str) -> dict:
        cached = self.ads.get(str(ad_id))
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        ad = await self._get(http, "/info", {"auto_id": ad_id})
        self.ads[str(ad_id)] = (time.monotonic(), ad)
        return ad

    async def search(self, make: str, model: str = "", year_from: int | None = None, year_to: int | None = None,
                     price_min_usd: int | None = None, price_max_usd: int | None = None,
                     sort: str = "default", offset: int = 0, no_damage: bool = False) -> dict[str, Any]:
        picked: list[dict] = []
        stopped = ""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as http:
            if self.marks is None:
                self.marks = await self._get(http, f"/categories/{CARS}/marks")
            mark = find_id(make, self.marks)
            if not mark:
                return {"error": f"AUTO.RIA has no make called {make!r}."}
            mark_id, mark_name = mark
            model_id, model_name = None, ""
            if model:
                if mark_id not in self.models:
                    self.models[mark_id] = await self._get(http, f"/categories/{CARS}/marks/{mark_id}/models")
                found = find_id(model, self.models[mark_id])
                if not found:
                    names = [m.get("name") for m in self.models[mark_id]][:30]
                    return {"error": f"No {mark_name} model called {model!r}.", "models": names}
                model_id, model_name = found

            query: list[tuple[str, Any]] = [("category_id", CARS), ("marka_id", mark_id), ("currency", USD),
                                            ("countpage", FETCH_IDS), ("page", 0),
                                            ("order_by", 1 if sort == "cheapest" else 0)]
            for name, value in (("model_id", model_id), ("s_yers", year_from), ("po_yers", year_to),
                                ("price_ot", price_min_usd), ("price_do", price_max_usd)):
                if value:
                    query.append((name, value))
            cached = self.searches.get(tuple(query))
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                ids, count = cached[1], cached[2]
            else:
                found_ids = await self._get(http, "/search", query)
                result = (found_ids.get("result") or {}).get("search_result") or {}
                ids, count = result.get("ids") or [], result.get("count") or 0
                self.searches[tuple(query)] = (time.monotonic(), ids, count)

            position = max(0, int(offset or 0))
            while len(picked) < SHOW and position < len(ids):
                try:
                    ad = await self._ad(http, ids[position])
                except LimitReached as e:
                    stopped = str(e)
                    break
                position += 1
                marks = seller_marks(ad) or []
                if no_damage and any(m in DAMAGE_MARKS for m in marks):
                    continue
                picked.append(ad)

        listings = [summarize_ad(a) for a in picked]
        if sort == "cheapest":
            listings.sort(key=lambda a: a["price_usd"] or 10**9)
        output: dict[str, Any] = {"search": f"{mark_name} {model_name}".strip(), "matching_listings": count,
                                  "listings": listings, "today": datetime.date.today().isoformat()}
        if position < len(ids):
            output["next_offset"] = position
        prices = sorted(a["price_usd"] for a in listings if a["price_usd"])
        notes = []
        if prices:
            output["price_range_usd_of_shown"] = [prices[0], prices[-1]]
            notes.append(f"Prices are for the {len(listings)} listings shown, not all {count}; not a market average.")
        if "next_offset" in output:
            notes.append("For more options, call again with the same search and offset = next_offset.")
        if stopped:
            notes.append(LIMIT_NOTE)
        output["note"] = " ".join(notes)
        log.info("auto_ria %s %s (%s-%s, %s-%s USD, %s, offset %d%s): %d listings; shown %s%s",
                 mark_name, model_name, year_from or "", year_to or "", price_min_usd or "", price_max_usd or "", sort,
                 offset or 0, ", no damage" if no_damage else "", count,
                 ", ".join(f"{a['title']} {a['price_usd']}$ marks={a['seller_marks']}" for a in listings) or "-",
                 f"; stopped: {stopped}" if stopped else "")
        return output


def make_tool(api_key: str) -> Tool:
    ria = AutoRia(api_key)

    async def auto_ria(**kw: Any) -> dict[str, Any]:
        try:
            return await ria.search(**kw)
        except LimitReached as e:
            log.warning("auto_ria: %s", e)
            return {"error": LIMIT_NOTE}
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("auto_ria: %s", e)
            return {"error": "AUTO.RIA did not answer. Tell the user; do not guess listings."}

    return Tool(name="auto_ria", description=DESCRIPTION, parameters=PARAMETERS, handler=auto_ria)


def from_env() -> Tool | None:
    key = os.environ.get("AUTO_RIA_API_KEY", "").strip()
    return make_tool(key) if key else None


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    tool = from_env()
    if not tool:
        sys.exit("AUTO_RIA_API_KEY is not set")
    args = sys.argv[1:]
    out = asyncio.run(tool.handler(make=args[0] if args else "Tesla", model=args[1] if len(args) > 1 else ""))
    print(json.dumps(out, ensure_ascii=False, indent=1))
