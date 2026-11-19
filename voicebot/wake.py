"""Always-on, fully local wake word detection.

- "Hey Jarvis": openWakeWord's pretrained model, run directly with onnxruntime
  (the openwakeword package itself would pull in scipy and scikit-learn).
- "Привет, Джарвис" (and the Ukrainian "Привіт, Джарвіс", which sounds close enough):
  a Vosk Russian model limited to that one phrase.

Detectors run in their own thread; mic frames are handed over through a queue so the
audio thread is never blocked.
"""

import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
OWW_CHUNK = 1280  # openWakeWord works on 80 ms chunks
MEL_CONTEXT = 160 * 3  # extra samples the mel model needs before each chunk
EMBEDDING_WINDOW = 76  # mel frames per embedding
EMBEDDING_STEP = 8  # mel frames per 80 ms chunk
FEATURE_FRAMES = 16  # embeddings per wake model input
MAX_MEL_FRAMES = 970  # ~10 s
WARMUP_CHUNKS = 5  # first scores after a reset are unreliable


class OpenWakeWord:
    """Streaming openWakeWord pipeline: audio -> mel spectrogram -> embeddings -> wake model score."""

    def __init__(self, model_dir: str, wake_model: str = "hey_jarvis_v0.1.onnx"):
        import numpy as np
        import onnxruntime as ort

        self.np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1

        def load(name: str):
            return ort.InferenceSession(os.path.join(model_dir, name), opts, providers=["CPUExecutionProvider"])

        self.mel = load("melspectrogram.onnx")
        self.embedding = load("embedding_model.onnx")
        self.wake = load(wake_model)
        self.reset()

    def reset(self) -> None:
        np = self.np
        self.pending = np.zeros(0, dtype=np.int16)
        self.raw = np.zeros(0, dtype=np.int16)
        self.mel_frames = np.ones((EMBEDDING_WINDOW, 32), dtype=np.float32)
        noise = np.random.default_rng(0).integers(-1000, 1000, SAMPLE_RATE * 4).astype(np.int16)
        self.features = self._embeddings_for(noise)
        self.chunks = 0

    def _mel(self, audio) -> "np.ndarray":
        x = audio.astype(self.np.float32)[None, :]
        spec = self.mel.run(None, {self.mel.get_inputs()[0].name: x})[0]
        return self.np.squeeze(spec) / 10 + 2

    def _embed(self, window) -> "np.ndarray":
        x = window[None, :, :, None].astype(self.np.float32)
        return self.np.squeeze(self.embedding.run(None, {self.embedding.get_inputs()[0].name: x})[0])

    def _embeddings_for(self, audio) -> "np.ndarray":
        mel = self._mel(audio)
        windows = [mel[i:i + EMBEDDING_WINDOW] for i in range(0, len(mel) - EMBEDDING_WINDOW + 1, EMBEDDING_STEP)]
        return self.np.stack([self._embed(w) for w in windows])

    def feed(self, frame: bytes) -> float | None:
        """Adds 16 kHz mono PCM16 audio; returns the latest wake score once per 80 ms chunk."""
        np = self.np
        self.pending = np.concatenate([self.pending, np.frombuffer(frame, dtype=np.int16)])
        score = None
        while len(self.pending) >= OWW_CHUNK:
            chunk, self.pending = self.pending[:OWW_CHUNK], self.pending[OWW_CHUNK:]
            self.raw = np.concatenate([self.raw, chunk])[-(OWW_CHUNK + MEL_CONTEXT):]
            if len(self.raw) < OWW_CHUNK + MEL_CONTEXT:
                continue
            self.mel_frames = np.vstack([self.mel_frames, self._mel(self.raw)])[-MAX_MEL_FRAMES:]
            embedding = self._embed(self.mel_frames[-EMBEDDING_WINDOW:])
            self.features = np.vstack([self.features, embedding[None, :]])[-FEATURE_FRAMES * 4:]
            x = self.features[-FEATURE_FRAMES:][None, :, :].astype(np.float32)
            out = self.wake.run(None, {self.wake.get_inputs()[0].name: x})[0]
            self.chunks += 1
            score = float(np.ravel(out)[0]) if self.chunks > WARMUP_CHUNKS else 0.0
        return score


