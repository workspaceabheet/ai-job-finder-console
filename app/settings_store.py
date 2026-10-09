import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SettingsProfile:
    role_keywords: str
    seniority: str
    location: str
    must_haves: str
    dealbreakers: str
    notes: str
    saved_at: str | None  # ISO-8601 string, or None if never explicitly saved
    is_default: bool  # True iff saved_at is None (convenience flag, derived)


DEFAULT_SETTINGS = SettingsProfile(
    role_keywords="",
    seniority="Mid-level",
    location="",
    must_haves="",
    dealbreakers="",
    notes="",
    saved_at=None,
    is_default=True,
)

_COLUMNS = (
    "role_keywords, seniority, location, must_haves, dealbreakers, notes, saved_at"
)


def _row_to_profile(row: sqlite3.Row) -> SettingsProfile:
    return SettingsProfile(
        role_keywords=row["role_keywords"],
        seniority=row["seniority"],
        location=row["location"],
        must_haves=row["must_haves"],
        dealbreakers=row["dealbreakers"],
        notes=row["notes"],
        saved_at=row["saved_at"],
        is_default=row["saved_at"] is None,
    )


def get_settings(conn: sqlite3.Connection) -> SettingsProfile:
    """Returns the single settings row, or DEFAULT_SETTINGS if the row has never
    been saved (row always exists after init_db with saved_at=NULL; this function
    maps that row to the dataclass, setting is_default = (saved_at is None))."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM settings_profile WHERE id = 1"
    ).fetchone()
    if row is None or row["saved_at"] is None:
        return DEFAULT_SETTINGS
    return _row_to_profile(row)


def save_settings(
    conn: sqlite3.Connection,
    *,
    role_keywords: str,
    seniority: str,
    location: str,
    must_haves: str,
    dealbreakers: str,
    notes: str,
) -> SettingsProfile:
    """Upserts the single row, sets saved_at = now (UTC ISO-8601), returns the
    resulting SettingsProfile with is_default=False."""
    saved_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO settings_profile
            (id, role_keywords, seniority, location, must_haves, dealbreakers,
             notes, saved_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            role_keywords = excluded.role_keywords,
            seniority = excluded.seniority,
            location = excluded.location,
            must_haves = excluded.must_haves,
            dealbreakers = excluded.dealbreakers,
            notes = excluded.notes,
            saved_at = excluded.saved_at
        """,
        (role_keywords, seniority, location, must_haves, dealbreakers, notes, saved_at),
    )
    conn.commit()
    return SettingsProfile(
        role_keywords=role_keywords,
        seniority=seniority,
        location=location,
        must_haves=must_haves,
        dealbreakers=dealbreakers,
        notes=notes,
        saved_at=saved_at,
        is_default=False,
    )
