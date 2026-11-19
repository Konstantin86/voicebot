"""GPT-Live brain: a full-duplex voice model (listens while it speaks) with a separate backend for tools.

The live model holds the conversation. When something needs a tool (TV, search, memory, reminders...) it hands
the task to a backend text model through the Responses API ("Responses delegation"); the backend's function
calls come back here and run on the Pi with the same tools as the Voice Live brain.

Protocol: WebSocket to <resource>.openai.azure.com/openai/v1/live/sessions. Audio is raw PCM16 mono at 24 kHz
both ways; the mic (16 kHz) is resampled here. There are no turn events: transcripts arrive as fragments, so
user and assistant turns are pieced together by timing.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import time
from typing import Any

import aiohttp
import numpy as np

from ..goodbye import is_goodbye
from ..language import add_language_hint, detect_language
from ..tools.memory_tools import END_SESSION, IGNORE_SPEECH
from .base import SessionIO

log = logging.getLogger(__name__)

LIVE_RATE = 24000
MIC_FRAMES_PER_MESSAGE = 2
TURN_GAP_SECONDS = 1.2  # no new transcript fragment for this long = that speaker's turn is over
NOT_ADDRESSED_SECONDS = 30  # people keep talking but the bot has not answered or worked for this long: hang up
GOODBYE_WAIT_SECONDS = 8  # after "bye", hang up once the reply ends, or after this long
VOICE_WAIT_SECONDS = 1.0
SPEECH_LEAD_MS = 1500  # fragments arrive after the words were said
MAX_GAP_FILL_MS = 1500  # silence put back between output pieces (the service leaves it out)
CLOSE_WAIT_SECONDS = 3
SAY_DONE_SECONDS = 0.8  # a voice sample is complete after this much silence
SILENT_LEVEL = 60
NUDGE_SECONDS = 4  # it said it would check, but handed nothing to the backend for this long: remind it
WAITING_WORDS = re.compile(
    r"\b(sec|second|moment|check|checking|look it up|let me see|one minute)\b|секунд|момент|провер|гляну|"
    r"посмотр|уточн|перевір|подивл|хвилин|минутк", re.IGNORECASE)
NUDGE = ("You told the user you are checking, but nothing is running: no tool or search was used. "
         "Delegate the user's request now and ask the backend to use its tools. "
         "Never say you are checking unless a task is running.")  # average loudness of a PCM16 piece below which it counts as silence
CONTEXT_CHARS = 1200  # one quiet-context message (the limit is 500 tokens)
# Backend speed: a reasoning model thinks before and after each tool, which the user hears as "let me check".
BACKEND_EFFORT = os.environ.get("GPTLIVE_BACKEND_EFFORT", "low")  # "minimal" stopped calling tools  # minimal, low, medium, high; empty = model's
PRICE_PER_MINUTE = float(os.environ.get("GPTLIVE_PRICE_PER_MINUTE", "0.05"))
# Backend text model, USD per 1M tokens: (input, cached input, output)
BACKEND_PRICES = {"gpt-5-mini": (0.25, 0.025, 2.0), "gpt-5-nano": (0.05, 0.005, 0.4), "gpt-5": (1.25, 0.125, 10.0),
                  "gpt-4.1-mini": (0.4, 0.1, 1.6)}
SKIPPED_TOOLS = {IGNORE_SPEECH}  # the live model decides itself when not to answer

LIVE_NOTES = """
How this conversation works:
- You hear the room all the time and can speak while others talk. Answer only when someone talks to you;
  stay silent for people talking to each other, children playing or the TV. Where these instructions say
  to call ignore_speech, just stay silent instead.
- You cannot use tools yourself. For anything that needs one, delegate it: TV, timers, reminders, alarms,
  weather, time abroad, web search, prices, cars, OLX, memory (remember/forget/what do you know),
  voices, past conversations, Telegram, and ending the conversation when someone says goodbye.
  Say a few words while it works only if it takes long; never invent the result. Never say you are
  checking something unless you have delegated it.
- Messages that start with "Context:" are silent notes from the system (who is speaking, their memory). They are
  not something anyone said: never reply to them, never greet because of them, never restart or repeat what you
  were saying. Just use them from now on.