# Common words the recogniser may choose instead of the wake phrase, so that
# "Привет, как дела" is not forced into "привет джарвис".
DECOY_WORDS = [
    "привет", "как", "дела", "давай", "что", "это", "да", "нет", "всем", "дарья", "даша", "где", "ты",
    "сейчас", "какая", "погода", "здравствуй", "пока", "я", "домой", "мы", "мама", "папа", "ну", "вот",
]
CONFIRM_DELAY_SECONDS = 0.3  # let the last word finish before re-checking
CONFIRM_WINDOW_SECONDS = 3.0
MIN_CONFIDENCE = 0.9


class VoskPhrase:
    """Vosk recogniser limited to the wake phrases plus decoy words.

    Stage 1: a phrase shows up in the live (partial) result -> candidate.
    Stage 2: 0.3 s later the last 3 s are re-recognised with word confidences;
    every word of the phrase must reach MIN_CONFIDENCE.
    """

    def __init__(self, model_dir: str, phrases: list[str]):
        import vosk

        vosk.SetLogLevel(-1)
        self.vosk = vosk
        self.phrases = phrases
        self.model = vosk.Model(model_dir)
        decoys = [w for w in DECOY_WORDS if w not in phrases]
        self.grammar = json.dumps(phrases + decoys + ["[unk]"], ensure_ascii=False)
        self.recognizer = vosk.KaldiRecognizer(self.model, SAMPLE_RATE, self.grammar)
        self.window = bytearray()
        self.candidate: str | None = None
        self.confirm_in = 0

    def reset(self) -> None:
        self.recognizer.Reset()
        self.window.clear()
        self.candidate = None

    def feed(self, frame: bytes) -> str | None:
        self.window.extend(frame)
        del self.window[: max(0, len(self.window) - int(CONFIRM_WINDOW_SECONDS * SAMPLE_RATE * 2))]
        if self.candidate:
            self.confirm_in -= len(frame)
            if self.confirm_in > 0:
                return None
            phrase, self.candidate = self.candidate, None
            self.recognizer.Reset()
            return phrase if self._confirmed(phrase) else None
        if self.recognizer.AcceptWaveform(frame):
            text = json.loads(self.recognizer.Result()).get("text", "")
        else:
            text = json.loads(self.recognizer.PartialResult()).get("partial", "")
        for phrase in self.phrases:
            if phrase in text:
                self.candidate = phrase
                self.confirm_in = int(CONFIRM_DELAY_SECONDS * SAMPLE_RATE * 2)
                break
        return None

    def _confirmed(self, phrase: str) -> bool:
        recognizer = self.vosk.KaldiRecognizer(self.model, SAMPLE_RATE, self.grammar)
        recognizer.SetWords(True)
        recognizer.AcceptWaveform(bytes(self.window))
        words = json.loads(recognizer.FinalResult()).get("result", [])
        wanted = phrase.split()
        for i in range(len(words) - len(wanted) + 1):
            run = words[i:i + len(wanted)]
            if [w["word"] for w in run] == wanted:
                confidence = min(w["conf"] for w in run)
                log.info("wake: '%s' candidate, confidence %.2f", phrase, confidence)
                return confidence >= MIN_CONFIDENCE
        log.info("wake: '%s' candidate rejected (heard %s)", phrase, " ".join(w["word"] for w in words) or "nothing")
        return False


