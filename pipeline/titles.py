"""One resolver for a meeting's human-readable title.

`meetings.title_hint` holds a mangled Drive file id - real values look like
"1orzS fOYO8qQnBfGwVkEmJ6PWkoxdCse 8 Aug 12 at 4 00 p" - so nothing user-facing
can show it raw. This lived inside dashboard.py, which meant the answer path grew
a second, weaker copy that read only the filename. One resolver, so the citation
in an AI answer and the title on a card can never disagree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def clean_meeting_title(
    source_name: str | None,
    title_hint: str | None,
    minutes_path: str | None,
    minutes_text: str | None = None,
) -> str:
    """Produce a clean human-readable title without technical hash prefixes."""
    if minutes_text:
        for line in minutes_text.splitlines()[:25]:
            if line.startswith("title:"):
                clean = line.split("title:", 1)[-1].strip().strip('"\'')
                if clean:
                    return clean
            if line.startswith("# ") and not line.startswith("##"):
                clean = line.lstrip("# ").strip()
                if clean:
                    return clean

    if minutes_path:
        stem = Path(minutes_path).stem
        parts = stem.split("-")
        if len(parts) >= 4:
            slug_words = parts[3:-1] if len(parts[-1]) in (8, 12) else parts[3:]
            if slug_words:
                return " ".join(slug_words).title()

    if title_hint and len(title_hint) < 60 and not re.match(r"^[0-9a-zA-Z_-]{8,}", title_hint) and not re.match(r"^[0-9a-zA-Z_\-\s]{12,}?\b\d+\b", title_hint):
        return title_hint

    if source_name:
        base = Path(source_name).name
        for ext in (".m4a", ".mp4", ".mp3", ".wav", ".aac", ".flac"):
            if base.lower().endswith(ext):
                base = base[:-len(ext)]
        base = base.replace("\u202f", " ").replace("\xa0", " ")
        parts = base.split("_")
        if len(parts) >= 3 and len(parts[0]) >= 15:
            base = "_".join(parts[2:])
        elif len(parts) >= 2 and len(parts[0]) >= 20:
            base = "_".join(parts[1:])
        else:
            space_parts = base.split()
            if len(space_parts) >= 4 and len(space_parts[0]) >= 8 and len(space_parts[1]) >= 12:
                base = " ".join(space_parts[3:] if space_parts[2].isdigit() else space_parts[2:])

        base = re.sub(r"^\d+[\s_]+", "", base)

        def _time_repl(m: Any) -> str:
            hh = m.group(1)
            mm = m.group(2) if m.group(2) else "00"
            ampm = m.group(3).upper() + "M"
            return f"{hh}:{mm} {ampm}"

        clean = re.sub(r"\b(\d{1,2})(?:[-:](\d{2}))?\s*([ap])\.?m\.?", _time_repl, base, flags=re.I)
        clean = clean.replace("_", " ").replace(" - ", " — ").replace("-", " ")
        clean = re.sub(r"\s+", " ", clean).strip()
        clean = clean.replace("uce", "UCE").replace("torc", "TORC").replace("usc", "USC").replace("dpm", "DPM")
        clean = re.sub(r"\b([0-9a-f]{8,12})\b", "", clean, flags=re.I).strip()
        if clean:
            return clean

    return "Untitled Meeting"
