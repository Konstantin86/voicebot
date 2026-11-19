"""Spot a goodbye in the user's words (EN/RU/UK), as a backup to the model's end_session tool."""

import re

MAX_WORDS = 8

# Whole phrases only; single words that also occur in normal sentences ("всё", "stop") are left out.
PHRASES = [
    # English
    "bye", "bye bye", "goodbye", "good bye", "see you", "see ya", "thats it", "that is it", "thats all", "that is all",
    "thats all for now", "im done", "i am done", "nothing else", "good night", "hang up",
    # Russian
    "пока", "пока пока", "до свидания", "это всё", "это все", "всё спасибо", "все спасибо", "спасибо всё",
    "спасибо все", "больше ничего", "отбой", "спокойной ночи", "до связи", "на сегодня всё", "на сегодня все",
    # Ukrainian
    "бувай", "бувайте", "па па", "до побачення", "це все", "все дякую", "дякую все", "більше нічого",
    "на сьогодні все", "на добраніч", "до звязку",
]
# Polite words that may surround a goodbye: "Thank you, that's it", "Ну ладно, пока".
FILLER = {
    "thank", "thanks", "you", "ok", "okay", "well", "so", "then", "great", "cool", "alright", "all", "right",
    "yeah", "yes", "fine", "good", "very", "much", "now", "for", "and",
    "спасибо", "благодарю", "ну", "ладно", "хорошо", "окей", "да", "тогда", "отлично", "понял", "поняла",
    "дякую", "добре", "гаразд", "так", "тоді", "чудово", "зрозумів", "зрозуміла", "ага", "и", "і", "ще", "раз",
    "понятно", "зрозуміло",
}
_PHRASES = sorted((p.split() for p in PHRASES), key=len, reverse=True)


def normalize(text: str) -> list[str]:
    text = text.lower().replace("'", "").replace("’", "").replace("ʼ", "")
    return re.findall(r"[a-zа-яіїєґё]+", text)


def is_goodbye(text: str) -> bool:
    """True when the utterance, or its last part, is only goodbye phrases plus polite filler words, e.g.
    "Thank you, that's it.", "Всё, спасибо, пока!" or "Добре, все зрозуміло, тоді бувай"."""
    if _only_goodbye(text):
        return True
    parts = [p for p in re.split(r"[.,!;:—–\n]+", text) if p.strip()]
    return len(parts) > 1 and _only_goodbye(parts[-1])


def _only_goodbye(text: str) -> bool:
    if "?" in text:
        return False
    words = normalize(text)
    if not words or len(words) > MAX_WORDS:
        return False
    i, found = 0, False
    while i < len(words):
        for phrase in _PHRASES:
            if words[i:i + len(phrase)] == phrase:
                i += len(phrase)
                found = True
                break
        else:
            if words[i] not in FILLER:
                return False
            i += 1
    return found
