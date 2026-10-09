"""Slice 4: real sourcing clients + RealSourcingPort, all network mocked via
httpx.MockTransport (deterministic, no real network)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, dedup, settings_store
from app.fakes.fake_scoring import FakeScoringPort
from app.fakes.fake_session import FakeSessionManager
from app.ports import RawPosting, SourcingContext
from app.run_orchestrator import RunOrchestrator
from app.settings_store import DEFAULT_SETTINGS
from app.sourcing import brave_search, real_sourcing
from app.sourcing.ashby import fetch_ashby
from app.sourcing.company_list import CompanySpec
from app.sourcing.greenhouse import fetch_greenhouse
from app.sourcing.lever import fetch_lever
from app.sourcing.real_sourcing import RealSourcingPort
from tests.s3_helpers import MID, collect, seed_resume

LEVER_UUID = "2193db3f-77c5-43b8-b030-8f92c9882bf1"
ASHBY_UUID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"


def gh_payload(*jobs: tuple[int, str]) -> dict:
    return {
        "jobs": [
            {
                "id": jid,
                "title": title,
                "location": {"name": "Remote - US"},
                "company_name": "Acme Inc",
                # Greenhouse really does entity-escape its HTML content.
                "content": "&lt;p&gt;Build &amp;amp; ship &lt;b&gt;Python&lt;/b&gt;"
                " services.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;5+ years&lt;/li&gt;&lt;/ul&gt;",
                "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{jid}",
            }
            for jid, title in jobs
        ]
    }


LEVER_PAYLOAD = [
    {
        "id": LEVER_UUID,
        "text": "Backend Engineer",
        "categories": {"location": "London", "team": "Platform"},
        "descriptionPlain": "We are hiring a backend engineer.",
        "lists": [{"text": "Requirements", "content": "<li>Go</li><li>Kafka</li>"}],
        "additionalPlain": "Benefits galore.",
        "hostedUrl": f"https://jobs.lever.co/widgets/{LEVER_UUID}",
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "id": ASHBY_UUID,
            "title": "Product Engineer",
            "location": "Europe",
            "isListed": True,
            "descriptionPlain": "Own features end to end. " + "x" * 400,
            "descriptionHtml": "<p>ignored when plain exists</p>",
            "jobUrl": f"https://jobs.ashbyhq.com/gizmo/{ASHBY_UUID}",
        },
        {
            "id": "unlisted-1",
            "title": "Hidden",
            "location": "",
            "isListed": False,
            "jobUrl": "https://jobs.ashbyhq.com/gizmo/unlisted-1",
        },
    ]
}


def mock_client(routes: dict[str, object], calls: list[str] | None = None):
    """routes: host+path prefix -> JSON payload | int status | Exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.host + request.url.path
        if calls is not None:
            calls.append(key)
        for prefix, value in routes.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                if isinstance(value, int):
                    return httpx.Response(value, json={"error": "boom"})
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={"ok": False, "error": "Document not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def ctx(role_keywords: str = "", chat_direction: str | None = None):
    s = settings_store.SettingsProfile(
        role_keywords=role_keywords,
        seniority="Senior",
        location="",
        must_haves="",
        dealbreakers="",
        notes="",
        saved_at="2026-01-01T00:00:00+00:00",
        is_default=False,
    )
    return SourcingContext(settings=s, chat_direction=chat_direction)


# --- field mapping ------------------------------------------------------------


def test_greenhouse_maps_fields_correctly():
    async def go():
        async with mock_client(
            {"boards-api.greenhouse.io/v1/boards/acme/jobs": gh_payload((42, "SRE"))}
        ) as c:
            return await fetch_greenhouse("acme", c, company="Acme")

    [p] = run(go())
    assert p == RawPosting(
        source="greenhouse",
        source_id="42",
        title="SRE",
        company="Acme",
        location="Remote - US",
        description=p.description,
        url="https://job-boards.greenhouse.io/acme/jobs/42",
        raw_text=p.raw_text,
    )
    assert "<" not in p.raw_text and "&amp;" not in p.raw_text
    assert "Build & ship Python services." in p.raw_text
    assert "5+ years" in p.raw_text
    assert p.description.startswith("Build & ship Python services.")


def test_greenhouse_company_falls_back_to_board_company_name():
    async def go():
        async with mock_client(
            {"boards-api.greenhouse.io": gh_payload((1, "SRE"))}
        ) as c:
            return await fetch_greenhouse("acme", c)

    assert run(go())[0].company == "Acme Inc"


def test_lever_maps_fields_correctly():
    async def go():
        async with mock_client(
            {"api.lever.co/v0/postings/widgets": LEVER_PAYLOAD}
        ) as c:
            return await fetch_lever("widgets", c, company="Widgets Co")

    [p] = run(go())
    assert p.source == "lever"
    assert p.source_id == LEVER_UUID
    assert p.title == "Backend Engineer"
    assert p.company == "Widgets Co"
    assert p.location == "London"
    assert p.url == f"https://jobs.lever.co/widgets/{LEVER_UUID}"
    assert p.description == "We are hiring a backend engineer."
    assert p.raw_text.startswith("We are hiring a backend engineer.")
    assert "Requirements" in p.raw_text and "Kafka" in p.raw_text
    assert "Benefits galore." in p.raw_text


def test_lever_bad_slug_raises():
    async def go():
        async with mock_client({}) as c:  # everything 404s
            return await fetch_lever("nope", c)

    with pytest.raises(httpx.HTTPError):
        run(go())


def test_ashby_maps_fields_correctly():
    async def go():
        async with mock_client(
            {"api.ashbyhq.com/posting-api/job-board/gizmo": ASHBY_PAYLOAD}
        ) as c:
            return await fetch_ashby("gizmo", c)

    [p] = run(go())  # the unlisted job is skipped
    assert p.source == "ashby"
    assert p.source_id == ASHBY_UUID
    assert p.title == "Product Engineer"
    assert p.company == "Gizmo"  # derived from the slug when no name given
    assert p.location == "Europe"
    assert p.url == f"https://jobs.ashbyhq.com/gizmo/{ASHBY_UUID}"
    assert p.raw_text.startswith("Own features end to end.")
    assert len(p.description) <= 300 and p.description.endswith("…")


# --- RealSourcingPort orchestration ---------------------------------------------

GH = CompanySpec("Acme", "greenhouse", "acme")
LV = CompanySpec("Widgets", "lever", "widgets")
AB = CompanySpec("Gizmo", "ashby", "gizmo")

ALL_UP = {
    "boards-api.greenhouse.io/v1/boards/acme/": gh_payload((1, "SRE")),
    "api.lever.co/v0/postings/widgets": LEVER_PAYLOAD,
    "api.ashbyhq.com/posting-api/job-board/gizmo": ASHBY_PAYLOAD,
}


def _port(routes, known=(GH, LV, AB), seed=(), key=None, calls=None):
    return RealSourcingPort(
        known_companies=list(known),
        seed_companies=list(seed),
        brave_api_key=key,
        http_client=mock_client(routes, calls),
    )


def test_ac15_one_platform_down_others_succeed():
    routes = {k: v for k, v in ALL_UP.items() if "greenhouse" not in k}
    routes["boards-api.greenhouse.io"] = httpx.TimeoutException("timed out")
    # a second Greenhouse company that also times out: still ONE "greenhouse"
    known = (GH, CompanySpec("Other", "greenhouse", "other"), LV, AB)
    result = run(_port(routes, known=known).fetch_candidates(ctx()))
    assert result.failed_sources == ["greenhouse"]
    assert {p.source for p in result.postings} == {"lever", "ashby"}


def test_one_company_failing_does_not_drop_its_platform_siblings():
    routes = dict(ALL_UP)
    routes["boards-api.greenhouse.io/v1/boards/broken/"] = 500
    known = (GH, CompanySpec("Broken", "greenhouse", "broken"), LV, AB)
    result = run(_port(routes, known=known).fetch_candidates(ctx()))
    assert result.failed_sources == ["greenhouse"]
    assert ("greenhouse", "1") in {(p.source, p.source_id) for p in result.postings}


def test_no_brave_key_skips_discovery_and_uses_seed(monkeypatch):
    async def must_not_call(*a, **k):
        raise AssertionError("discovery must be skipped without an API key")

    monkeypatch.setattr(brave_search, "discover_companies", must_not_call)
    calls: list[str] = []
    result = run(
        _port(ALL_UP, known=(GH,), seed=(LV, AB), calls=calls).fetch_candidates(ctx())
    )
    assert result.failed_sources == []
    assert {p.source for p in result.postings} == {"greenhouse", "lever", "ashby"}
    assert not any("brave" in c for c in calls)


def test_fallback_order_search_before_seed(monkeypatch):
    async def fake_discover(query, client, api_key, timeout=8.0):
        assert api_key == "k"
        return ["ashby:gizmo"]

    monkeypatch.setattr(brave_search, "discover_companies", fake_discover)
    calls: list[str] = []
    seed_spy = CompanySpec("SeedCo", "lever", "seedco-must-not-be-called")
    result = run(
        _port(
            ALL_UP, known=(GH,), seed=(seed_spy,), key="k", calls=calls
        ).fetch_candidates(ctx())
    )
    assert not any("seedco" in c for c in calls), calls
    search = [p for p in result.postings if p.source == "search"]
    assert len(search) == 1
    # search-sourced convention: source_id is the discovered posting URL
    assert search[0].source_id == search[0].url == ASHBY_PAYLOAD["jobs"][0]["jobUrl"]


def test_fallback_order_seed_used_when_search_empty(monkeypatch):
    async def fake_discover(query, client, api_key, timeout=8.0):
        return []

    monkeypatch.setattr(brave_search, "discover_companies", fake_discover)
    calls: list[str] = []
    result = run(
        _port(ALL_UP, known=(GH,), seed=(LV,), key="k", calls=calls).fetch_candidates(
            ctx()
        )
    )
    assert any("api.lever.co/v0/postings/widgets" in c for c in calls)
    assert "lever" in {p.source for p in result.postings}


def test_seed_used_when_every_discovered_guess_fails(monkeypatch):
    """Non-empty-but-fruitless discovery still counts as 'nothing usable'."""

    async def fake_discover(query, client, api_key, timeout=8.0):
        return ["lever:ghost-co", "ashby:also-missing"]

    monkeypatch.setattr(brave_search, "discover_companies", fake_discover)
    calls: list[str] = []
    result = run(
        _port(ALL_UP, known=(GH,), seed=(AB,), key="k", calls=calls).fetch_candidates(
            ctx()
        )
    )
    assert any("ghost-co" in c for c in calls)  # the guesses WERE attempted
    assert any("job-board/gizmo" in c for c in calls)  # then seed fallback ran
    assert "ashby" in {p.source for p in result.postings}
    assert result.failed_sources == []  # bad guesses are not failed sources


def test_discovery_skips_already_known_boards(monkeypatch):
    async def fake_discover(query, client, api_key, timeout=8.0):
        return ["greenhouse:acme"]  # already a known company

    monkeypatch.setattr(brave_search, "discover_companies", fake_discover)
    calls: list[str] = []
    run(_port(ALL_UP, known=(GH,), key="k", calls=calls).fetch_candidates(ctx()))
    assert sum("boards/acme" in c for c in calls) == 1


def test_within_run_dedup_applied_across_sources(monkeypatch):
    """A direct-query posting and a search-discovered copy of the SAME job
    (here: one Greenhouse company reachable under two board aliases) reach the
    final result exactly once -- the direct-API copy is kept."""

    async def fake_discover(query, client, api_key, timeout=8.0):
        return ["greenhouse:acme-alias"]

    monkeypatch.setattr(brave_search, "discover_companies", fake_discover)
    routes = {
        "boards-api.greenhouse.io/v1/boards/acme/": gh_payload((7, "SRE")),
        "boards-api.greenhouse.io/v1/boards/acme-alias/": gh_payload(
            (7, "SRE"), (8, "Data Engineer")
        ),
    }
    # GH listed twice -> an exact (source, source_id) repeat for dedup_within_run
    result = run(_port(routes, known=(GH, GH), key="k").fetch_candidates(ctx()))
    keys = [(p.source, p.source_id) for p in result.postings]
    assert keys.count(("greenhouse", "7")) == 1
    assert not any(k[0] == "search" and k[1].endswith("/jobs/7") for k in keys)
    # the genuinely-new discovered job survives as a search posting
    assert ("search", "https://job-boards.greenhouse.io/acme/jobs/8") in keys
    assert len(keys) == 2


def test_role_keywords_filter_titles():
    routes = {
        "boards-api.greenhouse.io/v1/boards/acme/": gh_payload(
            (1, "Senior Backend Engineer"), (2, "Account Executive"), (3, "SRE")
        )
    }
    result = run(
        _port(routes, known=(GH,)).fetch_candidates(ctx("backend engineer, sre"))
    )
    assert sorted(p.source_id for p in result.postings) == ["1", "3"]


def _gh_job(jid: int, title: str, content: str, published: str | None) -> dict:
    job = {
        "id": jid,
        "title": title,
        "location": {"name": "Remote"},
        "content": content,
        "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{jid}",
    }
    if published:
        job["updated_at"] = published
    return job


def test_role_keywords_title_only_match_still_passes():
    routes = {
        "boards-api.greenhouse.io/v1/boards/acme/": {
            "jobs": [
                _gh_job(1, "Staff Platform Engineer", "<p>Generic body.</p>", None),
                _gh_job(2, "Recruiter", "<p>Generic body.</p>", None),
            ]
        }
    }
    result = run(_port(routes, known=(GH,)).fetch_candidates(ctx("platform")))
    assert [p.source_id for p in result.postings] == ["1"]


def test_role_keywords_description_only_match_now_passes():
    """Title lacks the keyword, body has it -> kept (was dropped when the
    filter was title-only). A posting with the keyword nowhere is dropped."""
    routes = {
        "boards-api.greenhouse.io/v1/boards/acme/": {
            "jobs": [
                _gh_job(
                    1,
                    "Software Engineer II",
                    "<p>You will join us as a Backend Engineer on payments.</p>",
                    None,
                ),
                _gh_job(2, "Account Executive", "<p>Close deals.</p>", None),
            ]
        }
    }
    result = run(_port(routes, known=(GH,)).fetch_candidates(ctx("backend engineer")))
    assert [p.source_id for p in result.postings] == ["1"]
    assert "backend" not in result.postings[0].title.lower()


def test_matches_role_keywords_unit():
    p = RawPosting(
        source="lever",
        source_id="x",
        title="SDE",
        company="C",
        location="",
        description="short blurb",
        url="",
        raw_text="Distributed systems; Kafka; on-call.",
    )
    assert real_sourcing.matches_role_keywords(p, "")
    assert real_sourcing.matches_role_keywords(p, "sde")  # title
    assert real_sourcing.matches_role_keywords(p, "kafka")  # raw_text body
    assert real_sourcing.matches_role_keywords(p, "nope, short blurb")  # desc
    assert not real_sourcing.matches_role_keywords(p, "frontend, react")


def _many_jobs(n: int, title: str = "Backend Engineer") -> dict:
    # job i was published on day i (i=0 oldest); shuffle the input order so
    # the cap must actually sort rather than slice.
    base = datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=-4)))
    order = list(range(n))
    order = order[1::2] + order[0::2]
    return {
        "jobs": [
            _gh_job(
                i,
                title,
                "<p>Python services.</p>",
                (base + timedelta(days=i)).isoformat(),
            )
            for i in order
        ]
    }


