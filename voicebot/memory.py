"""Long-term facts and short-term conversation history, saved on disk."""

import datetime
import json
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_FACT_CHARS = 300
MAX_TURN_CHARS = 400


@dataclass
class Fact:
    id: int
    text: str
    created: str


class Memory:
    def __init__(self, directory: str, recent_hours: float = 12, recent_turns: int = 20, max_facts: int = 100):
        self.directory = directory
        self.recent_hours = recent_hours
        self.recent_turns = recent_turns
        self.max_facts = max_facts
        self.facts_file = os.path.join(directory, "facts.json")
        self.history_file = os.path.join(directory, "history.jsonl")
        try:
            os.makedirs(directory, mode=0o750, exist_ok=True)
        except OSError as e:
            log.warning("memory: cannot create %s: %s", directory, e)

    # Long-term facts

    def facts(self) -> list[Fact]:
        try:
            with open(self.facts_file, encoding="utf-8") as f:
                return [Fact(**item) for item in json.load(f)]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError) as e:
            log.warning("memory: cannot read facts: %s", e)
            return []

    def _save_facts(self, facts: list[Fact]) -> None:
        tmp = self.facts_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([fact.__dict__ for fact in facts], f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.facts_file)

    def remember(self, text: str) -> dict:
        text = " ".join(text.split())[:MAX_FACT_CHARS]
        if not text:
            return {"error": "Empty fact."}
        facts = self.facts()
        if any(f.text.lower() == text.lower() for f in facts):
            return {"status": "already remembered"}
        if len(facts) >= self.max_facts:
            return {"error": f"Memory is full ({self.max_facts} facts). Ask the user what to forget."}
        fact = Fact(id=max((f.id for f in facts), default=0) + 1, text=text, created=_today())
        facts.append(fact)
        try:
            self._save_facts(facts)
        except OSError as e:
            log.warning("memory: cannot save facts: %s", e)
            return {"error": "Could not save."}
        log.info("memory: remembered [%d] %s", fact.id, text)
        return {"status": "remembered", "id": fact.id}

    def forget(self, fact_id: int) -> dict:
        facts = self.facts()
        kept = [f for f in facts if f.id != fact_id]
        if len(kept) == len(facts):
            return {"error": f"No fact with id {fact_id}."}
        try:
            self._save_facts(kept)
        except OSError as e:
            log.warning("memory: cannot save facts: %s", e)
            return {"error": "Could not save."}
        log.info("memory: forgot [%d]", fact_id)
        return {"status": "forgotten", "id": fact_id}

    # Short-term history

    def save_session(self, turns: list[tuple[str, str]]) -> None:
        """Appends one session's turns (role, text). Sessions without user speech are skipped."""
        if not any(role == "user" for role, _ in turns):
            return
        record = {
            "time": time.time(),
            "turns": [[role, text[:MAX_TURN_CHARS]] for role, text in turns if text.strip()],
        }
        try:
            with open(self.history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("memory: cannot save history: %s", e)

    def recent(self, now: float | None = None) -> list[tuple[float, str, str]]:
        """(session time, role, text) for the last recent_turns turns from the last recent_hours."""
        now = now or time.time()
        cutoff = now - self.recent_hours * 3600
        turns: list[tuple[float, str, str]] = []
        try:
            with open(self.history_file, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("memory: cannot read history: %s", e)
            return []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("time", 0) >= cutoff:
                turns.extend((record["time"], role, text) for role, text in record.get("turns", []))
        if len(lines) > 500:
            self._trim_history(lines[-200:])
        return turns[-self.recent_turns:]

    def _trim_history(self, lines: list[str]) -> None:
        tmp = self.history_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, self.history_file)
        except OSError as e:
            log.warning("memory: cannot trim history: %s", e)

    # Prompt text

    def prompt_text(self, now: float | None = None) -> str:
        parts = []
        facts = self.facts()
        if facts:
            parts.append(
                "Long-term memory (facts the user asked you to remember; id in brackets):\n"
                + "\n".join(f"[{f.id}] {f.text}" for f in facts)
            )
        recent = self.recent(now)
        if recent:
            lines, last_session = [], None
            for session_time, role, text in recent:
                if session_time != last_session:
                    stamp = datetime.datetime.fromtimestamp(session_time).strftime("%H:%M")
                    lines.append(f"-- conversation at {stamp} --")
                    last_session = session_time
                lines.append(f"{'User' if role == 'user' else 'You'}: {text}")
            parts.append(
                "Recent conversations (earlier sessions today; use only if relevant, "
                "do not bring them up unprompted):\n" + "\n".join(lines)
            )
        return "\n\n".join(parts)


def _today() -> str:
    return datetime.date.today().isoformat()