class WakeListener(threading.Thread):
    """Feeds mic frames to the detectors while enabled; calls on_wake(name) on a detection."""

    def __init__(
        self,
        on_wake: Callable[[str], None],
        oww: OpenWakeWord | None,
        vosk_phrase: VoskPhrase | None,
        threshold: float = 0.5,
        cooldown: float = 2.0,
    ):
        super().__init__(name="wake", daemon=True)
        self.on_wake = on_wake
        self.oww = oww
        self.vosk_phrase = vosk_phrase
        self.threshold = threshold
        self.cooldown = cooldown
        self.frames: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self.enabled = threading.Event()
        self._reset_requested = threading.Event()
        self._last_wake = 0.0
        self._peak = 0.0
        self._peak_logged = time.monotonic()
        self.use_english = True
        self.use_russian = True

    def set_mode(self, mode: str) -> None:
        """'all': both phrases; 'russian': only "Привет, Джарвис"; 'off': the call button only."""
        english = mode == "all"
        if english and not self.use_english:
            self._reset_requested.set()  # its audio history is stale
        self.use_english, self.use_russian = english, mode in ("all", "russian")
        log.info("wake: mode %s", mode)

    def feed(self, frame: bytes) -> None:
        """Called from the audio thread; never blocks."""
        if not self.enabled.is_set():
            return
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass

    def pause(self) -> None:
        self.enabled.clear()

    def resume(self) -> None:
        self._reset_requested.set()
        self.enabled.set()

    def stop(self) -> None:
        self.enabled.clear()
        self.frames.put(None)

    def run(self) -> None:
        while True:
            frame = self.frames.get()
            if frame is None:
                return
            if self._reset_requested.is_set():
                self._reset_requested.clear()
                self._drain()
                for detector in (self.oww, self.vosk_phrase):
                    if detector:
                        detector.reset()
                continue
            if not self.enabled.is_set():
                continue
            try:
                name = self._detect(frame)
            except Exception:
                log.exception("wake detector failed")
                continue
            if name and time.monotonic() - self._last_wake > self.cooldown:
                self._last_wake = time.monotonic()
                self.pause()
                self.on_wake(name)

    def _drain(self) -> None:
        while True:
            try:
                if self.frames.get_nowait() is None:
                    self.frames.put(None)
                    return
            except queue.Empty:
                return

    def _detect(self, frame: bytes) -> str | None:
        if self.oww and self.use_english:
            score = self.oww.feed(frame)
            if score is not None:
                self._log_peak(score)
                if score >= self.threshold:
                    log.info("wake: 'hey jarvis' score %.2f", score)
                    return "hey jarvis"
        if self.vosk_phrase and self.use_russian:
            phrase = self.vosk_phrase.feed(frame)
            if phrase:
                log.info("wake: '%s'", phrase)
                return phrase
        return None

    def _log_peak(self, score: float) -> None:
        """Logs near-misses now and then, to help tune the threshold."""
        self._peak = max(self._peak, score)
        if time.monotonic() - self._peak_logged > 30:
            if self._peak >= 0.1:
                log.info("wake: highest 'hey jarvis' score in the last 30 s: %.2f", self._peak)
            self._peak, self._peak_logged = 0.0, time.monotonic()


def build(models_dir: str, on_wake: Callable[[str], None], threshold: float, vosk_phrases: list[str]) -> WakeListener | None:
    oww = vosk_phrase = None
    try:
        oww = OpenWakeWord(os.path.join(models_dir, "openwakeword"))
    except Exception as e:
        log.warning("wake: 'hey jarvis' detector not available: %s", e)
    if vosk_phrases:
        try:
            vosk_phrase = VoskPhrase(os.path.join(models_dir, "vosk-model-small-ru-0.22"), vosk_phrases)
        except Exception as e:
            log.warning("wake: Russian phrase detector not available: %s", e)
    if not oww and not vosk_phrase:
        return None
    log.info(
        "wake words: %s",
        ", ".join(filter(None, ["hey jarvis" if oww else "", *(vosk_phrases if vosk_phrase else [])])),
    )
    return WakeListener(on_wake, oww, vosk_phrase, threshold)
