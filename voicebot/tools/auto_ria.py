"""auto_ria tool: used cars on AUTO.RIA (official API, developers.ria.com, needs AUTO_RIA_API_KEY).

One question costs about 6 API requests: makes and models are looked up once and kept,
then one search and up to SHOW listing details. The average-price API is not used: on the free
plan it has only 30 requests.
Run as the bot's user to check it: python -m voicebot.tools.auto_ria Tesla "Model 3"
"""

import asyncio
import datetime
import logging
import os
import re
import sys
from typing import Any

import aiohttp

from . import Tool
from .web_search import DailyLimit

log = logging.getLogger(__name__)

BASE = "https://developers.ria.com/auto"
TIMEOUT_SECONDS = 10
SHOW = 6
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


def summarize_ad(ad: dict) -> dict[str, Any]:
    data = ad.get("autoData") or {}
    state = ad.get("stateData") or {}
    race = data.get("raceInt")
    return {
        "title": " ".join(filter(None, [ad.get("markName"), ad.get("modelName"), str(data.get("year") or "")])),
        "price_usd": ad.get("USD"),
        "mileage_km": race * 1000 if isinstance(race, int) else None,
        "fuel": data.get("fuelName"),
        "gearbox": data.get("gearboxName"),
        "drive": data.get("driveName"),
        "city": state.get("name"),
        "added": ad.get("addDate", "")[:10] or None,
        "vin_checked": bool(ad.get("checkedVin")),
        "description": re.sub(r"\s+", " ", data.get("description") or "")[:200] or None,
    }


class AutoRia:
    def __init__(self, api_key: str, limiter: DailyLimit | None = None):
        self.key = api_key
        self.limiter = limiter or DailyLimit(DAILY_REQUESTS)
        self.marks: list[dict] | None = None
        self.models: dict[int, list[dict]] = {}

    async def _get(self, http: aiohttp.ClientSession, path: str, params: dict | list | None = None) -> Any:
        if not self.limiter.take():
            raise RuntimeError("daily AUTO.RIA request limit reached")
        query = [("api_key", self.key)] + (list(params.items()) if isinstance(params, dict) else (params or []))
        async with http.get(f"{BASE}{path}", params=query) as resp:
            if resp.status != 200:
                text = (await resp.text())[:200].replace(self.key, "***")
                raise RuntimeError(f"{path}: HTTP {resp.status}: {text}")
            return await resp.json(content_type=None)

    async def search(self, make: str, model: str = "", year_from: int | None = None, year_to: int | None = None,
                     price_min_usd: int | None = None, price_max_usd: int | None = None,
                     sort: str = "default") -> dict[str, Any]:
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
            if model_id:
                query.append(("model_id", model_id))
            if year_from:
                query.append(("s_yers", year_from))
            if year_to:
                query.append(("po_yers", year_to))
            if price_min_usd:
                query.append(("price_ot", price_min_usd))
            if price_max_usd:
                query.append(("price_do", price_max_usd))
            found_ids = await self._get(http, "/search", query)
            result = (found_ids.get("result") or {}).get("search_result") or {}
            ids, count = result.get("ids") or [], result.get("count") or 0

            ads = await asyncio.gather(*(self._get(http, "/info", {"auto_id": i}) for i in ids[:SHOW]),
                                       return_exceptions=True)

        listings = [summarize_ad(a) for a in ads if isinstance(a, dict)]
        if sort == "cheapest":
            listings.sort(key=lambda a: a["price_usd"] or 10**9)
        output: dict[str, Any] = {"search": f"{mark_name} {model_name}".strip(), "matching_listings": count,
                                  "listings": listings, "today": datetime.date.today().isoformat()}
        prices = sorted(a["price_usd"] for a in listings if a["price_usd"])
        if prices:
            output["price_range_usd_of_shown"] = [prices[0], prices[-1]]
            output["note"] = (f"Prices are for the {len(listings)} listings shown, not all {count}. "
                              "Do not call it the market average.")
        log.info("auto_ria %s %s (%s-%s, %s-%s USD, %s): %d listings; shown %s",
                 mark_name, model_name, year_from or "", year_to or "", price_min_usd or "", price_max_usd or "", sort,
                 count,
                 ", ".join(f"{a['title']} {a['price_usd']}$" for a in listings) or "-")
        return output


def make_tool(api_key: str) -> Tool:
    ria = AutoRia(api_key)

    async def auto_ria(**kw: Any) -> dict[str, Any]:
        try:
            return await ria.search(**kw)
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("auto_ria: %s", e)
            return {"error": "AUTO.RIA did not answer. Tell the user."}

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
