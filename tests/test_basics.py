from datetime import datetime, timezone

from voicebot import cues
from voicebot.jabra import HookParser
from voicebot.prompt import build_instructions


def changes(reports):
    parser = HookParser()
    return [c for r in reports if (c := parser.feed(bytes.fromhex(r))) is not None]


def test_call_with_flicker_then_hangup():
    # Captured from the Jabra with the off-hook light confirmed.
    call = ["020100", "020300", "020200", "040200", "020300"]
    hangup = ["020200", "040000", "020000", "040000", "020000"]
    assert changes(call) == [True, False, True]
    assert changes(call + hangup) == [True, False, True, False]


def test_ignores_other_reports():
    assert changes(["040100", "0100", "020000", "020800"]) == []


def test_instructions_include_context(tmp_path):
    ctx = tmp_path / "context.md"
    ctx.write_text("Cat is called Murka.", encoding="utf-8")
    text = build_instructions(str(ctx), datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc))
    assert "Murka" in text and "13 November 2026" in text


def test_missing_context_is_ok(tmp_path):
    assert "Ukrainian" in build_instructions(str(tmp_path / "none.md"))


def test_cue_length():
    pcm = cues.render(cues.START, 24000)
    assert len(pcm) == 2 * sum(int(24000 * d) for _, d in cues.START)


def test_token_totals_cost():
    from voicebot.usage import TokenTotals, prices_for
    from azure.ai.voicelive.models import TokenUsage

    usage = TokenUsage({
        "total_tokens": 1700, "input_tokens": 1200, "output_tokens": 500,
        "input_token_details": {"cached_tokens": 200, "text_tokens": 400, "audio_tokens": 800, "image_tokens": 0,
                                "cached_tokens_details": {"text_tokens": 200, "audio_tokens": 0, "image_tokens": 0}},
        "output_token_details": {"text_tokens": 100, "audio_tokens": 400},
    })
    t = TokenTotals()
    t.add_usage(usage)
    assert (t.audio_in, t.text_in, t.text_in_cached, t.audio_out, t.text_out) == (800, 200, 200, 400, 100)
    expected = (800 * 10 + 200 * 0.6 + 200 * 0.06 + 400 * 20 + 100 * 2.4) / 1_000_000
    assert abs(t.cost(prices_for("gpt-realtime-mini")) - expected) < 1e-12
    assert t.cost(prices_for("unknown-model")) == 0.0


def test_price_override(monkeypatch):
    from voicebot.usage import prices_for

    monkeypatch.setenv("PRICE_GPT_REALTIME_MINI_AUDIO_OUT", "25")
    assert prices_for("gpt-realtime-mini")["audio_out"] == 25.0


def test_settings_cycle_and_persist(tmp_path):
    from voicebot.settings import RuntimeSettings

    path = str(tmp_path / "state.json")
    s = RuntimeSettings(path, ["marin", "cedar"], ["mini", "smart"])
    assert (s.voice, s.model) == ("marin", "mini")
    assert s.next_voice() == "cedar" and s.next_voice() == "marin" and s.next_voice() == "cedar"
    assert s.toggle_model() == "smart"
    again = RuntimeSettings(path, ["marin", "cedar"], ["mini", "smart"])
    assert (again.voice, again.model) == ("cedar", "smart")
    assert RuntimeSettings(path, ["coral"], ["mini", "smart"]).voice == "coral"


def test_voice_display_name():
    from voicebot.settings import voice_display_name

    assert voice_display_name("marin") == "Marin"
    assert voice_display_name("en-US-Ava:DragonHDLatestNeural") == "Ava HD"
    assert voice_display_name("en-US-Andrew2:DragonHDLatestNeural") == "Andrew HD"
    assert voice_display_name("en-US-Ava:DragonHDOmniLatestNeural") == "Ava Omni"
    assert voice_display_name("en-US-AndrewMultilingualNeural") == "Andrew"



def test_model_phrases_load_at_speaker_rate():
    assert len(cues.load_sound("model_smart", 24000)) > 24000
    assert cues.load_sound("model_smart", 16000) is None
    assert cues.load_sound("missing", 24000) is None



def test_instructions_switch_on_web_search(tmp_path):
    missing = str(tmp_path / "none.md")
    assert "no internet access" in build_instructions(missing)
    with_search = build_instructions(missing, web_search=True)
    assert "web_search tool" in with_search and "no internet access" not in with_search
    assert "{knowledge}" not in with_search


def test_web_search_daily_limit():
    from voicebot.tools.web_search import DailyLimit

    limit = DailyLimit(2)
    assert [limit.take() for _ in range(3)] == [True, True, False]


def test_web_search_tool_registered_only_with_key(monkeypatch):
    from voicebot.tools import default_registry

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("TV_SETTINGS", "/nonexistent/tv.json")
    assert default_registry().names() == ["end_session", "ignore_speech", "weather", "world_time", "olx"]
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    reg = default_registry()
    assert reg.names() == ["end_session", "ignore_speech", "weather", "world_time", "olx", "web_search", "price_lookup"]
    assert reg.specs()[5]["parameters"]["required"] == ["query"]



def test_tool_output_goes_through_sdk():
    import asyncio

    from azure.ai.voicelive.aio._patch import ConversationItemResource

    from voicebot.brains.voicelive import tool_output_item

    sent = []

    class FakeConnection:
        async def send(self, event):
            sent.append(event.as_dict())

    asyncio.run(ConversationItemResource(FakeConnection()).create(item=tool_output_item("c1", '{"a": 1}')))
    assert sent[0]["type"] == "conversation.item.create"
    assert sent[0]["item"] == {"type": "function_call_output", "call_id": "c1", "output": '{"a": 1}'}


def test_detect_language():
    from voicebot.language import detect_language

    assert detect_language("What's the latest news in Ukraine?") == "English"
    assert detect_language("Які останні новини?") == "Ukrainian"
    assert detect_language("Ты умеешь выходить в интернет?") == "Russian"
    assert detect_language("Привет, как дела") == "Russian"
    assert detect_language("") is None


def test_add_language_hint():
    import json

    from voicebot.language import add_language_hint

    out = json.loads(add_language_hint('{"answer": "x"}', "English"))
    assert out["answer"] == "x" and out["reply_language"] == "English"
    assert add_language_hint('{"a": 1}', None) == '{"a": 1}'
    assert json.loads(add_language_hint("plain", "Russian"))["result"] == "plain"


def test_tool_flow_adds_language_hint_and_responds():
    import asyncio
    import json
    from types import SimpleNamespace as NS

    from voicebot.brains.base import SessionIO
    from voicebot.brains.voicelive import VoiceLiveBrain
    from voicebot.tools import Tool, ToolRegistry
    from voicebot.usage import TokenTotals

    created, responses, played = [], [], []

    class FakeConn:
        def __init__(self, events):
            self.events = events
            self.conversation = NS(item=NS(create=self._create))
            self.response = NS(create=self._respond)

        async def _create(self, item):
            created.append(item.as_dict())

        async def _respond(self, *, response=None, event_id=None, additional_instructions=None):
            responses.append(additional_instructions)

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for delay, event in self.events:
                await asyncio.sleep(delay)
                yield event
            await asyncio.sleep(0.3)

    events = [
        (0, NS(type="input_audio_buffer.speech_stopped")),
        (0.05, NS(type="conversation.item.input_audio_transcription.completed", transcript="What's new in Ukraine?")),
        (0, NS(type="response.function_call_arguments.done", name="web_search", arguments='{"query": "q"}', call_id="c1")),
        (0, NS(type="response.done", response=None)),
    ]
    registry = ToolRegistry()
    registry.register(Tool("web_search", "d", {}, lambda query: {"answer": "news"}))
    speaker = NS(play=played.append, clear=lambda: None, is_playing=lambda: False)
    session = SessionIO(
        mic=None, speaker=speaker, instructions="", tools=registry, stop=asyncio.Event(),
        model="m", voice="v", thinking_sound=b"tick",
    )
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)
    brain.cfg = __import__("types").SimpleNamespace(transcribe_model="mai-transcribe")
    asyncio.run(brain._receive(FakeConn(events), session, TokenTotals()))

    assert played == [b"tick"]
    outputs = [c for c in created if c["type"] == "function_call_output"]
    rules = [c for c in created if c.get("role") == "system"]
    assert len(outputs) == 1 and len(rules) == 2
    output = json.loads(outputs[0]["output"])
    assert output["answer"] == "news" and output["reply_language"] == "English"
    # One reply after the transcript, one after the tool; both with the language rule.
    assert len(responses) == 2
    assert all("Reply only in English" in r for r in responses)



def test_search_query_similarity():
    from voicebot.tools.web_search import query_words, similar

    gas1 = query_words("latest prices on gas in Ukraine")
    gas2 = query_words("current gas prices in Ukraine November 2026")
    banks = query_words("latest prices of power banks in Ukraine November 2026")
    assert similar(gas1, gas2)
    assert not similar(gas1, banks)


def test_repeat_search_reuses_running_request():
    import asyncio

    from voicebot.tools import web_search as ws

    calls = []

    async def main():
        original = ws.aiohttp.ClientSession

        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body

            async def json(self):
                return {"answer": self.body["query"], "results": []}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, json, headers):
                calls.append(json["query"])
                return FakeResponse(json)

        ws.aiohttp.ClientSession = FakeSession
        try:
            tool = ws.make_tool("k", daily_limit=10)
            first, second = await asyncio.gather(
                tool.handler(query="latest prices on gas in Ukraine"),
                tool.handler(query="current gas prices in Ukraine November 2026"),
            )
            third = await tool.handler(query="power bank prices in Ukraine")
        finally:
            ws.aiohttp.ClientSession = original
        return first, second, third

    first, second, third = asyncio.run(main())
    assert calls == ["latest prices on gas in Ukraine", "power bank prices in Ukraine"]
    assert first == second and third["answer"] == "power bank prices in Ukraine"



def test_shopping_search_request_and_site_names():
    import asyncio

    from voicebot.tools import web_search as ws

    bodies = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"answer": "a", "results": [
                {"url": "https://www.rozetka.com.ua/rtx-5070/", "title": "RTX 5070", "content": "32 499 грн"},
                {"url": None, "title": "x", "content": "y"},
            ]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json, headers):
            bodies.append(json)
            return FakeResponse()

    async def main():
        original = ws.aiohttp.ClientSession
        ws.aiohttp.ClientSession = FakeSession
        try:
            tool = ws.make_tool("k", daily_limit=10, country="ukraine")
            shop = await tool.handler(query="rtx 5070 kharkiv", shopping=True)
            news = await tool.handler(query="ukraine headlines", topic="news")
        finally:
            ws.aiohttp.ClientSession = original
        return shop, news

    shop, news = asyncio.run(main())
    assert bodies[0]["country"] == "ukraine" and bodies[0]["include_domains_mode"] == "prefer"
    assert "rozetka.com.ua" in bodies[0]["include_domains"]
    assert "country" not in bodies[1] and "include_domains" not in bodies[1]
    assert [r["site"] for r in shop["results"]] == ["rozetka.com.ua", None]



