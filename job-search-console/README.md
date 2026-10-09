# Job Search Console

A local-first job search tool: it sources postings from Greenhouse, Lever, and
Ashby (plus optional search-engine discovery), scores each one against your
resume using Claude, and shows you a ranked, paginated shortlist. You click
through to apply yourself — nothing is auto-applied, and nothing leaves your
machine except the requests to source job boards and to Claude for scoring.

## What it does

- **Resume**: upload once (PDF/DOCX/TXT), persists across restarts.
- **Settings**: your job preferences (role, seniority, location, must-haves,
  dealbreakers), editable anytime.
- **Chat**: a lightweight, session-scoped way to redirect the next search
  ("focus on fully remote roles") without touching your saved settings.
- **Run**: sources postings, dedupes them (within a run and across runs),
  scores each against your resume via 4 weighted sub-criteria (skills,
  seniority, domain, responsibility), and shows every result — no hidden
  score floor, no arbitrary top-N cut, just everything scored, paginated,
  best first.
- **Resurfacing**: a previously-seen posting only reappears if your settings
  or chat direction changed *and* its score improved by 10+ points — so you
  don't get the same noise every run, but a real shift in what you're looking
  for gets a second look.

## Requirements

- Python 3.13+
- A Claude Pro (or Max) subscription, logged in via the Claude CLI
  (`claude login`) — scoring runs on your subscription, not a metered API key.
- (Optional) a [Brave Search API](https://brave.com/search/api/) key for
  discovering companies beyond the built-in seed list. Without one, the app
  falls back to a small hardcoded list of known companies.

## Setup

```zsh
cd job-search-console
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
claude login   # if you haven't already
```

## Run

```zsh
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. The app binds to localhost only — it's not
reachable from other machines.

Optional environment variables:
- `BRAVE_SEARCH_API_KEY` — enables search-engine-based company discovery.
- `JOB_CONSOLE_MODEL` — override the scoring model (defaults to
  `claude-haiku-4-5-20251001`, chosen for speed/cost; Sonnet and Opus also
  tested and work, just slower).

## Development

```zsh
.venv/bin/pytest tests/
.venv/bin/ruff check app tests
.venv/bin/ruff format app tests
```

## Architecture

- **Backend**: FastAPI + Uvicorn, SQLite for persistence, Server-Sent Events
  for live run progress.
- **Scoring**: Claude Agent SDK, one fresh session per run, real 4-sub-criteria
  structured scoring (never a raw LLM-picked 0-100 number — the final score
  is computed deterministically from the sub-scores).
- **Frontend**: plain HTML/CSS/vanilla JS, no framework, no build step.
- **Dedup**: within-run cross-source matching (URL pattern extraction +
  fuzzy company/title matching), plus cross-run "seen" tracking with
  context-fingerprint-based resurfacing (see above).

## Explicitly out of scope

No auto-apply, no ATS form-filling, no resume/cover-letter generation or
tailoring, no application/outcome tracking, no background/scheduled runs —
this is a find-and-rank tool, not an applier.
