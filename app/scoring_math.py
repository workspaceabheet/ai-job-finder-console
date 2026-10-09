from app.ports import RawPosting, SubScores

SCORE_WEIGHTS = {
    "skills": 0.45,
    "seniority": 0.25,
    "domain": 0.15,
    "responsibility": 0.15,
}
# A previously-seen posting re-scored under a changed context (settings or
# chat direction) is shown again only if its new final score beats its last
# recorded score by at least this many points (tech-spec §2.8, amended
# post-S4). There is no quality floor / top-N cut: every scored posting that
# is new this run is shown (§2.7, amended post-S4).
RESURFACE_MIN_DELTA = 10


def compute_final_score(sub: SubScores) -> int:
    """Deterministic weighted sum, app code only — the LLM never picks this
    number. Each sub-score is 0-4; weighted sum maxes at 4.0; scaled to 0-100.
    Rounds to nearest int."""
    raw = (
        sub.skills * SCORE_WEIGHTS["skills"]
        + sub.seniority * SCORE_WEIGHTS["seniority"]
        + sub.domain * SCORE_WEIGHTS["domain"]
        + sub.responsibility * SCORE_WEIGHTS["responsibility"]
    )
    return round((raw / 4.0) * 100)


# (posting, sub_scores, final_score, gap_note)
ScoredItem = tuple[RawPosting, SubScores, int, str]


def should_resurface(previous_score: int | None, new_score: int) -> bool:
    """For a previously-seen posting re-scored under a changed context: show it
    again only if it beats its last recorded score by RESURFACE_MIN_DELTA. A
    legacy row with no recorded score (None) has no baseline -> never."""
    return previous_score is not None and (
        new_score >= previous_score + RESURFACE_MIN_DELTA
    )
