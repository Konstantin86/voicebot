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
RECENT_CHARS = 8000  # about 2,500 tokens of recent conversations in each new session
FILE_MODE = 0o640  # bot and its group only: conversations are private


@dataclass
class Fact:
    id: int
    text: str
    created: str
    person: str | None = None  # None = shared by the household


class Memory:
    def __init__(self, directory: str, recent_hours: float = 12, recent_turns: int = 80, max_facts: int = 100):
        self.directory = directory
        self.recent_hours = recent_hours
        self.recent_turns = recent_turns
        self.max_facts = max_facts
        self.facts_file = os.path.join(directory, "facts.json")
        self.history_file = os.path.join(directory, "history.jsonl")
        try:
            os.makedirs(directory, mode=0o750, exist_ok=True)
            for path in (self.facts_file, self.history_file):
                if os.path.exists(path):
                    os.chmod(path, FILE_MODE)
        except OSError as e:
            log.warning("memory: cannot prepare %s: %s", directory, e)

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
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, self.facts_file)

    def remember(self, text: str, person: str | None = None) -> dict:
        text = " ".join(text.split())[:MAX_FACT_CHARS]
        if not text:
            return {"error": "Empty fact."}
        facts = self.facts()
        if any(f.text.lower() == text.lower() and f.person == person for f in facts):
            return {"status": "already remembered"}
        if len(facts) >= self.max_facts:
            return {"error": f"Memory is full ({self.max_facts} facts). Ask the user what to forget."}
        fact = Fact(id=max((f.id for f in facts), default=0) + 1, text=text, created=_today(), person=person)
        facts.append(fact)
        try:
            self._save_facts(facts)
        except OSError as e:
            log.warning("memory: cannot save facts: %s", e)
            return {"error": "Could not save."}
        log.info("memory: remembered [%d] %s%s", fact.id, text, f" (for {person})" if person else "")
        return {"status": "remembered", "id": fact.id, "for": person or "everyone"}

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

    def save_session(self, turns: list[tuple[str, str]], person: str | None = None,
                     people: dict[int, str] | None = None) -> None:
        """Appends one session's turns (role, text). people: history index of a user turn -> who said it;
        a reply belongs to the person whose sentence it answers. Sessions without user speech are skipped."""
        if not any(role == "user" for role, _ in turns):
            return
        rows = [[role, text[:MAX_TURN_CHARS], who] for role, text, who in with_people(turns, people, person)]
        record = {"time": time.time(), "turns": rows}
        try:
            fd = os.open(self.history_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            os.chmod(self.history_file, FILE_MODE)
        except OSError as e:
            log.warning("memory: cannot save history: %s", e)

    def _recent_turns(self, now: float | None) -> list[tuple[float, str | None, str, str]]:
        """(session time, person, role, text) from the last recent_hours, oldest first."""
        now = now or time.time()
        cutoff = now - self.recent_hours * 3600
        try:
            with open(self.history_file, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("memory: cannot read history: %s", e)
            return []
        turns = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("time", 0) < cutoff:
                continue
            for row in record.get("turns", []):
                who = row[2] if len(row) > 2 else record.get("person")
                turns.append((record["time"], who, row[0], row[1]))
        if len(lines) > 500:
            self._trim_history(lines[-200:])
        return turns

    def _within_budget(self, turns: list) -> list:
        kept, size = [], 0
        for turn in reversed(turns[-self.recent_turns:]):
            size += len(turn[-1])
            if size > RECENT_CHARS:
                break
            kept.append(turn)
        return kept[::-1]

    def recent(self, now: float | None = None, person: str | None = None) -> list[tuple[float, str, str]]:
        """(session time, role, text) of this person's recent turns (None = nobody recognised)."""
        return self._within_budget([(when, role, text) for when, who, role, text in self._recent_turns(now)
                                    if who == person])

    def recent_all(self, now: float | None = None) -> list[tuple[float, str | None, str, str]]:
        """The whole household's recent turns: (session time, person, role, text)."""
        return self._within_budget(self._recent_turns(now))

    def _trim_history(self, lines: list[str]) -> None:
        tmp = self.history_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, self.history_file)
        except OSError as e:
            log.warning("memory: cannot trim history: %s", e)

    # Prompt text

    def prompt_text(self, now: float | None = None, person: str | None = None) -> str:
        """Memory for the prompt. Without a person: shared facts and the whole household's recent
        conversations. With a person: only that person's own facts."""
        parts = []
        facts = [f for f in self.facts() if f.person == person]
        if facts:
            title = (f"{person}'s own memory (facts {person} asked you to remember; id in brackets)" if person
                     else "Long-term memory (shared household facts; id in brackets)")
            parts.append(title + ":\n" + "\n".join(f"[{f.id}] {f.text}" for f in facts))
        recent = [] if person else self.recent_all(now)
        if recent:
            lines, last_session = [], None
            for session_time, who, role, text in recent:
                if session_time != last_session:
                    stamp = datetime.datetime.fromtimestamp(session_time).strftime("%H:%M")
                    lines.append(f"-- conversation at {stamp} --")
                    last_session = session_time
                lines.append(f"{(who or 'Someone') if role == 'user' else 'You'}: {text}")
            parts.append(
                "Recent conversations of the household (earlier sessions in the last hours; 'Someone' = voice "
                "not recognised; use only if relevant, do not bring them up unprompted). Your past replies "
                "below may be in the wrong language; never copy their language:\n" + "\n".join(lines)
            )
        return "\n\n".join(parts)


def with_people(turns: list[tuple[str, str]], people: dict[int, str] | None,
                person: str | None = None) -> list[tuple[str, str, str | None]]:
    """(role, text, who) for each non-empty turn; a reply belongs to the person whose sentence it answers."""
    rows, owner = [], person
    for index, (role, text) in enumerate(turns):
        if role == "user" and people is not None:
            owner = people.get(index)
        if text.strip():
            rows.append((role, text, owner))
    return rows


def _today() -> str:
    return datetime.date.today().isoformat()
