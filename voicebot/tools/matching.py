"""Helpers for shopping tools: does a product name match the request, and what prices does a text mention."""

import re

# Latin words that describe rather than identify a product.
DESCRIPTIVE_WORDS = {
    "with", "inch", "inches", "the", "and", "for", "price", "prices", "cheap", "cheapest", "best",
    "buy", "new", "processor", "chip", "laptop", "notebook", "phone", "graphics", "card", "video",
    "kharkiv", "kharkov", "ukraine", "lowest", "minimum",
}
PRICE = re.compile(r"(\d{1,3}(?:[   ,.]\d{3})+|\d{3,7})\s*(?:грн|₴|uah)", re.IGNORECASE)
MIN_PRICE_UAH = 100


def normalize(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9а-яіїєґё]+", " ", text.lower()) + " "


def key_words(request: str) -> list[str]:
    """Words a matching product name must contain: tokens with digits ('5070', 'm5', '128gb')
    and Latin words of 4+ letters ('macbook'). Cyrillic words are ignored, since product
    names on shop sites use Latin brand and model names."""
    words = []
    for w in normalize(request).split():
        has_digit = any(c.isdigit() for c in w)
        latin_word = re.fullmatch(r"[a-z]{4,}", w) and w not in DESCRIPTIVE_WORDS
        if (has_digit or latin_word) and w not in words:
            words.append(w)
    return words


def matches(name: str | None, words: list[str]) -> bool:
    if not words:
        return True
    text = normalize(name or "")
    return all(f" {w} " in text or (any(c.isdigit() for c in w) and w in text) for w in words)


def prices_uah(text: str) -> list[int]:
    found = []
    for raw in PRICE.findall(text or ""):
        value = int(re.sub(r"\D", "", raw))
        if value >= MIN_PRICE_UAH:
            found.append(value)
    return found
