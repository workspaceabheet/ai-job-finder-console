import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from app import dedup, results_store, resume_store, scoring_math, settings_store
from app.ports import (
    ScoringError,
    ScoringInput,
    ScoringPort,
    SourcingContext,
    SourcingPort,
    SubScores,
)
from app.session_port import SessionManager


@dataclass(frozen=True)
class RunEvent:
    event: str  # "run_rejected_no_resume" | "source_started" | "source_failed" |
    # "scoring_progress" | "run_complete" | "run_error"
    data: dict


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _reasoning(sub: SubScores) -> dict:
    return {
        "skills": sub.skills_reasoning,
        "seniority": sub.seniority_reasoning,
        "domain": sub.domain_reasoning,
        "responsibility": sub.responsibility_reasoning,
    }


class RunOrchestrator:
    """The Run pipeline. Talks to the agent side ONLY through SourcingPort /
    ScoringPort / SessionManager — never to a concrete implementation."""

    def __init__(
        self,
        sourcing: SourcingPort,
        scoring: ScoringPort,
        session: SessionManager,
        conn: sqlite3.Connection,
    ):
        self.sourcing = sourcing
        self.scoring = scoring
        self.session = session
        self.conn = conn

    async def execute_run(self) -> AsyncIterator[RunEvent]:
        # 1. AC13: no resume -> reject before touching the session or run_history.
        resume = resume_store.get_resume(self.conn)
        if resume is None:
            yield RunEvent("run_rejected_no_resume", {})
            return

        # 2. AC14: settings never saved -> proceed on defaults, flag it.
        settings = settings_store.get_settings(self.conn)
        using_defaults = settings.is_default

        # 3-4. AC11: lazy reset check strictly BEFORE reading the direction.
        await self.session.maybe_reset()
        chat_direction = await self.session.get_active_direction()

        # 5. run_history row.
        cur = self.conn.execute(
            "INSERT INTO run_history (started_at, chat_direction, status) "
            "VALUES (?, ?, 'running')",
            (_now(), chat_direction),
        )
        run_id = cur.lastrowid
        self.conn.commit()

        noted = False
        try:
            # 6. Sourcing (AC15 mechanic: failed sources reported, run continues).
            ctx = SourcingContext(settings=settings, chat_direction=chat_direction)
            yield RunEvent("source_started", {})
            sourcing_result = await self.sourcing.fetch_candidates(ctx)
            for name in sourcing_result.failed_sources:
                yield RunEvent("source_failed", {"source": name})

            # 7. Within-run dedup (§2.8), before cross-run filter and scoring.
            deduped = dedup.dedup_within_run(sourcing_result.postings)

            # 8. Cross-run seen handling (AC7, §2.8 amended post-S4): never-seen
            # postings are candidates; seen ones are re-checked only if the
            # scoring context changed since they were last scored.
            context_hash = dedup.context_fingerprint(settings, chat_direction)
            seen = self._load_seen()
            never_seen, recheck, _ = dedup.partition_seen(deduped, seen, context_hash)
            seen_in_pool = len(deduped) - len(never_seen)

            # 8b. Safety cap, strictly AFTER the seen split: never-seen first
            # (newest first), remaining room filled with re-check postings.
            candidates = dedup.select_candidates(never_seen, recheck)

            # 9. Score each posting; ScoringError is caught PER POSTING (AC16).
            scored: list[scoring_math.ScoredItem] = []
            for i, posting in enumerate(candidates):
                yield RunEvent(
                    "scoring_progress", {"done": i, "total": len(candidates)}
                )
                try:
                    result = await self.scoring.score_posting(
                        ScoringInput(
                            resume_text=resume.extracted_text,
                            posting=posting,
                            settings=settings,
                            chat_direction=chat_direction,
                        )
                    )
                except ScoringError:
                    continue
                final_score = scoring_math.compute_final_score(result.sub_scores)
                scored.append(
                    (posting, result.sub_scores, final_score, result.gap_note)
                )

            # 10. AC16 pervasive-failure message (run still completes below).
            if candidates and not scored:
                yield RunEvent("run_error", {"reason": "all postings failed scoring"})

            # 11. No floor, no top-N: every newly-seen scored posting is shown;
            # a re-checked one only if it beat its last score by
            # RESURFACE_MIN_DELTA. Best score first (stable: ties keep
            # sourcing order).
            shown: list[scoring_math.ScoredItem] = []
            resurfaced_count = 0
            for item in scored:
                previous = seen.get(
                    dedup.identity_key(item[0].source, item[0].source_id)
                )
                if previous is None:
                    shown.append(item)
                elif scoring_math.should_resurface(previous.last_score, item[2]):
                    shown.append(item)
                    resurfaced_count += 1
            shown.sort(key=lambda item: item[2], reverse=True)
            # Seen postings in this run's pool that are not on screen: excluded
            # (context unchanged), re-checked but below the resurface bar,
            # capped out, or failed re-scoring.
            hidden_count = seen_in_pool - resurfaced_count
            # Never-seen postings shown; new_count + resurfaced_count == shown.
            new_count = len(shown) - resurfaced_count

            # 12. Persist shown results, in display order.
            for posting, sub, score, gap_note in shown:
                self.conn.execute(
                    """
                    INSERT INTO scored_results
                        (run_id, source, source_id, title, company, location,
                         description, url, score, sub_skills, sub_seniority,
                         sub_domain, sub_responsibility, reasoning_json, gap_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        posting.source,
                        posting.source_id,
                        posting.title,
                        posting.company,
                        posting.location,
                        posting.description,
                        posting.url,
                        score,
                        sub.skills,
                        sub.seniority,
                        sub.domain,
                        sub.responsibility,
                        json.dumps(_reasoning(sub)),
                        gap_note,
                    ),
                )
            # Every successfully scored posting is marked seen with its NEW
            # score + context, shown or not (a re-check that missed the
            # resurface bar is still updated, so later comparisons use the
            # latest values). first_seen_run_id / seen_at keep their original
            # values. ScoringError postings are untouched: a never-seen one
            # stays unseen, a re-check keeps its old context and is retried.
            seen_at = _now()
            self.conn.executemany(
                "INSERT INTO seen_postings (source, source_id, first_seen_run_id, "
                "seen_at, last_score, context_hash) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source, source_id) DO UPDATE SET "
                "last_score = excluded.last_score, "
                "context_hash = excluded.context_hash",
                [
                    (p.source, p.source_id, run_id, seen_at, score, context_hash)
                    for p, _, score, _ in scored
                ],
            )
            self.conn.execute(
                "UPDATE run_history SET finished_at = ?, new_count = ?, "
                "seen_hidden_count = ?, failed_sources = ?, status = 'complete' "
                "WHERE id = ?",
                (
                    _now(),
                    new_count,
                    hidden_count,
                    json.dumps(sourcing_result.failed_sources),
                    run_id,
                ),
            )
            self.conn.commit()

            # 13.
            self.session.note_run_completed()
            noted = True

            # 14.
            # 14. Carries page 1 of the results (+ pagination fields); later
            # pages come from GET /api/run/{run_id}/results.
            yield RunEvent(
                "run_complete",
                {
                    "new_count": new_count,
                    "resurfaced_count": resurfaced_count,
                    "seen_hidden_count": hidden_count,
                    "failed_sources": list(sourcing_result.failed_sources),
                    "using_default_settings": using_defaults,
                    **results_store.get_results_page(self.conn, run_id),
                },
            )
        # Total sourcing-layer failure (the only exception SourcingPort may raise)
        # or an unexpected bug: the run ends as 'error' with a run_error event.
        except Exception as exc:  # noqa: BLE001
            self.conn.rollback()
            self._mark_error(run_id)
            if not noted:
                self.session.note_run_completed()
                noted = True
            yield RunEvent("run_error", {"reason": f"run failed: {exc}"})
        finally:
            # Cancellation (client disconnect) lands here without passing
            # through `except Exception`; never leave a row stuck 'running'.
            if not noted:
                self.conn.rollback()
                self._mark_error(run_id)
                self.session.note_run_completed()

    def _load_seen(self) -> dict[str, dedup.SeenRecord]:
        return {
            dedup.identity_key(row[0], row[1]): dedup.SeenRecord(
                last_score=row[2], context_hash=row[3]
            )
            for row in self.conn.execute(
                "SELECT source, source_id, last_score, context_hash FROM seen_postings"
            )
        }

    def _mark_error(self, run_id: int) -> None:
        self.conn.execute(
            "UPDATE run_history SET finished_at = ?, status = 'error' "
            "WHERE id = ? AND status = 'running'",
            (_now(), run_id),
        )
        self.conn.commit()
