"""Resolve names as spoken in the meeting ("Mr Tan", "Ah Keong") to people in the company directory."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

HONORIFICS = {
    "mr", "mrs", "ms", "miss", "mdm", "madam", "dr", "prof", "sir", "encik", "en", "puan", "pn", "cik", "tuan",
    "dato", "datuk", "datin", "tan sri", "ir", "ts", "haji", "hajjah", "boss", "bro", "sis", "uncle", "auntie",
}


def normalize_name(name: str) -> str:
    s = re.sub(r"[^\w\s]", " ", name.lower())
    words = [w for w in s.split() if w not in HONORIFICS]
    return " ".join(words)


@dataclass(frozen=True)
class Candidate:
    person_id: int
    name: str
    aliases: tuple[str, ...] = ()
    attendee: bool = False


def _score(said: str, cand: Candidate) -> float:
    best = 0.0
    for n in (cand.name, *cand.aliases):
        target = normalize_name(n)
        if not target:
            continue
        if said == target:
            return 100.0
        tokens = target.split()
        # "Tan" or "Siti" matching one word of "Tan Ah Keong" / "Siti Aminah"
        if said in tokens or (len(said.split()) > 1 and said in target):
            best = max(best, 92.0)
        best = max(best, fuzz.token_set_ratio(said, target) * 0.95, fuzz.ratio(said, target))
    return best


def resolve(name_as_said: str | None, candidates: list[Candidate], *, min_score: float = 85.0,
            margin: float = 5.0) -> tuple[int | None, list[int]]:
    """(person_id or None if unsure, ids of plausible candidates). Attendees win ties."""
    if not name_as_said:
        return None, []
    said = normalize_name(name_as_said)
    if not said:
        return None, []
    scored = sorted(((_score(said, c) + (3.0 if c.attendee else 0.0), c) for c in candidates),
                    key=lambda t: t[0], reverse=True)
    plausible = [c.person_id for s, c in scored if s >= min_score]
    if not plausible:
        return None, []
    if len(scored) > 1 and scored[0][0] - scored[1][0] < margin and scored[1][0] >= min_score:
        return None, plausible
    return scored[0][1].person_id, plausible
