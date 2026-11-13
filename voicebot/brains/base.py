import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from ..audio import AudioIO
from ..tools import ToolRegistry


@dataclass
class SessionIO:
    """What a brain gets for one conversation."""

    mic: asyncio.Queue  # 20 ms mono PCM16 frames at AudioIO.mic_rate
    speaker: AudioIO
    instructions: str
    tools: ToolRegistry
    stop: asyncio.Event
    model: str
    voice: str
    greeting: str | None = None  # if set, the bot says this first
    thinking_sound: bytes | None = None  # played while a tool runs
    history: list[tuple[str, str]] = field(default_factory=list)  # (role, text) turns of this session
    end_ready: bool = False  # the bot said goodbye and wants to hang up
    user_speaking: bool = False
    last_activity: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        if self.user_speaking or self.speaker.is_playing():
            self.touch()
        return time.monotonic() - self.last_activity


class Brain(Protocol):
    async def run_session(self, session: SessionIO) -> None:
        """Run one conversation until session.stop is set or the brain ends it."""
