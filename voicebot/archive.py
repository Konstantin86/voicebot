"""Long-term conversation memory: a short summary of each conversation, searchable by meaning.

Summaries are turned into meaning vectors on the Pi (multilingual-e5-small, Russian/Ukrainian/English)
and kept in a local Qdrant collection. A plain copy goes to summaries.jsonl, so the index can be rebuilt.
Full conversation text is kept with each summary for TRANSCRIPT_DAYS, then removed.
"""

import asyncio
import datetime
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)

COLLECTION = "voicebot_memory"
DIMENSIONS = 384
MAX_TOKENS = 512
TRANSCRIPT_DAYS = 30
TRANSCRIPT_CHARS = 6000
BEST_TRANSCRIPT_CHARS = 3000  # of the closest match, returned with search results
MIN_SCORE = 0.7  # e5 scores sit high and close together: ranking is reliable, the score alone is not,
# so the closest few are returned and the model judges whether they answer the question
FILE_MODE = 0o640


class Embedder:
    """multilingual-e5-small: a list of 384 numbers describing the meaning of a text."""

    def __init__(self, model_dir: str, threads: int = 2):
        import numpy as np
        import onnxruntime
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(os.path.join(model_dir, "model.onnx"), options,
                                                    providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def embed(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """kind: 'query' for a question, 'passage' for stored text (the model expects these prefixes)."""
        np = self.np
        encoded = self.tokenizer.encode_batch([f"{kind}: {t}" for t in texts])
        ids = np.array([e.ids for e in encoded], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feeds)[0]
        summed = (hidden * mask[..., None]).sum(axis=1)
        mean = summed / np.maximum(mask.sum(axis=1, keepdims=True), 1)
        mean = mean / np.maximum(np.linalg.norm(mean, axis=1, keepdims=True), 1e-9)
        return mean.astype(float).tolist()


class Qdrant:
    """The few Qdrant REST calls needed (no client library)."""

    def __init__(self, url: str, api_key: str = ""):
        self.url = url.rstrip("/")
        self.api_key = api_key

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        if self.api_key:
            request.add_header("api-key", self.api_key)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")

    def ensure_collection(self) -> None:
        try:
            self.call("GET", f"/collections/{COLLECTION}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        self.call("PUT", f"/collections/{COLLECTION}", {"vectors": {"size": DIMENSIONS, "distance": "Cosine"}})
        self.call("PUT", f"/collections/{COLLECTION}/index", {"field_name": "time", "field_schema": "float"})
        log.info("archive: created Qdrant collection %s", COLLECTION)

    def upsert(self, point_id: str, vector: list[float], payload: dict) -> None:
        self.call("PUT", f"/collections/{COLLECTION}/points?wait=true",
                  {"points": [{"id": point_id, "vector": vector, "payload": payload}]})

    def search(self, vector: list[float], limit: int) -> list[dict]:
        result = self.call("POST", f"/collections/{COLLECTION}/points/search",
                           {"vector": vector, "limit": limit, "with_payload": True})
        return result.get("result", [])

    def drop_old_transcripts(self, before: float) -> None:
        self.call("POST", f"/collections/{COLLECTION}/points/payload/delete?wait=true",
                  {"keys": ["transcript"], "filter": {"must": [{"key": "time", "range": {"lt": before}}]}})


def transcript_text(turns: list[tuple[str, str, str | None]]) -> str:
    """(role, text, person) rows as plain lines: 'Костя: …', 'Someone: …', 'Bot: …'."""
    return "\n".join(f"{(who or 'Someone') if role == 'user' else 'Bot'}: {text}" for role, text, who in turns)


class Archive:
    def __init__(self, directory: str, embedder: Embedder, qdrant: Qdrant):
        self.file = os.path.join(directory, "summaries.jsonl")
        self.embedder = embedder
        self.qdrant = qdrant

    def start(self) -> None:
        self.qdrant.ensure_collection()
        try:
            self.qdrant.drop_old_transcripts(time.time() - TRANSCRIPT_DAYS * 86400)
        except Exception as e:
            log.warning("archive: could not remove old transcripts: %s", e)

    def add(self, when: float, people: list[str], summary: str, transcript: str) -> None:
        stamp = datetime.datetime.fromtimestamp(when)
        payload = {"time": when, "date": stamp.strftime("%Y-%m-%d %H:%M"), "people": people,
                   "summary": summary, "transcript": transcript[:TRANSCRIPT_CHARS]}
        vector = self.embedder.embed([summary])[0]
        self.qdrant.upsert(str(uuid.uuid5(uuid.NAMESPACE_URL, f"voicebot/{when}")), vector, payload)
        record = {k: v for k, v in payload.items() if k != "transcript"}
        fd = os.open(self.file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info("archive: saved summary of the conversation at %s: %s", payload["date"], summary)

    def search(self, query: str, limit: int = 3) -> list[dict]:
        vector = self.embedder.embed([query], kind="query")[0]
        hits = [h for h in self.qdrant.search(vector, limit) if h.get("score", 0) >= MIN_SCORE]
        log.info("archive: search %r -> %s", query, ", ".join(f"{h['payload']['date']} {h['score']:.2f}" for h in hits)
                 or "nothing")
        return hits


def search_tool(archive: Archive):
    from .tools import Tool

    async def search_memory(query: str) -> dict:
        try:
            hits = await asyncio.to_thread(archive.search, query)
        except Exception as e:
            log.warning("archive: search failed: %s", e)
            return {"error": "Memory search is not available right now."}
        if not hits:
            return {"found": [], "note": "Nothing in past conversations. Say you don't remember it."}
        found = [{"date": h["payload"]["date"], "people": h["payload"].get("people") or [],
                  "summary": h["payload"]["summary"]} for h in hits]
        best = hits[0]["payload"].get("transcript")
        result = {"found": found, "note": "These are the closest past conversations, best first; they may be "
                  "unrelated. Use only what answers the question and mention roughly when it was; if none fits, say "
                  "you don't remember. 'people' = who was recognised by voice (may be incomplete)."}
        if best:
            result["closest_transcript"] = best[:BEST_TRANSCRIPT_CHARS]
        return result

    return Tool(
        name="search_memory",
        description=(
            "Search summaries of past conversations (older than your recent conversations), by meaning, in any "
            "language. Use it when the user asks about something discussed before that you don't see in memory "
            "('what happened to our sofa?', 'what did we decide about the car?'), before saying you don't remember."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for, in a few words, "
                                     "e.g. 'dog peed on the sofa, cleaning'."}},
            "required": ["query"],
        },
        handler=search_memory,
    )


SUMMARY_INSTRUCTIONS = (
    "You write entries for a searchable memory of a home voice assistant. The user message is a transcript "
    "(it may be in Russian or Ukrainian). Write the entry in English only, whatever language the transcript is in: "
    "one short paragraph of 1-3 sentences, no list, with the concrete facts, events, decisions and plans "
    "(who, what, numbers, names, places). Name people as the transcript does, in Latin letters (Костя = Kostya, "
    "Алина = Alina, Владик = Vladik); 'Someone' means a voice that was not recognised. Leave out greetings, "
    "small talk and the assistant's general advice unless it was acted on. "
    "If nothing worth remembering happened, answer exactly: NOTHING"
)


def summary_request(transcript: str) -> str:
    return "Transcript:\n" + transcript


def build(models_dir: str, memory_dir: str, qdrant_url: str, qdrant_key: str = "") -> Archive | None:
    if not qdrant_url:
        return None
    try:
        archive = Archive(memory_dir, Embedder(os.path.join(models_dir, "e5")), Qdrant(qdrant_url, qdrant_key))
        archive.start()
    except Exception as e:
        log.warning("archive: long-term conversation memory not available: %s", e)
        return None
    log.info("archive: long-term conversation memory on (Qdrant %s, collection %s)", qdrant_url, COLLECTION)
    return archive
