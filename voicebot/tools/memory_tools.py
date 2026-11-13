"""remember / forget tools for long-term memory, and end_session."""

from ..memory import Memory
from . import Tool

END_SESSION = "end_session"


def make_tools(memory: Memory) -> list[Tool]:
    return [
        Tool(
            name="remember",
            description=(
                "Save a fact to long-term memory when the user asks you to remember something "
                "(any language: 'remember…', 'запам'ятай…', 'запомни…'). Write the fact as one short "
                "English sentence in the third person, e.g. 'The cat is called Murka.'"
            ),
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "One short English sentence."}},
                "required": ["fact"],
            },
            handler=lambda fact: memory.remember(fact),
        ),
        Tool(
            name="forget",
            description=(
                "Delete a fact from long-term memory when the user asks you to forget it. "
                "Use the id shown in brackets in your long-term memory. Call once per fact."
            ),
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "description": "Fact id from long-term memory."}},
                "required": ["id"],
            },
            handler=lambda id: memory.forget(int(id)),
        ),
    ]


def end_session_tool() -> Tool:
    return Tool(
        name=END_SESSION,
        description=(
            "End the conversation and hang up. Call it when the user says goodbye or clearly wants to stop "
            "talking, in any language (e.g. 'bye', 'that's all', 'пока', 'всё, спасибо', 'бувай', "
            "'на сьогодні все'). First say a very short goodbye in the user's language, then call this."
        ),
        parameters={"type": "object", "properties": {}},
        handler=lambda: {"status": "ending"},
    )
