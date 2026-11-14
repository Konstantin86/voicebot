"""Guess the reply language (English / Ukrainian / Russian) from a transcript."""

import json
import re

UKRAINIAN_ONLY = set("іїєґ")
RUSSIAN_ONLY = set("ыэъё")

# Common words that exist in only one of the two languages (spelled without the letters above).
RUSSIAN_WORDS = {
    "как", "что", "это", "какая", "какой", "какое", "какие", "где", "когда", "почему", "сколько",
    "будет", "тоже", "спасибо", "хорошо", "пожалуйста", "нет", "мне", "меня", "тебя", "сегодня",
    "привет", "пока", "ладно", "можно", "нужно", "расскажи", "подскажи", "его", "она",
    "они", "или", "если", "только", "очень", "сейчас", "здравствуй", "здравствуйте", "всего",
}
UKRAINIAN_WORDS = {
    "як", "що", "це", "яка", "який", "яке", "де", "коли", "скільки", "буде", "теж", "дякую", "добре",
    "будь", "ласка", "ні", "мені", "мене", "бувай", "гаразд", "можна", "треба",
    "розкажи", "підкажи", "чи", "або", "якщо", "тільки", "дуже", "зараз", "вона", "вони",
}


def detect_language(text: str) -> str | None:
    """Returns "English", "Ukrainian", "Russian" or None when unsure."""
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return None
    latin = sum("a" <= c <= "z" for c in letters)
    cyrillic = sum("а" <= c <= "я" or c in "іїєґёў" for c in letters)
    if latin > cyrillic:
        return "English"
    words = re.findall(r"\w+", text.lower())
    uk = sum(c in UKRAINIAN_ONLY for c in letters) + sum(w in UKRAINIAN_WORDS for w in words)
    ru = sum(c in RUSSIAN_ONLY for c in letters) + sum(w in RUSSIAN_WORDS for w in words)
    if uk > ru:
        return "Ukrainian"
    if ru > uk:
        return "Russian"
    return None


def add_language_hint(tool_output: str, language: str | None) -> str:
    """Adds reply_language to a JSON tool result so the model answers in the user's language."""
    if not language:
        return tool_output
    try:
        data = json.loads(tool_output)
    except ValueError:
        data = {"result": tool_output}
    if not isinstance(data, dict):
        data = {"result": data}
    data["reply_language"] = language
    data["instruction"] = f"Answer the user in {language} only."
    return json.dumps(data, ensure_ascii=False)
