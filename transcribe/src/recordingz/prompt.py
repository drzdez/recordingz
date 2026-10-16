"""Build the ASR initial prompt from what we know about the meeting (title, participants, glossary).

Whisper's prompt is a soft hint: it steers spelling of names and domain terms and the general style.
Keep it short (the model sees ~224 tokens of it).
"""

from __future__ import annotations

import re
import unicodedata

from .recording import Recording


def build_prompt(rec: Recording, glossary: tuple[str, ...] | list[str]) -> str | None:
    parts: list[str] = []
    title = rec.title.strip()
    if title and title != rec.stem:
        parts.append(f"Schůzka: {title}.")
    names = rec.participants
    if names:
        parts.append("Účastníci: " + ", ".join(names) + ".")
    if glossary:
        parts.append("Pojmy: " + ", ".join(dict.fromkeys(g.strip() for g in glossary if g.strip())) + ".")
    return " ".join(parts) if parts else None


PROMPT_LABELS = ("Schůzka", "Účastníci", "Pojmy")  # the labels build_prompt writes


def _words(text: str) -> list[str]:
    """Words without case, diacritics and punctuation: "Účastníci:" and "ucastnici" are the same word."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))
    return re.findall(r"\w+", plain.casefold())


def is_prompt_echo(text: str, title: str, names: list[str], glossary: tuple[str, ...] | list[str]) -> bool:
    """A reply that only repeats the ASR prompt. In silence Whisper writes its prompt back (2026-10-12: an on-site
    recording with only room noise came out as 18 replies "Pojmy <surname>.", "<name>.", "<title>.", "Účastníci.").
    Echo = every word is a word of the prompt (its labels, the title, the participants' names, the glossary), and it
    is more than one word or a word that stands alone in the prompt (a label, a term, a part of a name) – a real
    sentence has words of its own ("Jano, pošli to") and stays. Pure, testable."""
    words = _words(text)
    if not words:
        return False
    single = [_words(p) for p in (*PROMPT_LABELS, *glossary)]
    alone = {w[0] for w in single if len(w) == 1} | {w for n in names for w in _words(n)}
    vocab = alone | set(_words(title)) | {w for g in glossary for w in _words(g)}
    if not set(words) <= vocab:
        return False
    return len(words) > 1 or words[0] in alone


def drop_prompt_echoes(segments: list, rec: Recording, glossary: tuple[str, ...] | list[str]) -> tuple[list, list]:
    """(kept, dropped) of segments (anything with a .text) that only echo build_prompt(rec, glossary)."""
    if build_prompt(rec, glossary) is None:
        return list(segments), []
    title = rec.title.strip()
    title = title if title != rec.stem else ""
    kept, dropped = [], []
    for s in segments:
        (dropped if is_prompt_echo(s.text, title, rec.participants, glossary) else kept).append(s)
    return kept, dropped
