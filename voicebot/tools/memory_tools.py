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


GOODBYE_EXAMPLES = (
    "English: bye, goodbye, see you, that's it, that's all, that's all for now, I'm done, "
    "nothing else, no thanks that's all, thanks that's it, good night, stop, hang up. "
    "Russian: пока, до свидания, всё, это всё, всё спасибо, больше ничего, хватит, отбой, "
    "спокойной ночи, давай, до связи. "
    "Ukrainian: бувай, па-па, до побачення, все, це все, все дякую, більше нічого, досить, "
    "на сьогодні все, на добраніч, до зв'язку."
)


def end_session_tool() -> Tool:
    return Tool(
        name=END_SESSION,
        description=(
            "End the conversation and hang up when the user says goodbye or signals they are done, "
            "in any language. Examples of such phrases: " + GOODBYE_EXAMPLES + " "
            "A plain 'thank you' in the middle of a conversation is not a goodbye. "
            "Always say a very short goodbye in the user's language first, then call this."
        ),
        parameters={"type": "object", "properties": {}},
        handler=lambda: {"status": "ending"},
    )
