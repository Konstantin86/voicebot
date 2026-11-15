"""Choices changed with the Jabra dot button, saved so they survive restarts."""

import json
import logging
import os

log = logging.getLogger(__name__)


class RuntimeSettings:
    def __init__(self, path: str, voices: list[str], models: list[str]):
        self.path = path
        self.voices = voices
        self.models = models
        self.voice = voices[0]
        self.model = models[0]
        self.budget = 0.0  # daily spending limit in dollars; 0 = none
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("cannot read %s: %s", self.path, e)
            return
        if data.get("voice") in self.voices:
            self.voice = data["voice"]
        if data.get("model") in self.models:
            self.model = data["model"]
        if isinstance(data.get("budget"), (int, float)) and data["budget"] >= 0:
            self.budget = float(data["budget"])

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"voice": self.voice, "model": self.model, "budget": self.budget}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save %s: %s", self.path, e)

    def next_voice(self) -> str:
        self.voice = self.voices[(self.voices.index(self.voice) + 1) % len(self.voices)]
        self._save()
        return self.voice

    def set_voice(self, voice: str) -> None:
        if voice not in self.voices:
            raise ValueError(f"unknown voice {voice!r}")
        self.voice = voice
        self._save()

    def set_model(self, model: str) -> None:
        if model not in self.models:
            raise ValueError(f"unknown model {model!r}")
        self.model = model
        self._save()

    def set_budget(self, dollars: float) -> None:
        if dollars < 0:
            raise ValueError("the limit cannot be negative")
        self.budget = round(float(dollars), 2)
        self._save()

    def toggle_model(self) -> str:
        self.model = self.models[(self.models.index(self.model) + 1) % len(self.models)]
        self._save()
        return self.model


def voice_display_name(voice: str) -> str:
    """'marin' -> 'Marin', 'en-US-Andrew2:DragonHDLatestNeural' -> 'Andrew HD',
    'en-US-Ava:DragonHDOmniLatestNeural' -> 'Ava Omni'."""
    name, _, model = voice.partition(":")
    name = name.split("-")[-1]
    for suffix in ("MultilingualNeural", "Neural"):
        name = name.removesuffix(suffix)
    name = name.rstrip("0123456789")
    name = name[:1].upper() + name[1:]
    if "DragonHDOmni" in model:
        return f"{name} Omni"
    if "DragonHD" in model:
        return f"{name} HD"
    return name
