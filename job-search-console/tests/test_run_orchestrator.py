"""Run pipeline via the fakes: AC6 (amended: no floor/top-N, every scored
posting shown, paginated), AC7 (amended: context-fingerprint re-check gate),
AC13, AC14, AC15/AC16 mechanics, AC17, within-run dedup, SSE wire format."""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from app import db, dedup, results_store, settings_store
from app.fakes.fake_scoring import FakeScoringPort
from app.fakes.fake_session import FakeSessionManager
from app.fakes.fake_sourcing import FakeSourcingPort
from app.routers.run import start_run
from app.run_lock import run_lock
from app.run_orchestrator import RunOrchestrator
from tests.s3_helpers import (
    HIGH,
    LOW,
    MID,
    SCORE_50,
    SCORE_55,
    SCORE_59,
    SCORE_60,
    SCORE_62,
    collect,
    count_rows,
    hdrs,
    make_client,
    parse_sse,
    posting,
    seed_resume,
    sub,
)


@pytest.fixture(autouse=True)
def _clean_lock():
    run_lock.release()
    yield
    run_lock.release()


@pytest.fixture
def conn(data_dir):
    c = db.get_connection()
    yield c
    c.close()


def _orch(conn, postings, canned, failed=None, session=None, **scoring_kw):
    return RunOrchestrator(
        FakeSourcingPort(postings, failed_sources=failed),
        FakeScoringPort(canned, **scoring_kw),
        session or FakeSessionManager(),
        conn,
    )


def _complete(events):
    done = [e for e in events if e.event == "run_complete"]
    assert len(done) == 1, [e.event for e in events]
    return done[0].data


DEFAULT_CTX = dedup.context_fingerprint(settings_store.DEFAULT_SETTINGS, None)


def _seed_seen(conn, keys, *, last_score=50, context_hash=DEFAULT_CTX):
    conn.executemany(
        "INSERT INTO seen_postings (source, source_id, first_seen_run_id, seen_at, "
        "last_score, context_hash) VALUES (?, ?, 0, '2026-01-01', ?, ?)",
        [(src, sid, last_score, context_hash) for src, sid in keys],
    )
    conn.commit()


def _seen_rows(conn) -> dict[str, tuple]:
    """source_id -> (last_score, context_hash, first_seen_run_id)."""
    return {
        r[0]: (r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT source_id, last_score, context_hash, first_seen_run_id "
            "FROM seen_postings"
        )
    }


def _seen_ids(conn) -> set[str]:
    return set(_seen_rows(conn))


def _with_direction(text: str) -> FakeSessionManager:
    session = FakeSessionManager()
    asyncio.run(session.set_direction(text))
    return session


def test_ac7_cross_run_dedup(conn):
    seed_resume()
    _seed_seen(conn, [("greenhouse", "1"), ("lever", "2")])
    ps = [posting("greenhouse", "1"), posting("lever", "2"), posting("ashby", "3")]
    ps += [posting("ashby", "4")]
    canned = {(p.source, p.source_id): HIGH for p in ps}
    scoring = FakeScoringPort(canned)
    orch = RunOrchestrator(FakeSourcingPort(ps), scoring, FakeSessionManager(), conn)
    data = _complete(collect(orch))
    assert {r["source_id"] for r in data["results"]} == {"3", "4"}
    assert data["seen_hidden_count"] == 2
    assert data["new_count"] == 2
    assert data["resurfaced_count"] == 0
    # seen postings (unchanged context) were never even scored
    assert {c.posting.source_id for c in scoring.calls} == {"3", "4"}
    # and a second identical run surfaces nothing new, all 4 hidden
    data2 = _complete(collect(_orch(conn, ps, canned)))
    assert data2["results"] == [] and data2["seen_hidden_count"] == 4
    row = conn.execute(
        "SELECT seen_hidden_count, new_count, status FROM run_history WHERE id = ?",
        (data["run_id"],),
    ).fetchone()
    assert tuple(row) == (2, 2, "complete")