"""

BACKEND_NOTES = """
You are the backend of the voice assistant described above. A live voice model talks to the family and hands
you tasks. Use the tools to do them: for anything current (cinema, events, news, prices, schedules, weather)
call the tool first. Never answer with only a promise to check, and ask a question back only when no tool can
help. Your text answer is spoken by the voice model: one or two short sentences in the user's language with the
result, no lists, no links, no markdown. If someone said goodbye, call end_session.
"""


def live_url(endpoint: str) -> str:
    """'https://name.cognitiveservices.azure.com/' (or any host of the resource) -> the GPT-Live WebSocket URL."""
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    if not host.endswith(".openai.azure.com"):
        host = host.split(".", 1)[0] + ".openai.azure.com"
    return f"wss://{host}/openai/v1/live/sessions"


class Resampler:
    """16 kHz -> 24 kHz PCM16, linear, keeping the last sample so chunks join smoothly."""

    def __init__(self, rate_in: int, rate_out: int = LIVE_RATE):
        self.step = rate_in / rate_out
        self.last = 0.0
        self.pos = 0.0  # position of the next output sample, in input samples after `last`

    def __call__(self, pcm: bytes) -> bytes:
        if self.step == 1:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        joined = np.concatenate(([self.last], samples))
        points = np.arange(self.pos, len(samples), self.step)
        out = np.interp(points, np.arange(len(joined)), joined)
        self.pos = points[-1] + self.step - len(samples) if len(points) else self.pos - len(samples)
        self.last = float(samples[-1]) if len(samples) else self.last
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def is_silent(pcm: bytes) -> bool:
    samples = np.frombuffer(pcm[: len(pcm) & ~1], dtype=np.int16).astype(np.float32)
    return not len(samples) or float(np.sqrt(np.mean(samples * samples))) < SILENT_LEVEL


def backend_text(response: dict) -> str:
    """The backend's text answer from a finished Responses response."""
    parts = []
    for item in response.get("output") or []:
        for content in item.get("content") or [] if item.get("type") == "message" else []:
            if content.get("type") == "output_text":
                parts.append(content.get("text") or "")
    return " ".join(parts).strip()


def backend_cost(model: str, usage: dict) -> float:
    prices = BACKEND_PRICES.get(model)
    if not prices or not usage:
        return 0.0
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    fresh = (usage.get("input_tokens", 0) or 0) - cached
    return (fresh * prices[0] + cached * prices[1] + (usage.get("output_tokens", 0) or 0) * prices[2]) / 1_000_000


def chunks(text: str, size: int = CONTEXT_CHARS) -> list[str]:
    """Splits long text into pieces of at most `size` characters, at line ends where possible."""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    parts.append(text.strip())
    return [p for p in parts if p]


