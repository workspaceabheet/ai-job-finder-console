"""AC16 (real-agent version, stubbed LLM): any malformed reply -> ScoringError;
prompt construction re-asserts settings + chat direction on every call."""

import asyncio
import json

import pytest

from app import scoring_math
from app.agent.claude_scoring import ClaudeScoringPort, build_scoring_prompt
from app.ports import ScoringError, ScoringInput
from app.settings_store import DEFAULT_SETTINGS, SettingsProfile
from tests.s3_helpers import posting

GOOD = {
    "skills": 3,
    "skills_reasoning": "Python and Postgres overlap.",
    "seniority": 2,
    "seniority_reasoning": "Posting wants staff; resume is mid.",
    "domain": 4,
    "domain_reasoning": "Both fintech.",
    "responsibility": 1,
    "responsibility_reasoning": "No on-call ownership shown.",
}

SETTINGS = SettingsProfile(
    role_keywords="backend engineer",
    seniority="Senior",
    location="Remote (US)",
    must_haves="Python",
    dealbreakers="crypto",
    notes="prefers fintech",
    saved_at="2026-01-01T00:00:00+00:00",
    is_default=False,
)


class StubSession:
    """Scoring prompt -> `score_reply`; skills-gap prompts -> canned JSON."""

    def __init__(self, score_reply):
        self.score_reply = score_reply
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are scoring one job posting"):
            if isinstance(self.score_reply, Exception):
                raise self.score_reply
            return self.score_reply
        if prompt.startswith("Read the job posting text below"):
            return '{"required": ["Rust"], "preferred": []}'
        if prompt.startswith("Read the resume text below"):
            return '{"skills": ["Python"]}'
        return "Missing Rust experience."


def _input(direction="only fintech, no adtech", settings=SETTINGS):
    return ScoringInput(
        resume_text="Resume: Python, Postgres",
        posting=posting("greenhouse", "1"),
        settings=settings,
        chat_direction=direction,
    )


def test_valid_reply_parsed_and_final_score_computed_in_app_code():
    port = ClaudeScoringPort(StubSession(json.dumps(GOOD)))
    result = asyncio.run(port.score_posting(_input()))
    sub = result.sub_scores
    assert (sub.skills, sub.seniority, sub.domain, sub.responsibility) == (3, 2, 4, 1)
    assert sub.domain_reasoning == "Both fintech."
    assert result.gap_note == "Missing Rust experience."
    # 3*.45 + 2*.25 + 4*.15 + 1*.15 = 2.6 -> 65
    assert scoring_math.compute_final_score(sub) == 65


def test_reply_wrapped_in_fence_and_prose_still_parses():
    reply = "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```"
    result = asyncio.run(ClaudeScoringPort(StubSession(reply)).score_posting(_input()))
    assert result.sub_scores.skills == 3


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        json.dumps({k: v for k, v in GOOD.items() if k != "seniority"}),
        json.dumps({**GOOD, "skills": 5}),
        json.dumps({**GOOD, "domain": -1}),
        json.dumps({**GOOD, "skills": 2.5}),
        json.dumps({**GOOD, "skills": True}),
        json.dumps({**GOOD, "skills": "high"}),
        json.dumps({**GOOD, "domain_reasoning": ""}),
        "[1, 2, 3]",
    ],
)
def test_malformed_reply_raises_scoring_error(reply):
    with pytest.raises(ScoringError) as info:
        asyncio.run(ClaudeScoringPort(StubSession(reply)).score_posting(_input()))
    assert info.value.posting.source_id == "1"


def test_transport_failure_becomes_scoring_error():
    session = StubSession(RuntimeError("CLI process died"))
    with pytest.raises(ScoringError, match="CLI process died"):
        asyncio.run(ClaudeScoringPort(session).score_posting(_input()))


def test_gap_pipeline_failure_becomes_scoring_error():
    class BadGap(StubSession):
        async def ask(self, prompt):
            if prompt.startswith("Read the job posting text below"):
                return "garbage"
            return await super().ask(prompt)

    with pytest.raises(ScoringError):
        asyncio.run(ClaudeScoringPort(BadGap(json.dumps(GOOD))).score_posting(_input()))


def test_prompt_reasserts_settings_and_direction_with_distinct_locations():
    prompt = build_scoring_prompt(_input())
    assert "Preferred location: Remote (US)" in prompt
    assert "Posting location: Remote" in prompt  # posting()'s own location
    assert "Seniority target: Senior" in prompt
    assert "Dealbreakers: crypto" in prompt
    assert "only fintech, no adtech" in prompt
    assert "Resume: Python, Postgres" in prompt


def test_prompt_placeholders_when_empty():
    prompt = build_scoring_prompt(_input(direction=None, settings=DEFAULT_SETTINGS))
    assert "(none active)" in prompt
    assert "Role/title keywords: (none stated)" in prompt


def test_each_call_carries_its_own_direction():
    session = StubSession(json.dumps(GOOD))
    port = ClaudeScoringPort(session)
    asyncio.run(port.score_posting(_input(direction="first direction")))
    asyncio.run(port.score_posting(_input(direction=None)))
    score_prompts = [p for p in session.prompts if p.startswith("You are scoring")]
    assert "first direction" in score_prompts[0]
    assert "first direction" not in score_prompts[1]
    assert "(none active)" in score_prompts[1]
