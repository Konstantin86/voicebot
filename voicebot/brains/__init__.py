from ..config import Config
from .base import Brain, SessionIO


def create_brain(cfg: Config) -> Brain:
    if cfg.brain == "voicelive":
        from .voicelive import VoiceLiveBrain

        return VoiceLiveBrain(cfg)
    raise ValueError(f"unknown brain {cfg.brain!r}")


__all__ = ["Brain", "SessionIO", "create_brain"]