def test_every_scored_posting_shown_no_floor_no_top_n(conn):
    """No quality floor, no top-N: 18 postings incl. 6 that would have been
    below the old floor of 50 -> all 18 shown, best score first."""
    seed_resume()
    ps = [posting("ashby", str(i)) for i in range(18)]
    tiers = [HIGH, MID, LOW]
    canned = {(p.source, p.source_id): tiers[i % 3] for i, p in enumerate(ps)}
    data = _complete(collect(_orch(conn, ps, canned)))
    assert data["new_count"] == data["total_results"] == 18
    assert len(data["results"]) == 18  # all fit on page 1 (page_size 20)
    scores = [r["score"] for r in data["results"]]
    assert scores == sorted(scores, reverse=True)
    assert scores.count(25) == 6  # LOW postings are shown too
    assert count_rows("scored_results") == 18
    r0 = data["results"][0]
    assert set(r0) == {
        "source",
        "source_id",
        "title",
        "company",
        "location",
        "description",
        "url",
        "score",
        "sub_scores",
        "reasoning",
        "gap_note",
    }
    assert set(r0["reasoning"]) == {"skills", "seniority", "domain", "responsibility"}
    stored = conn.execute(
        "SELECT reasoning_json FROM scored_results LIMIT 1"
    ).fetchone()[0]
    assert set(json.loads(stored)) == {
        "skills",
        "seniority",
        "domain",
        "responsibility",
    }


def test_never_seen_posting_always_scored_shown_and_marked_seen(conn):
    seed_resume()
    ps = [posting("lever", "hi"), posting("lever", "zero")]
    canned = {("lever", "hi"): HIGH, ("lever", "zero"): sub(0, 0, 0, 0)}
    scoring = FakeScoringPort(canned)
    orch = RunOrchestrator(FakeSourcingPort(ps), scoring, FakeSessionManager(), conn)
    data = _complete(collect(orch))
    assert len(scoring.calls) == 2
    assert [(r["source_id"], r["score"]) for r in data["results"]] == [
        ("hi", 100),
        ("zero", 0),
    ]
    assert _seen_rows(conn) == {
        "hi": (100, DEFAULT_CTX, data["run_id"]),
        "zero": (0, DEFAULT_CTX, data["run_id"]),
    }


def test_seen_unchanged_context_is_excluded_and_never_scored(conn):
    seed_resume()
    ps = [posting("greenhouse", str(i)) for i in range(5)]
    canned = {(p.source, p.source_id): MID for p in ps}
    session = _with_direction("fintech only")
    first = _complete(collect(_orch(conn, ps, canned, session=session)))
    assert first["new_count"] == 5
    before = _seen_rows(conn)

    new_one = posting("greenhouse", "new")
    canned[("greenhouse", "new")] = HIGH
    scoring2 = FakeScoringPort(canned)
    orch2 = RunOrchestrator(FakeSourcingPort(ps + [new_one]), scoring2, session, conn)
    events2 = collect(orch2)
    data2 = _complete(events2)
    # same settings + same direction -> the 5 seen are never handed to scoring
    assert [c.posting.source_id for c in scoring2.calls] == ["new"]
    progress = [e.data for e in events2 if e.event == "scoring_progress"]
    assert progress == [{"done": 0, "total": 1}]
    assert [r["source_id"] for r in data2["results"]] == ["new"]
    assert data2["seen_hidden_count"] == 5
    assert {k: v for k, v in _seen_rows(conn).items() if k != "new"} == before


def _rescore_after_direction_change(conn, first_sub, second_sub):
    """Run 1 (no direction) scores 'p' with first_sub; then a direction is
    set and run 2 re-scores it with second_sub. Returns (run2 data, scoring2)."""
    seed_resume()
    p = posting("ashby", "p")
    _complete(collect(_orch(conn, [p], {("ashby", "p"): first_sub})))
    session = _with_direction("remote-first, fintech")
    scoring2 = FakeScoringPort({("ashby", "p"): second_sub})
    data2 = _complete(
        collect(RunOrchestrator(FakeSourcingPort([p]), scoring2, session, conn))
    )
    return data2, scoring2, session


