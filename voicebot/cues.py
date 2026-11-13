import logging
import math
import wave
from array import array
from pathlib import Path

log = logging.getLogger(__name__)
SOUNDS_DIR = Path(__file__).parent / "sounds"

START = [(660, 0.08), (0, 0.03), (880, 0.12)]
END = [(880, 0.08), (0, 0.03), (587, 0.14)]
ERROR = [(220, 0.12), (0, 0.05), (220, 0.12)]
MODEL_BASIC = [(440, 0.18)]
MODEL_SMART = [(880, 0.07), (0, 0.04), (1175, 0.07)]


def render(notes: list[tuple[float, float]], rate: int, volume: float = 0.25) -> bytes:
    samples = array("h")
    fade = int(rate * 0.005)
    for freq, duration in notes:
        count = int(rate * duration)
        for i in range(count):
            if freq == 0:
                samples.append(0)
                continue
            envelope = min(1.0, i / fade, (count - i) / fade)
            value = volume * envelope * math.sin(2 * math.pi * freq * i / rate)
            samples.append(int(value * 32767))
    return samples.tobytes()


def load_sound(name: str, rate: int) -> bytes | None:
    """Mono 16-bit WAV from voicebot/sounds at the speaker rate, or None if missing or in another format."""
    path = SOUNDS_DIR / f"{name}.wav"
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (rate, 1, 2):
                log.warning("%s is not %d Hz mono 16-bit, using a beep instead", path.name, rate)
                return None
            return w.readframes(w.getnframes())
    except FileNotFoundError:
        return None