def test_sourcing_returns_all_keyword_matches_uncapped():
    """The candidate cap no longer runs inside sourcing (it moved after the
    orchestrator's seen-filter), so sourcing hands back every match."""
    routes = {"boards-api.greenhouse.io/v1/boards/acme/": _many_jobs(400)}
    routes["boards-api.greenhouse.io/v1/boards/acme/"]["jobs"].append(
        _gh_job(999, "Account Executive", "<p>Close deals.</p>", None)
    )
    result = run(_port(routes, known=(GH,)).fetch_candidates(ctx("backend engineer")))
    assert len(result.postings) == 400  # > MAX_CANDIDATES, keyword filter only
    assert {int(p.source_id) for p in result.postings} == set(range(400))
    stamps = [p.posted_at for p in result.postings]
    assert all(s.endswith("+00:00") for s in stamps)  # normalized to UTC


def test_sourcing_blank_keywords_returns_everything_uncapped():
    routes = {"boards-api.greenhouse.io/v1/boards/acme/": _many_jobs(200, "Anything")}
    result = run(_port(routes, known=(GH,)).fetch_candidates(ctx("")))
    assert len(result.postings) == 200


def _orch_run(routes, keywords, conn, scoring):
    settings_store.save_settings(
        conn,
        role_keywords=keywords,
        seniority="Senior",
        location="",
        must_haves="",
        dealbreakers="",
        notes="",
    )
    orch = RunOrchestrator(
        _port(routes, known=(GH,)), scoring, FakeSessionManager(), conn
    )
    return collect(orch)