def test_changed_context_small_bump_rescored_but_stays_hidden(conn):
    """Context changed (direction set), new score only +5 (50 -> 55):
    re-scored, NOT shown, stored score/context updated to the new values."""
    data2, scoring2, session = _rescore_after_direction_change(conn, SCORE_50, SCORE_55)
    new_ctx = dedup.context_fingerprint(
        settings_store.DEFAULT_SETTINGS, "remote-first, fintech"
    )
    assert new_ctx != DEFAULT_CTX
    assert [c.posting.source_id for c in scoring2.calls] == ["p"]
    assert data2["results"] == [] and data2["new_count"] == 0
    assert data2["resurfaced_count"] == 0 and data2["seen_hidden_count"] == 1
    assert _seen_rows(conn)["p"][:2] == (55, new_ctx)
    assert count_rows("scored_results") == 1  # only run 1's row

    # ...and with the context now recorded, a third run leaves it alone.
    scoring3 = FakeScoringPort({("ashby", "p"): HIGH})
    data3 = _complete(
        collect(
            RunOrchestrator(
                FakeSourcingPort([posting("ashby", "p")]), scoring3, session, conn
            )
        )
    )
    assert scoring3.calls == [] and data3["results"] == []


def test_changed_context_big_bump_rescored_and_shown_again(conn):
    """Same as above but +12 (50 -> 62): re-scored AND resurfaced."""
    data2, scoring2, _ = _rescore_after_direction_change(conn, SCORE_50, SCORE_62)
    assert [c.posting.source_id for c in scoring2.calls] == ["p"]
    assert [(r["source_id"], r["score"]) for r in data2["results"]] == [("p", 62)]
    assert data2["new_count"] == 0 and data2["resurfaced_count"] == 1
    assert data2["total_results"] == 1 and data2["seen_hidden_count"] == 0
    assert _seen_rows(conn)["p"][0] == 62
    assert _seen_rows(conn)["p"][2] != data2["run_id"]  # first_seen kept


@pytest.mark.parametrize(
    ("second", "shown"), [(SCORE_60, True), (SCORE_59, False)], ids=["+10", "+9"]
)
def test_resurface_threshold_is_inclusive_at_10(conn, second, shown):
    data2, _, _ = _rescore_after_direction_change(conn, SCORE_50, second)
    assert bool(data2["results"]) is shown


def test_settings_change_also_triggers_recheck(conn):
    seed_resume()
    p = posting("lever", "x")
    _complete(collect(_orch(conn, [p], {("lever", "x"): SCORE_50})))
    settings_store.save_settings(
        conn,
        role_keywords="Platform",
        seniority="Senior",
        location="Remote",
        must_haves="",
        dealbreakers="",
        notes="",
    )
    scoring2 = FakeScoringPort({("lever", "x"): SCORE_62})
    data2 = _complete(
        collect(
            RunOrchestrator(FakeSourcingPort([p]), scoring2, FakeSessionManager(), conn)
        )
    )
    assert len(scoring2.calls) == 1
    assert data2["resurfaced_count"] == 1


def test_resaving_identical_settings_is_not_a_context_change(conn):
    seed_resume()
    fields = {
        "role_keywords": "Platform",
        "seniority": "Senior",
        "location": "Remote",
        "must_haves": "",
        "dealbreakers": "",
        "notes": "",
    }
    settings_store.save_settings(conn, **fields)
    p = posting("lever", "x")
    _complete(collect(_orch(conn, [p], {("lever", "x"): MID})))
    settings_store.save_settings(conn, **fields)  # new saved_at, same values
    scoring2 = FakeScoringPort({("lever", "x"): HIGH})
    collect(
        RunOrchestrator(FakeSourcingPort([p]), scoring2, FakeSessionManager(), conn)
    )
    assert scoring2.calls == []


def test_legacy_seen_row_without_context_is_rechecked_never_resurfaced(conn):
    """Rows written before last_score/context_hash existed (both NULL): treated
    as a changed context -> re-scored once to backfill, but with no baseline
    score they are not resurfaced."""
    seed_resume()
    _seed_seen(conn, [("greenhouse", "old")], last_score=None, context_hash=None)
    scoring = FakeScoringPort({("greenhouse", "old"): HIGH})
    data = _complete(
        collect(
            RunOrchestrator(
                FakeSourcingPort([posting("greenhouse", "old")]),
                scoring,
                FakeSessionManager(),
                conn,
            )
        )
    )
    assert len(scoring.calls) == 1 and data["results"] == []
    assert _seen_rows(conn)["old"][:2] == (100, DEFAULT_CTX)


