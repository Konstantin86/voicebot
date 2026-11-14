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
    assert voice_display_name("en-US-Ava:DragonHDLatestNeural") == "Ava"
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
    assert default_registry().names() == ["end_session", "weather"]
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    reg = default_registry()
    assert reg.names() == ["end_session", "weather", "web_search", "price_lookup"]
    assert reg.specs()[2]["parameters"]["required"] == ["query"]



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
    assert m.remember("The cat is called Murka.") == {"status": "remembered", "id": 1}
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
    assert "Recent conversations" in text and "User: Thanks" in text and "Cedar" not in text


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
