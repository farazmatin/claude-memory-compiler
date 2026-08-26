"""Plan deterministic person-name rewrites for compiled minutes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class TextRewrite:
    before: str
    after: str
    matched_spellings: tuple[str, ...]
    match_count: int


def discover_spellings(text: str, normalized_alias: str) -> tuple[str, ...]:
    """Return exact literal spellings observed for a normalized historical alias."""
    pattern = re.compile(
        rf"(?<!\w){re.escape(normalized_alias)}(?!\w)",
        flags=re.IGNORECASE,
    )
    observed: list[str] = []
    for match in pattern.finditer(text):
        spelling = match.group(0)
        if spelling not in observed:
            observed.append(spelling)
    return tuple(observed)


def plan_text(text: str, mappings: Mapping[str, str]) -> TextRewrite:
    """Return the text change that applying ``mappings`` would make."""
    if not mappings:
        return TextRewrite(text, text, (), 0)

    targets = set(mappings.values())
    terms = sorted(set(mappings) | targets, key=lambda term: (-len(term), term))
    alternatives = "|".join(re.escape(term) for term in terms)
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")
    matched_spellings: list[str] = []
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        spelling = match.group(0)
        if spelling in targets:
            return spelling
        count += 1
        if spelling not in matched_spellings:
            matched_spellings.append(spelling)
        return mappings[spelling]

    after = pattern.sub(replace, text)
    return TextRewrite(
        before=text,
        after=after,
        matched_spellings=tuple(matched_spellings),
        match_count=count,
    )