def test_cap_keeps_50_most_recent_with_keywords(data_dir):
    """End-to-end (real port + orchestrator): with keywords and nothing seen,
    scoring receives exactly the 50 newest keyword matches."""
    seed_resume()
    routes = {"boards-api.greenhouse.io/v1/boards/acme/": _many_jobs(400)}
    scoring = FakeScoringPort({}, default=MID)
    conn = db.get_connection()
    try:
        events = _orch_run(routes, "backend engineer", conn, scoring)
    finally:
        conn.close()
    assert len(scoring.calls) == dedup.MAX_CANDIDATES == 50
    scored_ids = [int(c.posting.source_id) for c in scoring.calls]
    assert set(scored_ids) == set(range(350, 400))  # the 50 newest
    assert scored_ids == sorted(scored_ids, reverse=True)  # newest first
    progress = [e.data for e in events if e.event == "scoring_progress"]
    assert progress[0] == {"done": 0, "total": 50}


def test_cap_applies_with_blank_keywords(data_dir):
    seed_resume()
    routes = {"boards-api.greenhouse.io/v1/boards/acme/": _many_jobs(200, "Anything")}
    scoring = FakeScoringPort({}, default=MID)
    conn = db.get_connection()
    try:
        _orch_run(routes, "", conn, scoring)
    finally:
        conn.close()
    assert {int(c.posting.source_id) for c in scoring.calls} == set(range(150, 200))