PN_PAGE = """<html><head><title>RTX 5070</title>
<script type="application/ld+json">{"@context": "http://schema.org/", "@type": "Product",
 "name": "Asus GeForce RTX 5070 Prime", "offers": {"@type": "AggregateOffer", "lowPrice": 32499,
 "highPrice": 35499, "priceCurrency": "UAH", "offerCount": 18}}</script></head>
<body><div>Продается в <b>18</b> магазинах (12 не из Харькова)</div></body></html>"""


def test_parse_pn_product_page():
    from voicebot.tools.price_lookup import parse_product_page

    assert parse_product_page(PN_PAGE) == {
        "name": "Asus GeForce RTX 5070 Prime", "price_from": 32499, "price_to": 35499,
        "currency": "UAH", "shops": 18, "shops_in_kharkiv": 6,
    }
    assert parse_product_page("<html><title>Ошибка 404</title></html>") is None


def test_pn_product_urls_are_clean_and_unique():
    from voicebot.tools.price_lookup import product_page_urls

    results = [
        {"url": "https://pn.com.ua/md/123/?city=5"},
        {"url": "https://www.pn.com.ua/md/123/"},
        {"url": "https://pn.com.ua/ct/1047/"},
        {"url": "https://pn.com.ua/md/456/"},
    ]
    assert product_page_urls(results) == ["https://pn.com.ua/md/123/", "https://pn.com.ua/md/456/"]


def test_price_lookup_end_to_end_sorted_cheapest_first():
    import asyncio

    from voicebot.tools import price_lookup as pl
    from voicebot.tools.web_search import DailyLimit

    cheap = PN_PAGE.replace("32499", "29999").replace("RTX 5070 Prime", "RTX 5070 Dual")
    unrelated = PN_PAGE.replace("Asus GeForce RTX 5070 Prime", "Deuter AC Lite 14 SL moss/apple").replace("32499", "2622")
    pages = {
        "https://pn.com.ua/md/1/": PN_PAGE,
        "https://pn.com.ua/md/2/": cheap,
        "https://pn.com.ua/md/3/": unrelated,
    }
    requests = []

    class FakeResponse:
        status = 200

        def __init__(self, data):
            self.data = data

        async def json(self):
            return self.data

        async def text(self):
            return self.data

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json, headers):
            requests.append(json)
            return FakeResponse({"results": [{"url": u} for u in pages]})

        def get(self, url, headers):
            return FakeResponse(pages[url])

    async def main():
        original = pl.aiohttp.ClientSession
        pl.aiohttp.ClientSession = FakeSession
        try:
            return await pl.make_tool("k", DailyLimit(5)).handler(product="RTX 5070")
        finally:
            pl.aiohttp.ClientSession = original

    out = asyncio.run(main())
    assert requests[0]["include_domains"] == ["pn.com.ua"] and requests[0]["include_domains_mode"] == "restrict"
    assert [p["price_from"] for p in out["products"]] == [29999, 32499]


def test_price_lookup_skips_pages_without_offers():
    from voicebot.tools.price_lookup import parse_product_page

    page = PN_PAGE.replace('"lowPrice": 32499,', "")
    assert parse_product_page(page)["price_from"] is None



def test_clean_site():
    from voicebot.tools.web_search import clean_site

    assert clean_site("https://www.Elmir.ua/videocards/") == "elmir.ua"
    assert clean_site("elmir.ua") == "elmir.ua"
    assert clean_site("Elmir") == ""
    assert clean_site(None) == ""


def test_site_search_restricts_and_is_not_reused():
    import asyncio

    from voicebot.tools import web_search as ws

    bodies = []

    class FakeResponse:
        status = 200

        async def json(self):
            return {"answer": "a", "results": [{"url": "https://elmir.ua/x", "title": "t", "content": "c"}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json, headers):
            bodies.append(json)
            return FakeResponse()

    async def main():
        original = ws.aiohttp.ClientSession
        ws.aiohttp.ClientSession = FakeSession
        try:
            tool = ws.make_tool("k", daily_limit=10, country="ukraine")
            await tool.handler(query="rtx 5070 kharkiv", shopping=True)
            await tool.handler(query="rtx 5070 kharkiv elmir.ua", shopping=True, site="elmir.ua")
            await tool.handler(query="rtx 5070 kharkiv on elmir.ua", shopping=True, site="https://elmir.ua")
        finally:
            ws.aiohttp.ClientSession = original

    asyncio.run(main())
    assert len(bodies) == 2
    assert bodies[1]["include_domains"] == ["elmir.ua"] and bodies[1]["include_domains_mode"] == "restrict"



def test_key_words_and_matching():
    from voicebot.tools.matching import key_words, matches

    words = key_words("MacBook 14-inch with M5 Pro")
    assert words == ["macbook", "14", "m5"]
    assert not matches("Apple iPad 10.9 Wi-Fi 64Gb Silver (MCM74NF/A)", words)
    assert not matches("Deuter AC Lite 14 SL moss/apple", words)
    assert matches('Apple MacBook Pro 14" M5 Pro 24/1TB Space Black', words)
    assert key_words("видеокарта RTX 5070") == ["5070"]
    assert matches("anything", [])


def test_prices_uah():
    from voicebot.tools.matching import prices_uah

    assert prices_uah("Ціна 114 999 грн, було 120 000 ₴; 92999 UAH; доставка 50 грн") == [114999, 120000, 92999]


def test_cheapest_offers():
    from voicebot.tools.web_search import cheapest_offers

    results = [
        {"url": "https://istore.ua/a", "title": "Apple MacBook Pro 14 M5 Pro", "content": "Ціна 114 999 грн"},
        {"url": "https://appleroom.ua/b", "title": "MacBook Pro 14 M5 Pro 24GB", "content": "від 92 999 грн"},
        {"url": "https://case.ua/c", "title": "Чохол для MacBook 14", "content": "499 грн"},
        {"url": "https://x.ua/d", "title": "MacBook Pro 14 M5 Pro review", "content": "no price"},
    ]
    offers = cheapest_offers("MacBook 14 M5 Pro", results)
    assert [(o["site"], o["price_uah"]) for o in offers] == [("appleroom.ua", 92999), ("istore.ua", 114999)]



def test_memory_facts_remember_forget(tmp_path):
    from voicebot.memory import Memory

    m = Memory(str(tmp_path / "mem"))
    assert m.remember("The cat is called Murka.") == {"status": "remembered", "id": 1, "for": "everyone"}
    assert m.remember("the cat is called murka.")["status"] == "already remembered"
    assert m.remember("Kostya likes jazz.")["id"] == 2
    assert m.forget(1)["status"] == "forgotten"
    assert "error" in m.forget(1)
    assert [f.text for f in Memory(str(tmp_path / "mem")).facts()] == ["Kostya likes jazz."]
    assert "[2] Kostya likes jazz." in m.prompt_text()


def test_memory_recent_history(tmp_path):
    import time

    from voicebot.memory import Memory

    m = Memory(str(tmp_path / "mem"), recent_hours=1, recent_turns=3)
    m.save_session([("assistant", "Hi, I'm Cedar.")])  # no user speech: not saved
    m.save_session([("user", "old question"), ("assistant", "old answer")])
    m.save_session([("user", "Where is Kharkiv?"), ("assistant", "In Ukraine."), ("user", "Thanks")])
    recent = m.recent()
    assert [text for _, _, text in recent] == ["Where is Kharkiv?", "In Ukraine.", "Thanks"]
    assert m.recent(now=time.time() + 7200) == []
    text = m.prompt_text()
    assert "Recent conversations" in text and "Someone: Thanks" in text and "Cedar" not in text


def test_memory_in_instructions(tmp_path):
    text = build_instructions(str(tmp_path / "none.md"), memory="Long-term memory:\n[1] Cat is Murka.")
    assert "[1] Cat is Murka.\n\nReminder: reply in the language" in text
    assert "end_session" in text


def test_memory_tools_registered(tmp_path):
    import asyncio
    import json

    from voicebot.memory import Memory
    from voicebot.tools import default_registry

    reg = default_registry(Memory(str(tmp_path / "mem")))
    assert {"end_session", "remember", "forget"} <= set(reg.names())
    out = json.loads(asyncio.run(reg.call("remember", json.dumps({"fact": "Dog is Rex."}))))
    assert out["status"] == "remembered"
    out = json.loads(asyncio.run(reg.call("forget", json.dumps({"id": out["id"]}))))
    assert out["status"] == "forgotten"


def test_goodbye_ends_session_without_follow_up():
    import asyncio
    from types import SimpleNamespace as NS

    from voicebot.brains.base import SessionIO
    from voicebot.brains.voicelive import VoiceLiveBrain
    from voicebot.tools import default_registry
    from voicebot.usage import TokenTotals

    responses = []

    class FakeConn:
        conversation = NS(item=NS(create=None))
        response = NS(create=lambda: responses.append(True))

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield NS(type="conversation.item.input_audio_transcription.completed", transcript="Пока, спасибо")
            yield NS(type="response.audio_transcript.done", transcript="Пока!")
            yield NS(type="response.function_call_arguments.done", name="end_session", arguments="{}", call_id="c")
            yield NS(type="response.done", response=None)
            await asyncio.sleep(0.05)

    speaker = NS(play=lambda b: None, clear=lambda: None, is_playing=lambda: False)
    session = SessionIO(
        mic=None, speaker=speaker, instructions="", tools=default_registry(), stop=asyncio.Event(),
        model="m", voice="v", thinking_sound=b"tick",
    )
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)
    brain.cfg = __import__("types").SimpleNamespace(transcribe_model="mai-transcribe")
    asyncio.run(brain._receive(FakeConn(), session, TokenTotals()))
    assert session.end_ready is True
    assert responses == []
    assert session.history == [("user", "Пока, спасибо"), ("assistant", "Пока!")]


def test_weather_home_from_context(tmp_path):
    from voicebot.tools.weather import home_from_context

    ctx = tmp_path / "context.md"
    ctx.write_text("# Household\n- Location: Kharkiv, Ukraine. Use metric units.\n", encoding="utf-8")
    assert home_from_context(str(ctx)) == "Kharkiv"
    assert home_from_context(str(tmp_path / "missing.md")) == ""


