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