def test_cap_is_noop_at_or_under_limit_and_undated_sort_last():
    def mk(i, ts):
        return RawPosting("ashby", str(i), "t", "c", "", "", "", "", posted_at=ts)

    few = [mk(i, None) for i in range(5)]
    assert dedup.cap_most_recent(few) == few  # order untouched
    mixed = [mk(0, None), mk(1, "2026-01-01T00:00:00+00:00"), mk(2, None)]
    mixed += [mk(3, "2026-03-01T00:00:00+00:00")]
    assert [p.source_id for p in dedup.cap_most_recent(mixed, limit=3)] == [
        "3",
        "1",
        "0",
    ]


def test_posted_at_mapped_per_platform():
    from app.sourcing.text import to_utc_iso

    assert to_utc_iso("2026-05-01T12:00:00-04:00") == "2026-05-01T16:00:00+00:00"
    assert to_utc_iso(1_700_000_000_000) == "2023-11-14T22:13:20+00:00"  # Lever ms
    assert to_utc_iso("2026-05-01T16:00:00.000Z") == "2026-05-01T16:00:00+00:00"
    assert to_utc_iso(None) is None and to_utc_iso("garbage") is None

    lever = [dict(LEVER_PAYLOAD[0], createdAt=1_700_000_000_000)]
    ashby = {
        "jobs": [
            dict(ASHBY_PAYLOAD["jobs"][0], publishedAt="2026-02-03T10:00:00.000+00:00")
        ]
    }
    gh = {"jobs": [_gh_job(5, "SRE", "<p>x</p>", "2026-05-01T12:00:00-04:00")]}

    async def go():
        async with mock_client(
            {
                "api.lever.co/v0/postings/widgets": lever,
                "api.ashbyhq.com": ashby,
                "boards-api.greenhouse.io": gh,
            }
        ) as c:
            return (
                await fetch_lever("widgets", c),
                await fetch_ashby("gizmo", c),
                await fetch_greenhouse("acme", c),
            )

    [lv], [ab], [g] = run(go())
    assert lv.posted_at == "2023-11-14T22:13:20+00:00"
    assert ab.posted_at == "2026-02-03T10:00:00+00:00"
    assert g.posted_at == "2026-05-01T16:00:00+00:00"


