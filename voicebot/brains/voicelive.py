import asyncio
import base64
import logging
from typing import Any

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import FunctionCallOutputItem
from azure.ai.voicelive.models import ServerEventType as E
from azure.core.credentials import AzureKeyCredential

from ..config import Config
from .base import SessionIO
from ..language import add_language_hint, detect_language
from ..tools.memory_tools import END_SESSION
from ..usage import DailyUsage, TokenTotals, prices_for

log = logging.getLogger(__name__)

MIC_FRAMES_PER_MESSAGE = 2
TRANSCRIPT_WAIT_SECONDS = 2


def tool_output_item(call_id: str, output: str) -> FunctionCallOutputItem:
    # The SDK rejects a plain dict here; it needs the typed item.
    return FunctionCallOutputItem(call_id=call_id, output=output)


class VoiceLiveBrain:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.today = DailyUsage()

    async def run_session(self, s: SessionIO) -> None:
        usage = TokenTotals()
        try:
            await self._run(s, usage)
        finally:
            prices = prices_for(s.model)
            cost = usage.cost(prices)
            price_note = f"est. ${cost:.4f}" if prices else f"no prices known for {s.model}"
            log.info("session tokens (%s): %s, %s", s.model, usage.describe(), price_note)
            log.info("today so far: est. $%.4f", self.today.add(cost))

    async def _run(self, s: SessionIO, usage: TokenTotals) -> None:
        kwargs: dict[str, Any] = {}
        if self.cfg.api_version:
            kwargs["api_version"] = self.cfg.api_version
        async with connect(
            endpoint=self.cfg.endpoint,
            credential=AzureKeyCredential(self.cfg.api_key),
            model=s.model,
            **kwargs,
        ) as conn:
            await conn.session.update(session=self._session_config(s))
            if s.greeting:
                await conn.response.create(response={"instructions": f"Say exactly this and nothing else: {s.greeting}"})
            tasks = {
                asyncio.create_task(self._send_mic(conn, s)),
                asyncio.create_task(self._receive(conn, s, usage)),
                asyncio.create_task(s.stop.wait()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    def _session_config(self, s: SessionIO) -> dict[str, Any]:
        cfg = self.cfg
        if "Neural" in s.voice:
            voice = {"type": "azure-standard", "name": s.voice}
        else:
            voice = {"type": "openai", "name": s.voice}
        turn_detection: dict[str, Any] = {
            "type": cfg.turn_detection,
            "interrupt_response": True,
            "silence_duration_ms": cfg.turn_silence_ms,
        }
        if cfg.turn_detection != "server_vad":
            turn_detection["auto_truncate"] = True
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "instructions": s.instructions,
            "voice": voice,
            "input_audio_format": "pcm16",
            "input_audio_sampling_rate": s.speaker.mic_rate,
            "output_audio_format": "pcm16",
            "turn_detection": turn_detection,
        }
        if cfg.transcribe_model:
            session["input_audio_transcription"] = {
                "model": cfg.transcribe_model,
                "prompt": "Speech is in English, Ukrainian or Russian. Never Belarusian.",
            }
        if cfg.noise_reduction:
            session["input_audio_noise_reduction"] = {"type": cfg.noise_reduction}
        if cfg.echo_cancellation:
            session["input_audio_echo_cancellation"] = {"type": "server_echo_cancellation"}
        if s.tools:
            session["tools"] = s.tools.specs()
            session["tool_choice"] = "auto"
        return session

    async def _send_mic(self, conn, s: SessionIO) -> None:
        while True:
            frames = [await s.mic.get()]
            while len(frames) < MIC_FRAMES_PER_MESSAGE and not s.mic.empty():
                frames.append(s.mic.get_nowait())
            audio = base64.b64encode(b"".join(frames)).decode("ascii")
            await conn.input_audio_buffer.append(audio=audio)

    async def _receive(self, conn, s: SessionIO, usage: TokenTotals) -> None:
        turn = _Turn()
        background: set[asyncio.Task] = set()
        tool_tasks: list[asyncio.Task] = []
        end_requested = False

        def spawn(coro) -> asyncio.Task:
            task = asyncio.create_task(coro)
            background.add(task)
            task.add_done_callback(background.discard)
            return task

        try:
            async for event in conn:
                kind = event.type
                if kind == E.RESPONSE_AUDIO_DELTA:
                    s.speaker.play(event.delta)
                    s.touch()
                elif kind == E.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                    s.user_speaking = True
                    s.speaker.clear()
                    s.touch()
                elif kind == E.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                    s.user_speaking = False
                    turn.transcript_ready.clear()
                    s.touch()
                elif kind == E.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                    turn.user_text = event.transcript.strip()
                    turn.transcript_ready.set()
                    s.history.append(("user", turn.user_text))
                    log.info("user: %s", turn.user_text)
                elif kind == E.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                    turn.transcript_ready.set()
                elif kind == E.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                    s.history.append(("assistant", event.transcript.strip()))
                    log.info("assistant: %s", event.transcript.strip())
                elif kind == E.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
                    log.info("tool call %s(%s)", event.name, event.arguments)
                    if event.name == END_SESSION:
                        # The goodbye was spoken in this same response; hang up once it has played.
                        end_requested = True
                        continue
                    if s.thinking_sound:
                        s.speaker.play(s.thinking_sound)
                    tool_tasks.append(spawn(self._run_tool(conn, s, event, turn)))
                elif kind == E.RESPONSE_DONE:
                    s.touch()
                    if event.response and event.response.usage:
                        usage.add_usage(event.response.usage)
                    if end_requested:
                        s.end_ready = True
                    elif tool_tasks:
                        spawn(self._respond_after(conn, tool_tasks))
                        tool_tasks = []
                elif kind == E.SESSION_UPDATED:
                    log.info("voice live session ready (model %s, voice %s)", s.model, s.voice)
                elif kind == E.ERROR:
                    log.error("voice live error %s: %s", event.error.code, event.error.message)
                elif kind == E.WARNING:
                    log.warning("voice live warning: %s", event.as_dict())
        finally:
            for task in background:
                task.cancel()

    async def _run_tool(self, conn, s: SessionIO, event, turn: "_Turn") -> None:
        output = await s.tools.call(event.name, event.arguments)
        # The transcript of the user's question can arrive after the tool call starts.
        try:
            await asyncio.wait_for(turn.transcript_ready.wait(), TRANSCRIPT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            pass
        language = detect_language(turn.user_text)
        if language:
            log.info("reply language hint: %s", language)
        await conn.conversation.item.create(item=tool_output_item(event.call_id, add_language_hint(output, language)))
        s.touch()

    async def _respond_after(self, conn, tool_tasks: list[asyncio.Task]) -> None:
        results = await asyncio.gather(*tool_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("tool failed: %r", result)
        await conn.response.create()


class _Turn:
    def __init__(self) -> None:
        self.user_text = ""
        self.transcript_ready = asyncio.Event()
        self.transcript_ready.set()
