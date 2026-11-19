"""Choices changed with the Jabra dot button, saved so they survive restarts."""

import json
import logging
import os

log = logging.getLogger(__name__)


WAKE_MODES = ("all", "russian", "off")


def family(model: str) -> str:
    """Models that share a voice list: 'live' (GPT-Live) or 'realtime' (Voice Live)."""
    return "live" if model.startswith("gpt-live") else "realtime"


class RuntimeSettings:
    def __init__(self, path: str, voices: list[str], models: list[str], live_voices: list[str] | None = None):
        self.path = path
        self.voice_lists = {"realtime": voices, "live": live_voices or []}
        self.chosen = {name: options[0] for name, options in self.voice_lists.items() if options}
        self.models = models
        self.model = models[0]
        self.budget = 0.0  # daily spending limit in dollars; 0 = none
        self.wake = "all"
        self._load()

    @property
    def voices(self) -> list[str]:
        """Voices of the chosen model."""
        return self.voices_for(self.model)

    @property
    def voice(self) -> str:
        return self.voice_for(self.model)

    def voices_for(self, model: str) -> list[str]:
        return self.voice_lists[family(model)]

    def voice_for(self, model: str) -> str:
        return self.chosen.get(family(model), "")

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("cannot read %s: %s", self.path, e)
            return
        if data.get("voice") in self.voice_lists["realtime"]:
            self.chosen["realtime"] = data["voice"]
        if data.get("live_voice") in self.voice_lists["live"]:
            self.chosen["live"] = data["live_voice"]
        if data.get("model") in self.models:
            self.model = data["model"]
        if data.get("wake") in WAKE_MODES:
            self.wake = data["wake"]
        if isinstance(data.get("budget"), (int, float)) and data["budget"] >= 0:
            self.budget = float(data["budget"])

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"voice": self.chosen.get("realtime"), "live_voice": self.chosen.get("live"),
                           "model": self.model, "budget": self.budget, "wake": self.wake}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot save %s: %s", self.path, e)

    def next_voice(self) -> str:
        voices = self.voices
        self.chosen[family(self.model)] = voices[(voices.index(self.voice) + 1) % len(voices)]
        self._save()
        return self.voice

    def set_voice(self, voice: str) -> None:
        if voice not in self.voices:
            raise ValueError(f"unknown voice {voice!r}")
        self.chosen[family(self.model)] = voice
        self._save()

    def set_model(self, model: str) -> None:
        if model not in self.models:
            raise ValueError(f"unknown model {model!r}")
        self.model = model
        self._save()

    def set_wake(self, mode: str) -> None:
        if mode not in WAKE_MODES:
            raise ValueError(f"unknown wake mode {mode!r}")
        self.wake = mode
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