def test_recheck_scoring_failure_keeps_old_record(conn):
    seed_resume()
    _seed_seen(conn, [("greenhouse", "r")], last_score=40, context_hash="stale")
    scoring = FakeScoringPort({("greenhouse", "r"): None})  # malformed
    _complete(
        collect(
            RunOrchestrator(
                FakeSourcingPort([posting("greenhouse", "r")]),
                scoring,
                FakeSessionManager(),
                conn,
            )
        )
    )
    assert len(scoring.calls) == 1
    assert _seen_rows(conn)["r"][:2] == (40, "stale")  # retried next run


def test_cap_fills_with_new_first_then_newest_rechecks(conn):
    """40 never-seen + 30 re-check candidates, cap 50: all 40 new are
    scored, plus the 10 most recent re-checks."""
    seed_resume()
    pool = _dated_pool(70)  # ids 0..69, higher id = newer
    recheck_ids = [str(i) for i in range(40, 70)]  # the NEWEST 30 are seen
    _seed_seen(conn, [("greenhouse", i) for i in recheck_ids], context_hash="old")
    scoring = FakeScoringPort({}, default=MID)
    _complete(
        collect(
            RunOrchestrator(FakeSourcingPort(pool), scoring, FakeSessionManager(), conn)
        )
    )
    scored_ids = [int(c.posting.source_id) for c in scoring.calls]
    assert len(scored_ids) == dedup.MAX_CANDIDATES == 50
    assert set(scored_ids[:40]) == set(range(40))  # every new one, first
    assert scored_ids[40:] == list(range(69, 59, -1))  # newest 10 re-checks


def _dated_pool(n: int) -> list:
    """n postings, id i published i days after 2024-01-01 (higher id = newer),
    in a shuffled input order so the cap has to sort, not slice."""
    base = datetime(2024, 1, 1, tzinfo=UTC)
    order = list(range(n))
    order = order[1::2] + order[0::2]
    return [
        dataclasses.replace(
            posting("greenhouse", str(i)),
            posted_at=(base + timedelta(days=i)).isoformat(),
        )
        for i in order
    ]


def test_cap_applies_after_seen_filter(conn):
    """300 candidates, the 100 NEWEST already seen. The cap must pick the 50
    newest of the 200 still-eligible (ids 150..199) -- not cap the full 300
    first (which would select ids 250..299, all of them already seen)."""
    seed_resume()
    ps = _dated_pool(300)
    _seed_seen(conn, [("greenhouse", str(i)) for i in range(200, 300)])
    scoring = FakeScoringPort({}, default=LOW)
    events = collect(
        RunOrchestrator(FakeSourcingPort(ps), scoring, FakeSessionManager(), conn)
    )
    data = _complete(events)
    scored_ids = [int(c.posting.source_id) for c in scoring.calls]
    assert len(scored_ids) == dedup.MAX_CANDIDATES == 50
    assert set(scored_ids) == set(range(150, 200))
    assert scored_ids == sorted(scored_ids, reverse=True)  # newest first
    progress = [e.data for e in events if e.event == "scoring_progress"]
    assert progress[0] == {"done": 0, "total": 50}
    assert data["seen_hidden_count"] == 100


def test_cap_window_advances_over_repeated_runs_on_static_pool(conn):
    """Regression for the staleness trap: a STATIC pool of 2000 postings, nothing
    new ever arrives, context never changes. Every scored posting is shown and
    marked seen, so each run settles its entire 50-window, and the next run's
    window must be the next-older 50 -- never the same newest slice."""
    seed_resume()
    pool = _dated_pool(2000)
    canned = {
        (p.source, p.source_id): (HIGH if int(p.source_id) % 10 == 0 else LOW)
        for p in pool
    }
    windows = []
    for _ in range(10):
        scoring = FakeScoringPort(canned)
        events = collect(
            RunOrchestrator(FakeSourcingPort(pool), scoring, FakeSessionManager(), conn)
        )
        data = _complete(events)
        progress = [e.data for e in events if e.event == "scoring_progress"]
        assert progress[0]["total"] == 50  # never starves to 0
        assert data["new_count"] == data["total_results"] == 50
        windows.append({int(c.posting.source_id) for c in scoring.calls})

    for k, window in enumerate(windows):
        hi = 2000 - 50 * k
        assert window == set(range(hi - 50, hi)), f"run {k + 1}"
    # windows are pairwise disjoint and progressively older
    assert len(set().union(*windows)) == 500
    assert [min(w) for w in windows] == sorted((min(w) for w in windows), reverse=True)
    assert count_rows("seen_postings") == 500


