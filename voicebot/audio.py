"""Jabra audio through aplay/arecord child processes.

The Jabra capture only works while a playback stream is open, so playback runs all
the time and writes silence when there is nothing to say.
"""

import fcntl
import logging
import subprocess
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

F_SETPIPE_SZ = 1031
FRAME_MS = 20
ALSA_BUFFER_US = 80_000


class AudioIO:
    def __init__(self, device: str, mic_rate: int, speaker_rate: int):
        self.device = device
        self.mic_rate = mic_rate
        self.speaker_rate = speaker_rate
        self.mic_frame_bytes = mic_rate * FRAME_MS // 1000 * 2
        self._spk_frame_bytes = speaker_rate * FRAME_MS // 1000 * 2
        self._silence = bytes(self._spk_frame_bytes)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._on_mic: Callable[[bytes], None] | None = None
        self._stop = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervise, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=3)

    def set_mic_callback(self, callback: Callable[[bytes], None] | None) -> None:
        """Called from the audio thread with 20 ms mono PCM16 frames."""
        self._on_mic = callback

    def play(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def is_playing(self) -> bool:
        with self._lock:
            return bool(self._buf)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception:
                log.exception("audio failure")
            self._kill()
            if not self._stop.is_set():
                log.warning("audio streams stopped, restarting in 2 s")
                self._stop.wait(2)

    def _spawn(self, cmd: list[str], **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, bufsize=0, **kwargs)
        self._procs = [p for p in self._procs if p.poll() is None] + [proc]
        threading.Thread(target=_log_stderr, args=(proc, cmd[0]), daemon=True).start()
        return proc

    def _run_once(self) -> None:
        fmt = ["-q", "-D", self.device, "-t", "raw", "-f", "S16_LE", "-c", "1", "-B", str(ALSA_BUFFER_US)]
        player = self._spawn(["aplay", *fmt, "-r", str(self.speaker_rate)], stdin=subprocess.PIPE)
        try:
            # A small pipe keeps queued audio short, so barge-in stops speech quickly.
            fcntl.fcntl(player.stdin.fileno(), F_SETPIPE_SZ, 4096)
        except OSError:
            pass
        threading.Thread(target=self._write_loop, args=(player,), name="audio-out", daemon=True).start()
        # After the Jabra has been idle, the first captures can fail. Retry the recorder only,
        # keeping playback open, with growing waits.
        delay = 0.3
        while not self._stop.is_set() and player.poll() is None:
            self._stop.wait(delay)
            recorder = self._spawn(["arecord", *fmt, "-r", str(self.mic_rate)], stdout=subprocess.PIPE)
            worked = self._read_loop(recorder)
            if recorder.poll() is None:
                recorder.terminate()
                recorder.wait(timeout=2)
            if self._stop.is_set():
                break
            if worked:
                log.warning("microphone stream stopped, restarting")
                delay = 0.3
            else:
                log.warning("microphone stream failed, retrying in %.1f s", delay)
                delay = min(delay * 2, 5)

    def _write_loop(self, player: subprocess.Popen) -> None:
        n = self._spk_frame_bytes
        try:
            while not self._stop.is_set() and player.poll() is None:
                with self._lock:
                    chunk = bytes(self._buf[:n])
                    del self._buf[:n]
                if len(chunk) < n:
                    chunk += self._silence[: n - len(chunk)]
                player.stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError):
            pass
        self._kill()

    def _read_loop(self, recorder: subprocess.Popen) -> bool:
        """Read mic frames until the recorder ends. Returns whether any audio arrived."""
        n = self.mic_frame_bytes
        got_audio = False
        while not self._stop.is_set():
            data = recorder.stdout.read(n)
            while data and len(data) < n:
                more = recorder.stdout.read(n - len(data))
                if not more:
                    break
                data += more
            if len(data) < n:
                break
            if not got_audio:
                got_audio = True
                log.info("audio streams open on %s", self.device)
            callback = self._on_mic
            if callback:
                callback(data)
        return got_audio

    def _kill(self) -> None:
        procs, self._procs = self._procs, []
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _log_stderr(proc: subprocess.Popen, name: str) -> None:
    for line in iter(proc.stderr.readline, b""):
        if b"Interrupted system call" in line:
            continue
        log.warning("%s: %s", name, line.decode(errors="replace").rstrip())
