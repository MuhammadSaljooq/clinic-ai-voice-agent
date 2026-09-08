"""Match a spoken question to a configured FAQ entry.

Token overlap with a floor, deliberately not a nearest-neighbour search. On a clinic
phone line a confidently wrong answer is a real harm -- answering a question about
symptoms with the parking directions is worse than saying we do not know -- so anything
below the floor returns None and the caller gets a person instead.
"""

from __future__ import annotations

import re

from clinic_agent.config import FaqEntry

# Words that carry no matching signal. Kept small: an aggressive list starts removing
# the words that actually distinguish questions.
STOPWORDS = frozenset({
    "a", "about", "am", "an", "and", "any", "are", "as", "at", "be", "been", "can",
    "could", "did", "do", "does", "for", "from", "get", "got", "had", "has", "have",
    "how", "i", "if", "in", "is", "it", "its", "just", "like", "me", "much", "my",
    "of", "on", "or", "should", "so", "some", "that", "the", "their", "there",
    "these", "they", "this", "to", "um", "uh", "was", "we", "were", "what", "when",
    "which", "will", "with", "would", "you", "your",
})

MINIMUM_SCORE = 0.4


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    # Crude singularisation so "hours" matches "hour". Cheap, and covers most of what
    # callers actually say.
    stems = {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}
    return {w for w in stems if w not in STOPWORDS}


def match_faq(question: str, entries: list[FaqEntry]) -> FaqEntry | None:
    """Best-matching entry, or None when nothing clears the confidence floor."""
    asked = _tokens(question)
    if not asked or not entries:
        return None

    best: FaqEntry | None = None
    best_score = 0.0

    for entry in entries:
        known = _tokens(entry.q)
        if not known:
            continue
        overlap = asked & known
        if not overlap:
            continue
        # Scored against the question's own content words, so a long stored answer
        # cannot win simply by containing more words.
        score = len(overlap) / len(asked)
        if score > best_score:
            best, best_score = entry, score

    return best if best_score >= MINIMUM_SCORE else None
