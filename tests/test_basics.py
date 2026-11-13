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
    assert default_registry().names() == []
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    reg = default_registry()
    assert reg.names() == ["web_search"]
    assert reg.specs()[0]["parameters"]["required"] == ["query"]



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
    assert detect_language("Привет, как дела") is None
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

        async def _respond(self):
            responses.append(True)

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for delay, event in self.events:
                await asyncio.sleep(delay)
                yield event
            await asyncio.sleep(0.3)

    events = [
        (0, NS(type="input_audio_buffer.speech_stopped")),
        (0, NS(type="response.function_call_arguments.done", name="web_search", arguments='{"query": "q"}', call_id="c1")),
        (0, NS(type="response.done", response=None)),
        (0.05, NS(type="conversation.item.input_audio_transcription.completed", transcript="What's new in Ukraine?")),
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
    output = json.loads(created[0]["output"])
    assert output["answer"] == "news" and output["reply_language"] == "English"
    assert responses == [True]



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
