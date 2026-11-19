"""Short "Hi, I'm Marin." clips, one per voice, made once with the brain and kept on disk."""

import logging
import os
import re
from collections.abc import Awaitable, Callable

from .settings import voice_display_name

log = logging.getLogger(__name__)

Say = Callable[[str, str], Awaitable[bytes]]  # (text, voice) -> PCM16


class VoicePreviews:
    def __init__(self, folder: str, say: Say):
        self.folder = folder
        self.say = say

    def _path(self, voice: str) -> str:
        return os.path.join(self.folder, re.sub(r"[^\w.-]", "_", voice) + ".pcm")

    def cached(self, voice: str) -> bytes | None:
        try:
            with open(self._path(voice), "rb") as f:
                return f.read() or None
        except OSError:
            return None

    async def get(self, voice: str) -> bytes | None:
        """The clip for this voice; made now if missing. None if it cannot be made."""
        clip = self.cached(voice)
        if clip:
            return clip
        try:
            clip = await self.say(f"Hi, I'm {voice_display_name(voice)}.", voice)
        except Exception as e:
            log.warning("voice preview for %s failed: %s", voice, e or repr(e))
            return None
        if not clip:
            return None
        try:
            os.makedirs(self.folder, exist_ok=True)
            tmp = self._path(voice) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(clip)
            os.replace(tmp, self._path(voice))
            log.info("voice preview for %s saved", voice)
        except OSError as e:
            log.warning("cannot save voice preview for %s: %s", voice, e)
        return clip

    async def prepare(self, voices: list[str]) -> None:
        """Makes the missing clips; stops at the first failure (each try may cost money)."""
        for voice in voices:
            if not self.cached(voice) and not await self.get(voice):
                log.warning("voice previews: stopped preparing after a failure")
                return