class _Speech:
    """Transcript fragments of one speaker, pieced into turns."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.first = self.last = 0.0  # monotonic time of the first and latest fragment
        self.start_ms: int | None = None
        self.end_ms: int | None = None

    def add(self, event: dict) -> None:
        now = time.monotonic()
        if not self.parts:
            self.first = now
            self.start_ms = event.get("start_ms")
        self.parts.append(event.get("delta") or "")
        self.last = now
        self.end_ms = event.get("end_ms", self.end_ms)

    def take(self) -> str:
        text = "".join(self.parts).strip()
        self.parts = []
        return text


class GptLiveBrain:
    def __init__(self, cfg, today, live_model: str, endpoint: str, api_key: str, backend: str):
        self.cfg = cfg
        self.today = today  # usage.DailyUsage, shared with the main brain
        self.live_model = live_model
        self.url = live_url(endpoint)
        self.api_key = api_key
        self.backend = backend

    async def run_session(self, s: SessionIO) -> None:
        state = _State(s)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(self.url, headers={"api-key": self.api_key}, heartbeat=20,
                                           max_msg_size=0) as ws:
                    state.ws = ws
                    await self._send(state, {"type": "session.start", "session": self._session_config(s)})
                    tasks = {asyncio.create_task(self._receive(state)), asyncio.create_task(s.stop.wait()),
                             asyncio.create_task(self._watch(state))}
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    await self._close(state)
                    for task in pending | state.background:
                        task.cancel()
                    await asyncio.gather(*pending, *state.background, return_exceptions=True)
                    for task in done:
                        if not task.cancelled() and task.exception():
                            raise task.exception()
        finally:
            self._finish_turns(state, force=True)
            voice_cost = state.seconds / 60 * PRICE_PER_MINUTE
            log.info("session usage (%s): %.0f s of voice, est. $%.4f; backend %s: est. $%.4f; "
                     "bot audio: %.0f s speech, %.0f s silence", self.live_model, state.seconds, voice_cost,
                     self.backend, state.backend_cost, state.spoken_seconds, state.silent_seconds)
            log.info("today so far: est. $%.4f", self.today.add(voice_cost + state.backend_cost))

    async def say(self, text: str, voice: str, timeout: float = 15) -> bytes:
        """Speaks one phrase in a GPT-Live voice and returns the PCM16 audio (for voice samples)."""
        audio = bytearray()

        async def feed_silence(ws) -> None:
            """GPT-Live moves its timeline on with incoming audio, so it gets quiet 'microphone' audio."""
            chunk = base64.b64encode(bytes(int(LIVE_RATE * 0.04) * 2)).decode("ascii")
            while True:
                await ws.send_str(json.dumps({"type": "session.input_audio.append", "audio": chunk}))
                await asyncio.sleep(0.04)

        async def receive(ws, background: set) -> None:
            last_sound = None
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    return
                event = json.loads(message.data)
                kind = event.get("type")
                if kind == "session.started":
                    background.add(asyncio.create_task(feed_silence(ws)))
                    await ws.send_str(json.dumps({"type": "session.commentary.append", "event_id": "say",
                                                  "delegation_id": None, "content": f"Say exactly: {text}"},
                                                 ensure_ascii=False))
                elif kind == "session.output_audio.delta":
                    pcm = base64.b64decode(event.get("delta") or "")
                    if not is_silent(pcm):
                        audio.extend(pcm)
                        last_sound = time.monotonic()
                    elif last_sound and time.monotonic() - last_sound > SAY_DONE_SECONDS:
                        return
                elif kind == "error":
                    raise RuntimeError((event.get("error") or {}).get("message", "error"))

        background: set[asyncio.Task] = set()
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(self.url, headers={"api-key": self.api_key}, max_msg_size=0) as ws:
                await ws.send_str(json.dumps({"type": "session.start", "event_id": "start", "session": {
                    "model": self.live_model, "audio": {"output": {"voice": voice}},
                    "instructions": "You only say the phrases you are given, exactly, warmly and naturally. "
                                    "Never add anything and never reply to sounds.",
                }}))
                try:
                    await asyncio.wait_for(receive(ws, background), timeout)
                except asyncio.TimeoutError:
                    if not audio:
                        raise RuntimeError(f"no speech in {timeout:.0f} s") from None
                finally:
                    for task in background:
                        task.cancel()
                with contextlib.suppress(Exception):
                    await ws.send_str(json.dumps({"type": "session.close", "event_id": "close"}))
        log.info("gpt-live: made a %.1f s sample of voice %s", len(audio) / 2 / LIVE_RATE, voice)
        return bytes(audio)

    def _session_config(self, s: SessionIO) -> dict[str, Any]:
        tools = [t for t in s.tools.specs() if t["name"] not in SKIPPED_TOOLS] if s.tools else []
        delegation: dict[str, Any] = {"type": "responses", "responses": {
            "model": self.backend,
            "instructions": s.instructions + "\n" + BACKEND_NOTES,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }}
        if BACKEND_EFFORT and self.backend.startswith(("gpt-5", "o")):
            delegation["responses"]["reasoning"] = {"effort": BACKEND_EFFORT}
            delegation["responses"]["text"] = {"verbosity": "low"}
        return {
            "model": s.model,
            "instructions": s.instructions + "\n" + LIVE_NOTES,
            "audio": {"output": {"voice": s.voice}},
            "delegation": delegation,
        }

    async def _send(self, state: "_State", event: dict) -> None:
        state.sent += 1
        event.setdefault("event_id", f"e{state.sent}")
        await state.ws.send_str(json.dumps(event, ensure_ascii=False))

    async def _close(self, state: "_State") -> None:
        if state.ws is None or state.ws.closed or state.closed.is_set():
            return
        try:
            await self._send(state, {"type": "session.close"})
            await asyncio.wait_for(state.closed.wait(), CLOSE_WAIT_SECONDS)
        except Exception as e:
            log.info("gpt-live: close: %s", e)

    async def _send_mic(self, state: "_State") -> None:
        s = state.s
        resample = Resampler(s.speaker.mic_rate)
        while True:
            frames = [await s.mic.get()]
            while len(frames) < MIC_FRAMES_PER_MESSAGE and not s.mic.empty():
                frames.append(s.mic.get_nowait())
            raw = b"".join(frames)
            if s.voice_session:
                s.voice_session.record(raw)
            if state.mic_started is None:
                state.mic_started = time.monotonic()
            pcm = resample(raw)
            await self._send(state, {"type": "session.input_audio.append",
                                     "audio": base64.b64encode(pcm).decode("ascii")})

    async def _context(self, state: "_State", text: str) -> None:
        """Quiet context for the live model (who is speaking, their memory)."""
        for part in chunks(text):
            await self._send(state, {"type": "session.thinking.append", "delegation_id": None,
                                     "content": "Context (silent note, do not react): " + part})

    async def _receive(self, state: "_State") -> None:
        s = state.s
        async for message in state.ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                if message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    log.warning("gpt-live: connection closed (%s)", state.ws.close_code)
                    return
                continue
            event = json.loads(message.data)
            kind = event.get("type", "")
            if kind == "session.output_audio.delta":
                self._play(state, event)
            elif kind == "session.input_transcript.delta":
                if state.bot.parts:
                    self._finish_bot(state)
                state.user.add(event)
                s.user_speaking = True
                s.touch()
            elif kind == "session.output_transcript.delta":
                if state.user.parts:
                    self._finish_user(state)
                state.bot.add(event)
                s.touch()
            elif kind == "session.started":
                log.info("gpt-live session ready (model %s, voice %s, backend %s)", s.model, s.voice, self.backend)
                state.background.add(asyncio.create_task(self._send_mic(state)))
                if s.greeting:
                    await self._send(state, {"type": "session.commentary.append", "delegation_id": None,
                                             "content": f"Say exactly this and nothing else: {s.greeting}"})
            elif kind == "session.delegation.created":
                delegation = event.get("delegation") or {}
                log.info("gpt-live: delegated to %s", delegation.get("target"))
                state.last_bot_activity = state.last_delegation = state.backend_step = time.monotonic()
                state.after_tools, state.backend_busy = False, True
                s.touch()
            elif kind == "response.event":
                state.last_bot_activity = time.monotonic()
                await self._on_backend(state, event.get("delegation_id"), event.get("event") or {})
            elif kind == "session.usage.updated":
                state.seconds = float((event.get("usage") or {}).get("seconds", state.seconds))
            elif kind == "session.closed":
                state.seconds = float((event.get("usage") or {}).get("seconds", state.seconds))
                log.info("gpt-live session closed (%s)", event.get("reason"))
                state.closed.set()
                return
            elif kind == "error":
                error = event.get("error") or {}
                log.error("gpt-live error %s: %s", error.get("code"), error.get("message"))

    def _play(self, state: "_State", event: dict) -> None:
        s = state.s
        pcm = base64.b64decode(event.get("delta") or "")
        start, end = event.get("start_ms"), event.get("end_ms")
        if start is not None and state.out_end is not None and 0 < start - state.out_end <= MAX_GAP_FILL_MS:
            s.speaker.play(bytes(int((start - state.out_end) * LIVE_RATE / 1000) * 2))
        if end is not None:
            state.out_end = end
        seconds = len(pcm) / 2 / LIVE_RATE
        if is_silent(pcm):
            state.silent_seconds += seconds  # not speech: no activity, nothing queued to play
            return
        state.spoken_seconds += seconds
        if state.backend_done:
            log.info("gpt-live started speaking the result %.1f s after the backend answered",
                     time.monotonic() - state.backend_done)
            state.backend_done = 0.0
        state.last_output = state.last_bot_activity = time.monotonic()
        s.engaged, s.ignored_in_row = True, 0
        s.speaker.play(pcm)
        s.touch()

    async def _on_backend(self, state: "_State", delegation_id: str | None, event: dict) -> None:
        kind = event.get("type", "")
        if kind == "response.output_text.delta":
            state.backend_parts.append(event.get("delta") or "")
        elif kind == "response.output_text.done":
            state.backend_parts = [event.get("text") or ""]
        elif kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                log.info("tool call %s(%s) after %.1f s of backend thinking", item.get("name"), item.get("arguments"),
                         time.monotonic() - state.backend_step)
                state.last_tool_call = time.monotonic()
                state.calls.append(asyncio.create_task(self._run_tool(state, item)))
        elif kind in ("response.completed", "response.done", "response.failed", "response.incomplete"):
            response = event.get("response") or {}
            state.backend_cost += backend_cost(self.backend, response.get("usage") or {})
            if kind != "response.completed" and kind != "response.done":
                log.warning("gpt-live backend %s: %s", kind, response.get("error") or response.get("status"))
            if state.calls:
                calls, state.calls = state.calls, []
                state.background.add(asyncio.create_task(self._continue(state, calls)))
            else:
                state.backend_done, state.backend_busy = time.monotonic(), False
                text = backend_text(response) or "".join(state.backend_parts).strip()
                state.backend_parts = []
                if text:
                    state.last_answer = state.backend_done
                log.info("gpt-live backend answered %.1f s after %s: %s", state.backend_done - state.backend_step,
                         "the tool results" if state.after_tools else "the delegation (no tool used)",
                         text[:300] or "(no text)")
        elif kind == "error":
            log.error("gpt-live backend error: %s", event.get("error") or event)

    async def _run_tool(self, state: "_State", item: dict) -> tuple[str, str]:
        s = state.s
        name, call_id = item.get("name", ""), item.get("call_id", "")
        if name == END_SESSION:
            state.goodbye_at = time.monotonic()
            return call_id, '{"status": "ending; say a short goodbye"}'
        started = time.monotonic()
        output = await s.tools.call(name, item.get("arguments") or "{}")
        log.info("tool %s took %.1f s", name, time.monotonic() - started)
        s.touch()
        return call_id, add_language_hint(output, state.language)

    async def _continue(self, state: "_State", calls: list[asyncio.Task]) -> None:
        """All of a backend reply's function calls are done: return their results and let it go on."""
        for result in await asyncio.gather(*calls, return_exceptions=True):
            if isinstance(result, Exception):
                log.error("tool failed: %r", result)
                continue
            call_id, output = result
            await self._send(state, {"type": "response.item.create",
                                     "item": {"type": "function_call_output", "call_id": call_id, "output": output}})
        await self._send(state, {"type": "response.create"})
        state.backend_step, state.after_tools = time.monotonic(), True

    def _finish_user(self, state: "_State") -> None:
        s = state.s
        speech = state.user
        start_ms, end_ms, first, last = speech.start_ms, speech.end_ms, speech.first, speech.last
        text = speech.take()
        s.user_speaking = False
        if not text:
            return
        s.history.append(("user", text))
        index = len(s.history) - 1
        state.last_user_turn = first
        log.info("user: %s", text)
        state.language = detect_language(text) or state.language
        if is_goodbye(text):
            log.info("goodbye heard; hanging up after the reply")
            state.goodbye_at = time.monotonic()
        voice = s.voice_session
        if voice and state.mic_started is not None:
            if start_ms is None or end_ms is None:
                start_ms = int((first - state.mic_started) * 1000) - SPEECH_LEAD_MS
                end_ms = int((last - state.mic_started) * 1000)
            pcm = voice.audio_between(max(0, start_ms), end_ms)
            state.background.add(asyncio.create_task(self._identify(state, pcm, index)))

    async def _identify(self, state: "_State", pcm: bytes, index: int) -> None:
        voice = state.s.voice_session
        try:
            await asyncio.wait_for(voice.add_turn(pcm), VOICE_WAIT_SECONDS * 5)
        except Exception as e:
            log.info("voice: %s", e)
            return
        if voice.turn_name:
            state.s.people[index] = voice.turn_name
        intro = voice.introduction()
        if intro:
            await self._context(state, intro)

    def _finish_bot(self, state: "_State") -> None:
        said_at = state.bot.first
        text = state.bot.take()
        if text:
            state.s.history.append(("assistant", text))
            log.info("assistant: %s", text)
            if is_goodbye(text) and not state.goodbye_at:
                log.info("the bot said goodbye; hanging up")
                state.goodbye_at = said_at
            if WAITING_WORDS.search(text) and state.last_tool_call < state.last_user_turn \
                    and state.nudged_turn != state.last_user_turn:
                state.nudged_turn = state.last_user_turn
                state.background.add(asyncio.create_task(self._nudge(state, said_at)))

    async def _nudge(self, state: "_State", said_at: float) -> None:
        """It promised to check: if no tool is running or was used for this question, remind it to delegate."""
        await asyncio.sleep(max(0.0, said_at + NUDGE_SECONDS - time.monotonic()))
        if state.calls or state.backend_busy or state.last_tool_call >= state.last_user_turn \
                or state.last_answer >= state.last_user_turn:
            return  # something ran, or the backend gave an answer (sums and the like need no tool)
        if state.s.speaker.is_playing() or time.monotonic() - state.last_output < TURN_GAP_SECONDS:
            return  # it is talking: a reminder now would cut it off
        log.warning("gpt-live: said it was checking but no tool ran; reminding it")
        await self._send(state, {"type": "session.instructions.append", "delegation_id": None, "content": NUDGE})

    def _finish_turns(self, state: "_State", force: bool = False) -> None:
        now = time.monotonic()
        if state.user.parts and (force or now - state.user.last > TURN_GAP_SECONDS):
            self._finish_user(state)
        if state.bot.parts and (force or now - state.bot.last > TURN_GAP_SECONDS):
            self._finish_bot(state)

    async def _watch(self, state: "_State") -> None:
        """Every 0.2 s: close finished turns, drop stale audio after an interruption, hang up after goodbye."""
        s = state.s
        while True:
            await asyncio.sleep(0.2)
            self._finish_turns(state)
            now = time.monotonic()
            if (state.user.parts and s.speaker.is_playing() and state.user.last > state.last_output
                    and now - state.last_output > 0.4):
                s.speaker.clear()  # the model stopped speaking because someone talked; drop what is queued
            if self._not_addressed(state, now):
                log.info("people keep talking but not to the bot for %d s; hanging up", NOT_ADDRESSED_SECONDS)
                s.ignored_in_row = 2  # the app logs this as "not addressed"
                break
            if state.goodbye_at:
                replied = state.last_output > state.goodbye_at
                if (replied and not s.speaker.is_playing() and now - state.last_output > TURN_GAP_SECONDS) \
                        or now - state.goodbye_at > GOODBYE_WAIT_SECONDS:
                    log.info("hanging up after the goodbye (%.1f s after it was heard)", now - state.goodbye_at)
                    break
        # The app hangs up once the last words have played; the connection stays open until then.
        s.end_ready = True
        await asyncio.Event().wait()


    @staticmethod
    def _not_addressed(state: "_State", now: float) -> bool:
        quiet_bot = now - state.last_bot_activity > NOT_ADDRESSED_SECONDS
        return quiet_bot and not state.calls and state.user.last > state.last_bot_activity \
            and now - state.user.last < TURN_GAP_SECONDS * 2


class _State:
    def __init__(self, s: SessionIO):
        self.s = s
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.sent = 0
        self.user = _Speech()
        self.bot = _Speech()
        self.language: str | None = None
        self.mic_started: float | None = None
        self.last_output = 0.0
        self.last_bot_activity = time.monotonic()  # the bot spoke or worked on a task
        self.last_delegation = 0.0
        self.backend_step = 0.0  # when the backend's current step started (delegation or tool results sent)
        self.after_tools = False
        self.backend_done = 0.0
        self.backend_busy = False
        self.last_tool_call = 0.0
        self.last_answer = 0.0  # when the backend last gave a text answer
        self.backend_parts: list[str] = []
        self.last_user_turn = 0.0
        self.nudged_turn = -1.0
        self.out_end: int | None = None
        self.goodbye_at = 0.0
        self.calls: list[asyncio.Task] = []
        self.background: set[asyncio.Task] = set()
        self.seconds = 0.0
        self.backend_cost = 0.0
        self.spoken_seconds = 0.0
        self.silent_seconds = 0.0
        self.closed = asyncio.Event()