def test_scoring_failure_is_not_marked_seen(conn):
    seed_resume()
    ps = [posting("greenhouse", "ok"), posting("greenhouse", "bad")]
    canned = {("greenhouse", "ok"): HIGH, ("greenhouse", "bad"): None}
    _complete(collect(_orch(conn, ps, canned)))
    assert _seen_ids(conn) == {"ok"}  # malformed -> retried on a future run


def test_ac13_no_resume_blocks_run(conn):
    sourcing = FakeSourcingPort([posting("greenhouse", "1")])
    session = FakeSessionManager(run_count_threshold=1)
    orch = RunOrchestrator(sourcing, FakeScoringPort({}), session, conn)
    events = collect(orch)
    assert [e.event for e in events] == ["run_rejected_no_resume"]
    assert count_rows("run_history") == 0
    assert sourcing.calls == []


def test_ac13_no_resume_via_http(data_dir):
    with make_client(
        FakeSourcingPort([]), FakeScoringPort({}), FakeSessionManager()
    ) as c:
        r = c.post("/api/run", headers=hdrs())
        assert r.status_code == 200
        assert parse_sse(r.text) == [("run_rejected_no_resume", {})]
        assert c.get("/api/run/status", headers=hdrs()).json() == {"in_progress": False}
    assert count_rows("run_history") == 0


def test_ac14_defaults_indicator(conn):
    seed_resume()
    assert settings_store.get_settings(conn).is_default
    ps = [posting("greenhouse", "1")]
    data = _complete(collect(_orch(conn, ps, {("greenhouse", "1"): HIGH})))
    assert data["using_default_settings"] is True
    settings_store.save_settings(
        conn,
        role_keywords="Backend",
        seniority="Senior",
        location="Remote",
        must_haves="",
        dealbreakers="",
        notes="",
    )
    data = _complete(collect(_orch(conn, [], {})))
    assert data["using_default_settings"] is False


def test_ac15_mechanic_failed_source_reported(conn):
    seed_resume()
    ps = [posting("greenhouse", "1"), posting("ashby", "2")]
    canned = {(p.source, p.source_id): HIGH for p in ps}
    events = collect(_orch(conn, ps, canned, failed=["lever"]))
    names = [e.event for e in events]
    assert ("source_failed", {"source": "lever"}) in [(e.event, e.data) for e in events]
    assert names.index("source_failed") < names.index("run_complete")
    data = _complete(events)
    assert data["failed_sources"] == ["lever"]
    assert len(data["results"]) == 2
    stored = conn.execute("SELECT failed_sources FROM run_history").fetchone()[0]
    assert json.loads(stored) == ["lever"]


def test_ac16_mechanic_malformed_dropped(conn):
    seed_resume()
    ps = [posting("greenhouse", str(i)) for i in range(4)]
    canned = {(p.source, p.source_id): HIGH for p in ps}
    canned[("greenhouse", "1")] = None  # malformed
    canned[("greenhouse", "2")] = ValueError("invalid JSON")  # malformed
    events = collect(_orch(conn, ps, canned))
    data = _complete(events)
    assert {r["source_id"] for r in data["results"]} == {"0", "3"}
    assert "run_error" not in [e.event for e in events]


def test_ac16_out_of_range_subscore_dropped(conn):
    seed_resume()
    ps = [posting("greenhouse", "1"), posting("greenhouse", "2")]
    canned = {("greenhouse", "1"): HIGH, ("greenhouse", "2"): sub(7, 1, 1, 1)}
    data = _complete(collect(_orch(conn, ps, canned)))
    assert [r["source_id"] for r in data["results"]] == ["1"]


