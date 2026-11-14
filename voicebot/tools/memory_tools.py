"""remember / forget tools for long-term memory, and end_session."""

from ..memory import Memory
from . import Tool

END_SESSION = "end_session"


def make_tools(memory: Memory, current=None) -> list[Tool]:
    """current: speakers.Current; facts go to the recognised speaker's own memory unless shared."""

    def remember(fact: str, shared: bool = False) -> dict:
        person = None if shared or current is None else current.speaker
        return memory.remember(fact, person)

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
                "properties": {
                    "fact": {"type": "string", "description": "One short English sentence."},
                    "shared": {"type": "boolean", "description": "True for facts about the whole household "
                               "(home, pets, shared plans); false for facts about the person speaking."},
                },
                "required": ["fact"],
            },
            handler=remember,
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


def voice_tools(voices, current) -> list[Tool]:
    """remember_voice / forget_voice. voices: speakers.Voices; current: speakers.Current."""

    async def remember_voice(name: str) -> dict:
        if not current.session:
            return {"error": "Voice recognition is not running."}
        return await current.session.remember_voice(name)

    def forget_voice(name: str) -> dict:
        for known in voices.names():
            if known.lower() == name.strip().lower():
                voices.forget(known)
                return {"status": "voice forgotten", "name": known}
        return {"error": f"No saved voice called {name!r}.", "known_voices": voices.names()}

    return [
        Tool(
            name="remember_voice",
            description=(
                "Save the voice of the person speaking so you recognise them later ('remember my voice', "
                "'запам'ятай мій голос', 'запомни мой голос'). First ask their name if you do not know it. "
                "Uses what they said in this conversation; it may ask for a few more sentences first."
            ),
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The person's name as they said it, e.g. 'Костя'."}},
                "required": ["name"],
            },
            handler=remember_voice,
        ),
        Tool(
            name="forget_voice",
            description="Delete a saved voice when asked ('forget my voice', 'forget Anna's voice').",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=forget_voice,
        ),
    ]
