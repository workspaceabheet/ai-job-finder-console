"""dedup_cross_source (research-cross-source-dedup.md): URL-pattern merge,
rapidfuzz title+company merge, and the cases that must NOT merge."""

from app import dedup
from app.ports import RawPosting

LEVER_UUID = "2193db3f-77c5-43b8-b030-8f92c9882bf1"
ASHBY_UUID = "d3bc1ced-3ce4-4086-a050-555055dbb1ff"


def mk(source, source_id, title, company, url=None, location="Remote"):
    return RawPosting(
        source=source,
        source_id=source_id,
        title=title,
        company=company,
        location=location,
        description=f"{title} desc",
        url=url or (source_id if source == "search" else f"https://x/{source_id}"),
        raw_text=f"{title} full text",
    )


def search(url, title="Whatever", company="Unrelated Co"):
    return mk("search", url, title, company, url=url)


# --- (a) URL-pattern merges ---------------------------------------------------


def test_greenhouse_url_merges_into_direct_posting():
    direct = mk("greenhouse", "4012345", "Senior Backend Engineer", "Acme")
    # different title/company text on purpose: the URL id alone must resolve it
    dup = search(
        "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=abc",
        title="Backend Eng (Senior)",
        company="ACME Payments",
    )
    out = dedup.dedup_cross_source([dup, direct])
    assert out == [direct]  # direct-API posting kept, search copy dropped


def test_new_greenhouse_host_and_embedded_gh_jid_also_resolve():
    direct = mk("greenhouse", "77", "SRE", "Acme")
    a = search("https://job-boards.greenhouse.io/acme/jobs/77")
    b = search("https://careers.acme.com/open-roles?gh_jid=77")
    assert dedup.dedup_cross_source([direct, a, b]) == [direct]


def test_lever_and_ashby_urls_merge_case_insensitively():
    lv = mk("lever", LEVER_UUID, "Backend Engineer", "Widgets")
    ab = mk("ashby", ASHBY_UUID, "Product Engineer", "Gizmo")
    lv_dup = search(f"https://jobs.lever.co/widgets/{LEVER_UUID.upper()}/apply")
    ab_dup = search(f"https://jobs.ashbyhq.com/gizmo/{ASHBY_UUID}/application")
    assert dedup.dedup_cross_source([lv, lv_dup, ab, ab_dup]) == [lv, ab]


def test_url_id_on_a_different_platform_does_not_merge():
    """Same id string, but the URL is a Lever URL and the direct posting is
    Ashby: ids are platform-scoped, so no URL merge (and fuzzy fails too)."""
    ab = mk("ashby", ASHBY_UUID, "Product Engineer", "Gizmo")
    lv_url = search(f"https://jobs.lever.co/gizmo/{ASHBY_UUID}", title="Recruiter")
    assert dedup.dedup_cross_source([ab, lv_url]) == [ab, lv_url]


def test_url_with_unknown_id_is_kept():
    direct = mk("greenhouse", "1", "Data Engineer", "Acme")
    other = search("https://boards.greenhouse.io/acme/jobs/2", title="Designer")
    assert dedup.dedup_cross_source([direct, other]) == [direct, other]


# --- (b) fuzzy title + exact normalized company -------------------------------


def test_fuzzy_merge_reworded_title_same_normalized_company():
    direct = mk("lever", LEVER_UUID, "Senior Backend Engineer", "Acme")
    reworded = search(
        "https://careers.acme.com/jobs/senior-backend",  # no ATS pattern
        title="Backend Engineer, Senior",
        company="Acme, Inc.",
    )
    assert dedup.dedup_cross_source([direct, reworded]) == [direct]


def test_fuzzy_merge_minor_punctuation_and_suffix():
    direct = mk("ashby", ASHBY_UUID, "Software Engineer II - Platform", "Match Group")
    dup = search(
        "https://example.org/listing/99",
        title="Software Engineer II (Platform)",
        company="MatchGroup LLC",
    )
    assert dedup.dedup_cross_source([direct, dup]) == [direct]


def test_no_merge_same_title_different_company():
    direct = mk("greenhouse", "1", "Senior Backend Engineer", "Acme")
    other = search(
        "https://careers.globex.com/1",
        title="Senior Backend Engineer",
        company="Globex",
    )
    assert dedup.dedup_cross_source([direct, other]) == [direct, other]


def test_no_merge_same_company_title_below_threshold():
    direct = mk("greenhouse", "1", "Senior Backend Engineer", "Acme")
    other = search(
        "https://careers.acme.com/2",
        title="Senior Frontend Engineer",
        company="Acme",
    )
    from rapidfuzz import fuzz

    score = fuzz.token_sort_ratio(
        dedup.normalize_title(direct.title), dedup.normalize_title(other.title)
    )
    assert score < dedup.FUZZY_TITLE_THRESHOLD  # the case really is sub-threshold
    assert dedup.dedup_cross_source([direct, other]) == [direct, other]


# --- invariants ----------------------------------------------------------------


def test_direct_postings_are_never_dropped_even_if_similar():
    a = mk("greenhouse", "1", "Backend Engineer", "Acme")
    b = mk("lever", LEVER_UUID, "Backend Engineer", "Acme")
    assert dedup.dedup_cross_source([a, b]) == [a, b]


def test_search_only_list_is_unchanged():
    ps = [search("https://a/1", "X", "A"), search("https://b/2", "X", "A")]
    assert dedup.dedup_cross_source(ps) == ps


def test_normalizers():
    assert dedup.normalize_company("Acme, Inc.") == "acme"
    assert dedup.normalize_company("Match Group LLC") == "matchgroup"
    assert dedup.normalize_company("Co") == "co"  # never strips to empty
    assert dedup.normalize_title("  Sr. Engineer -- Backend/API ") == (
        "sr engineer backend api"
    )
    assert dedup.extract_ats_job_id("https://jobs.lever.co/x/" + LEVER_UUID) == (
        "lever",
        LEVER_UUID,
    )
    assert dedup.extract_ats_job_id("https://careers.example.com/42") is None