def test_ac16_mechanic_pervasive_failure_message(conn):
    seed_resume()
    ps = [posting("greenhouse", str(i)) for i in range(3)]
    events = collect(_orch(conn, ps, {}))  # no canned scores -> all ScoringError
    names = [e.event for e in events]
    assert "run_error" in names
    err = next(e for e in events if e.event == "run_error")
    assert err.data == {"reason": "all postings failed scoring"}
    assert _complete(events)["results"] == []


def test_total_sourcing_failure_marks_run_error(conn):
    seed_resume()
    session = FakeSessionManager()
    orch = RunOrchestrator(
        FakeSourcingPort([], raise_exc=RuntimeError("all sources down")),
        FakeScoringPort({}),
        session,
        conn,
    )
    events = collect(orch)
    assert [e.event for e in events] == ["source_started", "run_error"]
    assert "all sources down" in events[-1].data["reason"]
    assert conn.execute("SELECT status FROM run_history").fetchone()[0] == "error"
    assert session._runs_since_reset == 1  # note_run_completed on error too


def test_within_run_cross_source_dedup_before_scoring(conn):
    seed_resume()
    dup_a = posting("greenhouse", "42", title="from company query")
    dup_b = posting("greenhouse", "42", title="from a second query")
    other_source = posting("search", "42")  # different source -> distinct key
    ps = [dup_a, other_source, dup_b]
    canned = {("greenhouse", "42"): HIGH, ("search", "42"): MID}
    scoring = FakeScoringPort(canned, strict_once=True)  # raises if scored twice
    orch = RunOrchestrator(FakeSourcingPort(ps), scoring, FakeSessionManager(), conn)
    events = collect(orch)
    data = _complete(events)
    assert len(scoring.calls) == 2
    keys = [(r["source"], r["source_id"]) for r in data["results"]]
    assert keys == [("greenhouse", "42"), ("search", "42")]
    assert data["results"][0]["title"] == "from company query"
    progress = [e.data for e in events if e.event == "scoring_progress"]
    assert progress == [{"done": 0, "total": 2}, {"done": 1, "total": 2}]


def test_ac17_double_run_rejected(data_dir):
    """Drives the real start_run handler: the first response's stream is held
    un-drained (run in flight) while a second call is made."""
    seed_resume()
    ps = [posting("greenhouse", "1")]
    ports = (
        FakeSourcingPort(ps),
        FakeScoringPort({("greenhouse", "1"): HIGH}),
        FakeSessionManager(),
    )

    async def scenario():
        assert run_lock.in_progress is False
        first = await start_run(*ports)
        assert first.status_code == 200
        assert first.media_type == "text/event-stream"
        assert run_lock.in_progress is True

        second = await start_run(*ports)
        assert second.status_code == 409
        assert json.loads(second.body) == {"error": "run already in progress"}
        assert run_lock.in_progress is True

        chunks = [chunk async for chunk in first.body_iterator]
        assert run_lock.in_progress is False  # released once the stream ends
        frames = parse_sse("".join(chunks))
        assert frames[-1][0] == "run_complete"
        assert len(frames[-1][1]["results"]) == 1  # first run unaffected

        third = await start_run(*ports)
        assert third.status_code == 200
        _ = [c async for c in third.body_iterator]
        assert run_lock.in_progress is False

    asyncio.run(scenario())


def test_ac17_http_409_while_lock_held(data_dir):
    seed_resume()
    with make_client(
        FakeSourcingPort([]), FakeScoringPort({}), FakeSessionManager()
    ) as c:
        assert run_lock.try_acquire()  # simulate an in-flight run
        r = c.post("/api/run", headers=hdrs())
        assert r.status_code == 409
        assert r.json() == {"error": "run already in progress"}
        assert c.get("/api/run/status", headers=hdrs()).json() == {"in_progress": True}
        run_lock.release()
        r = c.post("/api/run", headers=hdrs())
        assert r.status_code == 200
        assert parse_sse(r.text)[-1][0] == "run_complete"
        assert c.get("/api/run/status", headers=hdrs()).json() == {"in_progress": False}
    assert count_rows("run_history") == 1