def test_weather_summary():
    from voicebot.tools.weather import summarize

    data = {
        "current": {"time": "2026-11-13T23:00", "temperature_2m": 2.9, "apparent_temperature": -1.4,
                    "weather_code": 3, "wind_speed_10m": 4.1, "relative_humidity_2m": 80},
        "hourly": {"time": [f"2026-11-14T{h:02d}:00" for h in range(6)],
                   "temperature_2m": [2, 2, 1, 1, 0, 0], "precipitation_probability": [0, 0, 10, 20, 30, 40],
                   "weather_code": [3, 3, 61, 61, 63, 63]},
        "daily": {"time": ["2026-11-13"], "weather_code": [63], "temperature_2m_max": [8.8],
                  "temperature_2m_min": [1.8], "precipitation_probability_max": [100],
                  "precipitation_sum": [23.9], "wind_speed_10m_max": [6.0],
                  "sunrise": ["2026-11-13T07:32"], "sunset": ["2026-11-13T16:43"]},
    }
    out = summarize("Lviv, Ukraine", data)
    assert out["now"] == {"time": "23:00", "temp_c": 2.9, "feels_like_c": -1.4, "sky": "overcast",
                          "wind_m_s": 4.1, "humidity_pct": 80}
    assert [h["time"] for h in out["next_hours"]] == ["00:00", "03:00"]
    assert out["days"][0]["sky"] == "rain" and out["days"][0]["sunset"] == "16:43"
    assert out["days"][0]["day"] == "today"


def test_weather_day_labels():
    from voicebot.tools.weather import day_label

    assert day_label("2026-11-14", "2026-11-13") == "tomorrow"
    assert day_label("2026-11-16", "2026-11-13") == "Monday"


def test_goodbye_phrases_in_tool_description():
    from voicebot.tools.memory_tools import end_session_tool

    text = end_session_tool().description
    for phrase in ("that's it", "that's all", "всё спасибо", "це все", "на сьогодні все"):
        assert phrase in text


def test_memory_files_are_private(tmp_path):
    import os
    import stat

    from voicebot.memory import Memory

    m = Memory(str(tmp_path / "mem"))
    m.remember("Fact.")
    m.save_session([("user", "hi")])
    for name in ("facts.json", "history.jsonl"):
        assert stat.S_IMODE(os.stat(tmp_path / "mem" / name).st_mode) == 0o640



def test_is_goodbye():
    from voicebot.goodbye import is_goodbye

    for text in ["Bye.", "Thank you, that's it.", "Thank you, that is it.", "Thanks, that's all for now", "OK, I'm done",
                 "Всё, спасибо, пока!", "Ну ладно, до свидания", "Спасибо, больше ничего",
                 "Дякую, це все", "На сьогодні все", "Бувай!", "Добре, на добраніч"]:
        assert is_goodbye(text), text
    for text in ["Thank you", "That's it, what about the weather?", "Это всё хорошо, расскажи ещё",
                 "Всё понятно", "Stop talking about prices and tell me the weather",
                 "Пока не знаю, что спросить", "Bye the way, how much is it"]:
        assert not is_goodbye(text), text


def test_goodbye_in_transcript_ends_after_reply():
    import asyncio
    from types import SimpleNamespace as NS

    from voicebot.brains.base import SessionIO
    from voicebot.brains.voicelive import VoiceLiveBrain
    from voicebot.tools import ToolRegistry
    from voicebot.usage import TokenTotals

    class FakeConn:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield NS(type="response.created")
            yield NS(type="conversation.item.input_audio_transcription.completed", transcript="Thank you, that's it.")
            await asyncio.sleep(0.05)
            assert session.end_ready is False
            yield NS(type="response.audio_transcript.done", transcript="You're welcome! Goodbye!")
            yield NS(type="response.done", response=None)
            await asyncio.sleep(0.05)

    speaker = NS(play=lambda b: None, clear=lambda: None, is_playing=lambda: False)
    session = SessionIO(mic=None, speaker=speaker, instructions="", tools=ToolRegistry(), stop=asyncio.Event(),
                        model="m", voice="v")
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)
    brain.cfg = __import__("types").SimpleNamespace(transcribe_model="mai-transcribe")
    asyncio.run(brain._receive(FakeConn(), session, TokenTotals()))
    assert session.end_ready is True



def test_weather_detects_home_from_ip_once():
    import asyncio

    from voicebot.tools import weather as w

    calls = []

    class FakeResponse:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        async def json(self):
            return self.data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, params=None):
            calls.append(url)
            if url == w.IP_LOCATION_URL:
                return FakeResponse({"city": "Kharkiv"})
            if url == w.GEOCODE_URL:
                assert params["name"] == "Kharkiv"
                return FakeResponse({"results": [{"latitude": 50.0, "longitude": 36.2, "name": "Kharkiv",
                                                  "country": "Ukraine"}]})
            return FakeResponse({"current": {"time": "2026-11-13T23:00"}, "hourly": {}, "daily": {}})

    async def main():
        original = w.aiohttp.ClientSession
        w.aiohttp.ClientSession = FakeSession
        try:
            tool = w.make_tool("", detect_home=True)
            first = await tool.handler()
            second = await tool.handler()
            off = await w.make_tool("", detect_home=False).handler()
        finally:
            w.aiohttp.ClientSession = original
        return first, second, off

    first, second, off = asyncio.run(main())
    assert first["place"] == "Kharkiv, Ukraine" and second["place"] == "Kharkiv, Ukraine"
    assert calls.count(w.IP_LOCATION_URL) == 1
    assert "error" in off


def test_detect_language_by_common_words():
    from voicebot.language import detect_language

    assert detect_language("Как дела?") == "Russian"
    assert detect_language("Какая погода завтра в Харькове?") == "Russian"
    assert detect_language("Добре, бувай") == "Ukrainian"
    assert detect_language("Підкажи, коли буде Пасха") == "Ukrainian"
    assert detect_language("What's the weather?") == "English"
    assert detect_language("Зразумела") is None
    assert detect_language("А, давай поговорим на русском.") == "Russian"
    assert detect_language("Добре, бувай") == "Ukrainian"
    assert detect_language("Давай") is None
    assert detect_language("Все, все, поняв, поняв. До побачення.") == "Ukrainian"
    assert detect_language("А те ще наша церква кудись там перейшла.") == "Ukrainian"


def test_tv_find_by_name():
    from voicebot.tools.tv import find_by_name

    apps = {"youtube.leanback.v4": "YouTube", "netflix": "Netflix", "com.apple.appletv": "Apple TV"}
    assert find_by_name("youtube", apps) == "youtube.leanback.v4"
    assert find_by_name("Netflix app", apps) == "netflix"
    inputs = {"com.webos.app.hdmi1": "PS5 Игровая консоль", "com.webos.app.hdmi2": "HDMI 2"}
    assert find_by_name("PS5", inputs) == "com.webos.app.hdmi1"
    assert find_by_name("hdmi 2", inputs) == "com.webos.app.hdmi2"
    assert find_by_name("hdmi1", inputs) == "com.webos.app.hdmi1"
    assert find_by_name("Disney", apps) is None


def test_youtube_id_from_url():
    from voicebot.tools.web_search import YOUTUBE_ID

    assert YOUTUBE_ID.search("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1").group(1) == "dQw4w9WgXcQ"
    assert YOUTUBE_ID.search("https://youtu.be/dQw4w9WgXcQ").group(1) == "dQw4w9WgXcQ"
    assert YOUTUBE_ID.search("https://www.youtube.com/@channel") is None


def test_tv_settings_file_is_private(tmp_path):
    import os
    from voicebot.tools.tv import load_settings, make_tool, save_settings

    path = str(tmp_path / "tv.json")
    assert make_tool(path) is None
    save_settings(path, {"host": "1.2.3.4", "mac": "aa:bb:cc:dd:ee:ff", "client_key": "k"})
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert load_settings(path)["host"] == "1.2.3.4"
    assert make_tool(path).name == "tv"


