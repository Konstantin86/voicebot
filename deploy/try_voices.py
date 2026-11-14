"""Records each voice saying the same short sentence in English, Ukrainian and Russian.

Run as the bot (reads its settings, prints no secrets):
  sudo -u voicebot bash -c 'set -a; . /etc/voicebot/voicebot.env; cd /opt/voicebot && venv/bin/python deploy/try_voices.py'
WAV files go to /tmp/voicetest/ (one per voice; the three languages back to back).
"""

import asyncio
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voicebot.brains.voicelive import VoiceLiveBrain  # noqa: E402
from voicebot.config import Config  # noqa: E402

VOICES = sys.argv[1:] or [
    "marin",
    "en-US-Ava:DragonHDLatestNeural",
    "en-US-Andrew2:DragonHDLatestNeural",
    "en-US-Emma2:DragonHDLatestNeural",
    "en-US-Ava:DragonHDOmniLatestNeural",
    "uk-UA-Polina:DragonHDOmniLatestNeural",
    "uk-UA-Ostap:DragonHDOmniLatestNeural",
    "ru-RU-Svetlana:DragonHDOmniLatestNeural",
    "ru-RU-Dmitry:DragonHDOmniLatestNeural",
]
PHRASES = [
    "Tomorrow it'll be around five degrees and cloudy, so take a warm jacket.",
    "Завтра буде близько п'яти градусів і хмарно, тож візьми теплу куртку.",
    "Завтра будет около пяти градусов и облачно, так что возьми тёплую куртку.",
]
OUT = "/tmp/voicetest"
RATE = 24000
PAUSE = b"\x00\x00" * (RATE // 2)


async def main() -> None:
    cfg = Config.from_env()
    brain = VoiceLiveBrain(cfg)
    os.makedirs(OUT, exist_ok=True)
    os.chmod(OUT, 0o755)
    for voice in VOICES:
        audio = b""
        try:
            for phrase in PHRASES:
                audio += await brain.say(phrase, voice, cfg.model) + PAUSE
        except Exception as e:
            print(f"{voice:45} FAILED: {str(e)[:150]}")
            continue
        path = os.path.join(OUT, voice.replace(":", "_") + ".wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio)
        os.chmod(path, 0o644)
        print(f"{voice:45} OK, {len(audio) / 2 / RATE:.1f} s -> {path}")


asyncio.run(main())