def test_brave_search_failure_returns_empty_not_raise():
    async def go(status_or_exc):
        async with mock_client({"api.search.brave.com": status_or_exc}) as c:
            return await brave_search.discover_companies("q", c, "bad-key")

    assert run(go(500)) == []
    assert run(go(401)) == []
    assert run(go(httpx.ConnectError("down"))) == []


def test_brave_search_parses_board_slugs_and_sends_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Subscription-Token")
        seen["q"] = request.url.params.get("q")
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"url": "https://job-boards.greenhouse.io/Figma/jobs/123"},
                        {"url": "https://boards.greenhouse.io/figma"},
                        {"url": f"https://jobs.lever.co/zoox/{LEVER_UUID}"},
                        {"url": "https://jobs.ashbyhq.com/ramp"},
                        {"url": "https://www.linkedin.com/jobs/view/1"},
                        {"url": "https://boards.greenhouse.io/embed/job_board"},
                    ]
                }
            },
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            q = brave_search.build_discovery_query(
                ctx("platform engineer").settings, "fintech only"
            )
            return await brave_search.discover_companies(q, c, "secret")

    assert run(go()) == ["greenhouse:figma", "lever:zoox", "ashby:ramp"]
    assert seen["token"] == "secret"
    assert "platform engineer" in seen["q"] and "fintech only" in seen["q"]
    assert "site:jobs.lever.co" in seen["q"]