def test_lock_released_when_stream_abandoned(data_dir):
    """Client disconnect mid-run: lock released, row not left 'running'."""
    seed_resume()
    ps = [posting("greenhouse", str(i)) for i in range(3)]
    ports = (
        FakeSourcingPort(ps),
        FakeScoringPort({(p.source, p.source_id): HIGH for p in ps}),
        FakeSessionManager(),
    )

    async def scenario():
        resp = await start_run(*ports)
        it = resp.body_iterator
        await it.__anext__()  # source_started
        await it.aclose()  # consumer goes away
        assert run_lock.in_progress is False

    asyncio.run(scenario())
    conn = db.get_connection()
    assert conn.execute("SELECT status FROM run_history").fetchone()[0] == "error"
    conn.close()


def test_run_requires_auth(data_dir):
    with make_client(
        FakeSourcingPort([]), FakeScoringPort({}), FakeSessionManager()
    ) as c:
        assert c.post("/api/run").status_code == 401
        assert c.get("/api/run/status").status_code == 401
        assert c.post("/api/chat", json={"text": "x"}).status_code == 401
        assert c.get("/api/chat/history").status_code == 401
    assert run_lock.in_progress is False


def test_sse_wire_format_is_byte_exact(data_dir):
    seed_resume()
    with make_client(
        FakeSourcingPort([posting("greenhouse", "1")], failed_sources=["lever"]),
        FakeScoringPort({("greenhouse", "1"): HIGH}),
        FakeSessionManager(),
    ) as c:
        r = c.post("/api/run", headers=hdrs())
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith(
        'event: source_started\ndata: {}\n\nevent: source_failed\ndata: {"source": '
        '"lever"}\n\nevent: scoring_progress\ndata: {"done": 0, "total": 1}\n\n'
        "event: run_complete\ndata: {"
    )
    assert r.text.endswith("}\n\n")
    assert [name for name, _ in parse_sse(r.text)] == [
        "source_started",
        "source_failed",
        "scoring_progress",
        "run_complete",
    ]


def _paged_run(conn, n: int) -> dict:
    """One run over n postings with distinct scores (sub-score combos cycle)."""
    seed_resume()
    ps = [posting("greenhouse", f"{i:03d}") for i in range(n)]
    subs = [HIGH, SCORE_62, SCORE_60, SCORE_55, SCORE_50, MID, LOW]
    canned = {(p.source, p.source_id): subs[i % len(subs)] for i, p in enumerate(ps)}
    return _complete(collect(_orch(conn, ps, canned)))


def test_run_complete_carries_page_one_and_pagination_fields(conn):
    data = _paged_run(conn, 45)
    assert data["new_count"] == data["total_results"] == 45
    assert data["page"] == 1
    assert data["page_size"] == results_store.DEFAULT_PAGE_SIZE == 20
    assert data["total_pages"] == 3
    assert len(data["results"]) == 20
    assert count_rows("scored_results") == 45


def test_results_endpoint_paginates_across_pages(data_dir):
    conn = db.get_connection()
    try:
        run_id = _paged_run(conn, 45)["run_id"]
    finally:
        conn.close()
    with make_client(
        FakeSourcingPort([]), FakeScoringPort({}), FakeSessionManager()
    ) as c:
        url = f"/api/run/{run_id}/results"
        pages = [
            c.get(url, params={"page": n, "page_size": 20}, headers=hdrs()).json()
            for n in (1, 2, 3, 4)
        ]
        assert [len(pg["results"]) for pg in pages] == [20, 20, 5, 0]
        assert all(pg["total_results"] == 45 and pg["total_pages"] == 3 for pg in pages)
        flat = [r for pg in pages for r in pg["results"]]
        assert len({r["source_id"] for r in flat}) == 45  # no overlap, none lost
        scores = [r["score"] for r in flat]
        assert scores == sorted(scores, reverse=True)  # global order across pages
        # default params == page 1 / page_size 20; other sizes work too
        assert c.get(url, headers=hdrs()).json() == pages[0]
        small = c.get(url, params={"page": 5, "page_size": 7}, headers=hdrs()).json()
        assert small["results"] == flat[28:35] and small["total_pages"] == 7
        # validation + unknown run
        for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
            assert c.get(url, params=bad, headers=hdrs()).status_code == 422
        assert c.get("/api/run/9999/results", headers=hdrs()).status_code == 404
        assert c.get(url).status_code == 401