def test_youtube_finder_channel_latest(monkeypatch):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from voicebot.tools import youtube

    calls = []

    async def handler(request):
        q = dict(request.query)
        calls.append(q)
        if q["type"] == "channel":
            return web.json_response({"items": [{"id": {"channelId": "C1"},
                                                 "snippet": {"channelId": "C1", "channelTitle": "MrBeast"}}]})
        return web.json_response({"items": [{"id": {"videoId": "abcdefghijk"},
                                             "snippet": {"title": "I &quot;Won&quot;", "channelTitle": "MrBeast",
                                                         "publishedAt": "2026-11-10T12:00:00Z"}}]})

    async def run():
        app = web.Application()
        app.router.add_get("/search", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr(youtube, "SEARCH_URL", str(server.make_url("/search")))
            find = youtube.make_video_finder("KEY", quota=youtube.Quota(limit=1000))
            return await find(channel="mr beast", latest=True)

    video = asyncio.run(run())
    assert video == {"id": "abcdefghijk", "title": 'I "Won"', "channel": "MrBeast", "published": "2026-11-10"}
    assert calls[1]["channelId"] == "C1" and calls[1]["order"] == "date" and "q" not in calls[1]


def test_youtube_quota():
    from voicebot.tools.youtube import Quota

    quota = Quota(limit=250)
    assert quota.take(100) and quota.take(100) and not quota.take(100)


def test_transcription_settings():
    from types import SimpleNamespace as NS

    from voicebot.brains.voicelive import VoiceLiveBrain

    def session_for(model):
        cfg = NS(turn_detection="azure_semantic_vad_multilingual", turn_silence_ms=800, transcribe_model=model,
                 noise_reduction="", echo_cancellation=False)
        brain = VoiceLiveBrain(cfg)
        s = NS(instructions="", voice="coral", speaker=NS(mic_rate=16000), tools=None)
        return brain._session_config(s)

    mai = session_for("mai-transcribe")
    assert "Jarvis" in mai["input_audio_transcription"]["phrase_list"]
    assert "prompt" not in mai["input_audio_transcription"]
    assert mai["turn_detection"]["create_response"] is False
    gpt = session_for("gpt-4o-mini-transcribe")
    assert "Belarusian" in gpt["input_audio_transcription"]["prompt"]
    off = session_for("")
    assert "input_audio_transcription" not in off and off["turn_detection"]["create_response"] is True


def test_voice_previews_made_once_and_cached(tmp_path):
    import asyncio

    from voicebot.voice_preview import VoicePreviews

    said = []

    async def say(text, voice):
        said.append((text, voice))
        return b"\x01\x02" * 10

    previews = VoicePreviews(str(tmp_path / "voices"), say)
    assert asyncio.run(previews.get("marin")) == b"\x01\x02" * 10
    assert asyncio.run(previews.get("marin")) == b"\x01\x02" * 10
    asyncio.run(previews.prepare(["marin", "en-US-Ava:DragonHDLatestNeural"]))
    assert said == [("Hi, I'm Marin.", "marin"), ("Hi, I'm Ava HD.", "en-US-Ava:DragonHDLatestNeural")]


def test_voice_preview_failure_returns_none(tmp_path):
    import asyncio

    from voicebot.voice_preview import VoicePreviews

    async def say(text, voice):
        raise RuntimeError("offline")

    assert asyncio.run(VoicePreviews(str(tmp_path), say).get("cedar")) is None


def _olx_offer(oid, title, price, state="used", city="Харків", offer_type="offer", cat="electronics", cat_id=458):
    return {"id": oid, "title": title, "offer_type": offer_type, "category": {"type": cat, "id": cat_id},
            "created_time": "2026-11-01T10:00:00+02:00", "business": False,
            "location": {"city": {"name": city}},
            "params": [{"key": "price", "value": {"value": price, "currency": "UAH", "negotiable": True}},
                       {"key": "state", "value": {"key": state}}]}


def test_olx_pick_offers():
    from voicebot.tools.olx import city_id, pick_offers, summarize

    offers = [
        _olx_offer(1, "RTX 5070 Ti ROG Strix", 80000),
        _olx_offer(2, "Відеокарта Gigabyte RTX 5070 12GB", 31000),
        _olx_offer(3, "ПК Ryzen 5 / RTX 5070", 69999, cat_id=78),
        _olx_offer(8, "Продам коробку від RTX 5070", 300),
        _olx_offer(4, "Куплю RTX 5070", 20000, offer_type="buy"),
        _olx_offer(5, "Ремонт RTX 5070", 500, cat="services"),
        _olx_offer(6, "RTX 4070 Super", 25000),
        _olx_offer(7, "RTX 5070 MSI", 33000, state="new", city="Київ"),
    ]
    picked = pick_offers(offers, "RTX 5070", 280, "", None, None, "any")
    assert [o["id"] for o in picked] == [2, 7, 3]
    assert [o["id"] for o in pick_offers(offers, "RTX 5070", None, "Київ", None, None, "any")] == [7]
    assert [o["id"] for o in pick_offers(offers, "RTX 5070", 280, "", 32000, 70000, "used")] == [3]
    assert [o["id"] for o in pick_offers(offers, "RTX 5070 Ti", 280, "", None, None, "any")] == [1]
    assert [o["id"] for o in pick_offers(offers, "RTX 5070", 280, "", None, None, "any", part_only=True)] == [2, 7]
    assert summarize(offers[1])["price_uah"] == 31000
    assert city_id("Kharkiv") == 280 and city_id("харькове") == 280 and city_id("Bucha") is None


def test_olx_clean_text():
    from voicebot.tools.olx import clean_text

    assert clean_text("Стан<br />\n<br />відмінний &amp; коробка") == "Стан відмінний & коробка"


def test_olx_drops_accessories():
    from voicebot.tools.olx import drop_accessories

    offers = [_olx_offer(i, "Дитячий велосипед", p) for i, p in enumerate([100, 150, 1800, 2200, 2500, 3000])]
    assert [o["id"] for o in drop_accessories(offers)] == [2, 3, 4, 5]
    assert len(drop_accessories(offers[:3])) == 3


def test_olx_accessories_and_variants():
    from voicebot.tools.olx import pick_offers

    offers = [
        _olx_offer(1, "Чохол iPhone 15 Pro MagSafe", 250),
        _olx_offer(2, "iPhone 15 Pro 128GB", 30000),
        _olx_offer(3, "iPhone 15 Pro Max 256GB", 38000),
    ]
    assert [o["id"] for o in pick_offers(offers, "iPhone 15 Pro", 280, "", None, None, "any")] == [2]
    assert [o["id"] for o in pick_offers(offers, "чохол iPhone 15 Pro", 280, "", None, None, "any")] == [1, 2]
    assert [o["id"] for o in pick_offers(offers, "iPhone 15 Pro Max", 280, "", None, None, "any")] == [3]


def test_auto_ria_search(monkeypatch):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from voicebot.tools import auto_ria

    seen = []

    async def handler(request):
        seen.append((request.path, list(request.query.items())))
        assert request.query["api_key"] == "K"
        path = request.path
        if path.endswith("/marks"):
            return web.json_response([{"name": "Tata", "value": 1}, {"name": "Tesla", "value": 2233}])
        if path.endswith("/models"):
            return web.json_response([{"name": "Model S", "value": 10}, {"name": "Model 3", "value": 11}])
        if path.endswith("/search"):
            return web.json_response({"result": {"search_result": {"ids": ["5", "6"], "count": 42}}})
        if path.endswith("/info"):
            i = int(request.query["auto_id"])
            return web.json_response({"USD": 20000 + i, "markName": "Tesla", "modelName": "Model 3",
                                      "autoData": {"year": 2021, "raceInt": 55, "fuelName": "Електро"},
                                      "stateData": {"name": "Київ"}, "addDate": "2026-11-10 12:00:00"})
        if path.endswith("/average_price"):
            return web.json_response({"total": 120, "interQuartileMean": 21500.4})
        return web.json_response({}, status=404)

    async def run():
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr(auto_ria, "BASE", str(server.make_url("/auto")))
            tool = auto_ria.make_tool("K")
            out = await tool.handler(make="tesla", model="model 3", year_from=2020, sort="cheapest")
            again = await tool.handler(make="Tesla", model="Model S")
            return out, again

    out, again = asyncio.run(run())
    assert out["search"] == "Tesla Model 3" and out["matching_listings"] == 42
    assert out["price_range_usd_of_shown"] == [20005, 20006]
    assert not any(p.endswith("/average_price") for p, _ in seen)  # only 30 requests on the free plan
    assert [a["price_usd"] for a in out["listings"]] == [20005, 20006]
    assert out["listings"][0]["mileage_km"] == 55000 and out["listings"][0]["city"] == "Київ"
    search_query = dict(next(q for p, q in seen if p.endswith("/search")))
    assert search_query["marka_id"] == "2233" and search_query["model_id"] == "11"
    assert search_query["order_by"] == "1" and search_query["s_yers"] == "2020"
    assert sum(p.endswith("/marks") for p, _ in seen) == 1  # makes are looked up once
    assert again["search"] == "Tesla Model S"


def test_auto_ria_find_id():
    from voicebot.tools.auto_ria import find_id

    models = [{"name": "Model 3", "value": 1}, {"name": "Model S", "value": 2}, {"name": "Model X", "value": 3}]
    assert find_id("model3", models) == (1, "Model 3")
    assert find_id("Model X Plaid", models) is None
    assert find_id("", models) is None


def test_auto_ria_seller_marks():
    from voicebot.tools.auto_ria import seller_marks

    assert seller_marks({}) is None
    assert seller_marks({"autoInfoBar": {"damage": False, "custom": False}}) == []
    assert seller_marks({"autoInfoBar": {"damage": True, "underCredit": True}}) == [
        "damaged (after an accident)", "on credit"]


def test_auto_ria_listing_checks():
    from voicebot.tools.auto_ria import listing_checks

    ad = {"verifiedByInspectionCenter": True, "technicalChecked": False, "auctionPossible": True,
          "haveInfotechReport": 1, "dealer": {"id": 5}}
    assert listing_checks(ad) == ["checked at an inspection centre", "vehicle history report attached",
                                 "price negotiable", "sold by a dealer"]
    assert listing_checks({}) == []


def test_stalled_reply_is_retried_once(monkeypatch):
    import asyncio
    import time
    from types import SimpleNamespace as NS

    from voicebot.brains import voicelive
    from voicebot.brains.voicelive import VoiceLiveBrain, _Turn

    monkeypatch.setattr(voicelive, "STALL_SECONDS", 0.5)
    calls = []

    async def create(**kw):
        calls.append("create")

    async def cancel(**kw):
        calls.append("cancel")

    async def item_create(item):
        calls.append("rule")

    conn = NS(response=NS(create=create, cancel=cancel), conversation=NS(item=NS(create=item_create)))
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)

    async def run():
        turn = _Turn()
        turn.number, turn.waiting_for_audio, turn.last_event = 1, True, time.monotonic() - 5
        await asyncio.wait_for(brain._watch_reply(conn, turn, 1), 5)

    asyncio.run(run())
    assert calls == ["cancel", "create"]  # no language detected, so no rule message


def test_car_rules_only_with_car_tool(tmp_path):
    from voicebot.prompt import build_instructions

    ctx = str(tmp_path / "none.md")
    with_cars = build_instructions(ctx, web_search=True, cars=True)
    assert "always call auto_ria" in with_cars
    assert with_cars.index("always call auto_ria") < with_cars.index("For where-to-buy questions")
    assert "auto_ria" not in build_instructions(ctx, web_search=True)


def test_auto_ria_cache_offset_damage_and_limit(monkeypatch):
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from voicebot.tools import auto_ria

    seen, state = [], {"limit": False}

    async def handler(request):
        path = request.path
        seen.append(path)
        if state["limit"] and not path.endswith("/marks"):
            return web.json_response({"error_type": "HourOverlimit"}, status=429)
        if path.endswith("/marks"):
            return web.json_response([{"name": "Tesla", "value": 2233}])
        if path.endswith("/search"):
            return web.json_response({"result": {"search_result": {"ids": [str(i) for i in range(1, 11)], "count": 10}}})
        i = int(request.query["auto_id"])
        return web.json_response({"USD": 20000 + i, "markName": "Tesla", "modelName": "Model 3",
                                  "autoData": {"year": 2021}, "autoInfoBar": {"damage": i in (2, 3)}})

    async def run():
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        async with TestServer(app) as server:
            monkeypatch.setattr(auto_ria, "BASE", str(server.make_url("/auto")))
            tool = auto_ria.make_tool("K")
            first = await tool.handler(make="Tesla", no_damage=True)
            searches_before = sum(p.endswith("/search") for p in seen)
            again = await tool.handler(make="Tesla", no_damage=True)
            more = await tool.handler(make="Tesla", no_damage=True, offset=first["next_offset"])
            searches_after = sum(p.endswith("/search") for p in seen)
            state["limit"] = True
            limited = await tool.handler(make="Tesla", year_from=2024)
            calls_when_blocked = len(seen)
            blocked = await tool.handler(make="Tesla", year_from=2023)
            return first, again, more, searches_before, searches_after, limited, blocked, calls_when_blocked

    first, again, more, before, after, limited, blocked, calls = asyncio.run(run())
    assert [a["price_usd"] for a in first["listings"]] == [20001, 20004, 20005, 20006]  # 2 and 3 damaged
    assert first["next_offset"] == 6 and again == first and before == after == 1
    assert [a["price_usd"] for a in more["listings"]] == [20007, 20008, 20009, 20010]
    assert "hourly" in limited["error"] and "hourly" in blocked["error"]
    assert len(seen) == calls  # while blocked, no request is sent


