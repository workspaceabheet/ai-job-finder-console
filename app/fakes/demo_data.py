"""Canned demo data for the Slice 3 fake wiring in app/main.py (used for the
manual live check; replaced wholesale when Slices 4/5 land).

Shape: 23 raw postings = 21 distinct identity keys + 2 exact (source,
source_id) repeats; one posting whose canned score is malformed; a spread of
high and low scores (the old floor/top-15 cut is gone post-S4, so all 20
scored postings are shown, best first); "lever" reported as a failed source."""

from app.ports import RawPosting, SubScores

DEMO_FAILED_SOURCES = ["lever"]

# (source, source_id, title, company, location, (skills, seniority, domain, resp))
_SPECS: list[tuple[str, str, str, str, str, tuple[int, int, int, int] | None]] = [
    (
        "greenhouse",
        "gh-1001",
        "Senior Backend Engineer",
        "Acme Pay",
        "Remote",
        (4, 4, 3, 4),
    ),
    ("greenhouse", "gh-1002", "Platform Engineer", "Northwind", "NYC", (4, 3, 3, 3)),
    (
        "greenhouse",
        "gh-1003",
        "Staff Software Engineer",
        "Globex",
        "Remote",
        (3, 4, 4, 3),
    ),
    ("greenhouse", "gh-1004", "Data Engineer", "Initech", "Austin", (3, 3, 2, 3)),
    (
        "greenhouse",
        "gh-1005",
        "ML Platform Engineer",
        "Umbrella",
        "Remote",
        (3, 3, 3, 2),
    ),
    ("ashby", "ab-2001", "Backend Engineer, Payments", "Stark Fin", "SF", (4, 3, 4, 3)),
    (
        "ashby",
        "ab-2002",
        "Infrastructure Engineer",
        "Wayne Cloud",
        "Remote",
        (3, 3, 2, 2),
    ),
    ("ashby", "ab-2003", "Software Engineer II", "Hooli", "Seattle", (3, 2, 2, 3)),
    ("ashby", "ab-2004", "Senior SRE", "Pied Piper", "Remote", (2, 3, 2, 2)),
    (
        "ashby",
        "ab-2005",
        "Distributed Systems Engineer",
        "Vandelay",
        "Boston",
        (4, 4, 2, 3),
    ),
    (
        "search",
        "https://jobs.example.com/a1",
        "Senior Python Engineer",
        "Soylent",
        "Remote",
        (3, 4, 3, 3),
    ),
    (
        "search",
        "https://jobs.example.com/a2",
        "API Engineer",
        "Tyrell",
        "LA",
        (3, 2, 3, 3),
    ),
    (
        "search",
        "https://jobs.example.com/a3",
        "Backend Lead",
        "Cyberdyne",
        "Remote",
        (2, 4, 2, 3),
    ),
    (
        "search",
        "https://jobs.example.com/a4",
        "Full-Stack Engineer",
        "Oscorp",
        "NYC",
        (2, 2, 2, 2),
    ),
    (
        "search",
        "https://jobs.example.com/a5",
        "Frontend Engineer",
        "Massive Dyn",
        "Remote",
        (1, 2, 1, 1),
    ),
    ("greenhouse", "gh-1006", "iOS Engineer", "Gringotts", "London", (0, 2, 1, 1)),
    ("ashby", "ab-2006", "Engineering Manager", "Aperture", "Remote", (1, 1, 2, 1)),
    (
        "search",
        "https://jobs.example.com/a6",
        "Data Platform Engineer",
        "Wonka",
        "Remote",
        (3, 3, 3, 3),
    ),
    (
        "greenhouse",
        "gh-1008",
        "Backend Engineer",
        "Dunder Tech",
        "Scranton",
        (3, 3, 2, 3),
    ),
    (
        "ashby",
        "ab-2007",
        "Senior Software Engineer",
        "Prestige",
        "Remote",
        (4, 3, 2, 3),
    ),
    # Malformed: the fake scorer raises ScoringError for this one (AC16 mechanic).
    ("greenhouse", "gh-1007", "Senior Go Engineer", "Black Mesa", "Remote", None),
]


def _posting(source, source_id, title, company, location) -> RawPosting:
    url = (
        source_id if source == "search" else f"https://{source}.example.com/{source_id}"
    )
    return RawPosting(
        source=source,
        source_id=source_id,
        title=title,
        company=company,
        location=location,
        description=f"{title} at {company} ({location}). Demo posting.",
        url=url,
        raw_text=f"{title}\n{company}\n{location}\nFull demo posting text.",
    )


_DISTINCT = [_posting(*spec[:5]) for spec in _SPECS]

# Two exact repeats (same source AND source_id) as if returned by two queries.
DEMO_POSTINGS: list[RawPosting] = _DISTINCT + [_DISTINCT[0], _DISTINCT[10]]


def _sub(scores: tuple[int, int, int, int]) -> SubScores:
    s, se, d, r = scores
    return SubScores(
        skills=s,
        seniority=se,
        domain=d,
        responsibility=r,
        skills_reasoning=f"skills {s}/4 (demo)",
        seniority_reasoning=f"seniority {se}/4 (demo)",
        domain_reasoning=f"domain {d}/4 (demo)",
        responsibility_reasoning=f"responsibility {r}/4 (demo)",
    )


DEMO_SCORES: dict[tuple[str, str], SubScores | None] = {
    (spec[0], spec[1]): (_sub(spec[5]) if spec[5] is not None else None)
    for spec in _SPECS
}
