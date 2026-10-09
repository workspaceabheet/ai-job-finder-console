"""Deterministic final score (tech-spec §2.6) + the resurface gate (§2.8, amended
post-S4). The floor/top-N selection (§2.7) was removed post-S4."""

from app import scoring_math
from tests.s3_helpers import SCORE_50, SCORE_55, SCORE_59, SCORE_60, SCORE_62, sub


def test_compute_final_score_weights():
    assert scoring_math.compute_final_score(sub(4, 4, 4, 4)) == 100
    assert scoring_math.compute_final_score(sub(0, 0, 0, 0)) == 0
    assert scoring_math.compute_final_score(sub(4, 0, 0, 0)) == 45
    assert scoring_math.compute_final_score(sub(0, 4, 0, 0)) == 25
    assert scoring_math.compute_final_score(sub(0, 0, 4, 4)) == 30
    assert scoring_math.compute_final_score(sub(2, 2, 2, 2)) == 50


def test_resurface_score_fixtures_are_exact():
    got = [
        scoring_math.compute_final_score(x)
        for x in (SCORE_50, SCORE_55, SCORE_59, SCORE_60, SCORE_62)
    ]
    assert got == [50, 55, 59, 60, 62]


def test_should_resurface_needs_at_least_10_more_points():
    assert scoring_math.RESURFACE_MIN_DELTA == 10
    assert scoring_math.should_resurface(50, 62)
    assert scoring_math.should_resurface(50, 60)  # inclusive
    assert not scoring_math.should_resurface(50, 59)
    assert not scoring_math.should_resurface(50, 55)
    assert not scoring_math.should_resurface(50, 30)
    assert not scoring_math.should_resurface(None, 100)  # legacy: no baseline