FILM_NAMES = [
    "Cartoons/Moana (2016) BDRip 1080p [HEVC].mkv",
    "Cartoons/Moana.2.2024.2160p.MA.WEB-DL.DV.HDR.TYMBLER.mkv",
    "Cartoons/Moana.2026.MA.x265.WEB-DL.2160p.HDR-DV.mkv",
    "Cartoons/Frozen II (2019) BDRip 1080p [HEVC].mkv",
    "Cartoons/Frozen.2013.BDREMUX.2160p.HDR.seleZen.mkv",
    "Cartoons/#02 Монстры на каникулах 2 (2015).mkv",
    "Cartoons/#01 Монстры на каникулах (2012).mkv",
    "Inception.2010.BDRip.1080p.Rus.Eng.mkv",
    "Paddington.2.2017.TRIPLE.BDRip.1080p.-HELLYWOOD.mkv",
    "Paddington.2014.1080p.BluRay.x264-LEONARDO_[scarabey.org].mkv",
    "01. Трансформеры.mkv",
    "Cartoons/1. Decpicable Me (2010) BDRip 1080p [HEVC].mkv",
    "Black.Doves.S01.1080p.NF.WEB-DL.H.264-EniaHD/Black.Doves.S01E02.A.Little.Black.Dove.1080p.NF.WEB-DL.H.264-EniaHD.mkv",
]


def test_film_titles_parsed():
    from voicebot.tools.films import parse

    assert (parse(FILM_NAMES[0]).title, parse(FILM_NAMES[0]).year) == ("Moana", 2016)
    assert parse(FILM_NAMES[1]).title == "Moana 2"
    assert parse(FILM_NAMES[5]).title == "Монстры на каникулах 2"
    assert parse(FILM_NAMES[10]).title == "Трансформеры"
    assert parse(FILM_NAMES[8]).title == "Paddington 2"


def test_film_find():
    from voicebot.tools.films import find, parse

    library = [parse(n) for n in FILM_NAMES]

    def top(*titles, year=None):
        return [f.path for _, f in find(library, list(titles), year)]

    assert top("Inception", "Начало")[0] == "Inception.2010.BDRip.1080p.Rus.Eng.mkv"
    assert top("Frozen", "Крижане серце", "Холодное сердце")[0] == "Cartoons/Frozen.2013.BDREMUX.2160p.HDR.seleZen.mkv"
    assert top("Frozen 2")[0] == "Cartoons/Frozen II (2019) BDRip 1080p [HEVC].mkv"  # II = 2
    assert top("Моана", "Moana", year=2016)[0] == FILM_NAMES[0]
    assert top("Monsters on vacation", "Монстри на канікулах")[0] == FILM_NAMES[6]  # Ukrainian ~ Russian file name
    assert top("Despicable Me")  # file is named "Decpicable Me" (typo)
    moana = top("Moana")
    assert set(moana[:2]) == {FILM_NAMES[0], FILM_NAMES[2]}  # two films called just "Moana": ask
    assert top("Moana", year=2016)[0] == FILM_NAMES[0]
    assert top("Moana 2")[0] == FILM_NAMES[1]
    assert top("Hotel Transylvania", "Монстри на канікулах", "Монстры на каникулах")[0] == FILM_NAMES[6]
    assert top("Paddington 2")[0] == FILM_NAMES[8]
    assert top("Black Doves episode 2", "Black Doves S01E02") != []
    assert top("Shrek") == []


def test_tv_plays_film_or_asks(tmp_path):
    import asyncio

    from voicebot.tools.tv import TV

    index = tmp_path / "films.tsv"
    index.write_text("\n".join(f"{n}\t1" for n in FILM_NAMES), encoding="utf-8")
    launched = []

    async def launch(app, params):
        launched.append(params["payload"][0]["fullPath"])

    client = __import__("types").SimpleNamespace(launch_app_with_params=launch)
    tv = TV("unused", films_index=str(index), media_root="/tmp/usb/sda/sda2")

    asked = asyncio.run(tv._play_film(client, ["Moana", "Моана", ""], None, ""))
    assert "options" in asked and not launched
    played = asyncio.run(tv._play_film(client, ["Inception", "Начало", ""], None, ""))
    assert played["title"] == "Inception"
    assert launched == ["/tmp/usb/sda/sda2/Inception.2010.BDRip.1080p.Rus.Eng.mkv"]
    picked = asyncio.run(tv._play_film(client, [], None, asked["options"][0]["file"]))
    assert picked["result"] == "Playing on the TV." and len(launched) == 2


def test_play_film_waits_for_disk_after_power_on(tmp_path, monkeypatch):
    import asyncio

    from voicebot.tools import tv as tv_module
    from voicebot.tools.tv import TV

    monkeypatch.setattr(tv_module, "DISK_READY_SECONDS", 0.3)
    index = tmp_path / "films.tsv"
    index.write_text("Inception.2010.BDRip.1080p.Rus.Eng.mkv\t1", encoding="utf-8")
    times = []

    async def launch(app, params):
        times.append(asyncio.get_running_loop().time())

    client = __import__("types").SimpleNamespace(launch_app_with_params=launch)
    tv = TV("unused", films_index=str(index), media_root="/m")

    async def run():
        tv.turned_on_at = asyncio.get_running_loop().time()
        out = await tv._play_film(client, ["Inception"], None, "")
        return out, times[0] - tv.turned_on_at

    out, waited = asyncio.run(run())
    assert waited >= 0.29 and "end_session" in out["note"]


def test_tv_browse():
    import asyncio

    from voicebot.tools.tv import TV

    opened = []

    async def request(uri, payload):
        opened.append((uri, payload["target"]))

    client = __import__("types").SimpleNamespace(request=request)
    tv = TV("unused")
    asyncio.run(tv._browse(client, "нові мультфільми 2026", ""))
    asyncio.run(tv._browse(client, "", "multiplex.ua"))
    assert opened[0] == ("system.launcher/open",
                         "https://www.google.com/search?q=%D0%BD%D0%BE%D0%B2%D1%96+%D0%BC%D1%83%D0%BB%D1%8C%D1%82%D1%84%D1%96%D0%BB%D1%8C%D0%BC%D0%B8+2026")
    assert opened[1][1] == "https://multiplex.ua"


def test_memory_per_person(tmp_path):
    from voicebot.memory import Memory

    m = Memory(str(tmp_path))
    m.remember("The cat is called Murka.")
    m.remember("Kostya likes dark chocolate.", person="Kostya")
    m.remember("Anna is learning French.", person="Anna")
    m.save_session([("user", "привет"), ("assistant", "Привет, Костя!")], person="Kostya")
    m.save_session([("user", "hello"), ("assistant", "Hi!")])
    shared = m.prompt_text()
    kostya = m.prompt_text(person="Kostya")
    assert "Murka" in shared and "chocolate" not in shared and "Привет, Костя" in shared and "Hi!" in shared
    assert "chocolate" in kostya and "Murka" not in kostya and "French" not in kostya and "Привет, Костя" not in kostya


class _FakeVoices:
    """Stand-in for speakers.Voices: a 'voice' is the first byte of the audio."""

    def __init__(self, profiles=None):
        import numpy as np

        self.np = np
        self.profiles = profiles or {}
        self.threshold, self.margin = 0.6, 0.08
        self.saved = []

    def names(self):
        return sorted(self.profiles)

    def embed(self, pcm):
        v = self.np.zeros(4, dtype=self.np.float32)
        v[pcm[0] % 4] = 1.0
        return v

    def identify(self, vector):
        from voicebot.speakers import Voices

        return Voices.identify(self, vector)

    def scores(self, vector):
        from voicebot.speakers import Voices

        return Voices.scores(self, vector)

    def enrol(self, name, vector, seconds):
        self.profiles[name] = {"embedding": list(vector), "seconds": seconds}
        self.saved.append((name, round(seconds)))


