# SKILL.md — what this tool does and how the QA pipeline works

## What it does

This system reviews senior-living job postings against a style guide and writes
QA tickets to a SQLite database (`qa_tickets.db`). Each ticket is classified by
**Category** (what kind of issue), **Issue Type** (the specific problem), and
**Section** (where in the posting it is). A Flask dashboard presents the tickets,
grouped by community and check type, with controls for managing each check.

The core idea is a **two-layer split**: cheap deterministic rules catch the
mechanical problems, and an AI layer handles the judgment calls.

## The pipeline

```
Hireology careers API
        │
        ▼
fetch_jobs.py ──► AUTO regex checks      (Layer 1 — deterministic)
        │           e.g. unfilled [PLACEHOLDER], raw recruiter email,
        │           missing EEO statement, highlighted text, ALL-CAPS body
        ▼
pre_scan.py  ──► more AUTO regex checks  (Layer 1 — runs just before the AI)
        │           e.g. inline font-size / color, underline tags, nested
        │           lists, excessive exclamation points, missing brand
        │           boilerplate
        ▼
run_ai_review.py ──► Claude review       (Layer 2 — judgment)
        │           missing required sections, tone, resident/patient
        │           terminology drift, generic vs. community-specific
        │           language, compensation clarity, spelling & grammar
        ▼
   SQLite (tickets, email_controls, …)
        │
        ▼
qa_dashboard.py (Flask) ──► dashboard: Tickets, Communities, Controls,
                            Templates, login + per-ticket audit trail
```

**Layer 1 (deterministic)** lives in `fetch_jobs.py` and `pre_scan.py`. These
are fast regex/structural checks with no API cost. They write tickets tagged
`detected_by='AUTO'`. Example: an `[INSERT LOCATION]` placeholder is an exact
pattern, so a regex flags it every time.

**Layer 2 (AI)** lives in `run_ai_review.py`. It calls the Claude API for the
checks that need reading comprehension and tagged `detected_by='CLAUDE'`. The
style guide (`docs/style_guide.md`) is read into the system prompt at runtime, so
updating the guide changes what the AI flags. Example: deciding whether a posting
*actually* lacks a "What We Offer" section, or whether it just uses a synonym
heading, is a judgment call — that's the AI's job.

`pre_scan.py` also hands the AI a short brief of what the deterministic layer
already caught, so the two layers don't double-flag the same issue.

## How results are handled

Each AI finding returns `category`, `section`, and `issue_type`. The router
checks whether the `(category, issue_type)` pair is a known check in the
`email_controls` table:

- **Known pair → live ticket**, written to `tickets`.
- **Unknown pair → dropped** (with a debug log). New checks are added
  deliberately, not invented at runtime.
- Fuzzy matching catches minor wording drift so near-matches still route
  correctly.
- Grammar/spelling findings auto-route to the single `Content / Spelling and
  Grammar` pair; exclamation findings collapse to `Tone / Excessive Exclamation
  Points`.

## Database tables (brief)

| Table | Contents |
|---|---|
| `tickets` | Every QA ticket (AUTO + CLAUDE), with category / issue_type / section / severity / status. |
| `email_controls` | One row per `(category, issue_type)` — the source of truth for which checks exist, their severity, and per-check settings. |
| `rejected_issues` | Retired checks (tombstones) the AI should never flag again. |
| `issue_aliases` | Maps AI wording drift to canonical pairs. |
| `users`, `ticket_notes`, `audit_events` | Login accounts, per-ticket notes, and an append-only action log. |

## Running it

```bash
pip install -r requirements.txt
python seed_demo_data.py --include-ai-examples   # synthetic demo data
python qa_dashboard.py                            # http://localhost:5000
```

The AI layer needs an `ANTHROPIC_API_KEY`; the deterministic layer and the
seeded demo run fine without one. See `README.md` for full setup and
`CLAUDE.md` for architecture/safety notes.
