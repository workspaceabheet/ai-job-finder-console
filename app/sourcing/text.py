"""Small text helpers shared by the platform clients."""

import html
import re
from datetime import UTC, datetime

DESCRIPTION_MAX_CHARS = 300

_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|li)\b[^>]*>")
_ANY_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse returns it) -> plain text."""
    if not raw:
        return ""
    s = html.unescape(raw)  # Greenhouse double-escapes: &lt;p&gt; -> <p>
    s = _BLOCK_TAGS.sub("\n", s)
    s = _ANY_TAG.sub("", s)
    s = html.unescape(s)  # entities inside the now-unescaped markup
    s = s.replace("\xa0", " ")
    s = _SPACES.sub(" ", s)
    s = _BLANK_LINES.sub("\n\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


def truncate(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    """Single-line, at most `limit` chars, cut on a word boundary + ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1].rsplit(" ", 1)[0]
    return cut + "…"


def to_utc_iso(value: object) -> str | None:
    """Normalize a source timestamp (ISO-8601 string with any offset, or a
    Lever-style epoch in milliseconds) to a sortable UTC ISO-8601 string.
    Returns None for missing/unparseable values."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int | float):
            dt = datetime.fromtimestamp(value / 1000, tz=UTC)
        else:
            dt = datetime.fromisoformat(str(value))  # 3.11+: accepts "Z"
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def display_name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", slug) if part)
