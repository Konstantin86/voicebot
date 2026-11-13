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

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"voice": self.voice, "model": self.model}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save %s: %s", self.path, e)

    def next_voice(self) -> str:
        self.voice = self.voices[(self.voices.index(self.voice) + 1) % len(self.voices)]
        self._save()
        return self.voice

    def toggle_model(self) -> str:
        self.model = self.models[(self.models.index(self.model) + 1) % len(self.models)]
        self._save()
        return self.model


def voice_display_name(voice: str) -> str:
    """'marin' -> 'Marin', 'en-US-Ava:DragonHDLatestNeural' -> 'Ava'."""
    name = voice.split(":")[0].split("-")[-1]
    for suffix in ("MultilingualNeural", "Neural"):
        name = name.removesuffix(suffix)
    return name[:1].upper() + name[1:]
