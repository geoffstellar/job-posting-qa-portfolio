# CLAUDE.md — notes for an AI assistant working in this repo

This is a small Flask + SQLite app that QA-checks senior-living job postings.
If you're an AI assistant making changes here, read this first. It's short.

## Project structure

| File | What it is |
|---|---|
| `qa_dashboard.py` | Flask app — all web pages and API endpoints. |
| `fetch_jobs.py` | Pulls postings from Hireology's public careers API + runs the deterministic AUTO checks. |
| `pre_scan.py` | Extra regex checks run just before the AI review. |
| `run_ai_review.py` | The Claude review layer (the judgment-call checks). |
| `db.py` | Database access layer + the safe read/write helpers. **Read the DB-safety section below before writing to the DB.** |
| `taxonomy.py` | The Category / Section / Issue-Type model and routing rules. |
| `docs/style_guide.md` | The QA standard. **Also read at runtime by the AI prompt** — see "Style guide = config." |
| `migrations/` | Alembic schema migrations (one baseline revision). |
| `templates/`, `static/` | Jinja templates + CSS/JS for the dashboard. |
| `seed_demo_data.py`, `jobs_raw.json` | Build a runnable demo database from synthetic postings. |

## Database write safety (the one rule that matters)

All DB writes go through `db.py`'s copy helpers — never hold a long-lived
`sqlite3.connect()` open against the live DB file and write to it directly
(it can corrupt SQLite on synced/networked filesystems). The pattern:

```python
import db as _db
con, tmp = _db.read_copy(db_path)   # copy to temp, get a connection
_db.ensure_tables(con)              # idempotent; call BEFORE any BEGIN
# ... INSERT / UPDATE / DELETE ...
con.commit()
con.close()
_db.write_copy(tmp, db_path)        # atomically swap the temp copy back
```

`ensure_tables()` does its own commit, so call it before starting a manual
transaction, not inside one. Read-only scripts can use
`db.connect_readonly()`. `USE_DB_COPY_WORKAROUND` toggles the copy-vs-direct
behavior for synced folders; the default is fine for normal filesystems and
containers.

## The taxonomy (how a finding is classified)

Every ticket has three fields:

- **Category** — the *what*. Five values in `taxonomy.CATEGORIES`: `Tone`,
  `Content`, `Formatting`, `Structure` (the four the AI may emit) plus
  `Templates` (system-managed; the AI must never emit it).
- **Issue Type** — the specific problem (e.g. `Missing Required Section`,
  `Inline Font Size`, `Excessive Exclamation Points`).
- **Section** — display-only metadata for *where* in the posting the issue is.
  Never used for routing.

The identity of a check is the `(category, issue_type)` pair. **`email_controls`
is the source of truth for which checks exist** — filters and the Controls page
read from it, not from the `tickets` table. An AI-emitted pair that isn't in
`email_controls` is silently dropped (so a new check needs a row there). Some
issue types always route to a fixed category regardless of context — see
`taxonomy.CONSOLIDATED_TYPES`.

## Style guide = configuration

`docs/style_guide.md` isn't just docs — `run_ai_review.py` reads it into the
system prompt at runtime, and `pre_scan.py` / `fetch_jobs.py` key on a few
concrete phrases in it (the brand-boilerplate markers, the generic-location
phrase, the EEO phrase). If you change a flagging rule, update **both** the
style guide and the matching check.

## Conventions

- When adding a pip dependency, update `requirements.txt` in the same change.
- New schema changes go through Alembic (`migrations/`), not ad-hoc SQL.
- Keep `requirements.txt`, this file, and `SKILL.md` current with what you change.

## Running it

```bash
python -m venv venv && venv\Scripts\activate    # or: source venv/bin/activate
pip install -r requirements.txt
python seed_demo_data.py --include-ai-examples   # build the demo DB
python qa_dashboard.py                           # http://localhost:5000
```
