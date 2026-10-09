"""Grounded skills-gap note (tech-spec §2.6).

1. LLM extracts the posting's required / preferred skills (structured JSON).
2. LLM extracts the resume's skills (structured JSON), cached per resume text.
3. Pure-Python fuzzy diff (rapidfuzz, same function/normalizer app/dedup.py
   uses for titles) -> missing_required / missing_preferred.
4. LLM only phrases 1-2 items taken from those missing lists. It is never
   asked to judge the gap itself. If nothing is missing, no LLM call is made.
"""

import hashlib
import json
import re
from collections.abc import Awaitable, Callable

from rapidfuzz import fuzz

from app.dedup import normalize_title  # reused exactly as dedup.py uses it
from app.ports import ScoringInput

# One prompt -> one text reply (ClaudeSessionManager.ask in production).
Ask = Callable[[str], Awaitable[str]]

NO_GAPS_NOTE = "No notable gaps found."

REQUIREMENTS_PROMPT = """Read the job posting text below and extract its \
skill/technology requirements. Follow these steps.

STEP 1 - Find every skill, tool, technology, or domain expertise area the \
posting mentions as needed or wanted. Ignore soft-skill phrases (e.g. "team \
player", "communication skills") and ignore years-of-experience phrases \
(e.g. "5+ years"). Only concrete things like "React", "PostgreSQL", \
"Kubernetes", "machine learning" belong in the lists.

STEP 2 - Sort each item into exactly one of two lists:
- "required": anything the posting states as needed or a must-have, or does \
not explicitly mark as optional.
- "preferred": anything the posting explicitly marks as nice-to-have, a \
bonus, or preferred.

STEP 3 - Write each item short (1-4 words), using its common canonical name \
(e.g. "React" not "React.js framework experience").

STEP 4 - Output ONLY this JSON object, nothing else -- no markdown code \
fencing, no prose before or after it:
{{"required": ["<skill>", ...], "preferred": ["<skill>", ...]}}

EXAMPLE of the exact output FORMAT only -- unrelated to the posting below \
and must NOT influence your actual answer:
{{"required": ["Python", "AWS", "SQL"], "preferred": ["Kubernetes", \
"Terraform"]}}

Posting text:
{raw_text}"""

RESUME_SKILLS_PROMPT = """Read the resume text below and extract the \
skills/technologies it demonstrates. Follow these steps.

STEP 1 - Find every skill, tool, technology, or domain expertise area the \
resume shows evidence of, via jobs held, projects described, or an explicit \
skills section.

STEP 2 - Write each item short (1-4 words), using its common canonical name \
(e.g. "React", "PostgreSQL", "Kubernetes").

STEP 3 - Output ONLY this JSON object, nothing else -- no markdown code \
fencing, no prose before or after it:
{{"skills": ["<skill>", ...]}}

EXAMPLE of the exact output FORMAT only -- unrelated to the resume below \
and must NOT influence your actual answer:
{{"skills": ["Python", "AWS", "SQL", "Docker"]}}

Resume text:
{resume_text}"""

GAP_NOTE_PROMPT = """A candidate is missing these job requirements. Follow \
these steps.

Missing required items: {missing_required}
Missing preferred items (less critical): {missing_preferred}

STEP 1 - Pick 1-2 of the most important missing items. If the required-items \
list above is non-empty, pick from it; only pick from the preferred-items \
list if the required-items list is empty.

STEP 2 - Write one short sentence (at most two short sentences) for a \
job-search UI card, naming only the 1-2 items you picked in step 1, spelled \
exactly as they appear above. Do not mention any item that is not in the \
lists above. Do not add commentary about the candidate's overall fit or \
anything else.

STEP 3 - Output ONLY that sentence (or two sentences), nothing else: no \
preamble, no quotes around it, no JSON.

EXAMPLE of the exact output FORMAT only -- unrelated to the items above and \
must NOT influence your actual answer:
Missing required Kubernetes experience; GraphQL would also help.

Now write the note, following steps 1-3 above:"""

FUZZY_MATCH_THRESHOLD = 85  # looser than dedup's FUZZY_TITLE_THRESHOLD (88):
# skill-phrase wording varies more than job titles do.

MAX_NOTE_CHARS = 400


def parse_json_object(raw: str) -> dict:
    """Extracts the outermost {...} (tolerates a stray markdown fence or
    prose) and json.loads it. Raises ValueError on anything unparseable."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in response")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise TypeError("response JSON is not an object")
    return data


def _str_list(data: dict, key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise TypeError(f"expected a JSON list for {key!r}")
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


async def extract_requirements(ask: Ask, raw_text: str) -> tuple[list[str], list[str]]:
    data = parse_json_object(await ask(REQUIREMENTS_PROMPT.format(raw_text=raw_text)))
    return _str_list(data, "required"), _str_list(data, "preferred")


async def extract_resume_skills(
    ask: Ask, resume_text: str, cache: dict[str, list[str]]
) -> list[str]:
    """Cached by a hash of the resume text: runs once per distinct resume per
    process, not once per posting."""
    key = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    if key not in cache:
        data = parse_json_object(
            await ask(RESUME_SKILLS_PROMPT.format(resume_text=resume_text))
        )
        cache[key] = _str_list(data, "skills")
    return cache[key]


def skill_matched(requirement: str, resume_skills: list[str]) -> bool:
    req = normalize_title(requirement)
    return any(
        fuzz.token_sort_ratio(req, normalize_title(s)) >= FUZZY_MATCH_THRESHOLD
        for s in resume_skills
    )


def diff_skills(
    required: list[str], preferred: list[str], resume_skills: list[str]
) -> tuple[list[str], list[str]]:
    """Pure Python, no LLM. A requirement is matched if any resume skill's
    normalized text scores >= FUZZY_MATCH_THRESHOLD against it
    (rapidfuzz.fuzz.token_sort_ratio). Returns (missing_required,
    missing_preferred), input order preserved."""
    return (
        [r for r in required if not skill_matched(r, resume_skills)],
        [p for p in preferred if not skill_matched(p, resume_skills)],
    )


def fallback_note(missing_required: list[str], missing_preferred: list[str]) -> str:
    """Deterministic phrasing, used if the phrasing call returns junk."""
    items = (missing_required or missing_preferred)[:2]
    kind = "required" if missing_required else "preferred"
    return f"Gap ({kind}): {' and '.join(items)}."


async def phrase_gap_note(
    ask: Ask, missing_required: list[str], missing_preferred: list[str]
) -> str:
    if not missing_required and not missing_preferred:
        return NO_GAPS_NOTE
    note = (
        (
            await ask(
                GAP_NOTE_PROMPT.format(
                    missing_required=json.dumps(missing_required),
                    missing_preferred=json.dumps(missing_preferred),
                )
            )
        )
        .strip()
        .strip('"')
        .strip()
    )
    if not note or len(note) > MAX_NOTE_CHARS:
        return fallback_note(missing_required, missing_preferred)
    return note


async def compute_gap_note(
    ask: Ask, input: ScoringInput, resume_skills_cache: dict[str, list[str]]
) -> str:
    required, preferred = await extract_requirements(ask, input.posting.raw_text)
    resume_skills = await extract_resume_skills(
        ask, input.resume_text, resume_skills_cache
    )
    missing_required, missing_preferred = diff_skills(
        required, preferred, resume_skills
    )
    return await phrase_gap_note(ask, missing_required, missing_preferred)