def test_init_db_adds_context_columns_to_legacy_seen_postings(data_dir):
    import sqlite3

    data_dir.mkdir(parents=True)
    legacy = sqlite3.connect(data_dir / "app.db")
    legacy.execute(
        "CREATE TABLE seen_postings (source TEXT NOT NULL, source_id TEXT NOT NULL, "
        "first_seen_run_id INTEGER NOT NULL, seen_at TEXT NOT NULL, "
        "PRIMARY KEY (source, source_id))"
    )
    legacy.execute("INSERT INTO seen_postings VALUES ('lever', '1', 1, '2026-01-01')")
    legacy.commit()
    legacy.close()
    conn = db.get_connection()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_postings)")}
        assert {"last_score", "context_hash"} <= cols
        row = conn.execute("SELECT last_score, context_hash FROM seen_postings")
        assert tuple(row.fetchone()) == (None, None)
        db.init_db(conn)  # idempotent
    finally:
        conn.close()


def test_run_last_endpoint(data_dir):
    """GET /api/run/last: {"has_run": false} on an empty run_history, else the
    newest row (any status) -- used by the UI to hydrate on page load."""
    seed_resume()
    ps = [posting("greenhouse", "1"), posting("greenhouse", "2")]
    with make_client(
        FakeSourcingPort(ps, failed_sources=["lever"]),
        FakeScoringPort({(p.source, p.source_id): HIGH for p in ps}),
        FakeSessionManager(),
    ) as c:
        assert c.get("/api/run/last").status_code == 401
        assert c.get("/api/run/last", headers=hdrs()).json() == {"has_run": False}

        frames = parse_sse(c.post("/api/run", headers=hdrs()).text)
        run_id = frames[-1][1]["run_id"]
        last = c.get("/api/run/last", headers=hdrs()).json()
        assert last["has_run"] is True and last["run_id"] == run_id
        assert last["status"] == "complete" and last["finished_at"]
        assert last["new_count"] == 2 and last["seen_hidden_count"] == 0
        assert last["failed_sources"] == ["lever"]
        assert last["chat_direction"] is None and last["started_at"]

        # second run: everything already seen -> newest row wins
        parse_sse(c.post("/api/run", headers=hdrs()).text)
        last2 = c.get("/api/run/last", headers=hdrs()).json()
        assert last2["run_id"] == run_id + 1
        assert last2["new_count"] == 0 and last2["seen_hidden_count"] == 2
        # existing results route unaffected, still addressable by the old id
        assert c.get(f"/api/run/{run_id}/results", headers=hdrs()).status_code == 200
        # a still-'running' row (no counts yet) is reported as-is
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO run_history (started_at, status) VALUES ('t', 'running')"
        )
        conn.commit()
        conn.close()
        last3 = c.get("/api/run/last", headers=hdrs()).json()
        assert last3["status"] == "running" and last3["new_count"] is None
        assert last3["failed_sources"] == [] and last3["finished_at"] is None


def test_startup_reconciles_stuck_running_rows(data_dir):
    """A run_history row left at status='running' by an unclean shutdown
    (crash, kill -9, reboot) must be reconciled to 'error' on the next
    process startup -- otherwise GET /api/run/last reports status:"running"
    forever with no automatic recovery (Stage 7 validation finding)."""
    conn = db.get_connection()
    conn.execute("INSERT INTO run_history (started_at, status) VALUES ('t', 'running')")
    conn.commit()
    conn.close()

    # A fresh app instance's lifespan startup must reconcile it.
    with make_client(FakeSourcingPort([]), FakeScoringPort({}), FakeSessionManager()):
        pass

    conn = db.get_connection()
    row = conn.execute(
        "SELECT status FROM run_history WHERE started_at = 't'"
    ).fetchone()
    conn.close()
    assert row[0] == "error"
