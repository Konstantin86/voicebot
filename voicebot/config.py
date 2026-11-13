import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ (existing variables win)."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    brain: str
    endpoint: str
    api_key: str
    model: str
    smart_model: str
    api_version: str
    voice: str
    voices: list[str]
    turn_detection: str
    turn_silence_ms: int
    transcribe_model: str
    noise_reduction: str
    echo_cancellation: bool
    audio_device: str
    audio_card: str
    mixer_control: str
    mic_rate: int
    speaker_rate: int
    jabra_name: str
    context_file: str
    idle_timeout: float
    max_session: float
    state_file: str

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            brain=_env("BRAIN", "voicelive"),
            endpoint=_env("AZURE_VOICELIVE_ENDPOINT"),
            api_key=_env("AZURE_VOICELIVE_API_KEY"),
            model=_env("VOICELIVE_MODEL", "gpt-realtime-mini"),
            smart_model=_env("VOICELIVE_SMART_MODEL", "gpt-realtime"),
            api_version=_env("VOICELIVE_API_VERSION"),
            voice=_env("VOICE", "marin"),
            voices=[v.strip() for v in _env("VOICES", "marin,cedar,coral,ash,sage").split(",") if v.strip()],
            turn_detection=_env("TURN_DETECTION", "azure_semantic_vad_multilingual"),
            turn_silence_ms=int(_env("TURN_SILENCE_MS", "800")),
            transcribe_model=_env("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
            noise_reduction=_env("NOISE_REDUCTION"),
            echo_cancellation=_env_bool("ECHO_CANCELLATION", False),
            audio_device=_env("AUDIO_DEVICE", "plughw:CARD=UC,DEV=0"),
            audio_card=_env("AUDIO_CARD", "UC"),
            mixer_control=_env("MIXER_CONTROL", "PCM"),
            mic_rate=int(_env("MIC_RATE", "16000")),
            speaker_rate=int(_env("SPEAKER_RATE", "24000")),
            jabra_name=_env("JABRA_NAME", "Jabra"),
            context_file=_env("CONTEXT_FILE", "/etc/voicebot/context.md"),
            idle_timeout=float(_env("IDLE_TIMEOUT", "30")),
            max_session=float(_env("MAX_SESSION", "900")),
            state_file=_env("STATE_FILE", "/var/lib/voicebot/state.json"),
        )
        if cfg.voice not in cfg.voices:
            cfg.voices.insert(0, cfg.voice)
        if cfg.brain == "voicelive" and not (cfg.endpoint and cfg.api_key):
            raise ConfigError("AZURE_VOICELIVE_ENDPOINT and AZURE_VOICELIVE_API_KEY must be set")
        return cfg
