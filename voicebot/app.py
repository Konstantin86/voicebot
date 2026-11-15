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
from .language import detect_language
from .memory import Memory, with_people
from .prompt import build_instructions
from .settings import RuntimeSettings, voice_display_name
from .tools import default_registry
from .tools.weather import home_from_context
from .voice_preview import VoicePreviews
from . import archive, reminders, speakers, telegram, wake

log = logging.getLogger(__name__)

MIC_QUEUE_FRAMES = 400  # 8 s: holds what is said right after the wake word while connecting
PLAYBACK_TAIL = 0.25
MIN_SENTENCES_TO_SUMMARISE = 2
RING_SECONDS = {"alarm": 180, "timer": 60, "reminder": 0}  # how long it keeps repeating until stopped
RING_PAUSE = 2.0
MIN_CHARS_TO_SUMMARISE = 60
DOUBLE_PRESS_SECONDS = 0.4
INTERCOM_INTRO = {"Russian": "{name} пишет: ", "Ukrainian": "{name} пише: ", "English": "{name} writes: "}
CUES = ("START", "END", "ERROR", "MODEL_BASIC", "MODEL_SMART", "THINKING", "REMINDER", "ALARM")


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
        self.reminders = reminders.Reminders(cfg.reminders_file)
        self.tools.register(reminders.make_tool(
            self.reminders, self._reminder_audio if hasattr(self.brain, "say") else None))
        self._ring_stop: asyncio.Event | None = None
        self.telegram = telegram.build(cfg.telegram_token, cfg.telegram_owner, cfg.telegram_file, telegram.Home(
            settings=self.settings,
            announce=self._announce,
            cost_today=lambda: self.brain.today.today() if hasattr(self.brain, "today") else 0.0,
            summaries=lambda day: self.archive.on_day(day) if self.archive else [],
            voice_name=voice_display_name,
            known_names=lambda: self.voices.names() if self.voices else [],
            basic_model=cfg.model,
            over_budget=self._over_budget,
            reminders=self.reminders,
        ), cfg.digest_time)
        if self.telegram:
            self.tools.register(telegram.send_tool(self.telegram, self.current_voice))
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
            finally:
                self._check_budget()

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
        reminder_task = asyncio.create_task(self._reminder_loop())
        self._background.add(reminder_task)
        if self.telegram:
            self.telegram.start(loop)
            self._background.add(asyncio.create_task(self.telegram.digest_loop()))
        if self.previews:
            task = asyncio.create_task(self.previews.prepare(self.settings.voices))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

        await self._shutdown.wait()
        log.info("shutting down")
        if self.telegram:
            self.telegram.stop()
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
        if self._ring_stop and not self._ring_stop.is_set() and event not in ("volume_up", "volume_down"):
            log.info("reminders: stopped by a button")
            self._ring_stop.set()
            if event == "hook_on":
                self.hook.set_off_hook_light(False)
            return
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
        if self._ring_stop and not self._ring_stop.is_set():
            log.info("reminders: stopped by the wake word")
            self._ring_stop.set()
            return
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

    def _over_budget(self) -> bool:
        budget = self.settings.budget
        return bool(budget) and hasattr(self.brain, "today") and self.brain.today.today() >= budget

    def _session_model(self) -> str:
        if self.settings.model != self.cfg.model and self._over_budget():
            log.info("daily limit reached: using %s instead of %s", self.cfg.model, self.settings.model)
            return self.cfg.model
        return self.settings.model

    def _check_budget(self) -> None:
        """Once a day, when the estimated cost reaches the limit: warn the owner (the basic model is used)."""
        if not self._over_budget() or self.brain.today.warned:
            return
        self.brain.today.warned = True
        self.brain.today.save()
        cost, budget = self.brain.today.today(), self.settings.budget
        log.warning("daily limit reached: est. $%.2f of $%.2f", cost, budget)
        if self.telegram:
            switched = (" The basic model is used until midnight." if self.settings.model != self.cfg.model
                        else " The bot already uses the basic model and keeps working.")
            self._spawn(self.telegram.tell_owner(
                f"Daily limit reached: about ${cost:.2f} of ${budget:.2f} today.{switched} Change it with /budget."))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _announce(self, text: str, sender: str) -> None:
        """A message from someone's phone, read aloud at home."""
        if not hasattr(self.brain, "say"):
            raise RuntimeError("this brain cannot speak a message")
        intro = INTERCOM_INTRO[detect_language(text) or "Russian"].format(name=sender)
        pcm = await self.brain.say(intro + text, self.settings.voice, self.cfg.model)
        await self._play_aloud(self.cue["REMINDER"] + pcm)

    async def _play_aloud(self, pcm: bytes) -> None:
        if self._active():
            while self.audio.is_playing():
                await asyncio.sleep(0.1)
            self.audio.play(pcm)
            return
        if self.wake:
            self.wake.pause()  # the bot's own voice must not wake it
        try:
            self.audio.play(pcm)
            while self.audio.is_playing():
                await asyncio.sleep(0.05)
            await asyncio.sleep(PLAYBACK_TAIL)
        finally:
            if not self._active():
                self._listen_for_wake_word()

    async def _reminder_audio(self, item: reminders.Reminder) -> None:
        try:
            pcm = await self.brain.say(item.text, self.settings.voice, self.cfg.model)
            await asyncio.to_thread(self.reminders.save_audio, item, pcm)
        except Exception as e:
            log.warning("reminders: could not make the spoken text for #%d (will chime only): %s", item.id, e)

    async def _reminder_loop(self) -> None:
        """Checks the list every second; a due one waits until no conversation is running."""
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for item in self.reminders.due_now(now):
                if self.reminders.is_stale(item, now):
                    log.info("reminders: %s #%d was due long ago (Pi off?); skipping it", item.kind, item.id)
                    self.reminders.finished(item, missed=True)
                    continue
                if self._dot_lock.locked():
                    break
                try:
                    if self._active():
                        # During a conversation: say it once, between the bot's sentences.
                        log.info("reminders: %s #%d is due during a conversation: %s", item.kind, item.id, item.text)
                        while self.audio.is_playing():
                            await asyncio.sleep(0.1)
                        self.audio.play(self.cue["REMINDER"] + (self.reminders.audio(item) or b""))
                    else:
                        await self._ring(item)
                except Exception:
                    log.exception("reminders: could not play #%d", item.id)
                self.reminders.finished(item)

    async def _ring(self, item: reminders.Reminder) -> None:
        log.info("reminders: %s #%d is due: %s", item.kind, item.id, item.text)
        speech = self.reminders.audio(item)
        cue = self.cue["ALARM" if item.kind == "alarm" else "REMINDER"]
        self._ring_stop = stop = asyncio.Event()
        if self.wake:
            self.wake.pause()  # the bot's own voice must not wake it
        try:
            until = time.monotonic() + RING_SECONDS[item.kind]
            while not stop.is_set():
                self.audio.play(cue + (speech or b""))
                while self.audio.is_playing() and not stop.is_set():
                    await asyncio.sleep(0.05)
                if self.wake:
                    self.wake.resume()  # between rounds, the wake word stops it
                if time.monotonic() >= until:
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), RING_PAUSE)
                if self.wake and not stop.is_set():
                    self.wake.pause()
        finally:
            if stop.is_set():
                self.audio.clear()
            self._ring_stop = None
            if not self._active():
                self._listen_for_wake_word()

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
                telegram=self.telegram is not None,
            ),
            tools=self.tools,
            stop=stop,
            model=self._session_model(),
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
            self._check_budget()
            self.current_voice.session = None
            people = sorted(set(session.people.values()))
            if people:
                log.info("session was with %s", ", ".join(people))
            log.info("session ended (%s) after %.0f s", reason, time.monotonic() - started)
            if not self._restarting:
                self._listen_for_wake_word()
