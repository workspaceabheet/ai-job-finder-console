import hashlib
import json
import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.ports import RawPosting
from app.settings_store import SettingsProfile

log = logging.getLogger(__name__)


def identity_key(source: str, source_id: str) -> str:
    """The single committed dedup identity key from tech-spec §2.8, used for
    BOTH cross-run and within-run dedup. Never re-derive a different key
    elsewhere."""
    return f"{source}:{source_id}"


def dedup_within_run(postings: list[RawPosting]) -> list[RawPosting]:
    """Collapses postings sharing an identity_key down to one (first
    occurrence wins, preserving input order otherwise). Applied BEFORE
    cross-run seen handling and BEFORE scoring."""
    seen: set[str] = set()
    out: list[RawPosting] = []
    for p in postings:
        key = identity_key(p.source, p.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# --- Cross-run "seen" handling (tech-spec §2.8, amended post-S4) ------------
# A seen posting is excluded by default. It is re-scored only when the scoring
# context (settings profile + active chat direction) has changed since it was
# last scored, and resurfaced only if that re-score beats its last recorded
# score by scoring_math.RESURFACE_MIN_DELTA.

# Settings fields that feed scoring. saved_at / is_default are deliberately
# excluded: re-saving identical values must not count as a context change.
_CONTEXT_SETTINGS_FIELDS = (
    "role_keywords",
    "seniority",
    "location",
    "must_haves",
    "dealbreakers",
    "notes",
)


def context_fingerprint(settings: SettingsProfile, chat_direction: str | None) -> str:
    """Deterministic sha256 hex of the scoring context: the scoring-relevant
    settings fields plus the active chat direction (None and "" both mean
    "no direction"). Equal inputs -> equal hash, so "did the context change"
    is a plain string comparison."""
    payload = {
        "settings": {f: getattr(settings, f) for f in _CONTEXT_SETTINGS_FIELDS},
        "chat_direction": chat_direction or "",
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SeenRecord:
    """One seen_postings row's scoring memory. Both fields are None for rows
    written before these columns existed."""

    last_score: int | None
    context_hash: str | None


def partition_seen(
    postings: list[RawPosting],
    seen: dict[str, SeenRecord],
    context_hash: str,
) -> tuple[list[RawPosting], list[RawPosting], int]:
    """Splits postings (input order preserved within each group) into
    (never_seen, recheck, excluded_count):
      never_seen -- identity_key not in `seen`: always scoring candidates;
      recheck    -- seen, but its stored context_hash differs from
                    `context_hash` (incl. a NULL legacy hash): re-score;
      excluded   -- seen with an identical context_hash: never touched."""
    never_seen: list[RawPosting] = []
    recheck: list[RawPosting] = []
    excluded = 0
    for p in postings:
        record = seen.get(identity_key(p.source, p.source_id))
        if record is None:
            never_seen.append(p)
        elif record.context_hash != context_hash:
            recheck.append(p)
        else:
            excluded += 1
    return never_seen, recheck, excluded


# Hard ceiling on how many postings one run hands to scoring. Applied by the
# orchestrator AFTER partition_seen (never inside sourcing), so each run's
# window is drawn from the still-eligible pool and the frontier moves into
# older postings as newer ones get marked seen.
MAX_CANDIDATES = 50


def cap_most_recent(
    postings: list[RawPosting], limit: int = MAX_CANDIDATES
) -> list[RawPosting]:
    """If more than `limit` postings, keep the `limit` most recently posted
    (posted_at descending; postings with no posted_at sort last, ties keep
    input order). At or under the limit, returns the list unchanged."""
    if len(postings) <= limit:
        return list(postings)
    dated = sorted(
        (p for p in postings if p.posted_at), key=lambda p: p.posted_at, reverse=True
    )
    undated = [p for p in postings if not p.posted_at]
    return (dated + undated)[:limit]


def select_candidates(
    never_seen: list[RawPosting],
    recheck: list[RawPosting],
    limit: int = MAX_CANDIDATES,
) -> list[RawPosting]:
    """The run's scoring batch, at most `limit` postings: never-seen postings
    first (most recent first when over the limit), then any remaining room
    filled with re-check postings, again most recent first."""
    fresh = cap_most_recent(never_seen, limit)
    room = limit - len(fresh)
    return fresh + (cap_most_recent(recheck, room) if room > 0 else [])


# --- Cross-source dedup (research-cross-source-dedup.md §4) -----------------
# Runs AFTER dedup_within_run. Collapses a search-discovered posting onto the
# direct-API posting for the same real-world job, which identity_key alone
# can't see (search postings use their URL as source_id).

_ATS_JOB_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # boards.greenhouse.io and the newer job-boards.greenhouse.io host
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/[^/?#]+/jobs/(\d+)")),
    # Greenhouse boards embedded on a company's own careers domain
    ("greenhouse", re.compile(r"[?&]gh_jid=(\d+)")),
    ("lever", re.compile(r"jobs\.lever\.co/[^/?#]+/([0-9a-fA-F-]{8,36})")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/[^/?#]+/([0-9a-fA-F-]{8,36})")),
]

FUZZY_TITLE_THRESHOLD = 88

_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "corp",
    "corporation",
    "co",
    "ltd",
    "limited",
}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def extract_ats_job_id(url: str) -> tuple[str, str] | None:
    """(platform, job_id) parsed from a Greenhouse/Lever/Ashby posting URL, or
    None. Ids are lowercased (Lever/Ashby UUIDs; Greenhouse ids are numeric)."""
    for platform, pattern in _ATS_JOB_URL_PATTERNS:
        m = pattern.search(url or "")
        if m:
            return platform, m.group(1).lower()
    return None


def normalize_title(title: str) -> str:
    """lowercase, punctuation -> space, collapse whitespace."""
    return " ".join(_NON_ALNUM.sub(" ", title.lower()).split())


def normalize_company(company: str) -> str:
    """lowercase, strip punctuation, drop trailing legal suffixes, then remove
    whitespace (so "Match Group, Inc." == "matchgroup")."""
    tokens = _NON_ALNUM.sub(" ", company.lower()).split()
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _is_fuzzy_dup(search: RawPosting, direct: RawPosting) -> bool:
    return normalize_company(search.company) == normalize_company(direct.company) and (
        fuzz.token_sort_ratio(
            normalize_title(search.title), normalize_title(direct.title)
        )
        >= FUZZY_TITLE_THRESHOLD
    )


def dedup_cross_source(postings: list[RawPosting]) -> list[RawPosting]:
    """Drops every source == "search" posting that resolves to a direct-API
    (greenhouse/lever/ashby) posting in the same list, keeping the direct one
    (richer fields). Resolution, in order:
      (a) the search posting's url / source_id matches a Greenhouse, Lever or
          Ashby job-URL pattern and the extracted id equals a direct posting's
          source_id on that same platform;
      (b) otherwise, exact normalized-company match AND rapidfuzz
          token_sort_ratio(normalized titles) >= FUZZY_TITLE_THRESHOLD.
    Anything unresolved is kept as a distinct posting (no embedding fallback).
    Non-search postings are never dropped; input order is preserved."""
    direct = [p for p in postings if p.source != "search"]
    if not direct:
        return list(postings)
    direct_ids = {(p.source, p.source_id.lower()) for p in direct}

    out: list[RawPosting] = []
    for p in postings:
        if p.source != "search":
            out.append(p)
            continue
        extracted = extract_ats_job_id(p.url) or extract_ats_job_id(p.source_id)
        if extracted is not None and extracted in direct_ids:
            log.debug("cross-source dedup (url) dropped %s", p.url)
            continue
        match = next((d for d in direct if _is_fuzzy_dup(p, d)), None)
        if match is not None:
            log.debug(
                "cross-source dedup (fuzzy) dropped %r @ %s ~ %s:%s",
                p.title,
                p.company,
                match.source,
                match.source_id,
            )
            continue
        out.append(p)
    return out
