"""AC12: grounded skills-gap -- the pure diff and the pipeline (stubbed LLM)."""

import asyncio
import json

import pytest

from app.agent import skills_gap
from app.agent.skills_gap import (
    NO_GAPS_NOTE,
    compute_gap_note,
    diff_skills,
    fallback_note,
    parse_json_object,
)
from app.ports import ScoringInput
from app.settings_store import DEFAULT_SETTINGS
from tests.s3_helpers import posting


def test_diff_exact_and_fuzzy_matches_are_not_missing():
    missing_req, missing_pref = diff_skills(
        required=["Python", "PostgreSQL", "Kubernetes", "Rust"],
        preferred=["GraphQL", "Terraform"],
        resume_skills=["python 3", "Postgres SQL", "kubernetes", "Terraform"],
    )
    assert missing_req == ["Rust"]
    assert missing_pref == ["GraphQL"]


def test_diff_normalizes_case_and_punctuation():
    missing_req, _ = diff_skills(["Node.js", "C#"], [], ["node js", "c"])
    assert missing_req == []


def test_diff_dissimilar_skills_stay_missing():
    # Java vs JavaScript must NOT count as a match.
    missing_req, missing_pref = diff_skills(["JavaScript"], ["Go"], ["Java"])
    assert missing_req == ["JavaScript"]
    assert missing_pref == ["Go"]


def test_diff_empty_resume_skills_everything_missing():
    assert diff_skills(["A"], ["B"], []) == (["A"], ["B"])


def test_parse_json_object_tolerates_fences_and_rejects_junk():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(ValueError):
        parse_json_object("{not: valid}")


def test_fallback_note_prefers_required():
    assert fallback_note(["Rust", "Go", "Zig"], ["X"]) == "Gap (required): Rust and Go."
    assert fallback_note([], ["X"]) == "Gap (preferred): X."


class ScriptedAsk:
    """Answers by prompt type; records every prompt."""

    def __init__(self, requirements, resume_skills, note="Consider learning Rust."):
        self.requirements = requirements
        self.resume_skills = resume_skills
        self.note = note
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("Read the job posting text below"):
            return json.dumps(self.requirements)
        if prompt.startswith("Read the resume text below"):
            return json.dumps({"skills": self.resume_skills})
        return self.note


def _input(raw_text="We need Rust and Python.", resume="Python dev"):
    p = posting("greenhouse", "1")
    p = type(p)(**{**p.__dict__, "raw_text": raw_text})
    return ScoringInput(
        resume_text=resume, posting=p, settings=DEFAULT_SETTINGS, chat_direction=None
    )


def test_gap_note_phrases_only_diffed_missing_items():
    ask = ScriptedAsk({"required": ["Rust", "Python"], "preferred": []}, ["Python"])
    note = asyncio.run(compute_gap_note(ask, _input(), {}))
    assert note == "Consider learning Rust."
    phrase_prompt = ask.prompts[-1]
    assert '["Rust"]' in phrase_prompt  # only the diff's missing list is sent
    assert (
        "Python" not in phrase_prompt.split("Missing required items:")[1].split("\n")[0]
    )


def test_no_gaps_skips_the_phrasing_call():
    ask = ScriptedAsk({"required": ["Python"], "preferred": []}, ["Python"])
    assert asyncio.run(compute_gap_note(ask, _input(), {})) == NO_GAPS_NOTE
    assert len(ask.prompts) == 2  # requirements + resume skills only


def test_resume_skills_extracted_once_per_resume_text():
    ask = ScriptedAsk({"required": ["Python"], "preferred": []}, ["Python"])
    cache: dict[str, list[str]] = {}
    for _ in range(3):
        asyncio.run(compute_gap_note(ask, _input(), cache))
    resume_calls = [
        p for p in ask.prompts if p.startswith("Read the resume text below")
    ]
    assert len(resume_calls) == 1
    asyncio.run(compute_gap_note(ask, _input(resume="Rust dev"), cache))
    resume_calls = [
        p for p in ask.prompts if p.startswith("Read the resume text below")
    ]
    assert len(resume_calls) == 2


def test_empty_or_runaway_phrasing_falls_back_to_deterministic_note():
    for bad in ["  ", "x" * (skills_gap.MAX_NOTE_CHARS + 1)]:
        ask = ScriptedAsk({"required": ["Rust"], "preferred": []}, [], note=bad)
        assert asyncio.run(compute_gap_note(ask, _input(), {})) == (
            "Gap (required): Rust."
        )


def test_malformed_requirements_raise():
    async def ask(prompt):
        return '{"required": "Rust"}'  # not a list

    with pytest.raises(TypeError):
        asyncio.run(compute_gap_note(ask, _input(), {}))
