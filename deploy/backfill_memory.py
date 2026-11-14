"""Summarises conversations already saved in history.jsonl into the searchable long-term memory.

Run once as the bot (reads its settings, prints no secrets):
  sudo -u voicebot bash -c 'set -a; . /etc/voicebot/voicebot.env; cd /opt/voicebot && venv/bin/python deploy/backfill_memory.py'
Conversations already in summaries.jsonl are skipped, so running it again is safe.
With --redo it first removes all saved summaries (Qdrant collection and summaries.jsonl) and writes them again.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voicebot import archive  # noqa: E402
from voicebot.app import MIN_CHARS_TO_SUMMARISE, MIN_SENTENCES_TO_SUMMARISE  # noqa: E402
from voicebot.brains.voicelive import VoiceLiveBrain  # noqa: E402
from voicebot.config import Config  # noqa: E402

SAME_SESSION_SECONDS = 5  # one conversation was briefly saved as several records, one per person


def sessions(path: str) -> list[tuple[float, list[tuple[str, str, str | None]]]]:
    grouped: list[tuple[float, list]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rows = [(r[0], r[1], r[2] if len(r) > 2 else record.get("person")) for r in record.get("turns", [])]
            if grouped and record["time"] - grouped[-1][0] < SAME_SESSION_SECONDS:
                grouped[-1][1].extend(rows)
            else:
                grouped.append((record["time"], rows))
    return grouped


async def main() -> None:
    cfg = Config.from_env()
    store = archive.build(cfg.models_dir, cfg.memory_dir, cfg.qdrant_url, cfg.qdrant_api_key)
    if not store:
        sys.exit("long-term memory is not available (see the warning above)")
    if "--redo" in sys.argv:
        store.qdrant.call("DELETE", f"/collections/{archive.COLLECTION}")
        store.qdrant.ensure_collection()
        if os.path.exists(store.file):
            os.remove(store.file)
        print("removed the saved summaries; writing them again")
    done = set()
    if os.path.exists(store.file):
        with open(store.file, encoding="utf-8") as f:
            done = {round(json.loads(line)["time"]) for line in f if line.strip()}
    brain = VoiceLiveBrain(cfg)
    added = 0
    for when, rows in sessions(os.path.join(cfg.memory_dir, "history.jsonl")):
        said = [text for role, text, _ in rows if role == "user"]
        if round(when) in done or len(said) < MIN_SENTENCES_TO_SUMMARISE \
                or sum(len(t) for t in said) < MIN_CHARS_TO_SUMMARISE:
            continue
        transcript = archive.transcript_text(rows)
        summary = await brain.summarize(archive.SUMMARY_INSTRUCTIONS, archive.summary_request(transcript),
                                            cfg.model)
        if not summary or summary.upper().startswith("NOTHING"):
            continue
        names = sorted({who for _, _, who in rows if who})
        store.add(when, names, summary, transcript)
        added += 1
        print(f"{archive.datetime.datetime.fromtimestamp(when):%Y-%m-%d %H:%M}  {', '.join(names) or '-'}: {summary}")
    print(f"added {added} summaries")


asyncio.run(main())