def test_voice_session_enrol_and_recognise():
    import asyncio

    from voicebot.speakers import BYTES_PER_SECOND, VoiceSession

    voices = _FakeVoices()
    one_second = bytes([1, 0]) * (BYTES_PER_SECOND // 2)

    async def run():
        session = VoiceSession(voices, lambda person: f"{person}'s own memory: likes tea")
        await session.add_turn(one_second * 3)
        first = await session.remember_voice("костя")
        await session.add_turn(one_second * 4)
        second = await session.remember_voice("костя")
        later = VoiceSession(voices, lambda person: f"{person}'s own memory: likes tea")
        await later.add_turn(one_second * 2)
        return first, second, later

    first, second, later = asyncio.run(run())
    assert first["status"] == "need more speech"
    assert second["status"] == "voice saved" and voices.saved == [("Костя", 7)]
    assert later.speaker == "Костя"
    intro = later.introduction()
    assert "Костя" in intro and "likes tea" in intro and later.introduction() is None


def test_voice_session_cuts_sentences_from_recording():
    from voicebot.speakers import BYTES_PER_SECOND, RECORD_SECONDS, VoiceSession

    session = VoiceSession(_FakeVoices())
    session.record(b"\x00\x00" * (BYTES_PER_SECOND // 2) * (RECORD_SECONDS + 10))  # 70 s, first 10 s dropped
    session.record(b"\x07\x00" * (BYTES_PER_SECOND // 2))  # second 70..71
    cut = session.audio_between(70_000, 71_000)
    assert len(cut) == BYTES_PER_SECOND and cut[:2] == b"\x07\x00"
    assert session.audio_between(1_000, 2_000) == b""  # already dropped


def test_remember_goes_to_the_speaker(tmp_path):
    from types import SimpleNamespace

    from voicebot.memory import Memory
    from voicebot.tools.memory_tools import make_tools

    memory = Memory(str(tmp_path))
    current = SimpleNamespace(speaker="Kostya")
    remember = next(t for t in make_tools(memory, current) if t.name == "remember").handler
    assert remember("Kostya likes tea.")["for"] == "Kostya"
    assert remember("The Wi-Fi is called KL_Link.", shared=True)["for"] == "everyone"


def test_speech_not_for_the_bot_is_ignored_then_hangs_up():
    import asyncio
    from types import SimpleNamespace as NS

    from voicebot.brains.base import SessionIO
    from voicebot.brains.voicelive import VoiceLiveBrain
    from voicebot.tools import default_registry
    from voicebot.usage import TokenTotals

    items, responses = [], []

    async def create_item(item):
        items.append(item)

    class FakeConn:
        conversation = NS(item=NS(create=create_item))
        response = NS(create=lambda **kw: responses.append(kw))

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for n in (1, 2):
                yield NS(type="response.function_call_arguments.done", name="ignore_speech", arguments="{}",
                         call_id=f"c{n}")
                yield NS(type="response.done", response=None)
                if n == 1:
                    assert session.end_ready is False and session.ignored_in_row == 1
            await asyncio.sleep(0.01)

    speaker = NS(play=lambda b: None, clear=lambda: None, is_playing=lambda: False)
    session = SessionIO(
        mic=None, speaker=speaker, instructions="", tools=default_registry(), stop=asyncio.Event(),
        model="m", voice="v", thinking_sound=b"tick",
    )
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)
    brain.cfg = NS(transcribe_model="mai-transcribe")
    asyncio.run(brain._receive(FakeConn(), session, TokenTotals()))
    assert session.end_ready is True
    assert session.engaged is False
    assert responses == []
    assert len(items) == 2 and all(isinstance(i.output, str) for i in items)


def test_voice_not_saved_under_someone_elses_name():
    import asyncio

    from voicebot.speakers import BYTES_PER_SECOND, VoiceSession

    alina = [0.0, 1.0, 0.0, 0.0]
    voices = _FakeVoices({"Алина": {"embedding": alina, "seconds": 14}})
    child = bytes([2, 0]) * (BYTES_PER_SECOND // 2)

    async def run():
        session = VoiceSession(voices)
        await session.add_turn(child * 8)
        return await session.remember_voice("Алина")

    result = asyncio.run(run())
    assert "error" in result and voices.saved == []
    assert voices.profiles["Алина"]["embedding"] == alina


def test_enrolment_leaves_out_another_known_person():
    import asyncio

    from voicebot.speakers import BYTES_PER_SECOND, VoiceSession

    voices = _FakeVoices({"Костя": {"embedding": [0.0, 1.0, 0.0, 0.0], "seconds": 13}})
    kostya = bytes([1, 0]) * (BYTES_PER_SECOND // 2)
    child = bytes([2, 0]) * (BYTES_PER_SECOND // 2)

    async def run():
        session = VoiceSession(voices)
        await session.add_turn(kostya * 10)
        first = await session.remember_voice("Настя")
        await session.add_turn(child * 7)
        second = await session.remember_voice("Настя")
        return first, second

    first, second = asyncio.run(run())
    assert first["status"] == "need more speech"
    assert second["status"] == "voice saved" and voices.saved == [("Настя", 7)]


def test_voice_session_follows_who_speaks_each_sentence():
    import asyncio

    from voicebot.speakers import BYTES_PER_SECOND, VoiceSession

    voices = _FakeVoices({"Костя": {"embedding": [0.0, 1.0, 0.0, 0.0], "seconds": 13},
                          "Алина": {"embedding": [0.0, 0.0, 1.0, 0.0], "seconds": 13}})
    second = BYTES_PER_SECOND // 2
    kostya, alina = bytes([1, 0]) * second, bytes([2, 0]) * second

    async def run():
        session = VoiceSession(voices)
        seen = []
        for pcm in (kostya * 3, kostya * 2, alina * 3, kostya * 3, kostya * 2):
            await session.add_turn(pcm)
            seen.append((session.speaker, session.introduction()))
        return session, seen

    session, seen = asyncio.run(run())
    assert [s for s, _ in seen] == ["Костя", "Костя", "Алина", "Костя", "Костя"]
    assert "Greet" in seen[0][1] and seen[1][1] is None and "Алина" in seen[2][1]
    assert "again" in seen[3][1] and seen[4][1] is None
    assert session.turns_by == {"Костя": 4, "Алина": 1}


def test_sentence_with_two_voices_is_not_given_to_either():
    import asyncio

    from voicebot.speakers import BYTES_PER_SECOND, VoiceSession

    voices = _FakeVoices({"Костя": {"embedding": [0.0, 1.0, 0.0, 0.0], "seconds": 13}})
    second = BYTES_PER_SECOND // 2
    kostya, child = bytes([1, 0]) * second, bytes([2, 0]) * second

    async def run():
        session = VoiceSession(voices)
        await session.add_turn(child * 3 + kostya * 3)
        mixed = session.speaker
        await session.add_turn(kostya * 6)
        return mixed, session.speaker

    assert asyncio.run(run()) == (None, "Костя")


def test_old_conversation_items_are_dropped():
    import asyncio
    from types import SimpleNamespace as NS

    from voicebot.brains import voicelive
    from voicebot.brains.base import SessionIO
    from voicebot.brains.voicelive import VoiceLiveBrain
    from voicebot.tools import default_registry
    from voicebot.usage import TokenTotals

    deleted = []

    async def delete(item_id):
        deleted.append(item_id)

    class FakeConn:
        conversation = NS(item=NS(create=None, delete=delete))
        response = NS(create=None)

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for n in range(voicelive.KEEP_ITEMS + 5):
                yield NS(type="conversation.item.created", item=NS(id=f"i{n}"))
            yield NS(type="response.done", response=None)
            await asyncio.sleep(0.02)

    speaker = NS(play=lambda b: None, clear=lambda: None, is_playing=lambda: False)
    session = SessionIO(mic=None, speaker=speaker, instructions="", tools=default_registry(),
                        stop=asyncio.Event(), model="m", voice="v")
    brain = VoiceLiveBrain.__new__(VoiceLiveBrain)
    brain.cfg = NS(transcribe_model="mai-transcribe")
    asyncio.run(brain._receive(FakeConn(), session, TokenTotals()))
    assert deleted == ["i0", "i1", "i2", "i3", "i4"]


def test_conversation_saved_with_who_said_each_sentence(tmp_path):
    from voicebot.memory import Memory

    memory = Memory(str(tmp_path))
    turns = [("user", "I like medium steaks"), ("assistant", "Noted."),
             ("user", "I love Tanjiro"), ("assistant", "Cool!"),
             ("user", "mumble"), ("assistant", "Sorry?")]
    memory.save_session(turns, people={0: "Костя", 2: "Владик"})
    assert [text for _, _, text in memory.recent(person="Костя")] == ["I like medium steaks", "Noted."]
    assert [text for _, _, text in memory.recent()] == ["mumble", "Sorry?"]
    text = memory.prompt_text()
    assert "Костя: I like medium steaks" in text and "Владик: I love Tanjiro" in text and "Someone: mumble" in text
    assert "Tanjiro" not in memory.prompt_text(person="Костя")


class _FakeEmbedder:
    """Meaning = which of a few topic words the text contains."""

    TOPICS = ["sofa", "iphone", "cartoon"]

    def embed(self, texts, kind="passage"):
        rows = []
        for text in texts:
            v = [1.0 if t in text.lower() else 0.0 for t in self.TOPICS] + [0.1]
            n = sum(x * x for x in v) ** 0.5
            rows.append([x / n for x in v])
        return rows


class _FakeQdrant:
    def __init__(self):
        self.points = {}

    def upsert(self, point_id, vector, payload):
        self.points[point_id] = (vector, payload)

    def search(self, vector, limit):
        scored = [{"score": sum(a * b for a, b in zip(vector, v)), "payload": p} for v, p in self.points.values()]
        return sorted(scored, key=lambda h: -h["score"])[:limit]


def test_archive_saves_and_finds_past_conversations(tmp_path):
    import asyncio
    import json

    from voicebot import archive

    store = archive.Archive(str(tmp_path), _FakeEmbedder(), _FakeQdrant())
    store.add(1_700_000_000, ["Костя"], "The dog peed on the sofa; cleaned with vinegar.", "Костя: собака…")
    store.add(1_700_100_000, ["Алина"], "Alina looked for an iPhone 15 on OLX.", "Алина: айфон…")
    tool = archive.search_tool(store)
    out = asyncio.run(tool.handler(query="sofa accident"))
    assert out["found"][0]["summary"].startswith("The dog peed on the sofa")
    assert out["found"][0]["people"] == ["Костя"] and out["closest_transcript"] == "Костя: собака…"
    saved = [json.loads(line) for line in open(tmp_path / "summaries.jsonl", encoding="utf-8")]
    assert [s["people"] for s in saved] == [["Костя"], ["Алина"]] and "transcript" not in saved[0]


def test_archive_transcript_names_speakers():
    from voicebot.archive import transcript_text
    from voicebot.memory import with_people

    rows = with_people([("user", "Hi"), ("assistant", "Hello"), ("user", "mm"), ("assistant", "")], {0: "Костя"})
    assert transcript_text(rows) == "Костя: Hi\nBot: Hello\nSomeone: mm"


def test_past_conversations_rule_only_with_archive(tmp_path):
    with_archive = build_instructions(str(tmp_path / "none.md"), past=True)
    assert "search_memory" in with_archive and "Never search for ordinary questions" in with_archive
    assert "search_memory" not in build_instructions(str(tmp_path / "none.md"))


def test_embedder_matches_meaning_across_languages():
    import os

    import pytest

    model = os.environ.get("E5_MODEL_DIR", "")
    if not os.path.exists(os.path.join(model, "model.onnx")):
        pytest.skip("set E5_MODEL_DIR to the multilingual-e5-small folder to run")
    from voicebot.archive import Embedder

    e = Embedder(model)
    docs = e.embed(["The dog peed on the blue sofa; cleaned it with vinegar and baking soda.",
                    "Alina searched OLX for a used iPhone 15 in Kharkiv."])
    query = e.embed(["что случилось с нашим диваном?"], kind="query")[0]
    scores = [sum(a * b for a, b in zip(query, d)) for d in docs]
    assert scores[0] > scores[1]


def test_world_time_from_local_clock():
    from datetime import datetime, timezone

    from voicebot.tools.clock import world_time

    now = datetime(2026, 11, 14, 22, 50, tzinfo=timezone.utc)  # 00:50 on the 15th in Kyiv
    out = world_time("Pacific/Auckland", "New Zealand", now=now)
    assert out["time_there"] == "11:50" and out["date_there"].startswith("Sunday, 15 November")
    assert "error" in world_time("Middle/Earth")


def test_reminder_times():
    from datetime import datetime

    from voicebot.reminders import next_time, parse_due

    now = datetime(2026, 11, 15, 22, 30)  # a Sunday
    assert parse_due(in_minutes=10, now=now) == datetime(2026, 11, 15, 22, 40)
    assert parse_due("23:00", now=now) == datetime(2026, 11, 15, 23, 0)
    assert parse_due("07:00", now=now) == datetime(2026, 11, 16, 7, 0)
    assert parse_due("2026-11-20 18:30", now=now) == datetime(2026, 11, 20, 18, 30)
    assert parse_due("07:00", repeat="weekends", now=now) == datetime(2026, 11, 21, 7, 0)
    assert next_time(datetime(2026, 11, 20, 7, 0), "weekdays") == datetime(2026, 11, 23, 7, 0)
    for bad in ({"at": "2020-01-01 10:00"}, {"at": "soon"}, {"in_minutes": 0}):
        try:
            parse_due(now=now, **bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_reminders_tool_add_list_cancel_and_finish(tmp_path):
    import asyncio
    from types import SimpleNamespace as NS

    from voicebot.reminders import Reminders, make_tool

    store = Reminders(str(tmp_path / "reminders.json"))
    made = []

    async def make_audio(item):
        made.append(item.id)
        store.save_audio(item, b"\x01\x00")

    tool = make_tool(store, make_audio)

    async def run():
        timer = await tool.handler(action="add", kind="timer", text="Макароны готовы.", label="pasta", in_minutes=10)
        alarm = await tool.handler(action="add", kind="alarm", text="Пора вставать.", at="07:00", repeat="weekdays")
        bad = await tool.handler(action="add", kind="timer", text="", in_minutes=5)
        await asyncio.sleep(0.01)
        listed = await tool.handler(action="list")
        return timer, alarm, bad, listed

    timer, alarm, bad, listed = asyncio.run(run())
    assert timer["status"] == "set" and "in 10 min" in timer["when"] and "error" in bad
    assert [i["kind"] for i in listed["items"]] == ["timer", "alarm"] and store.items[0].person is None
    assert made == [1, 2] and store.audio(store.items[0]) == b"\x01\x00"

    again = Reminders(str(tmp_path / "reminders.json"))
    assert [r.text for r in again.upcoming()] == ["Макароны готовы.", "Пора вставать."]
    first_due = store.items[1].due
    store.finished(store.items[1])  # repeating: moves to the next weekday
    assert store.items[1].due > first_due and len(store.items) == 2
    store.finished(store.items[0])  # one-off: removed with its audio
    assert [r.kind for r in store.items] == ["alarm"] and not (tmp_path / "reminders" / "1.pcm").exists()
    assert asyncio.run(tool.handler(action="cancel", id=2))["status"] == "cancelled" and store.items == []


def _telegram_bot(tmp_path, owner=1, spoken=None):
    import asyncio
    import datetime as dt

    from voicebot import telegram
    from voicebot.settings import RuntimeSettings

    sent = []

    class FakeApi:
        def call(self, method, **params):
            sent.append((method, params))
            return {}

    async def announce(text, sender):
        if spoken is not None:
            spoken.append((sender, text))

    settings = RuntimeSettings(str(tmp_path / "state.json"), ["marin", "cedar"], ["mini", "smart"])
    home = telegram.Home(settings=settings, announce=announce, cost_today=lambda: 0.42,
                         summaries=lambda day: [{"date": f"{day:%Y-%m-%d} 14:05", "people": ["Костя"],
                                                 "summary": "The dog peed on the sofa."}],
                         voice_name=str.title, known_names=lambda: ["Костя", "Алина"], basic_model="mini",
                         over_budget=lambda: False)
    bot = telegram.Bot(FakeApi(), telegram.Chats(str(tmp_path / "telegram.json"), owner), home, "21:00")
    return bot, sent, settings, asyncio, dt


def _texts(sent):
    return [p["text"] for m, p in sent if m == "sendMessage"]


def _msg(chat, text, date=None):
    import time
    return {"message": {"chat": {"id": chat, "type": "private"}, "from": {"first_name": "X"}, "text": text,
                        "date": date or time.time()}}


def test_telegram_stranger_gets_id_and_owner_is_told(tmp_path):
    bot, sent, _, asyncio, _ = _telegram_bot(tmp_path)
    asyncio.run(bot.handle(_msg(55, "hi")))
    asyncio.run(bot.handle(_msg(55, "hi again")))
    texts = _texts(sent)
    assert len(texts) == 2 and "55" in texts[0] and "/allow 55" in texts[1]
    assert [p["chat_id"] for m, p in sent] == [55, 1]


def test_telegram_intercom_and_old_messages(tmp_path):
    spoken = []
    bot, sent, _, asyncio, _ = _telegram_bot(tmp_path, spoken=spoken)
    bot.chats.allow(7, "Алина")
    asyncio.run(bot.handle(_msg(7, "Ужин готов")))
    asyncio.run(bot.handle(_msg(7, "Old one", date=1)))
    assert spoken == [("Алина", "Ужин готов")]
    assert "Read aloud" in _texts(sent)[0] and "offline" in _texts(sent)[1]


def test_telegram_owner_settings(tmp_path):
    bot, sent, settings, asyncio, _ = _telegram_bot(tmp_path)
    asyncio.run(bot.handle(_msg(1, "/budget 2,5")))
    assert settings.budget == 2.5
    asyncio.run(bot.handle(_msg(1, "/voice")))
    markup = sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert markup[0][1]["callback_data"] == "voice:1"
    asyncio.run(bot.handle({"callback_query": {"id": "q", "data": "voice:1",
                                               "message": {"chat": {"id": 1}, "message_id": 9}}}))
    assert settings.voice == "cedar"
    bot.chats.allow(7, "Алина")
    asyncio.run(bot.handle(_msg(7, "/budget 100")))
    assert settings.budget == 2.5 and "Only the owner" in _texts(sent)[-1]
    asyncio.run(bot.handle(_msg(7, "/today")))
    assert "dog peed" in _texts(sent)[-1] and "$0.42" in _texts(sent)[-1]


def test_send_to_telegram_tool_picks_speaker_and_expands_links(tmp_path):
    from voicebot import telegram
    from voicebot.links import LINKS

    bot, sent, _, asyncio, _ = _telegram_bot(tmp_path)
    bot.chats.allow(7, "Алина")

    class Current:
        speaker = "Алина"

    tool = telegram.send_tool(bot, Current())
    ref = LINKS.add("https://example.com/car", "BMW X5 2019")
    result = asyncio.run(tool.handler(text="Вот машина", links=[ref, "L99999"]))
    assert result["status"] == "sent" and result["to"] == "Алина" and result["missing_links"] == ["L99999"]
    assert sent[-1][1]["chat_id"] == 7 and "https://example.com/car" in sent[-1][1]["text"]
    Current.speaker = "Вадик"
    result = asyncio.run(tool.handler(text="hi"))
    assert sent[-1][1]["chat_id"] == 1 and "Вадик has no Telegram" in result["note"]
    assert asyncio.run(tool.handler(text="hi", to="Alina"))["to"] == "Alina"
    assert "error" in asyncio.run(tool.handler(text="hi", to="Nastya"))


def test_link_book_reuses_and_forgets():
    from voicebot.links import LinkBook

    book = LinkBook(keep=2)
    a = book.add("https://a", "A")
    assert book.add("https://a") == a and book.add(None) is None
    book.add("https://b")
    book.add("https://c")
    assert book.get(a) is None and book.get(a.lower()) is None


def test_daily_usage_survives_restart(tmp_path):
    from voicebot.usage import DailyUsage

    path = str(tmp_path / "usage.json")
    first = DailyUsage(path)
    first.add(0.5)
    first.warned = True
    first.save()
    again = DailyUsage(path)
    assert again.today() == 0.5 and again.warned


def test_split_long_telegram_text():
    from voicebot.telegram import split_text

    parts = split_text("line\n" * 2000, limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts).count("line") == 2000


def test_telegram_reminders_list_next_and_past(tmp_path):
    from datetime import datetime, timedelta

    from voicebot import reminders, telegram

    store = reminders.Reminders(str(tmp_path / "reminders.json"))
    soon = store.add("alarm", datetime.now() + timedelta(hours=1), "Пора вставать", repeat="daily")
    done = store.add("timer", datetime.now() + timedelta(minutes=1), "Макароны готовы")
    store.finished(done)
    store.finished(store.add("reminder", datetime.now() + timedelta(minutes=2), "Позвонить маме"), missed=True)
    text = telegram.reminders_text(reminders.Reminders(str(tmp_path / "reminders.json")))
    next_part, past_part = text.split("Past:")
    assert "Пора вставать" in next_part and "every day" in next_part and soon.text not in past_part
    assert past_part.index("Позвонить маме") < past_part.index("Макароны") and "missed" in past_part


def test_telegram_menu_for_everyone_and_owner(tmp_path):
    bot, sent, _, _, _ = _telegram_bot(tmp_path)
    bot._set_menu()
    menus = [p for m, p in sent if m == "setMyCommands"]
    assert len(menus) == 2 and "scope" not in menus[0] and menus[1]["scope"]["chat_id"] == 1
    assert {"reminders", "help"} <= {c["command"] for c in menus[0]["commands"]}
    assert "budget" in {c["command"] for c in menus[1]["commands"]}


def test_telegram_remove_reminder_by_button(tmp_path):
    from datetime import datetime, timedelta

    from voicebot import reminders

    bot, sent, _, asyncio, _ = _telegram_bot(tmp_path)
    store = reminders.Reminders(str(tmp_path / "reminders.json"))
    bot.home.reminders = store
    item = store.add("timer", datetime.now() + timedelta(minutes=5), "Макароны готовы", label="pasta")
    bot.chats.allow(7, "Алина")
    asyncio.run(bot.handle(_msg(7, "/reminders")))
    button = sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == f"cancel:{item.id}" and "pasta" in button["text"]
    asyncio.run(bot.handle({"callback_query": {"id": "q", "data": button["callback_data"],
                                               "message": {"chat": {"id": 7}, "message_id": 3}}}))
    assert store.upcoming() == []
    assert "Removed" in sent[-2][1]["text"] and "nothing set" in sent[-1][1]["text"]
    asyncio.run(bot.handle({"callback_query": {"id": "q", "data": "cancel:1",
                                               "message": {"chat": {"id": 99}, "message_id": 3}}}))
    assert sent[-1][1].get("text") is None or "Removed" not in sent[-1][1].get("text", "")


def test_telegram_wake_mode_saved_and_applied(tmp_path):
    from voicebot.settings import RuntimeSettings

    bot, sent, settings, asyncio, _ = _telegram_bot(tmp_path)
    applied = []
    bot.home.apply_wake = applied.append
    asyncio.run(bot.handle(_msg(1, "/wake")))
    rows = sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["text"].startswith("✓") and rows[1][0]["callback_data"] == "wake:1"
    asyncio.run(bot.handle({"callback_query": {"id": "q", "data": "wake:1",
                                               "message": {"chat": {"id": 1}, "message_id": 2}}}))
    assert applied == ["russian"] and settings.wake == "russian"
    assert RuntimeSettings(str(tmp_path / "state.json"), ["marin"], ["mini"]).wake == "russian"


def test_wake_listener_modes():
    from voicebot.wake import WakeListener

    class Detector:
        def __init__(self, result):
            self.result, self.fed = result, 0

        def feed(self, frame):
            self.fed += 1
            return self.result

    english, russian = Detector(0.99), Detector(None)
    listener = WakeListener(lambda name: None, english, russian)
    assert listener._detect(b"x") == "hey jarvis"
    listener.set_mode("russian")
    assert listener._detect(b"x") is None and english.fed == 1 and russian.fed == 1
    listener.set_mode("off")
    assert listener._detect(b"x") is None and russian.fed == 1


def test_settings_keep_a_voice_per_model_family(tmp_path):
    from voicebot.settings import RuntimeSettings

    path = str(tmp_path / "state.json")
    s = RuntimeSettings(path, ["marin", "cedar"], ["mini", "smart", "gpt-live-1"], ["marin", "quartz", "ripple"])
    s.set_voice("cedar")
    s.set_model("gpt-live-1")
    assert s.voices == ["marin", "quartz", "ripple"] and s.voice == "marin"
    s.set_voice("quartz")
    assert s.voice_for("mini") == "cedar" and s.voice_for("gpt-live-1") == "quartz"
    again = RuntimeSettings(path, ["marin", "cedar"], ["mini", "smart", "gpt-live-1"], ["marin", "quartz", "ripple"])
    assert again.model == "gpt-live-1" and again.voice == "quartz" and again.voice_for("smart") == "cedar"
    plain = RuntimeSettings(str(tmp_path / "other.json"), ["marin"], ["mini"])
    assert plain.voice == "marin" and plain.voices == ["marin"]


def test_gptlive_helpers():
    import numpy as np

    from voicebot.brains.gptlive import Resampler, backend_cost, chunks, live_url

    assert live_url("https://kosti-x-swedencentral.cognitiveservices.azure.com/") == \
        "wss://kosti-x-swedencentral.openai.azure.com/openai/v1/live/sessions"
    assert live_url("https://a.openai.azure.com") == "wss://a.openai.azure.com/openai/v1/live/sessions"
    resample = Resampler(16000)
    out = b"".join(resample(np.full(320, 1000, dtype=np.int16).tobytes()) for _ in range(50))
    assert abs(len(out) // 2 - 24000) <= 2 and set(np.frombuffer(out, dtype=np.int16)[10:]) == {1000}
    assert all(len(p) <= 100 for p in chunks("word " * 200, 100)) and chunks("") == []
    usage = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 400}, "output_tokens": 100}
    assert round(backend_cost("gpt-5-mini", usage), 8) == round((600 * 0.25 + 400 * 0.025 + 100 * 2.0) / 1e6, 8)


def test_gptlive_runs_backend_function_calls_and_continues(tmp_path):
    import asyncio
    import json

    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import GptLiveBrain, _State
    from voicebot.tools import Tool, ToolRegistry
    from voicebot.usage import DailyUsage

    class Speaker:
        mic_rate = 16000

        def play(self, pcm):
            pass

        def clear(self):
            pass

        def is_playing(self):
            return False

    registry = ToolRegistry()
    registry.register(Tool("weather", "w", {"type": "object", "properties": {}}, lambda location="": {"t": 5}))
    s = SessionIO(mic=None, speaker=Speaker(), instructions="hi", tools=registry, stop=None, model="gpt-live-1",
                  voice="marin")
    brain = GptLiveBrain(None, DailyUsage(), "gpt-live-1", "https://x.cognitiveservices.azure.com", "k", "gpt-5-mini")
    sent = []

    class FakeWs:
        closed = False

        async def send_str(self, text):
            sent.append(json.loads(text))

    async def main():
        state = _State(s)
        state.ws = FakeWs()
        config = brain._session_config(s)
        assert config["delegation"]["responses"]["tools"][0]["name"] == "weather"
        await brain._on_backend(state, "d1", {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "c1", "name": "weather", "arguments": '{"location": "Kyiv"}'}})
        await brain._on_backend(state, "d1", {"type": "response.completed", "response": {
            "usage": {"input_tokens": 100, "output_tokens": 10}}})
        await asyncio.gather(*state.background)
        return state

    state = asyncio.run(main())
    assert [e["type"] for e in sent] == ["response.item.create", "response.create"]
    assert sent[0]["item"]["call_id"] == "c1" and json.loads(sent[0]["item"]["output"])["t"] == 5
    assert state.backend_cost > 0


def test_gptlive_pieces_transcript_fragments_into_turns():
    import time

    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import GptLiveBrain, _State
    from voicebot.usage import DailyUsage

    class Speaker:
        mic_rate = 16000

        def is_playing(self):
            return False

    s = SessionIO(mic=None, speaker=Speaker(), instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    brain = GptLiveBrain(None, DailyUsage(), "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")
    state = _State(s)
    for piece in ["Привет, ", "как дела?"]:
        state.user.add({"delta": piece})
    brain._finish_turns(state)
    assert s.history == []
    state.user.last = time.monotonic() - 5
    brain._finish_turns(state)
    state.bot.add({"delta": "Отлично. Пока!"})
    brain._finish_turns(state, force=True)
    assert s.history == [("user", "Привет, как дела?"), ("assistant", "Отлично. Пока!")]
    assert state.language == "Russian"


def test_goodbye_at_the_end_of_a_longer_turn():
    from voicebot.goodbye import is_goodbye

    assert is_goodbye("Да, да, добре, я зрозумів. Добре, все зрозуміло, тоді бувай")
    assert is_goodbye("Okay, I got it. Thanks, bye!")
    assert not is_goodbye("Скажи, какая погода будет завтра, пока я одеваюсь")
    assert not is_goodbye("Расскажи сказку. Пока не надо про драконов?")


def test_gptlive_hangs_up_when_nobody_talks_to_it():
    import time

    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import NOT_ADDRESSED_SECONDS, GptLiveBrain, _State

    s = SessionIO(mic=None, speaker=None, instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    state = _State(s)
    now = time.monotonic()
    state.last_bot_activity = now - NOT_ADDRESSED_SECONDS - 1
    assert not GptLiveBrain._not_addressed(state, now)  # nobody spoke since: plain silence is the app's job
    state.user.last = now - 0.5
    assert GptLiveBrain._not_addressed(state, now)
    state.last_bot_activity = now - 5
    assert not GptLiveBrain._not_addressed(state, now)


def test_gptlive_silent_audio_is_not_activity():
    import numpy as np

    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import GptLiveBrain, _State, is_silent

    played = []

    class Speaker:
        def play(self, pcm):
            played.append(pcm)

    s = SessionIO(mic=None, speaker=Speaker(), instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    state = _State(s)
    quiet = np.zeros(2400, dtype=np.int16).tobytes()
    loud = (np.sin(np.arange(2400) / 5) * 3000).astype(np.int16).tobytes()
    assert is_silent(quiet) and not is_silent(loud)
    brain = GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")
    before = state.last_bot_activity
    brain._play(state, {"delta": __import__("base64").b64encode(quiet).decode()})
    assert played == [] and state.last_bot_activity == before and not s.engaged
    brain._play(state, {"delta": __import__("base64").b64encode(loud).decode()})
    assert played == [loud] and s.engaged and state.spoken_seconds == 0.1


def test_gptlive_reminds_it_to_delegate_when_it_only_says_it_is_checking():
    import asyncio
    import json
    import time

    from voicebot.brains import gptlive
    from voicebot.brains.base import SessionIO

    class Speaker:
        def is_playing(self):
            return False

    s = SessionIO(mic=None, speaker=Speaker(), instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    brain = gptlive.GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")
    sent = []

    class FakeWs:
        async def send_str(self, text):
            sent.append(json.loads(text))

    async def main(delegate):
        state = gptlive._State(s)
        state.ws = FakeWs()
        state.user.add({"delta": "What's the weather?"})
        brain._finish_user(state)
        state.bot.add({"delta": "Sure, just a sec."})
        state.bot.first = time.monotonic() - gptlive.NUDGE_SECONDS
        brain._finish_bot(state)
        if delegate:
            state.last_tool_call = time.monotonic()
        await asyncio.gather(*state.background)

    asyncio.run(main(delegate=False))
    assert [e["type"] for e in sent] == ["session.instructions.append"]
    sent.clear()
    asyncio.run(main(delegate=True))
    assert sent == []


def test_gptlive_backend_thinks_little():
    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import GptLiveBrain

    s = SessionIO(mic=None, speaker=None, instructions="x", tools=None, stop=None, model="gpt-live-1", voice="m")
    config = GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")._session_config(s)
    assert config["delegation"]["responses"]["reasoning"] == {"effort": "low"}
    other = GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-4.1-mini")._session_config(s)
    assert "reasoning" not in other["delegation"]["responses"]


def test_gptlive_backend_text():
    from voicebot.brains.gptlive import backend_text

    response = {"output": [{"type": "reasoning"}, {"type": "message", "content": [
        {"type": "output_text", "text": "Сегодня в кино идёт «Аватар»."}]}]}
    assert backend_text(response) == "Сегодня в кино идёт «Аватар»." and backend_text({}) == ""


def test_gptlive_hangs_up_after_its_own_goodbye():
    import time

    from voicebot.brains.base import SessionIO
    from voicebot.brains.gptlive import GptLiveBrain, _State

    s = SessionIO(mic=None, speaker=None, instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    state = _State(s)
    brain = GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")
    state.bot.add({"delta": "Есть сеансы в 18:00."})
    brain._finish_bot(state)
    assert not state.goodbye_at
    state.bot.add({"delta": "Окей. Пока!"})
    brain._finish_bot(state)
    assert 0 < state.goodbye_at <= time.monotonic()


def test_gptlive_no_reminder_when_the_backend_answered_without_tools():
    import asyncio
    import json
    import time

    from voicebot.brains import gptlive
    from voicebot.brains.base import SessionIO

    class Speaker:
        def is_playing(self):
            return False

    s = SessionIO(mic=None, speaker=Speaker(), instructions="", tools=None, stop=None, model="gpt-live-1", voice="m")
    brain = gptlive.GptLiveBrain(None, None, "gpt-live-1", "https://x.openai.azure.com", "k", "gpt-5-mini")
    sent = []

    class FakeWs:
        async def send_str(self, text):
            sent.append(json.loads(text))

    async def main():
        state = gptlive._State(s)
        state.ws = FakeWs()
        state.user.add({"delta": "Умножь 233 на 147"})
        brain._finish_user(state)
        state.bot.add({"delta": "Секунду, посчитаю."})
        state.bot.first = time.monotonic() - gptlive.NUDGE_SECONDS
        brain._finish_bot(state)
        await brain._on_backend(state, "d", {"type": "response.output_text.delta", "delta": "34 251"})
        await brain._on_backend(state, "d", {"type": "response.completed", "response": {}})
        await asyncio.gather(*state.background)

    asyncio.run(main())
    assert sent == []
