"""Who is speaking: voiceprints made on the Pi (sherpa-onnx + WeSpeaker), one per person.

A voiceprint is a list of 256 numbers describing how a voice sounds, whatever the language.
Profiles live in voices.json (bot-only). Nothing leaves the Pi.
"""

import asyncio
import datetime
import json
import logging
import os
from collections.abc import Callable

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
IDENTIFY_SECONDS = 4.0  # only the end of a long sentence is needed to recognise the voice
ENROL_MIN_SECONDS = 6.0
ENROL_MAX_SECONDS = 25.0
MIN_TURN_SECONDS = 0.6
RECORD_SECONDS = 60
FILE_MODE = 0o640


class Voices:
    """The household's voiceprints and the model that makes them."""

    def __init__(self, model_path: str, profiles_path: str, threshold: float = 0.6, margin: float = 0.08,
                 threads: int = 2):
        import numpy as np
        import sherpa_onnx

        self.np = np
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=threads)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.path = profiles_path
        self.threshold = threshold
        self.margin = margin
        self.profiles = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("voices: cannot read %s: %s", self.path, e)
            return {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.profiles, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def names(self) -> list[str]:
        return sorted(self.profiles)

    def embed(self, pcm: bytes):
        """Voiceprint (unit length) of 16 kHz mono PCM16 audio."""
        np = self.np
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        vector = np.array(self.extractor.compute(stream), dtype=np.float32)
        return vector / (np.linalg.norm(vector) or 1.0)

    def scores(self, vector) -> list[tuple[float, str]]:
        np = self.np
        return sorted(((float(np.dot(vector, np.array(p["embedding"], dtype=np.float32))), name)
                       for name, p in self.profiles.items()), reverse=True)

    def identify(self, vector) -> tuple[str | None, list[tuple[float, str]]]:
        ranked = self.scores(vector)
        if not ranked:
            return None, ranked
        best, name = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        if best >= self.threshold and best - second >= self.margin:
            return name, ranked
        return None, ranked

    def enrol(self, name: str, vector, seconds: float) -> None:
        """Saves a voiceprint; for a known name, blends it with the old one (weighted by speech time)."""
        np = self.np
        old = self.profiles.get(name)
        if old:
            total = old.get("seconds", 0) + seconds
            vector = (np.array(old["embedding"]) * old.get("seconds", 0) + vector * seconds) / total
            vector = vector / (np.linalg.norm(vector) or 1.0)
            seconds = total
        self.profiles[name] = {"embedding": [round(float(x), 5) for x in vector], "seconds": round(seconds, 1),
                               "updated": datetime.date.today().isoformat()}
        self._save()
        log.info("voices: saved voiceprint for %s (%.0f s of speech)", name, seconds)

    def forget(self, name: str) -> bool:
        if name not in self.profiles:
            return False
        del self.profiles[name]
        self._save()
        log.info("voices: forgot %s", name)
        return True


class VoiceSession:
    """One conversation: the audio of each sentence, and who is speaking."""

    def __init__(self, voices: Voices, person_context: Callable[[str], str] | None = None):
        self.voices = voices
        self.person_context = person_context
        self.segments: list[bytes] = []
        self.prints: list[tuple[object, float]] = []
        self.speaker: str | None = None
        self.announced = False
        self.recorded = bytearray()
        self.dropped = 0  # bytes cut from the front of the recording

    def record(self, pcm: bytes) -> None:
        """Keeps the last RECORD_SECONDS of what was sent to Azure, to cut out each sentence."""
        self.recorded.extend(pcm)
        extra = len(self.recorded) - int(RECORD_SECONDS * BYTES_PER_SECOND)
        if extra > 0:
            del self.recorded[:extra]
            self.dropped += extra

    def audio_between(self, start_ms: int, end_ms: int) -> bytes:
        start = max(0, int(start_ms * BYTES_PER_SECOND / 1000) - self.dropped) & ~1
        end = max(0, int(end_ms * BYTES_PER_SECOND / 1000) - self.dropped) & ~1
        return bytes(self.recorded[start:end])

    async def add_turn(self, pcm: bytes) -> None:
        seconds = len(pcm) / BYTES_PER_SECOND
        if seconds < MIN_TURN_SECONDS:
            return
        self.segments.append(pcm[-int(ENROL_MAX_SECONDS * BYTES_PER_SECOND):])
        if not self.voices.profiles:
            return
        tail = pcm[-int(IDENTIFY_SECONDS * BYTES_PER_SECOND):]
        vector = await asyncio.to_thread(self.voices.embed, tail)
        self.prints.append((vector, min(seconds, IDENTIFY_SECONDS)))
        name, ranked = self.voices.identify(self._average())
        log.info("voice: %s (%.1f s this turn, %d turns); scores %s", name or "not recognised", seconds,
                 len(self.prints), ", ".join(f"{n} {s:.2f}" for s, n in ranked[:3]))
        if name and not self.speaker:
            self.speaker = name

    def _average(self):
        np = self.voices.np
        total = sum(w for _, w in self.prints)
        mean = sum(v * w for v, w in self.prints) / total
        return mean / (np.linalg.norm(mean) or 1.0)

    def introduction(self) -> str | None:
        """Once per session, after the speaker is recognised: who it is and their own memory."""
        if not self.speaker or self.announced:
            return None
        self.announced = True
        text = (f"The person speaking now is {self.speaker} (recognised by voice). Greet them by name once, "
                f"naturally, in their language. Facts they ask you to remember are saved to their own memory.")
        if self.person_context:
            extra = self.person_context(self.speaker)
            if extra:
                text += "\n\n" + extra
        return text

    async def remember_voice(self, name: str) -> dict:
        name = " ".join(name.split()).strip().title()[:40]
        if not name:
            return {"error": "A name is needed."}
        seconds = sum(len(s) for s in self.segments) / BYTES_PER_SECOND
        if seconds < ENROL_MIN_SECONDS:
            return {"status": "need more speech", "seconds_heard": round(seconds, 1),
                    "instruction": "Ask the user to say a few sentences about anything (about 10 seconds), "
                                   "then call remember_voice again with the same name."}
        audio = b"".join(self.segments)[-int(ENROL_MAX_SECONDS * BYTES_PER_SECOND):]
        vector = await asyncio.to_thread(self.voices.embed, audio)
        self.voices.enrol(name, vector, min(seconds, ENROL_MAX_SECONDS))
        self.speaker, self.announced = name, True
        return {"status": "voice saved", "name": name,
                "instruction": f"Tell {name} you will recognise their voice from now on."}


class Current:
    """The running conversation's VoiceSession, for tools (one conversation at a time)."""

    session: VoiceSession | None = None

    @property
    def speaker(self) -> str | None:
        return self.session.speaker if self.session else None


def build(models_dir: str, profiles_path: str, threshold: float, margin: float) -> Voices | None:
    path = os.path.join(models_dir, "speaker", "wespeaker_en_voxceleb_resnet34_LM.onnx")
    try:
        voices = Voices(path, profiles_path, threshold, margin)
    except Exception as e:
        log.warning("voices: voice recognition not available: %s", e)
        return None
    log.info("voices: recognition on; known voices: %s", ", ".join(voices.names()) or "none yet")
    return voices
