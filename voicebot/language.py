"""Guess the reply language (English / Ukrainian / Russian) from a transcript."""

import json

UKRAINIAN_ONLY = set("іїєґ")
RUSSIAN_ONLY = set("ыэъё")


def detect_language(text: str) -> str | None:
    """Returns "English", "Ukrainian", "Russian" or None when unsure."""
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return None
    latin = sum("a" <= c <= "z" for c in letters)
    cyrillic = sum("а" <= c <= "я" or c in "іїєґёў" for c in letters)
    if latin > cyrillic:
        return "English"
    uk = sum(c in UKRAINIAN_ONLY for c in letters)
    ru = sum(c in RUSSIAN_ONLY for c in letters)
    if uk and not ru:
        return "Ukrainian"
    if ru and not uk:
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
