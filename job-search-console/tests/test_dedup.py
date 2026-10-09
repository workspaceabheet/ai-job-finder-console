"""Dedup identity key, within-run dedup, cross-run seen handling: context
fingerprint, seen partition, candidate cap (tech-spec §2.8, amended post-S4)."""

import dataclasses

from app import dedup
from app.settings_store import DEFAULT_SETTINGS
from tests.s3_helpers import posting


def test_identity_key_format():
    assert dedup.identity_key("greenhouse", "123") == "greenhouse:123"
    assert (
        dedup.identity_key("search", "https://x.example/job/9")
        == "search:https://x.example/job/9"
    )


def test_within_run_dedup_collapses_duplicate_source_and_id():
    a1 = posting("greenhouse", "1", title="first")
    a2 = posting("greenhouse", "1", title="second copy")
    b = posting("lever", "1")  # same source_id, different source: NOT a dup
    c = posting("ashby", "7")
    out = dedup.dedup_within_run([a1, b, a2, c])
    assert out == [a1, b, c]  # first occurrence wins, order preserved
    assert out[0].title == "first"


def test_context_fingerprint_same_inputs_same_hash():
    s1 = DEFAULT_SETTINGS
    s2 = dataclasses.replace(DEFAULT_SETTINGS)
    assert dedup.context_fingerprint(s1, "fintech") == dedup.context_fingerprint(
        s2, "fintech"
    )
    assert len(dedup.context_fingerprint(s1, None)) == 64  # sha256 hex
    # no direction: None and "" are the same context
    assert dedup.context_fingerprint(s1, None) == dedup.context_fingerprint(s1, "")


def test_context_fingerprint_changes_with_direction_or_settings():
    base = dedup.context_fingerprint(DEFAULT_SETTINGS, None)
    assert dedup.context_fingerprint(DEFAULT_SETTINGS, "fintech") != base
    assert dedup.context_fingerprint(DEFAULT_SETTINGS, "fintech") != (
        dedup.context_fingerprint(DEFAULT_SETTINGS, "healthtech")
    )
    for field in (
        "role_keywords",
        "seniority",
        "location",
        "must_haves",
        "dealbreakers",
        "notes",
    ):
        changed = dataclasses.replace(DEFAULT_SETTINGS, **{field: "something else"})
        assert dedup.context_fingerprint(changed, None) != base, field


def test_context_fingerprint_ignores_save_metadata():
    saved = dataclasses.replace(
        DEFAULT_SETTINGS, saved_at="2026-10-06T00:00:00+00:00", is_default=False
    )
    assert dedup.context_fingerprint(saved, "x") == dedup.context_fingerprint(
        DEFAULT_SETTINGS, "x"
    )


def test_partition_seen_three_way():
    ps = [posting("greenhouse", str(i)) for i in range(5)]
    seen = {
        "greenhouse:1": dedup.SeenRecord(50, "ctx-now"),  # unchanged -> excluded
        "greenhouse:2": dedup.SeenRecord(50, "ctx-old"),  # changed -> recheck
        "greenhouse:3": dedup.SeenRecord(None, None),  # legacy -> recheck
        "lever:4": dedup.SeenRecord(50, "ctx-now"),  # other source: irrelevant
    }
    never, recheck, excluded = dedup.partition_seen(ps, seen, "ctx-now")
    assert [p.source_id for p in never] == ["0", "4"]
    assert [p.source_id for p in recheck] == ["2", "3"]
    assert excluded == 1


def test_select_candidates_new_first_then_recheck_by_recency():
    def dated(i, day):
        return dataclasses.replace(
            posting("ashby", str(i)), posted_at=f"2026-01-{day:02d}T00:00:00+00:00"
        )

    new = [dated(0, 1), dated(1, 3), dated(2, 2)]
    recheck = [dated(10, 5), dated(11, 9), dated(12, 7)]
    ids = lambda xs: [p.source_id for p in xs]
    assert ids(dedup.select_candidates(new, recheck, limit=10)) == [
        "0",
        "1",
        "2",
        "10",
        "11",
        "12",
    ]  # under the cap: everything, new first
    assert ids(dedup.select_candidates(new, recheck, limit=5)) == [
        "0",
        "1",
        "2",
        "11",
        "12",
    ]  # room for 2 re-checks: the 2 newest
    assert ids(dedup.select_candidates(new, recheck, limit=2)) == ["1", "2"]
    assert dedup.select_candidates([], [], limit=5) == []
