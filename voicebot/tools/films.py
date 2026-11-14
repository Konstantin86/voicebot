"""Film list for the disk attached to the TV: find a film by a spoken title.

The list is a text file with one line per video: "<path on the disk>\\t<size in bytes>".
Make it with the disk attached to the Pi (see deploy/index_films.sh).
"""

import difflib
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Release details that are not part of a title.
NOISE = re.compile(
    r"\b(\d{3,4}p|bdrip|bdremux|remux|blu-?ray|web-?dl|webrip|hdtv|hdr\d*\+?|dv|dovi|x26[45]|h\.?26[45]|hevc|avc|"
    r"10bit|uhd|4k|imax|hybrid|open matte|lic|dual|triple|rus|eng|ukr|mvo|avo|dub|ddp?\d?\.?\d?|dts|atmos|"
    r"aac\d?\.?\d?|ac3|ma|nf|amzn|atvp|dsnp|hmax)\b",
    re.IGNORECASE,
)
YEAR = re.compile(r"\b(19[3-9]\d|20[0-4]\d)\b")
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8"}
QUERY_SHARE = 0.7  # the rest of the score is how much of the title the request covers
SIMILAR = 0.8  # word similarity that still counts, for typos and spelling differences
NEAR_WEIGHT = 0.8  # a near word counts a little less than an exact one
STOP_WORDS = {"the", "a", "an", "and", "of", "film", "movie", "фильм", "фільм", "мультик", "мультфильм"}


@dataclass
class Film:
    path: str
    size: int
    title: str
    year: int | None
    words: set[str]
    numbers: set[str]
    title_words: set[str]


def words_of(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9а-яіїєґё']+", text.lower())
    return [ROMAN.get(w, w) for w in words if w not in STOP_WORDS]


def word_match(word: str, words: set[str]) -> float:
    """1 for the same word; NEAR_WEIGHT for a near one (typo, other ending: 'моану' ~ 'моана')."""
    if word in words:
        return 1.0
    if word.isdigit() or len(word) < 4:
        return 0.0
    for other in words:
        if other.isdigit() or len(other) < 4:
            continue
        common = os.path.commonprefix([word, other])
        if len(common) >= 4 and len(common) >= min(len(word), len(other)) - 2:
            return NEAR_WEIGHT
        if difflib.SequenceMatcher(None, word, other).ratio() >= SIMILAR:
            return NEAR_WEIGHT
    return 0.0


def parse(path: str, size: int = 0) -> Film:
    folder, name = os.path.split(path)
    name = os.path.splitext(name)[0]
    name = re.sub(r"^(#?\d{1,2}[.\s-]+)", "", name)  # "#01 ", "1. ", "01. "
    text = re.sub(r"[._\[\]()]+", " ", name)
    year_match = YEAR.search(text)
    title = text[: year_match.start()] if year_match else text
    title = re.split(NOISE, title)[0] if NOISE.search(title) else title
    title = re.sub(r"\s+", " ", title).strip(" -")
    folder_words = set(words_of(re.sub(r"[._]+", " ", NOISE.sub(" ", folder))))
    title_words = words_of(title)
    return Film(
        path=path,
        size=size,
        title=title or name,
        year=int(year_match.group(1)) if year_match else None,
        words=set(title_words) | folder_words,
        numbers={w for w in title_words if w.isdigit() and w != "1"},  # "Шрек 1" is the original film
        title_words=set(title_words),
    )


def load(index_path: str) -> list[Film]:
    films = []
    try:
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                path, _, size = line.rstrip("\n").partition("\t")
                if path:
                    films.append(parse(path, int(size or 0)))
    except FileNotFoundError:
        return []
    return films


def score(film: Film, queries: list[str], year: int | None) -> float:
    best = 0.0
    for query in queries:
        wanted = words_of(query)
        if not wanted:
            continue
        found = sum(word_match(w, film.words) for w in wanted) / len(wanted)
        if found == 0:
            continue
        # Prefer titles the request covers fully: "Frozen" is "Frozen", not "Olaf's Frozen Adventure".
        title = film.title_words - {"1"}
        covered = sum(word_match(t, set(wanted)) for t in title) / len(title) if title else 0.0
        found = QUERY_SHARE * found + (1 - QUERY_SHARE) * covered
        wanted_numbers = {w for w in wanted if w.isdigit()}
        extra = film.numbers - wanted_numbers  # "Moana 2" when "Moana" was asked
        missing = wanted_numbers - film.numbers - ({str(film.year)} if film.year else set())
        value = found - 0.3 * len(extra) - 0.3 * len(missing)
        best = max(best, value)
    if year and film.year == year:
        best += 0.2
    return best


def find(films: list[Film], queries: list[str], year: int | None = None, limit: int = 5) -> list[tuple[float, Film]]:
    ranked = sorted(((score(f, queries, year), f) for f in films), key=lambda x: (-x[0], x[1].path))
    return [(s, f) for s, f in ranked if s >= 0.5][:limit]
