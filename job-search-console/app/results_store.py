"""Paginated reads of a run's shown results (scored_results rows).

Every posting shown by a run is persisted (no top-N cut, tech-spec §2.7
amended post-S4), so a run can produce up to dedup.MAX_CANDIDATES results;
they are served a page at a time, best score first."""

import json
import math
import sqlite3

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _row_to_result(row: sqlite3.Row) -> dict:
    """Wire shape of one result card (run_complete.results[i] and the
    paginated results endpoint)."""
    return {
        "source": row["source"],
        "source_id": row["source_id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "description": row["description"],
        "url": row["url"],
        "score": row["score"],
        "sub_scores": {
            "skills": row["sub_skills"],
            "seniority": row["sub_seniority"],
            "domain": row["sub_domain"],
            "responsibility": row["sub_responsibility"],
        },
        "reasoning": json.loads(row["reasoning_json"]),
        "gap_note": row["gap_note"],
    }


def run_exists(conn: sqlite3.Connection, run_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM run_history WHERE id = ?", (run_id,)).fetchone()
        is not None
    )


def get_results_page(
    conn: sqlite3.Connection,
    run_id: int,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """One page (1-based) of run `run_id`'s results, sorted by score
    descending (ties keep insertion order, i.e. sourcing order). A page past
    the end returns an empty `results` list, never an error."""
    total = conn.execute(
        "SELECT COUNT(*) FROM scored_results WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM scored_results WHERE run_id = ? "
        "ORDER BY score DESC, id ASC LIMIT ? OFFSET ?",
        (run_id, page_size, (page - 1) * page_size),
    ).fetchall()
    return {
        "run_id": run_id,
        "page": page,
        "page_size": page_size,
        "total_results": total,
        "total_pages": math.ceil(total / page_size),
        "results": [_row_to_result(r) for r in rows],
    }