# --- duck-types against the real Slice 3 orchestrator --------------------------


def test_real_port_drives_run_orchestrator(data_dir):
    """S4 done-checklist: RealSourcingPort substituted into Slice 3's
    RunOrchestrator (unmodified) produces a completed run with real-shaped
    postings and the failed platform reported."""
    seed_resume()
    routes = {k: v for k, v in ALL_UP.items() if "lever" not in k}
    routes["api.lever.co"] = httpx.ConnectError("unreachable")
    port = _port(routes)
    conn = db.get_connection()
    try:
        orch = RunOrchestrator(
            port, FakeScoringPort({}, default=MID), FakeSessionManager(), conn
        )
        events = collect(orch)
    finally:
        conn.close()
    names = [e.event for e in events]
    assert ("source_failed", {"source": "lever"}) in [(e.event, e.data) for e in events]
    done = events[names.index("run_complete")].data
    assert done["failed_sources"] == ["lever"]
    assert {(r["source"], r["source_id"]) for r in done["results"]} == {
        ("greenhouse", "1"),
        ("ashby", ASHBY_UUID),
    }
    assert json.dumps(done)  # serializable for SSE
    assert DEFAULT_SETTINGS.role_keywords == ""  # defaults -> no title filter


def test_module_fetchers_registry():
    assert set(real_sourcing.PLATFORM_FETCHERS) == {"greenhouse", "lever", "ashby"}
