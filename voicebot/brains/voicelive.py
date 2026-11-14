import asyncio
import base64
import logging
import time
from typing import Any

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import FunctionCallOutputItem, InputTextContentPart, SystemMessageItem
from azure.ai.voicelive.models import ServerEventType as E
from azure.core.credentials import AzureKeyCredential

from ..config import Config
from .base import SessionIO
from ..goodbye import is_goodbye
from ..language import add_language_hint, detect_language
from ..tools.memory_tools import END_SESSION
from ..usage import DailyUsage, TokenTotals, prices_for

log = logging.getLogger(__name__)

MIC_FRAMES_PER_MESSAGE = 2
TRANSCRIPT_WAIT_SECONDS = 2
GOODBYE_REPLY_START_SECONDS = 2
GOODBYE_REPLY_MAX_SECONDS = 15
REPLY_WITHOUT_TRANSCRIPT_SECONDS = 2.0
TRANSCRIBE_PHRASES = ["Jarvis", "Джарвис", "Джарвіс", "YouTube", "Netflix", "PS5", "Price Navigator"]


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

    async def say(self, text: str, voice: str, model: str, timeout: float = 15) -> bytes:
        """Speaks one phrase in the given voice and returns the PCM16 audio (used for voice previews)."""
        kwargs: dict[str, Any] = {}
        if self.cfg.api_version:
            kwargs["api_version"] = self.cfg.api_version
        audio = bytearray()

        async def run() -> None:
            async with connect(
                endpoint=self.cfg.endpoint, credential=AzureKeyCredential(self.cfg.api_key), model=model, **kwargs
            ) as conn:
                await conn.session.update(session={
                    "modalities": ["text", "audio"],
                    "voice": _voice_config(voice),
                    "output_audio_format": "pcm16",
                    "turn_detection": None,
                })
                await conn.response.create(
                    response={"instructions": f"Say exactly this, warmly and naturally, and nothing else: {text}"}
                )
                async for event in conn:
                    if event.type == E.RESPONSE_AUDIO_DELTA:
                        audio.extend(event.delta)
                    elif event.type == E.RESPONSE_DONE:
                        return
                    elif event.type == E.ERROR:
                        raise RuntimeError(event.error.message)

        await asyncio.wait_for(run(), timeout)
        return bytes(audio)

    def _session_config(self, s: SessionIO) -> dict[str, Any]:
        cfg = self.cfg
        voice = _voice_config(s.voice)
        turn_detection: dict[str, Any] = {
            "type": cfg.turn_detection,
            "interrupt_response": True,
            "silence_duration_ms": cfg.turn_silence_ms,
            # With a transcript, we start each reply ourselves so the reply language can be set.
            "create_response": not cfg.transcribe_model,
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
            transcription: dict[str, Any] = {"model": cfg.transcribe_model}
            if cfg.transcribe_model.startswith("mai-transcribe"):
                # MAI Transcribe has no Belarusian, so it cannot mistake Ukrainian or Russian for it.
                transcription["phrase_list"] = TRANSCRIBE_PHRASES
            else:
                transcription["prompt"] = "Speech is in English, Ukrainian or Russian. Never Belarusian."
            session["input_audio_transcription"] = transcription
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
                    if turn.waiting_for_audio:
                        turn.waiting_for_audio = False
                        log.info("first answer audio %.1f s after speech ended", time.monotonic() - turn.stopped_at)
                    s.speaker.play(event.delta)
                    s.touch()
                elif kind == E.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                    s.user_speaking = True
                    s.speaker.clear()
                    s.touch()
                elif kind == E.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                    s.user_speaking = False
                    turn.transcript_ready.clear()
                    turn.number += 1
                    turn.stopped_at = time.monotonic()
                    if self.cfg.transcribe_model:
                        turn.awaiting_reply = True
                        spawn(self._reply_if_no_transcript(conn, turn, turn.number))
                    s.touch()
                elif kind == E.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                    turn.user_text = event.transcript.strip()
                    turn.transcript_ready.set()
                    s.history.append(("user", turn.user_text))
                    log.info("user: %s", turn.user_text)
                    language = detect_language(turn.user_text)
                    if language:
                        turn.language = language
                    if turn.user_text:
                        await self._reply(conn, turn)
                    else:
                        turn.awaiting_reply = False  # only noise was heard
                    if is_goodbye(turn.user_text):
                        log.info("goodbye heard; hanging up after the reply")
                        spawn(self._end_after_reply(s, turn))
                elif kind == E.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                    turn.transcript_ready.set()
                    await self._reply(conn, turn)
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
                elif kind == E.RESPONSE_CREATED:
                    turn.responding.set()
                    turn.response_finished.clear()
                elif kind == E.RESPONSE_DONE:
                    turn.responding.clear()
                    turn.response_finished.set()
                    s.touch()
                    status = getattr(event.response, "status", None)
                    if status and status != "completed":
                        details = getattr(event.response, "status_details", None)
                        log.warning("response %s: %s", status, details.as_dict() if hasattr(details, "as_dict") else details)
                    if event.response and event.response.usage:
                        usage.add_usage(event.response.usage)
                    if end_requested:
                        s.end_ready = True
                    elif tool_tasks:
                        spawn(self._respond_after(conn, turn, tool_tasks))
                        tool_tasks = []
                elif kind == E.SESSION_UPDATED:
                    log.info("voice live session ready (model %s, voice %s, transcription %s)",
                             s.model, s.voice, self.cfg.transcribe_model or "off")
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
        await conn.conversation.item.create(
            item=tool_output_item(event.call_id, add_language_hint(output, turn.language))
        )
        s.touch()

    async def _end_after_reply(self, s: SessionIO, turn: "_Turn") -> None:
        """Let the model's reply (usually a goodbye) start and finish, then mark the session to end.
        The reply may already be done when the transcript arrives."""
        if not turn.responding.is_set():
            try:
                await asyncio.wait_for(turn.responding.wait(), GOODBYE_REPLY_START_SECONDS)
            except asyncio.TimeoutError:
                s.end_ready = True
                return
        try:
            await asyncio.wait_for(turn.response_finished.wait(), GOODBYE_REPLY_MAX_SECONDS)
        except asyncio.TimeoutError:
            pass
        s.end_ready = True

    async def _respond_after(self, conn, turn: "_Turn", tool_tasks: list[asyncio.Task]) -> None:
        results = await asyncio.gather(*tool_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.error("tool failed: %r", result)
        await self._create_response(conn, turn)

    async def _create_response(self, conn, turn: "_Turn") -> None:
        """The language rule goes in twice: as the newest message (the model follows recent context
        most) and as extra instructions for this response."""
        rule = _language_rule(turn.language)
        if rule:
            await conn.conversation.item.create(item=SystemMessageItem(content=[InputTextContentPart(text=rule)]))
        await conn.response.create(additional_instructions=rule)

    async def _reply(self, conn, turn: "_Turn") -> None:
        """Starts the reply to the user's last turn (once per turn), in the user's language."""
        if not turn.awaiting_reply:
            return
        turn.awaiting_reply = False
        turn.waiting_for_audio = True
        log.info("reply started %.1f s after speech ended, language %s",
                 time.monotonic() - turn.stopped_at, turn.language or "not detected")
        await self._create_response(conn, turn)

    async def _reply_if_no_transcript(self, conn, turn: "_Turn", number: int) -> None:
        await asyncio.sleep(REPLY_WITHOUT_TRANSCRIPT_SECONDS)
        if turn.number == number and turn.awaiting_reply and not turn.transcript_ready.is_set():
            log.warning("no transcript in %.0f s; replying without it", REPLY_WITHOUT_TRANSCRIPT_SECONDS)
            await self._reply(conn, turn)


def _voice_config(name: str) -> dict[str, str]:
    return {"type": "azure-standard" if "Neural" in name else "openai", "name": name}


def _language_rule(language: str | None) -> str | None:
    if not language:
        return None
    return f"The user's last utterance was in {language}. Reply only in {language}, whatever language was used before."


class _Turn:
    def __init__(self) -> None:
        self.user_text = ""
        self.language: str | None = None  # last detected; kept when a turn is unclear
        self.number = 0  # counts the user's finished turns
        self.awaiting_reply = False  # the user's finished turn has no reply yet
        self.stopped_at = 0.0
        self.waiting_for_audio = False
        self.transcript_ready = asyncio.Event()
        self.transcript_ready.set()
        self.responding = asyncio.Event()
        self.response_finished = asyncio.Event()
