import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time

from . import cues
from .audio import AudioIO
from .brains import SessionIO, create_brain
from .config import Config
from .jabra import HookButtons, MediaKeys
from .memory import Memory, with_people
from .prompt import build_instructions
from .settings import RuntimeSettings, voice_display_name
from .tools import default_registry
from .tools.weather import home_from_context
from .voice_preview import VoicePreviews
from . import archive, speakers, wake

log = logging.getLogger(__name__)

MIC_QUEUE_FRAMES = 400  # 8 s: holds what is said right after the wake word while connecting
PLAYBACK_TAIL = 0.25
MIN_SENTENCES_TO_SUMMARISE = 2
MIN_CHARS_TO_SUMMARISE = 60
DOUBLE_PRESS_SECONDS = 0.4
CUES = ("START", "END", "ERROR", "MODEL_BASIC", "MODEL_SMART", "THINKING")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.audio = AudioIO(cfg.audio_device, cfg.mic_rate, cfg.speaker_rate)
        self.brain = create_brain(cfg)
        self.memory = Memory(cfg.memory_dir, cfg.memory_recent_hours, cfg.memory_recent_turns)
        if cfg.weather_home:
            weather_home, source = cfg.weather_home, "WEATHER_HOME"
        else:
            weather_home, source = home_from_context(cfg.context_file), "context.md"
        if weather_home:
            log.info("weather home: %s (from %s)", weather_home, source)
        else:
            log.info("weather home: not set; %s", "will detect from IP" if cfg.weather_detect_home else "ask the user")
        self.voices = (speakers.build(cfg.models_dir, cfg.voices_file, cfg.voice_threshold, cfg.voice_margin)
                       if cfg.voice_id else None)
        self.current_voice = speakers.Current()
        self.tools = default_registry(self.memory, weather_home, cfg.weather_detect_home,
                                      self.voices, self.current_voice)
        self.archive = archive.build(cfg.models_dir, cfg.memory_dir, cfg.qdrant_url, cfg.qdrant_api_key)
        if self.archive:
            self.tools.register(archive.search_tool(self.archive))
        self.settings = RuntimeSettings(cfg.state_file, cfg.voices, [cfg.model, cfg.smart_model])
        self.cue = {
            name: cues.load_sound(name.lower(), cfg.speaker_rate) or cues.render(getattr(cues, name), cfg.speaker_rate)
            for name in CUES
        }
        self._shutdown = asyncio.Event()
        self._session_task: asyncio.Task | None = None
        self._session_stop: asyncio.Event | None = None
        self._restarting = False
        self._dot_timer: asyncio.TimerHandle | None = None
        self._dot_lock = asyncio.Lock()
        self.wake: wake.WakeListener | None = None
        self.previews: VoicePreviews | None = None
        if hasattr(self.brain, "say"):
            self.previews = VoicePreviews(
                os.path.join(os.path.dirname(cfg.state_file), "voices"),
                lambda text, voice: self.brain.say(text, voice, cfg.model),
            )
        self._background: set[asyncio.Task] = set()

    def _summarise_later(self, history: list[tuple[str, str]], people: dict[int, str]) -> None:
        """After a conversation, a short summary goes to the searchable long-term memory (in the background)."""
        if not self.archive or not hasattr(self.brain, "summarize"):
            return
        rows = with_people(history, people)
        said = [text for role, text, _ in rows if role == "user"]
        if len(said) < MIN_SENTENCES_TO_SUMMARISE or sum(len(t) for t in said) < MIN_CHARS_TO_SUMMARISE:
            return
        when = time.time()
        transcript = archive.transcript_text(rows)
        names = sorted({who for _, _, who in rows if who})

        async def run() -> None:
            try:
                summary = await self.brain.summarize(archive.SUMMARY_INSTRUCTIONS, archive.summary_request(transcript),
                                                     self.cfg.model)
                if not summary or summary.strip().upper().startswith("NOTHING"):
                    log.info("archive: nothing worth remembering in that conversation")
                    return
                await asyncio.to_thread(self.archive.add, when, names, summary, transcript)
            except Exception as e:
                log.warning("archive: could not save a summary: %s", e)

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def run(self, start_now: bool = False) -> None:
        loop = asyncio.get_running_loop()

        def from_thread(event: str) -> None:
            loop.call_soon_threadsafe(self._on_button, event)

        if self.cfg.wake_word:
            self.wake = wake.build(
                self.cfg.models_dir,
                lambda name: loop.call_soon_threadsafe(self._on_wake, name),
                self.cfg.wake_threshold,
                self.cfg.wake_phrases,
            )
        self.audio.start()
        if self.wake:
            self.wake.start()
            self._listen_for_wake_word()
        self.hook = HookButtons(self.cfg.jabra_name, from_thread)
        readers = [self.hook, MediaKeys(self.cfg.jabra_name, from_thread)]
        for reader in readers:
            reader.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown.set)
        loop.add_signal_handler(signal.SIGUSR1, self._on_button, "toggle")
        log.info(
            "ready: %s (model %s, voice %s)",
            "say the wake word or press call" if self.wake else "press the call button to talk",
            self.settings.model, self.settings.voice,
        )
        if start_now:
            self._on_button("toggle")
        if self.previews:
            task = asyncio.create_task(self.previews.prepare(self.settings.voices))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

        await self._shutdown.wait()
        log.info("shutting down")
        if self._session_task and not self._session_task.done():
            self._session_stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._session_task, 10)
        for reader in readers:
            reader.stop()
        if self.wake:
            self.wake.stop()
        self.audio.stop()

    def _active(self) -> bool:
        return self._session_task is not None and not self._session_task.done()

    def _on_button(self, event: str) -> None:
        log.info("button: %s", event)
        if event in ("volume_up", "volume_down"):
            self._change_volume(+1 if event == "volume_up" else -1)
        elif event == "play_pause":
            self._on_dot_press()
        elif event in ("hook_on", "toggle") and not self._active():
            self._start_session()
        elif event in ("hook_off", "toggle") and self._active():
            self._session_stop.set()
        elif event == "hook_off":
            self.hook.set_off_hook_light(False)

    def _start_session(self, greeting: str | None = None, from_wake: bool = False) -> None:
        if self.wake:
            self.wake.pause()
        self._session_stop = asyncio.Event()
        self._session_task = asyncio.create_task(self._session(self._session_stop, greeting, from_wake))

    def _on_wake(self, name: str) -> None:
        log.info("wake word: %s", name)
        if not self._active():
            self._start_session(from_wake=True)

    def _listen_for_wake_word(self) -> None:
        """Between sessions, mic frames go to the wake word listener."""
        if self.wake:
            self.audio.set_mic_callback(self.wake.feed)
            self.wake.resume()
        else:
            self.audio.set_mic_callback(None)

    async def _stop_session(self) -> None:
        if self._active():
            self._session_stop.set()
            with contextlib.suppress(Exception):
                await self._session_task

    def _on_dot_press(self) -> None:
        """Single press = next voice, double press = switch model."""
        if self._dot_timer is not None:
            self._dot_timer.cancel()
            self._dot_timer = None
            asyncio.create_task(self._on_dot(double=True))
            return

        def single() -> None:
            self._dot_timer = None
            asyncio.create_task(self._on_dot(double=False))

        self._dot_timer = asyncio.get_running_loop().call_later(DOUBLE_PRESS_SECONDS, single)

    async def _on_dot(self, double: bool) -> None:
        async with self._dot_lock:
            was_active = self._active()
            self._restarting = True
            try:
                await self._stop_session()
            finally:
                self._restarting = False
            if double:
                model = self.settings.toggle_model()
                log.info("model switched to %s", model)
                await self._play_cue("MODEL_BASIC" if model == self.cfg.model else "MODEL_SMART")
                if was_active:
                    self._start_session()
            else:
                voice = self.settings.next_voice()
                log.info("voice switched to %s", voice)
                clip = None if was_active or not self.previews else await self.previews.get(voice)
                if clip:
                    # Between conversations, only let the new voice introduce itself; pressing
                    # again moves on to the next one.
                    self.audio.clear()
                    self.audio.play(clip)
                else:
                    self._start_session(greeting=f"Hi, I'm {voice_display_name(voice)}.")

    def _change_volume(self, step: int) -> None:
        sign = "+" if step > 0 else "-"
        subprocess.run(
            ["amixer", "-q", "-c", self.cfg.audio_card, "sset", self.cfg.mixer_control, f"1{sign}"],
            check=False,
        )

    async def _play_cue(self, name: str) -> None:
        self.audio.play(self.cue[name])
        while self.audio.is_playing():
            await asyncio.sleep(0.02)
        await asyncio.sleep(PLAYBACK_TAIL)

    async def _session(self, stop: asyncio.Event, greeting: str | None = None, from_wake: bool = False) -> None:
        loop = asyncio.get_running_loop()
        mic: asyncio.Queue = asyncio.Queue(maxsize=MIC_QUEUE_FRAMES)

        def put_frame(frame: bytes) -> None:
            if mic.full():
                mic.get_nowait()
            mic.put_nowait(frame)

        voice_session = None
        if self.voices:
            voice_session = speakers.VoiceSession(
                self.voices, lambda person: self.memory.prompt_text(person=person)
            )
        self.current_voice.session = voice_session
        session = SessionIO(
            mic=mic,
            speaker=self.audio,
            instructions=build_instructions(
                self.cfg.context_file,
                web_search="web_search" in self.tools.names(),
                memory=self.memory.prompt_text(),
                cars="auto_ria" in self.tools.names(),
                people=self.voices.names() if self.voices else None,
                past=self.archive is not None,
            ),
            tools=self.tools,
            stop=stop,
            model=self.settings.model,
            voice=self.settings.voice,
            greeting=greeting,
            thinking_sound=self.cue["THINKING"],
            voice_session=voice_session,
        )
        started = time.monotonic()
        log.info("session started%s", " by wake word" if from_wake else "")
        self.hook.set_off_hook_light(True)

        def forward_mic() -> None:
            self.audio.set_mic_callback(lambda frame: loop.call_soon_threadsafe(put_frame, frame))

        if from_wake:
            # The question usually follows the wake word at once: keep it while the beep plays
            # and the connection opens (the Jabra's echo cancelling removes the beep).
            forward_mic()
        await self._play_cue("START")
        session.touch()
        if not from_wake:
            forward_mic()
        brain_task = asyncio.create_task(self.brain.run_session(session))
        stop_wait = asyncio.create_task(stop.wait())
        reason = "unknown"
        try:
            while True:
                await asyncio.wait({brain_task, stop_wait}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    reason = "hang-up or stop"
                    break
                if session.end_ready and not self.audio.is_playing():
                    await asyncio.sleep(PLAYBACK_TAIL)
                    reason = "goodbye" if session.ignored_in_row < 2 else "not addressed"
                    break
                if brain_task.done():
                    reason = "connection closed"
                    break
                limit = self.cfg.idle_timeout
                if from_wake and not session.engaged:
                    limit = self.cfg.wake_idle_timeout  # likely a false wake: nobody is talking to the bot
                if session.idle_seconds() > limit:
                    reason = "silence"
                    break
                if time.monotonic() - started > self.cfg.max_session:
                    reason = "max session length"
                    break
        finally:
            self.audio.set_mic_callback(None)
            stop.set()
            stop_wait.cancel()
            failed = False
            try:
                await asyncio.wait_for(brain_task, 5)
            except asyncio.TimeoutError:
                log.warning("brain did not stop in time")
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("session failed")
                failed = True
            self.audio.clear()
            if not self._restarting:
                # Keeping the light on during a restart stops the Jabra from sending a hang-up
                # that would end the new session.
                self.hook.set_off_hook_light(False)
            await self._play_cue("ERROR" if failed else "END")
            self.memory.save_session(session.history, people=session.people)
            self._summarise_later(session.history, session.people)
            self.current_voice.session = None
            people = sorted(set(session.people.values()))
            if people:
                log.info("session was with %s", ", ".join(people))
            log.info("session ended (%s) after %.0f s", reason, time.monotonic() - started)
            if not self._restarting:
                self._listen_for_wake_word()
