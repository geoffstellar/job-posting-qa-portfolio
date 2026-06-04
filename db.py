"""
db.py — Single source of truth for all database access in the QA system.

Every SQLite query in the app should eventually live here. Other modules
call functions like db.read_copy(), db.get_known_pairs(), db.save_ticket()
instead of writing raw SQL.

Designed to be imported by: qa_dashboard.py, run_ai_review.py, fetch_jobs.py,
cleanup_tickets.py, and any future scripts.

Migration note: this file is being populated incrementally — each time we
touch a module, its queries move here. Until migration is complete, some
raw SQL still lives in the calling modules.

Table map (2026-04-16):
  tickets            -- live findings. Refreshed by write_db() each run.
  email_controls     -- MANAGED active checks. Rows created via Add Check
                        wizard (Controls page). fetch_jobs.write_db() never
                        touches this table.
  rejected_issues    -- Retired check tombstones. retired_settings JSON
                        preserves the original email_controls row for clean
                        revive via Recipe 10 / Revive button.
  issue_aliases      -- (alias_area, alias_issue_type) → canonical pair.
                        Applied by run_ai_review.py to remap AI emissions.
  custom_areas       -- User-defined category names beyond canonical set.
  discovery_log      -- AI-assisted check suggestions (accepted + rejected)
                        with notes for long-term discovery-prompt tuning.
  pending_tickets    -- DEPRECATED 2026-04-16. Kept for rollback safety; no
                        code reads or writes it. Unknown AI pairs are now
                        silently dropped by run_ai_review.py.
"""

import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(BASE, 'qa_tickets.db')


# ── Engine layer (added 2026-04-22, Phase 2 of containerize+DB abstraction) ──
# SQLAlchemy parses DATABASE_URL so the same code path works for SQLite (pilot
# deployment on Fly.io) and Postgres (later cutover to Azure). The engine is
# lazily created — importing db.py doesn't touch the DB, so pure helpers like
# taxonomy coercion remain cheap to import.
#
# DATABASE_URL formats supported:
#   sqlite:///relative/path.db            (3 slashes → relative path)
#   sqlite:////abs/path.db                (4 slashes → absolute path)
#   postgresql+psycopg://user:pw@host/db  (Postgres via psycopg v3)
#
# If DATABASE_URL is unset, the repo-local path (qa_tickets.db next to this
# file) is used — which matches the pre-2026-04-22 behavior so
# `python qa_dashboard.py` on Windows is unchanged.
#
# Phase 2 scope note: runtime queries still use sqlite3 under the hood.
# DATABASE_URL pointing at Postgres will work for Alembic migrations but
# runtime query execution is deferred to a follow-up PR (query-level
# translation from `?` to named params). the maintainer's pilot runs on SQLite, so
# this split is intentional.

def _database_url():
    """Return the configured DATABASE_URL, or the SQLite default.

    Default derivation order (Phase 3, 2026-04-22):
      1. DATABASE_URL env var wins if set.
      2. Otherwise, build sqlite:///<APP_DATA_DIR>/qa_tickets.db.
      3. APP_DATA_DIR defaults to the repo folder so `python qa_dashboard.py`
         on Windows behaves identically to pre-Phase-3 (writes the DB next
         to the code). Containers override APP_DATA_DIR=/app/data so the
         DB lives on a mounted volume instead.
    """
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    data_dir = os.environ.get('APP_DATA_DIR') or BASE
    return f'sqlite:///{os.path.join(data_dir, "qa_tickets.db")}'


def _is_sqlite_url(url):
    return url.startswith('sqlite:')


def _sqlite_path_from_url(url):
    """Extract the filesystem path from a sqlite:// URL.

    sqlite:///foo.db   → foo.db   (3 slashes: relative)
    sqlite:////a/b.db  → /a/b.db  (4 slashes: absolute)
    sqlite://          → ':memory:' (rare; for completeness)
    """
    if not url.startswith('sqlite:'):
        raise ValueError(f'Not a sqlite URL: {url!r}')
    # Strip the scheme. What remains is '///foo.db' or '////a/b.db' or '//'.
    rest = url[len('sqlite:'):]
    # SQLAlchemy convention: one extra slash = relative, two = absolute.
    if rest.startswith('////'):
        return rest[3:]            # absolute: '/a/b.db'
    if rest.startswith('///'):
        return rest[3:]            # relative to CWD: 'foo.db'
    if rest == '//':
        return ':memory:'
    # Fallback — treat whatever comes after the scheme as a path.
    return rest.lstrip('/')


_engine_cache = None  # populated on first get_engine() call


def get_engine():
    """Return a cached SQLAlchemy engine bound to DATABASE_URL.

    Used by Alembic for schema migrations and by any future query code that
    migrates off raw sqlite3. Runtime query code in Phase 2 does NOT go
    through this engine — it uses sqlite3 directly via read_copy/write_copy.
    """
    global _engine_cache
    if _engine_cache is not None:
        return _engine_cache
    from sqlalchemy import create_engine
    url = _database_url()
    # For SQLite, SQLAlchemy needs check_same_thread=False when the engine is
    # shared across Flask request threads. Postgres has no such flag.
    connect_args = {'check_same_thread': False} if _is_sqlite_url(url) else {}
    _engine_cache = create_engine(url, connect_args=connect_args, future=True)
    return _engine_cache


# ── Connection helpers ────────────────────────────────────────────────────────
# The copy-to-temp workaround was built for a Windows mounted path where
# SQLite's fsync/lock behavior was unreliable. In containers (Fly.io, Azure)
# and on most native filesystems it's unnecessary overhead. Gate it behind
# USE_DB_COPY_WORKAROUND so containers run the fast direct path.
#
# USE_DB_COPY_WORKAROUND=1  → copy-to-temp + atomic rename (legacy Windows dev)
# USE_DB_COPY_WORKAROUND=0  → direct sqlite3.connect on the DB file (default)
#
# Once a deployment is on Postgres this workaround is moot and the env var
# + this branching can go away.

def _use_copy_workaround():
    return os.environ.get('USE_DB_COPY_WORKAROUND', '0') == '1'


def _resolve_sqlite_path(db_path):
    """Return the on-disk path for the SQLite DB.

    Caller-supplied db_path wins (used by fetch_jobs, etc.).
    Otherwise we parse DATABASE_URL. If DATABASE_URL points at Postgres
    (or any non-sqlite scheme) we raise NotImplementedError with a clear
    message — silent fallback to a repo-local SQLite file would create a
    side DB that the app accidentally writes into while the "real" DB
    (Postgres) sits empty. Fail loud, fix forward.

    Alembic migrations still work on Postgres via db.get_engine() — this
    guard only protects the sqlite3-style runtime query helpers.
    """
    if db_path:
        return db_path
    url = _database_url()
    if _is_sqlite_url(url):
        return _sqlite_path_from_url(url)
    raise NotImplementedError(
        f'Runtime query support for DATABASE_URL={url!r} is not yet '
        'implemented. DATABASE_URL must be a sqlite:// URL for application '
        'runtime. Alembic migrations work on Postgres via db.get_engine(); '
        'a follow-up PR will translate runtime queries so Postgres can '
        'serve requests too. For now, use the sqlite profile in '
        'docker-compose.yml or point DATABASE_URL at a sqlite:// URL.'
    )


# Sentinel suffix used in direct mode. read_copy() returns a zero-byte file
# with this suffix so every call site that does `os.remove(tmp)` / `_discard_
# db_copy(tmp)` continues to work unchanged — the file exists and is cheap
# to create/remove. write_copy() detects the suffix and skips the atomic
# swap that would otherwise overwrite the live DB with an empty file.
_DIRECT_MODE_SUFFIX = '.db-direct-sentinel'


def read_copy(db_path=None):
    """Open a writable connection to the DB and return (con, tmp_path).

    Two modes:
      USE_DB_COPY_WORKAROUND=1 (legacy): copies the DB to /tmp, returns a
        connection to the copy, and relies on write_copy() to atomically
        swap the copy back over the live file. Used on Windows OneDrive /
        WSL mount paths where SQLite fsync was unreliable.
      USE_DB_COPY_WORKAROUND=0 (default): opens sqlite3.connect(db_path)
        directly. Returns (con, sentinel_path) where sentinel_path is a
        zero-byte file with suffix `.db-direct-sentinel` — write_copy()
        cleans it up without touching the live DB, and the many call
        sites that do `os.remove(tmp)` in a finally: block work unchanged.

    Caller must close con and either call write_copy() (persist) or
    cleanup_tmp() (discard). Both handle the sentinel path safely.
    """
    path = _resolve_sqlite_path(db_path)
    if _use_copy_workaround():
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        shutil.copy2(path, tmp.name)
        con = sqlite3.connect(tmp.name)
        con.row_factory = sqlite3.Row
        return con, tmp.name
    # Direct path — no temp copy, no atomic rename. Commits land on the DB
    # file immediately when the caller runs COMMIT. A sentinel file is
    # returned as tmp_path so callers can unconditionally os.remove() it.
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    sentinel = tempfile.NamedTemporaryFile(suffix=_DIRECT_MODE_SUFFIX,
                                           delete=False)
    sentinel.close()
    return con, sentinel.name


def write_copy(tmp_path, db_path=None):
    """Persist the write-side of a read_copy() pair.

    Behavior depends on tmp_path:
      None (legacy direct-mode caller) → no-op.
      ends with `.db-direct-sentinel` → direct mode; remove the sentinel
        but do NOT overwrite the live DB. The con.execute('COMMIT') on
        the live connection already persisted the writes.
      anything else (workaround mode) → atomic-rename the temp file over
        the live DB. Falls back to shutil.copy2 on cross-device rename
        failures.

    This dual behavior lets callers continue using the read_copy → modify
    → write_copy pattern unchanged regardless of which mode is active.
    """
    if tmp_path is None:
        return
    if isinstance(tmp_path, str) and tmp_path.endswith(_DIRECT_MODE_SUFFIX):
        # Direct mode — nothing to swap. Best-effort remove the sentinel.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return
    path = _resolve_sqlite_path(db_path)
    staging = path + '.tmp-write'
    try:
        shutil.copy2(tmp_path, staging)
        os.replace(staging, path)          # atomic on same filesystem
    except OSError:
        # Cross-device or unsupported — direct copy as last resort
        try:
            os.remove(staging)
        except OSError:
            pass
        shutil.copy2(tmp_path, path)
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def cleanup_tmp(tmp_path):
    """Remove a temp DB file without writing back (discard changes).

    Accepts None or an empty string as a no-op so callers never need to
    branch on tmp_path type. Works for both real workaround-mode temp files
    and direct-mode sentinel files (the filename tells them apart; cleanup
    logic is identical — just delete the path).
    """
    if not tmp_path:
        return
    try:
        os.remove(tmp_path)
    except OSError:
        pass


# ── Read-only connection helper (Phase 2, 2026-04-22) ────────────────────────
# Five call sites in qa_dashboard.py currently open `sqlite3.connect(DB)`
# directly for read-only queries (auth lookup, note listing, etc.). This
# helper centralises the connection setup so DATABASE_URL/USE_DB_COPY_WORKAROUND
# gating happens in one place. Always uses sqlite3.Row as the row factory so
# existing `row['col']` access patterns keep working.
#
# Read-only = do not call COMMIT/ROLLBACK on the returned connection. For
# writes, use read_copy → write_copy as before.

def connect_readonly(db_path=None):
    """Return a sqlite3.Connection for read-only use.

    Caller is responsible for closing the connection. Row factory is set
    to sqlite3.Row so columns are accessible by name.
    """
    path = _resolve_sqlite_path(db_path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con




# ── Alembic bootstrap (Phase 2, 2026-04-22) ──────────────────────────────────
# On every startup we want one of three things to happen:
#
#   1. Brand-new DB (no tables yet) → run `alembic upgrade head` to create
#      the full schema. Triggered on container first boot against an empty
#      named volume or a freshly provisioned Postgres.
#   2. Existing DB with live schema but no alembic_version table → run
#      `alembic stamp head` to record the baseline without replaying the
#      initial revision (which would try to CREATE TABLE tickets on a DB
#      that already has tickets). Triggered on the maintainer's Windows dev DB the
#      first time the new code runs.
#   3. Existing DB with alembic_version already set → run `alembic upgrade
#      head` to apply any pending revisions. Normal subsequent boots.
#
# stamp_or_upgrade() handles all three cases idempotently. Safe to call on
# every app start. Safe to call concurrently (alembic takes an advisory lock).

def stamp_or_upgrade():
    """Bring the DB schema to head. Stamps existing pre-Alembic DBs first.

    Must be called at app startup before the first query runs. Returns the
    action taken ('upgrade' | 'stamp+upgrade' | 'noop-no-db') for logging.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg_path = os.path.join(BASE, 'alembic.ini')
    if not os.path.exists(cfg_path):
        # Alembic not installed in this checkout (shouldn't happen post-Phase 2,
        # but stays graceful so older dev environments don't hard-fail).
        return 'noop-no-config'

    cfg = Config(cfg_path)
    cfg.set_main_option('script_location', os.path.join(BASE, 'migrations'))

    engine = get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    action = 'upgrade'
    # Pre-Alembic DB heuristic: tickets table exists but alembic_version doesn't.
    # If we just ran upgrade head here, the initial revision would fail trying
    # to CREATE TABLE tickets. Stamp first, then upgrade picks up future revisions.
    if 'tickets' in existing and 'alembic_version' not in existing:
        command.stamp(cfg, 'head')
        action = 'stamp+upgrade'

    command.upgrade(cfg, 'head')
    return action


# ── Schema initialisation ────────────────────────────────────────────────────

def ensure_tables(con):
    """Create auxiliary tables if they don't exist and run column migrations.

    Safe to call repeatedly — all operations are idempotent (CREATE IF NOT
    EXISTS, ALTER ADD COLUMN wrapped in try/except).
    """
    con.executescript("""
        -- pending_tickets is DEPRECATED (2026-04-16, Phase 2 of Check
        -- Management feature). The table is kept for rollback safety but
        -- no code reads or writes it. Unknown AI-emitted pairs are now
        -- silently dropped; new checks come through the Claude
        -- qa-rules-maintenance skill (the in-dashboard Add Check wizard
        -- was removed 2026-04-24). See CLAUDE.md for the full lifecycle.
        CREATE TABLE IF NOT EXISTS pending_tickets (
            ticket_id      TEXT PRIMARY KEY,
            date_flagged   TEXT,
            req_id         TEXT,
            job_title      TEXT,
            community      TEXT,
            severity       TEXT,
            check_type     TEXT,
            issue_summary  TEXT,
            offending_text TEXT,
            detected_by    TEXT,
            status         TEXT DEFAULT 'Pending',
            notes          TEXT,
            category           TEXT,
            issue_type     TEXT,
            closest_area       TEXT,
            closest_issue_type TEXT,
            scope          TEXT DEFAULT 'ALL'
        );
        CREATE TABLE IF NOT EXISTS rejected_issues (
            category            TEXT NOT NULL,
            issue_type      TEXT NOT NULL,
            rejected_at     TEXT,
            notes           TEXT,
            PRIMARY KEY (category, issue_type)
        );
        CREATE TABLE IF NOT EXISTS custom_areas (
            category        TEXT PRIMARY KEY,
            created_at  TEXT,
            notes       TEXT
        );
        CREATE TABLE IF NOT EXISTS issue_aliases (
            alias_area          TEXT NOT NULL,
            alias_issue_type    TEXT NOT NULL,
            canonical_area      TEXT NOT NULL,
            canonical_issue     TEXT NOT NULL,
            created_at          TEXT,
            notes               TEXT,
            PRIMARY KEY (alias_area, alias_issue_type)
        );
        -- users: admin-managed accounts (added 2026-04-20, Migration:
        -- User accounts + feedback infrastructure). page_permissions is a
        -- JSON blob {page_name: bool}; admins bypass the check entirely.
        CREATE TABLE IF NOT EXISTS users (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            email            TEXT UNIQUE NOT NULL,
            name             TEXT,
            password_hash    TEXT NOT NULL,
            is_admin         INTEGER NOT NULL DEFAULT 0,
            active           INTEGER NOT NULL DEFAULT 1,
            page_permissions TEXT,
            created_at       TEXT,
            last_login_at    TEXT
        );
        -- ticket_notes: plain-text community notes attributed to a user
        -- (added 2026-04-20). No FK constraint on ticket_id / user_id — if a
        -- ticket disappears, notes become orphaned rather than cascade-
        -- deleted, which matches how the rest of the schema handles refs.
        CREATE TABLE IF NOT EXISTS ticket_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            note_text   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ticket_notes_ticket_id
            ON ticket_notes(ticket_id);

        -- audit_events: append-only event log. Phase 1 of pilot tester
        -- activity audit trail (2026-04-27). Generic by entity_type so
        -- Phase 4 can extend to email_controls actions without a schema
        -- change. See "Audit Trail Is Append-Only" standing rule in
        -- CLAUDE.md for the writer/reader contract.
        CREATE TABLE IF NOT EXISTS audit_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type          TEXT NOT NULL,
            entity_id            TEXT NOT NULL,
            user_id              INTEGER NOT NULL,
            actor_email_snapshot TEXT NOT NULL,
            action               TEXT NOT NULL,
            payload_json         TEXT,
            bulk_id              TEXT,
            created_at           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_entity
            ON audit_events(entity_type, entity_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_user_time
            ON audit_events(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_created_at
            ON audit_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_audit_bulk
            ON audit_events(bulk_id);
    """)

    # Defensive column adds for older databases
    pt_cols = {r[1] for r in con.execute('PRAGMA table_info(pending_tickets)').fetchall()}
    for col, default in [('closest_area', None), ('closest_issue_type', None),
                         ('scope', "'ALL'")]:
        if col not in pt_cols:
            ddl = f'ALTER TABLE pending_tickets ADD COLUMN {col} TEXT'
            if default:
                ddl += f' DEFAULT {default}'
            con.execute(ddl)

    ec_cols = {r[1] for r in con.execute('PRAGMA table_info(email_controls)').fetchall()}
    if 'show_on_community' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN show_on_community INTEGER NOT NULL DEFAULT 1')
    if 'scope' not in ec_cols:
        con.execute("ALTER TABLE email_controls ADD COLUMN scope TEXT DEFAULT 'ALL'")
    if 'default_severity' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN default_severity TEXT')

    # Mute / pause toggle (April 2026) — view-only filter. Muted pairs keep
    # firing and generating tickets, but are hidden from Live Tickets and
    # Community pages. See the internal issue tracker → "Mute / pause toggle
    # for issue types and areas" for the original design notes.
    if 'muted' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN muted INTEGER NOT NULL DEFAULT 0')
    if 'muted_at' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN muted_at TEXT')
    if 'muted_reason' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN muted_reason TEXT')

    # Taxonomy Flatten Phase 2 (2026-04-17) — adds Section axis and
    # multi_location_behavior routing flag. See
    # docs/TAXONOMY_FLATTEN_HANDOFF_2026-04-17.md.
    if 'multi_location_behavior' not in ec_cols:
        con.execute(
            "ALTER TABLE email_controls ADD COLUMN multi_location_behavior TEXT DEFAULT 'dedup'"
        )

    # Templates Category (2026-04-21) — the 7th Category. Each row under
    # category='Templates' carries a non-null template_pattern holding the exact
    # offending_text to match against. Non-Templates rows leave this column
    # NULL. See CLAUDE.md "Templates Category" standing rule (template-pattern routing).
    if 'template_pattern' not in ec_cols:
        con.execute('ALTER TABLE email_controls ADD COLUMN template_pattern TEXT')

    t_cols = {r[1] for r in con.execute('PRAGMA table_info(tickets)').fetchall()}
    if 'section' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN section TEXT')
    # Standardized reason capture (Migration 2026-04-20). Dedicated column so
    # Flag/Resolve/Acknowledge reasons are queryable, separate from free-text
    # notes. Historical rows keep reason = NULL; no retroactive backfill.
    if 'reason' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN reason TEXT')
    # Templates Category audit breadcrumb (2026-04-21). Free-text label set at
    # capture time and at auto-link time recording the ticket's pre-template
    # (Category / Issue Type). Never used for routing, filtering, or grouping
    # — purely a readability aid when a human opens a captured ticket and
    # wants to know what it started life as.
    if 'captured_from' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN captured_from TEXT')

    # Last-action denorm cache (2026-04-27, audit trail Phase 1). Updated
    # alongside every state-change event so the live ticket viewer can render
    # an attribution badge ("Resolved by the maintainer · Apr 22") without a
    # correlated subquery on audit_events. Source of truth is still
    # audit_events; these columns are a render-time convenience.
    if 'last_action_by' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN last_action_by INTEGER')
    if 'last_action_at' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN last_action_at TEXT')
    if 'last_action' not in t_cols:
        con.execute('ALTER TABLE tickets ADD COLUMN last_action TEXT')

    # Add archived_at to rejected_issues for soft-delete (April 2026)
    ri_cols = {r[1] for r in con.execute('PRAGMA table_info(rejected_issues)').fetchall()}
    if 'archived_at' not in ri_cols:
        con.execute('ALTER TABLE rejected_issues ADD COLUMN archived_at TEXT')

    # retired_settings: JSON blob storing the email_controls row at time of
    # retirement, so Revive can restore severity/notes/fix_instruction cleanly.
    # Added 2026-04-16 (Phase 2 of Check Management feature).
    if 'retired_settings' not in ri_cols:
        con.execute('ALTER TABLE rejected_issues ADD COLUMN retired_settings TEXT')

    # discovery_log: stores AI-assisted check suggestions (accepted + rejected)
    # with notes for periodic fine-tuning. Added 2026-04-16 (Phase 2).
    con.execute("""
        CREATE TABLE IF NOT EXISTS discovery_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date            TEXT NOT NULL,
            suggested_area      TEXT NOT NULL,
            suggested_issue_type TEXT NOT NULL,
            suggested_severity  TEXT,
            suggested_ownership TEXT,
            example_text        TEXT,
            draft_rule          TEXT,
            decision            TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
            notes               TEXT,
            prompt_version      TEXT
        )
    """)

    # Rename legacy location → category (very old schemas)
    for tbl in ('tickets', 'email_controls', 'pending_tickets'):
        tbl_cols = {r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()}
        if 'location' in tbl_cols and 'category' not in tbl_cols and 'area' not in tbl_cols:
            con.execute(f'ALTER TABLE {tbl} RENAME COLUMN location TO category')

    # Rename area → category (2026-05-21 taxonomy refactor). Defense-in-depth
    # alongside the Alembic migration a7b3c8d4e2f1; this catches DBs where the
    # migration hasn't run yet (e.g., a developer pulling fresh code without
    # running `alembic upgrade head`). Five tables get the column rename.
    for tbl in ('tickets', 'email_controls', 'rejected_issues',
                'pending_tickets', 'custom_areas'):
        try:
            tbl_cols = {r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()}
            if 'area' in tbl_cols and 'category' not in tbl_cols:
                con.execute(f'ALTER TABLE {tbl} RENAME COLUMN area TO category')
        except Exception:
            pass

    # Ensure category + issue_type columns exist on all core tables
    for tbl in ('tickets', 'email_controls', 'pending_tickets'):
        try:
            tbl_cols = {r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()}
            if 'category' not in tbl_cols:
                con.execute(f'ALTER TABLE {tbl} ADD COLUMN category TEXT')
            if 'issue_type' not in tbl_cols:
                con.execute(f'ALTER TABLE {tbl} ADD COLUMN issue_type TEXT')
        except Exception:
            pass

    # Add job_url column to tickets and pending_tickets (April 2026)
    for tbl in ('tickets', 'pending_tickets'):
        try:
            tbl_cols = {r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()}
            if 'job_url' not in tbl_cols:
                con.execute(f'ALTER TABLE {tbl} ADD COLUMN job_url TEXT')
        except Exception:
            pass

    con.commit()


# ── Read queries ──────────────────────────────────────────────────────────────

def get_known_pairs(con):
    """Return the set of (category, issue_type) pairs from email_controls."""
    rows = con.execute(
        "SELECT category, issue_type FROM email_controls "
        "WHERE category IS NOT NULL AND category != '' "
        "  AND issue_type IS NOT NULL AND issue_type != ''"
    ).fetchall()
    return {(r[0], r[1]) for r in rows if r[0] and r[1]}


def get_rejected_pairs(con):
    """Return the set of active (non-archived) (category, issue_type) pairs from rejected_issues."""
    rows = con.execute(
        "SELECT category, issue_type FROM rejected_issues "
        "WHERE category IS NOT NULL AND issue_type IS NOT NULL "
        "AND archived_at IS NULL"
    ).fetchall()
    return {(r[0], r[1]) for r in rows if r[0] and r[1]}


def get_custom_areas(con):
    """Return the set of user-defined category names from custom_areas."""
    try:
        return {r[0] for r in con.execute("SELECT category FROM custom_areas").fetchall() if r[0]}
    except sqlite3.OperationalError:
        return set()


def get_default_severities(con):
    """Return dict mapping (category, issue_type) -> severity string from email_controls.

    Only returns pairs where default_severity is set to a non-empty value.
    Used to enforce consistent severity for AI-generated and AUTO tickets via
    the severity-lock mechanism in run_ai_review.py. Severities are normalized
    to upper-case ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW').
    """
    out = {}
    try:
        rows = con.execute(
            "SELECT category, issue_type, default_severity FROM email_controls "
            "WHERE default_severity IS NOT NULL AND TRIM(default_severity) != ''"
        ).fetchall()
        for category, itype, sev in rows:
            if not (category and itype):
                continue
            s = (sev or '').strip().upper()
            if s in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
                out[(category, itype)] = s
    except sqlite3.OperationalError:
        pass
    return out


def get_multi_location_behaviors(con):
    """Return dict mapping (category, issue_type) -> 'dedup' | 'per_section'.

    Reads email_controls.multi_location_behavior (added 2026-04-17 as part of
    the Taxonomy Flatten). Rows with NULL or unrecognized values default to
    'dedup' and are omitted from the returned dict (callers fall back to
    'dedup' for any missing key).
    """
    out = {}
    try:
        rows = con.execute(
            "SELECT category, issue_type, multi_location_behavior FROM email_controls "
            "WHERE multi_location_behavior IS NOT NULL"
        ).fetchall()
        for category, itype, mlb in rows:
            if not (category and itype):
                continue
            v = (mlb or '').strip().lower()
            if v in ('dedup', 'per_section'):
                out[(category, itype)] = v
    except sqlite3.OperationalError:
        pass
    return out


def get_aliases(con):
    """Return dict mapping (alias_area, alias_issue_type) → (canonical_area, canonical_issue)."""
    aliases = {}
    try:
        rows = con.execute(
            "SELECT alias_area, alias_issue_type, canonical_area, canonical_issue "
            "FROM issue_aliases"
        ).fetchall()
        for a_area, a_itype, c_area, c_itype in rows:
            if a_area and a_itype and c_area and c_itype:
                aliases[(a_area, a_itype)] = (c_area, c_itype)
    except sqlite3.OperationalError:
        pass
    return aliases


def get_fire_counts(con):
    """Return dict mapping (category, issue_type) → ticket count from the tickets table."""
    try:
        rows = con.execute(
            "SELECT category, issue_type, COUNT(*) FROM tickets "
            "WHERE category IS NOT NULL AND category != '' "
            "  AND issue_type IS NOT NULL AND issue_type != '' "
            "GROUP BY category, issue_type"
        ).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}
    except sqlite3.OperationalError:
        return {}


def get_reviewed_req_ids(con):
    """Return set of req_ids that already have at least one CLAUDE ticket."""
    rows = con.execute(
        "SELECT DISTINCT req_id FROM tickets WHERE detected_by = 'CLAUDE'"
    ).fetchall()
    return {r[0] for r in rows}


def get_max_ticket_num(con):
    """Return the highest sequential ticket number across both tables.

    Only considers IDs whose numeric suffix is <= 99999 (i.e. normal
    sequential QA-NNNN IDs). Timestamp-style IDs (e.g. from test seeds
    or bugs) are ignored so they can't poison the counter.
    """
    max_num = 0
    for row in con.execute(
        "SELECT ticket_id FROM tickets UNION SELECT ticket_id FROM pending_tickets"
    ):
        m = re.search(r'(\d+)$', row[0] or '')
        if m:
            num = int(m.group(1))
            if num <= 99999:
                max_num = max(max_num, num)
    return max_num


# ── User account helpers (Migration 2026-04-20) ──────────────────────────────
# Admin-managed authentication. Reads are safe via read_copy() or a direct
# sqlite3.connect(); writes MUST go through the read_copy → write_copy pattern.
# page_permissions is stored as a JSON blob on users.page_permissions.

# Gated pages. 'communities' is viewer-default on; the rest default off.
# 'users_admin' is only reachable by users with is_admin=1.
GATED_PAGES = (
    'communities', 'qa_tools', 'controls',
    'ai_check', 'bulk_edit', 'users_admin',
    # Templates Category — Phase 2 (2026-04-21). Gates the "Capture as
    # Template" action on the Tickets page and the forthcoming /templates
    # management page (Phase 3). Default False for viewers; admins bypass
    # via is_admin.
    'templates',
)


def default_viewer_permissions():
    """Return the dict of default per-page toggles for a new non-admin user."""
    perms = {p: False for p in GATED_PAGES}
    perms['communities'] = True
    return perms


def _row_to_user_dict(row):
    """Convert a users row into a plain dict, parsing page_permissions JSON."""
    import json as _json
    if row is None:
        return None
    if hasattr(row, 'keys'):
        d = {k: row[k] for k in row.keys()}
    else:
        d = dict(row)
    raw = d.get('page_permissions')
    if raw:
        try:
            d['page_permissions'] = _json.loads(raw)
        except (ValueError, TypeError):
            d['page_permissions'] = {}
    else:
        d['page_permissions'] = {}
    d['is_admin'] = bool(d.get('is_admin'))
    d['active'] = bool(d.get('active'))
    return d


def get_user_by_email(con, email):
    """Return user dict for the given email (case-insensitive), or None."""
    if not email:
        return None
    row = con.execute(
        "SELECT * FROM users WHERE LOWER(email) = LOWER(?) LIMIT 1",
        (email,),
    ).fetchone()
    return _row_to_user_dict(row)


def get_user_by_id(con, user_id):
    """Return user dict for the given id, or None."""
    if user_id is None:
        return None
    row = con.execute(
        "SELECT * FROM users WHERE id = ? LIMIT 1", (user_id,)
    ).fetchone()
    return _row_to_user_dict(row)


def list_users(con):
    """Return all users as dicts, ordered by id ASC."""
    rows = con.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
    return [_row_to_user_dict(r) for r in rows]


def count_users(con):
    """Return total number of users (used for bootstrap decision)."""
    try:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def create_user(con, *, email, name, password_hash, is_admin=False, permissions=None):
    """Insert a new user. Returns the new user id.

    `permissions` is a dict {page_name: bool}; None → default_viewer_permissions()
    (admin users get full access implicitly via is_admin bypass, but we still
    store an explicit permissions dict so the /users UI renders consistent
    toggles).
    """
    import json as _json
    from datetime import datetime as _dt
    if permissions is None:
        perms = default_viewer_permissions()
        if is_admin:
            perms = {p: True for p in GATED_PAGES}
    else:
        perms = {p: bool(permissions.get(p, False)) for p in GATED_PAGES}
        if is_admin:
            perms = {p: True for p in GATED_PAGES}
    cur = con.execute(
        "INSERT INTO users (email, name, password_hash, is_admin, active, "
        "page_permissions, created_at) VALUES (?,?,?,?,1,?,?)",
        (
            email.strip().lower(),
            (name or '').strip(),
            password_hash,
            1 if is_admin else 0,
            _json.dumps(perms),
            _dt.utcnow().isoformat(timespec='seconds'),
        ),
    )
    return cur.lastrowid


def update_user_permissions(con, user_id, *, is_admin=None, permissions=None):
    """Update is_admin and/or page_permissions for an existing user."""
    import json as _json
    sets, args = [], []
    if is_admin is not None:
        sets.append("is_admin = ?")
        args.append(1 if is_admin else 0)
    if permissions is not None:
        perms = {p: bool(permissions.get(p, False)) for p in GATED_PAGES}
        if is_admin:
            perms = {p: True for p in GATED_PAGES}
        sets.append("page_permissions = ?")
        args.append(_json.dumps(perms))
    if not sets:
        return 0
    args.append(user_id)
    cur = con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", args)
    return cur.rowcount


def set_user_password(con, user_id, password_hash):
    cur = con.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    return cur.rowcount


def set_user_active(con, user_id, active):
    cur = con.execute(
        "UPDATE users SET active = ? WHERE id = ?",
        (1 if active else 0, user_id),
    )
    return cur.rowcount


def touch_last_login(con, user_id):
    from datetime import datetime as _dt
    con.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (_dt.utcnow().isoformat(timespec='seconds'), user_id),
    )


# ── Ticket notes (community-facing, Migration 2026-04-20) ───────────────────

def list_ticket_notes(con, ticket_id):
    """Return notes for a ticket as a list of dicts, oldest first.

    Joins against users to include the author's display name + email so the
    UI doesn't need a second round-trip. Deactivated users still show by name
    for historical attribution.
    """
    rows = con.execute(
        """
        SELECT tn.id, tn.ticket_id, tn.user_id, tn.note_text,
               tn.created_at, tn.updated_at,
               u.name AS user_name, u.email AS user_email
        FROM ticket_notes tn
        LEFT JOIN users u ON u.id = tn.user_id
        WHERE tn.ticket_id = ?
        ORDER BY tn.created_at ASC, tn.id ASC
        """,
        (ticket_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        out.append(d)
    return out


def get_note_counts(con, ticket_ids):
    """Return {ticket_id: note_count} for the given ticket_ids.

    Batch lookup so the community page can render the per-ticket notes badge
    at first paint instead of defaulting to "—" until the notes drawer is
    opened. ticket_ids absent from the result have zero notes.
    """
    ids = [t for t in ticket_ids if t]
    if not ids:
        return {}
    placeholders = ','.join('?' for _ in ids)
    rows = con.execute(
        f"""
        SELECT ticket_id, COUNT(*) AS n
        FROM ticket_notes
        WHERE ticket_id IN ({placeholders})
        GROUP BY ticket_id
        """,
        ids,
    ).fetchall()
    return {r['ticket_id']: r['n'] for r in rows}


def add_ticket_note(con, ticket_id, user_id, note_text):
    """Insert a new note. Returns the new note id."""
    from datetime import datetime as _dt
    now = _dt.utcnow().isoformat(timespec='seconds')
    cur = con.execute(
        "INSERT INTO ticket_notes (ticket_id, user_id, note_text, created_at) "
        "VALUES (?, ?, ?, ?)",
        (ticket_id, user_id, note_text, now),
    )
    return cur.lastrowid


def update_ticket_note(con, note_id, note_text):
    """Admin edit: replace note_text, stamp updated_at. Returns rowcount."""
    from datetime import datetime as _dt
    now = _dt.utcnow().isoformat(timespec='seconds')
    cur = con.execute(
        "UPDATE ticket_notes SET note_text = ?, updated_at = ? WHERE id = ?",
        (note_text, now, note_id),
    )
    return cur.rowcount


def delete_ticket_note(con, note_id):
    """Admin delete. Returns rowcount."""
    cur = con.execute("DELETE FROM ticket_notes WHERE id = ?", (note_id,))
    return cur.rowcount


# ── Audit events (Phase 1, 2026-04-27) ──────────────────────────────────────
# Append-only event log for user-attributed actions on tickets and (Phase 4)
# email_controls. Writers must run inside the same transaction as the state
# change they describe so an event is never written for an action that didn't
# persist. The "Audit Trail Is Append-Only" standing rule in CLAUDE.md is the
# governance layer; these are the implementation primitives.

# Action verbs accepted today. New actions need a CLAUDE.md entry plus a
# corresponding writer call site — don't add freeform strings inline.
AUDIT_TICKET_ACTIONS = frozenset({
    'resolve', 'acknowledge', 'flag_incorrect', 'restore', 'archive',
    'notes_update', 'note_create', 'note_edit', 'note_delete',
    # Synthetic verb written only by archive/one-shot-scripts/
    # backfill_audit_events.py for tickets closed before the audit
    # trail landed (2026-04-27). Never written by request handlers —
    # excluded from the default activity feed; surfaces in per-ticket
    # history as "Action taken before audit started".
    'pre_audit',
})

# Phase 4 (2026-04-27 follow-on) — admin actions on email_controls.
# Same audit_events table, entity_type='email_controls', entity_id is
# encoded as f"{category}||{issue_type}" (delimited string; SQLite has no
# composite-PK foreign keys to lean on, and encoding it once at write
# time keeps queries simple — `WHERE entity_id = 'Content||Spelling and
# Grammar'`). See record_control_action().
AUDIT_CONTROL_ACTIONS = frozenset({
    'edit',     # severity / fix_instruction / show_on_community / etc.
    'rename',   # rename a pair (atomic across email_controls + tickets)
    'mute',     # view-only suppression toggle ON
    'unmute',   # view-only suppression toggle OFF
    'retire',   # move to rejected_issues (with snapshot)
    'revive',   # restore from rejected_issues
    'capture',  # Templates: create a new Templates row + reassign tickets
})

# Status-changing actions that should also bump tickets.last_action_*.
# Note actions are recorded but don't update the badge — the badge is for
# state changes, not conversation activity.
_STATUS_CHANGING_ACTIONS = frozenset({
    'resolve', 'acknowledge', 'flag_incorrect', 'restore', 'archive',
})


def control_entity_id(category, issue_type):
    """Encode an (category, issue_type) pair as a single audit_events.entity_id.

    Centralised so writers and readers agree on the encoding. The `||`
    delimiter is unlikely to appear in legitimate category/issue_type values;
    if it ever does we'll discover it in the matching read query and add
    explicit escaping then.
    """
    return f"{category}||{issue_type}"


def write_event(con, *, entity_type, entity_id, user, action,
                payload=None, bulk_id=None):
    """Append one row to audit_events. Returns the new event id.

    Caller must have an open transaction (or autocommit). Does NOT commit —
    the surrounding handler controls the transaction so the event and the
    state change land or roll back together.

    `user` is the dict returned by qa_dashboard._current_user(); we need
    `id` and `email` from it. `actor_email_snapshot` captures the email at
    write time so the audit row stays self-contained even if the user is
    later deactivated or renamed.

    `payload` is any JSON-serialisable dict (typical shape:
    `{"prior_status": ..., "new_status": ..., "reason": ...}`); stored as
    a JSON string. Pass None to leave NULL.

    `bulk_id` ties events together when one user action produces many
    rows (e.g., bulk-resolve). Generate it once in the handler and pass
    the same value to every per-ticket call.
    """
    import json
    from datetime import datetime as _dt
    if not user or 'id' not in user or 'email' not in user:
        raise ValueError('write_event requires a user dict with id + email')
    payload_str = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    now = _dt.utcnow().isoformat(timespec='seconds')
    cur = con.execute(
        """
        INSERT INTO audit_events
            (entity_type, entity_id, user_id, actor_email_snapshot,
             action, payload_json, bulk_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_type, entity_id, user['id'], user['email'],
         action, payload_str, bulk_id, now),
    )
    return cur.lastrowid


def record_ticket_action(con, *, ticket_id, user, action,
                         payload=None, bulk_id=None):
    """Write an audit event for a ticket and update the denorm cache.

    Convenience wrapper around write_event() for state-changing handlers.
    Updates tickets.last_action_by / last_action_at / last_action so the
    Phase 3 live ticket viewer can render attribution badges without a
    correlated subquery on audit_events. For non-state-changing actions
    (notes), call write_event() directly.

    Returns the new event id.
    """
    if action not in AUDIT_TICKET_ACTIONS:
        raise ValueError(f'unknown audit action: {action!r}')
    event_id = write_event(
        con,
        entity_type='ticket',
        entity_id=ticket_id,
        user=user,
        action=action,
        payload=payload,
        bulk_id=bulk_id,
    )
    if action in _STATUS_CHANGING_ACTIONS:
        from datetime import datetime as _dt
        now = _dt.utcnow().isoformat(timespec='seconds')
        con.execute(
            """
            UPDATE tickets
               SET last_action_by = ?, last_action_at = ?, last_action = ?
             WHERE ticket_id = ?
            """,
            (user['id'], now, action, ticket_id),
        )
    return event_id


def record_control_action(con, *, category, issue_type, user, action,
                          payload=None, bulk_id=None):
    """Write an audit event for an admin action on an email_controls pair.

    Phase 4 (2026-04-27 follow-on). Used by the Controls page handlers
    (edit / mute / unmute / retire / revive / rename) and by the
    Templates page handlers (capture / rename / mute / unmute / retire).

    `entity_id` is encoded via control_entity_id() so the same pair always
    produces the same string. There is no `last_action_*` denorm cache on
    email_controls — admins don't get a "Last edited by X" badge per row
    on the Controls page (low value, high cost). Activity ticker on the
    Users page is the single read surface.
    """
    if action not in AUDIT_CONTROL_ACTIONS:
        raise ValueError(f'unknown control action: {action!r}')
    return write_event(
        con,
        entity_type='email_controls',
        entity_id=control_entity_id(category, issue_type),
        user=user,
        action=action,
        payload=payload,
        bulk_id=bulk_id,
    )


# ── Audit reads (Phase 2, 2026-04-27) ──────────────────────────────────────

def get_users_map(con, user_ids):
    """Return {id: {'name': ..., 'email': ...}} for the given user_ids.

    Used to enrich ticket / event rows with the current display name of the
    actor without doing a JOIN at query time. `users` is small (handful of
    accounts in the pilot), so a single batch lookup is the cheapest pattern.
    Missing or NULL ids are silently skipped — callers fall back to the
    audit row's `actor_email_snapshot` for display.
    """
    ids = sorted({int(u) for u in user_ids if u is not None})
    if not ids:
        return {}
    placeholders = ','.join('?' * len(ids))
    return {
        r['id']: {'name': r['name'], 'email': r['email']}
        for r in con.execute(
            f"SELECT id, name, email FROM users WHERE id IN ({placeholders})",
            ids,
        )
    }


def list_audit_events(con, *, limit=50, offset=0,
                      user_id=None, entity_id=None, entity_type=None,
                      action=None, since=None, bulk_id=None,
                      exclude_pre_audit=False):
    """Return audit_events rows as dicts, newest first, with optional filters.

    Resolves `payload_json` to a Python dict on the way out (`payload`), and
    LEFT JOINs `users` to surface the actor's *current* display name as
    `actor_name`. The immutable `actor_email_snapshot` is the authoritative
    identity — readers should fall back to it when `actor_name` is NULL
    (deleted user).

    Filters are AND-combined and all optional. Pass `since` as an ISO-8601
    string (e.g. `'2026-04-27T00:00:00'`) to get events at or after that time.

    `exclude_pre_audit=True` filters out synthetic `pre_audit` events
    written by the 2026-04-27 backfill script. Used by the cross-user
    activity feed (so the ticker doesn't drown in historical placeholders).
    Per-ticket history queries leave it False so the drawer can show
    "Action taken before audit started" rows naturally.
    """
    import json as _json
    where = []
    params = []
    if user_id is not None:
        where.append('ae.user_id = ?'); params.append(user_id)
    if entity_id is not None:
        where.append('ae.entity_id = ?'); params.append(entity_id)
    if entity_type is not None:
        where.append('ae.entity_type = ?'); params.append(entity_type)
    if action is not None:
        where.append('ae.action = ?'); params.append(action)
    if since is not None:
        where.append('ae.created_at >= ?'); params.append(since)
    if bulk_id is not None:
        where.append('ae.bulk_id = ?'); params.append(bulk_id)
    if exclude_pre_audit:
        where.append("ae.action != 'pre_audit'")
    sql = """
        SELECT ae.id, ae.entity_type, ae.entity_id, ae.user_id,
               ae.actor_email_snapshot, ae.action, ae.payload_json,
               ae.bulk_id, ae.created_at,
               u.name AS actor_name
        FROM audit_events ae
        LEFT JOIN users u ON u.id = ae.user_id
    """
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY ae.created_at DESC, ae.id DESC LIMIT ? OFFSET ?'
    params.extend([int(limit), int(offset)])

    out = []
    for r in con.execute(sql, params):
        d = {k: r[k] for k in r.keys()}
        raw = d.pop('payload_json', None)
        if raw:
            try:
                d['payload'] = _json.loads(raw)
            except (ValueError, TypeError):
                d['payload'] = None
        else:
            d['payload'] = None
        out.append(d)
    return out


def list_ticket_history(con, ticket_id, *, limit=500):
    """Convenience wrapper: full event timeline for one ticket, oldest first.

    Per-ticket history is bounded (a single ticket rarely accumulates more
    than a handful of events), so we return up to 500 with no pagination
    and reverse to chronological order for the UI's timeline view.
    """
    events = list_audit_events(
        con, entity_type='ticket', entity_id=ticket_id, limit=limit,
    )
    # list_audit_events returns newest-first; reverse for a left-to-right
    # timeline read.
    events.reverse()
    return events


def user_has_page_access(user, page_name):
    """Return True if the given user dict can access page_name.

    Admins always get True. Inactive users always get False. Unknown page
    names default to False to fail closed.
    """
    if not user or not user.get('active'):
        return False
    if user.get('is_admin'):
        return True
    perms = user.get('page_permissions') or {}
    return bool(perms.get(page_name, False))


# ── Write queries ─────────────────────────────────────────────────────────────

def insert_live_tickets(con, rows):
    """Insert ticket rows into the live tickets table.

    Each row: (ticket_id, date_flagged, req_id, job_title, community,
    severity, category, issue_type, issue_summary, offending_text,
    detected_by, status, notes, job_url[, section[, captured_from]])

    Accepts:
      - 13-column (legacy, no job_url),
      - 14-column (with job_url),
      - 15-column (with job_url + section) — post-flatten format,
      - 16-column (with job_url + section + captured_from) — post-Templates
        (2026-04-21).
    Shorter tuples get padded with empty values so the INSERT always
    targets 16 columns.
    """
    if not rows:
        return 0
    normalised = []
    for r in rows:
        r = tuple(r)
        if len(r) == 13:
            r = r + ('', None, None)     # add job_url + section + captured_from
        elif len(r) == 14:
            r = r + (None, None)          # add section + captured_from
        elif len(r) == 15:
            r = r + (None,)                # add captured_from (post-Templates 2026-04-21)
        normalised.append(r)
    con.executemany("""
        INSERT OR IGNORE INTO tickets
          (ticket_id, date_flagged, req_id, job_title, community, severity,
           category, issue_type, issue_summary, offending_text, detected_by,
           status, notes, job_url, section, captured_from)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, normalised)
    return len(normalised)


def insert_pending_tickets(con, rows):
    """Insert 15-column rows into pending_tickets.

    Each row: (ticket_id, date_flagged, req_id, job_title, community,
    severity, category, issue_type, issue_summary, offending_text,
    detected_by, status, notes, closest_area, closest_issue_type)
    """
    if not rows:
        return 0
    con.executemany("""
        INSERT OR IGNORE INTO pending_tickets
          (ticket_id, date_flagged, req_id, job_title, community, severity,
           category, issue_type, issue_summary, offending_text, detected_by,
           status, notes, closest_area, closest_issue_type)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    return len(rows)


def capture_template(con, pattern, template_name, ticket_ids,
                     created_by_user_id=None):
    """Capture a rollup of tickets as a Templates-Category row.

    Creates a new ``email_controls`` row with ``category='Templates'`` and
    ``issue_type=template_name``, then rewrites every eligible ticket in
    ``ticket_ids`` to that pair, preserving each ticket's original pair in
    ``captured_from`` for audit.

    Severity inheritance: HIGHEST-wins per the maintainer's 2026-04-21 decision. If
    any eligible ticket is CRITICAL, the Templates row's ``default_severity``
    locks at CRITICAL. Ranking: CRITICAL > HIGH > MEDIUM > LOW.

    The new row is created ``muted=1`` by default (capture suppresses admin
    view of the pattern while the central fix is in flight). Community
    pages keep showing the tickets via the mute-rule Templates carve-out
    in ``/api/community/<name>/tickets``.

    Eligibility: only tickets in "active" status are rewritten. Tickets
    with status in ``('Resolved','Flagged Incorrectly','Archived','Acknowledged')``
    are left untouched \u2014 rewriting a resolved ticket's Category would muddy
    historical audit for no gain. The email_controls row is still created
    even if 0 eligible tickets are found (forward-looking capture; new
    findings that match the pattern will route here on future runs).

    Args:
      con: active sqlite connection inside a read_copy/write_copy transaction
      pattern: the exact offending_text the template matches (stored raw;
               normalization happens at read/match time)
      template_name: human-readable name; becomes the ``issue_type``. Must
               not collide with an existing Templates row.
      ticket_ids: list of ticket_id strings to rewrite
      created_by_user_id: optional user.id for audit in the notes field

    Returns:
      (pair, rewritten_count, inherited_severity) where pair is
      ``('Templates', template_name)``.

    Raises:
      ValueError: validation failure \u2014 empty pattern/name, name collision,
        or empty ticket_ids. Caller should translate to a 400 response.

    See CLAUDE.md "Templates Category" standing rule (template-pattern routing).
    """
    pattern = (pattern or '').strip()
    template_name = (template_name or '').strip()
    if not pattern:
        raise ValueError('pattern is empty')
    if not template_name:
        raise ValueError('template_name is empty')
    if not ticket_ids:
        raise ValueError('no ticket_ids provided')

    # Collision check \u2014 one (Templates, name) pair at a time.
    existing = con.execute(
        'SELECT 1 FROM email_controls WHERE category = ? AND issue_type = ? LIMIT 1',
        ('Templates', template_name),
    ).fetchone()
    if existing:
        raise ValueError(
            f'A Templates pair named "{template_name}" already exists'
        )

    # Fetch the eligible tickets \u2014 exclude terminal statuses so we don't
    # rewrite already-resolved history. The exclude list matches CLAUDE.md's
    # canonical community-facing exclude list.
    placeholders = ','.join('?' for _ in ticket_ids)
    excluded_statuses = ('Resolved', 'Flagged Incorrectly',
                         'Archived', 'Acknowledged')
    exc_placeholders = ','.join('?' for _ in excluded_statuses)
    rows = con.execute(
        f"""SELECT ticket_id, category, issue_type, severity
              FROM tickets
             WHERE ticket_id IN ({placeholders})
               AND status NOT IN ({exc_placeholders})""",
        list(ticket_ids) + list(excluded_statuses),
    ).fetchall()

    # Highest-wins severity across eligible rows.
    sev_rank = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}
    best_sev = 'MEDIUM'
    best_rank = 0
    for r in rows:
        try:
            s = (r['severity'] or '').upper()
        except (TypeError, IndexError, KeyError):
            s = (r[3] or '').upper()
        rk = sev_rank.get(s, 0)
        if rk > best_rank:
            best_rank = rk
            best_sev = s

    now = datetime.now().isoformat(timespec='seconds')
    note_parts = [f'Captured {len(rows)} ticket(s) on {now[:10]}']
    if created_by_user_id:
        note_parts.append(f'by user #{created_by_user_id}')
    notes = ' '.join(note_parts)

    # INSERT the email_controls row. template_pattern holds the raw pattern
    # verbatim; both get_template_patterns() and match_template() normalize
    # at read/match time, so storing raw preserves fidelity for display on
    # the Phase 3 Templates management page.
    con.execute("""
        INSERT INTO email_controls
          (category, issue_type, email_setting, show_on_community,
           default_severity, fix_instruction, notes, scope,
           muted, muted_at, muted_reason,
           multi_location_behavior, template_pattern)
        VALUES (?, ?, 'Include in emails', 1,
                ?, '', ?, 'ALL',
                1, ?, 'Captured from rollup',
                'dedup', ?)
    """, (
        'Templates', template_name, best_sev, notes, now, pattern,
    ))

    # Rewrite each eligible ticket to the new pair.
    rewritten = 0
    for r in rows:
        try:
            tid       = r['ticket_id']
            old_area  = r['category']
            old_itype = r['issue_type']
        except (TypeError, IndexError, KeyError):
            tid, old_area, old_itype = r[0], r[1], r[2]
        con.execute(
            """UPDATE tickets
                  SET category = ?, issue_type = ?, captured_from = ?
                WHERE ticket_id = ?""",
            ('Templates', template_name,
             f'{old_area} / {old_itype}', tid),
        )
        rewritten += 1

    return (('Templates', template_name), rewritten, best_sev)


def get_template_patterns(con):
    """Return ``{normalized_pattern: (category, issue_type)}`` for every
    email_controls row whose ``template_pattern`` is non-null and non-empty.

    Used by the routing layer to match incoming AUTO and CLAUDE findings
    against captured templates. Built once per run; handed to both
    ``run_ai_review.route_and_write`` / ``write_prescan_tickets`` and
    ``fetch_jobs.write_db`` as the single source of truth for which
    offending_text strings route to Templates.

    Added 2026-04-21. Returns an empty dict if no templates are captured
    yet (the normal state during Phase 1, before the capture UI ships),
    or if the column is missing on a very old schema.

    See CLAUDE.md "Templates Category" standing rule (template-pattern routing).
    """
    from taxonomy import normalize_template_pattern
    out = {}
    try:
        rows = con.execute("""
            SELECT category, issue_type, template_pattern
              FROM email_controls
             WHERE template_pattern IS NOT NULL AND template_pattern != ''
        """).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            category  = r['category']
            itype = r['issue_type']
            pat   = r['template_pattern']
        except (TypeError, IndexError, KeyError):
            category, itype, pat = r[0], r[1], r[2]
        key = normalize_template_pattern(pat)
        if key and category and itype:
            out[key] = (category, itype)
    return out


# ── Templates management helpers (Phase 3, 2026-04-21) ──────────────────────
# Backing the dedicated /templates page. See CLAUDE.md "Templates Category"
# standing rule (template-pattern routing).


def list_captured_templates(con):
    """Return one dict per captured template row in email_controls.

    Each dict carries everything the /templates page needs to render a
    template card: display fields (name, pattern, severity, fix_instruction),
    lifecycle state (muted), audit metadata (captured_at, notes, age_days),
    and ticket counts broken down by state (open / resolved / total).

    Ordered by captured_at (muted_at is stamped at capture time) descending
    so the most recent capture surfaces first. Callers can re-sort client-
    side if they want a different order.
    """
    try:
        rows = con.execute("""
            SELECT ec.issue_type       AS name,
                   ec.template_pattern AS pattern,
                   ec.default_severity AS severity,
                   ec.muted            AS muted,
                   ec.muted_at         AS muted_at,
                   ec.muted_reason     AS muted_reason,
                   ec.fix_instruction  AS fix_instruction,
                   ec.notes            AS notes,
                   ec.email_setting    AS email_setting,
                   ec.show_on_community AS show_on_community,
                   (SELECT COUNT(*) FROM tickets t
                     WHERE t.category = 'Templates' AND t.issue_type = ec.issue_type
                       AND t.status = 'Open') AS open_count,
                   (SELECT COUNT(*) FROM tickets t
                     WHERE t.category = 'Templates' AND t.issue_type = ec.issue_type
                       AND t.status = 'Resolved') AS resolved_count,
                   (SELECT COUNT(*) FROM tickets t
                     WHERE t.category = 'Templates' AND t.issue_type = ec.issue_type) AS total_count
              FROM email_controls ec
             WHERE ec.category = 'Templates'
             ORDER BY ec.muted_at DESC NULLS LAST, ec.issue_type ASC
        """).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    today = datetime.now().date()
    for r in rows:
        d = dict(r) if hasattr(r, 'keys') else {}
        if not d:
            continue
        # Compute age_days from muted_at (= captured_at by our capture flow).
        # Falls back to None if the timestamp is missing or malformed.
        age_days = None
        ts = d.get('muted_at') or ''
        if ts:
            try:
                captured_at = datetime.fromisoformat(ts).date()
                age_days = (today - captured_at).days
            except ValueError:
                age_days = None
        d['age_days'] = age_days
        d['muted']    = bool(d.get('muted'))
        out.append(d)
    return out


def list_template_tickets(con, template_name):
    """Return a minimal dict per ticket linked to the given Templates pair.

    Used for the inline-expansion ticket list on a template card. Includes
    tickets of all statuses so admins can see historical + current context,
    sorted newest-first within each status grouping (Open first).
    """
    try:
        rows = con.execute("""
            SELECT ticket_id, community, job_title, req_id, date_flagged,
                   severity, status, captured_from, offending_text,
                   issue_summary, job_url
              FROM tickets
             WHERE category = 'Templates' AND issue_type = ?
             ORDER BY
               CASE status
                 WHEN 'Open' THEN 1
                 WHEN 'Acknowledged' THEN 2
                 WHEN 'Resolved' THEN 3
                 WHEN 'Flagged Incorrectly' THEN 4
                 WHEN 'Archived' THEN 5
                 ELSE 9
               END,
               ticket_id DESC
        """, (template_name,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def rename_template(con, old_name, new_name):
    """Atomically rename a Templates-Category row (issue_type) across
    ``email_controls`` + ``tickets``.

    Validates that ``new_name`` is non-empty and doesn't collide with an
    existing ``(Templates, <new_name>)`` row. On success, updates the
    email_controls row and every ticket whose ``(category, issue_type) =
    ('Templates', old_name)``. Caller manages the surrounding transaction.

    Raises ValueError on:
      - empty new_name
      - same-name no-op (old == new)
      - collision with an existing Templates row
      - source template not found

    Returns (updated_ec_rows, updated_ticket_rows). The email_controls count
    is 0 or 1; the ticket count can be 0+.
    """
    old_name = (old_name or '').strip()
    new_name = (new_name or '').strip()
    if not new_name:
        raise ValueError('new_name is empty')
    if not old_name:
        raise ValueError('old_name is empty')
    if old_name == new_name:
        raise ValueError('new_name is the same as the existing name')

    # Collision check first — a (Templates, new_name) already existing would
    # cause a PK violation on UPDATE and leave the transaction in a bad state.
    collision = con.execute(
        'SELECT 1 FROM email_controls WHERE category = ? AND issue_type = ? LIMIT 1',
        ('Templates', new_name),
    ).fetchone()
    if collision:
        raise ValueError(
            f'A Templates pair named "{new_name}" already exists'
        )

    # Source existence check — fail loudly rather than silently no-op.
    source = con.execute(
        'SELECT 1 FROM email_controls WHERE category = ? AND issue_type = ? LIMIT 1',
        ('Templates', old_name),
    ).fetchone()
    if not source:
        raise ValueError(
            f'No Templates pair named "{old_name}" found'
        )

    ec_cur = con.execute(
        """UPDATE email_controls
              SET issue_type = ?
            WHERE category = ? AND issue_type = ?""",
        (new_name, 'Templates', old_name),
    )
    t_cur = con.execute(
        """UPDATE tickets
              SET issue_type = ?
            WHERE category = ? AND issue_type = ?""",
        (new_name, 'Templates', old_name),
    )
    return ec_cur.rowcount, t_cur.rowcount


def retire_template(con, name, retire_tickets=False, notes=''):
    """Full retirement of a Templates-Category pair.

    Mirrors the existing Controls-page Retire flow for non-Templates pairs:
      1. Snapshot the current ``email_controls`` row into a JSON blob on
         ``rejected_issues.retired_settings`` (for clean Revive later).
      2. DELETE the ``email_controls`` row.
      3. Tickets linked to the pair are LEFT IN PLACE by default \u2014 their
         ``(category, issue_type)`` still reads ``('Templates', <name>)``, which
         preserves audit history. The pair won't appear in filter dropdowns
         any more (Filter Dropdowns Must Read from email_controls rule).

    When ``retire_tickets=True``, all currently-Open tickets linked to the
    pair are additionally resolved in the same transaction with a
    ``reason = 'Resolved via template retirement'`` tag. Non-Open tickets
    (Resolved / Flagged Incorrectly / Archived / Acknowledged) are left alone.

    Raises ValueError if the template doesn't exist. Caller manages the
    surrounding transaction.

    Returns (pair, tickets_resolved) \u2014 ``pair`` is ``('Templates', name)`` and
    ``tickets_resolved`` is the number of Open tickets closed (0 when
    ``retire_tickets=False``).
    """
    import json as _json
    name = (name or '').strip()
    if not name:
        raise ValueError('name is empty')

    ec_row = con.execute(
        "SELECT * FROM email_controls WHERE category = ? AND issue_type = ?",
        ('Templates', name),
    ).fetchone()
    if not ec_row:
        raise ValueError(f'No Templates pair named "{name}" found')

    try:
        settings = {k: ec_row[k] for k in ec_row.keys()}
    except (TypeError, AttributeError):
        settings = dict(ec_row) if hasattr(ec_row, '__iter__') else {}
    settings_json = _json.dumps(settings, default=str)

    now = datetime.now().isoformat(timespec='seconds')
    retire_note = notes or f'Retired from /templates page on {now[:10]}'

    # Upsert into rejected_issues (INSERT OR REPLACE — a re-retire with the
    # same name should refresh the tombstone, not fail).
    con.execute("""
        INSERT OR REPLACE INTO rejected_issues
          (category, issue_type, rejected_at, notes, retired_settings)
        VALUES (?, ?, ?, ?, ?)
    """, ('Templates', name, now, retire_note, settings_json))

    # DELETE the live row. Tickets stay on the ('Templates', name) pair.
    con.execute(
        "DELETE FROM email_controls WHERE category = ? AND issue_type = ?",
        ('Templates', name),
    )

    tickets_resolved = 0
    if retire_tickets:
        cur = con.execute(
            """UPDATE tickets
                  SET status = 'Resolved',
                      reason = 'Resolved via template retirement'
                WHERE category = ? AND issue_type = ? AND status = 'Open'""",
            ('Templates', name),
        )
        tickets_resolved = cur.rowcount or 0

    return (('Templates', name), tickets_resolved)


def resolve_template_tickets(con, name):
    """Bulk-resolve every currently-Open ticket linked to the given Templates
    pair. Non-Open tickets (Resolved / Flagged Incorrectly / Archived /
    Acknowledged) are left untouched. Returns the number of tickets resolved.

    Used by the /templates page's "Resolve Current Tickets" action to clear
    the admin's current open queue for a template without retiring the
    template itself. The template row in email_controls stays as-is; future
    matching findings continue to auto-route here.

    Caller manages the surrounding transaction.
    """
    name = (name or '').strip()
    if not name:
        return 0
    cur = con.execute(
        """UPDATE tickets
              SET status = 'Resolved',
                  reason = 'Resolved via template bulk-resolve'
            WHERE category = ? AND issue_type = ? AND status = 'Open'""",
        ('Templates', name),
    )
    return cur.rowcount or 0


def auto_register_pair(con, category, issue_type):
    """Insert a grammar-class (category, issue_type) pair into email_controls
    with sensible defaults. Uses INSERT OR IGNORE so it's safe to call
    repeatedly.

    Grammar-class coercion (2026-04-21): if the issue_type matches the
    auto-approve keyword set (grammar / spelling / typo / punctuation /
    syntax / capitalization), it is forced to the single canonical pair
    ``('Content', 'Spelling and Grammar')`` before insertion. Without this,
    AI drift produced duplicate orphan rows like ``Content / Spelling Error``,
    ``Content / Grammar Error``, ``Formatting / Grammar`` — each a different
    wording of the same check. The canonical pair already exists in
    ``email_controls`` with ``default_severity = 'HIGH'``, so this short-
    circuits to a no-op INSERT OR IGNORE on the existing row.

    Consolidated-type coercion still runs for non-grammar classes — e.g., if
    the AI puts 'Spelling and Grammar' under 'Qualifications', it gets
    re-homed to 'Content' the same way.
    """
    if not (category and issue_type):
        return
    # Import here to avoid circular dependency at module level
    from taxonomy import coerce_consolidated_type, is_auto_approve
    if is_auto_approve(issue_type, category):
        # Grammar-class drift guard: ANY wording of grammar / spelling / typo /
        # punctuation / syntax / capitalization routes to the single canonical
        # pair. Keeps email_controls clean no matter how the AI phrases it.
        category, issue_type = 'Content', 'Spelling and Grammar'
    else:
        category, issue_type = coerce_consolidated_type(category, issue_type)
    try:
        con.execute("""
            INSERT OR IGNORE INTO email_controls
              (category, issue_type, email_setting, show_on_community,
               fix_instruction, notes, scope)
            VALUES (?, ?, 'Include in emails', 1, '',
                    'Auto-approved grammar/spelling class', 'ALL')
        """, (category, issue_type))
    except sqlite3.OperationalError:
        pass


# ── Mute / pause helpers (email_controls) ────────────────────────────────────
# Muting is a view-only filter. Muted pairs keep firing and generating
# tickets; they're just hidden from the Live Tickets page and Community
# pages (see the internal issue tracker → "Mute / pause toggle" for the
# original design). Callers manage read_copy/write_copy transactions
# around set_muted/unmute.

def is_muted(con, category, issue_type):
    """Return True if the (category, issue_type) pair is currently muted.

    Returns False for pairs that don't exist in email_controls (a pair must
    exist and have muted=1 to be considered muted).
    """
    if not (category and issue_type):
        return False
    row = con.execute(
        'SELECT muted FROM email_controls WHERE category = ? AND issue_type = ?',
        (category, issue_type),
    ).fetchone()
    if row is None:
        return False
    # row_factory may or may not be set — support both tuple and Row
    try:
        return bool(row['muted'])
    except (TypeError, IndexError, KeyError):
        return bool(row[0])


def muted_pairs(con):
    """Return set of (category, issue_type) pairs where muted=1.

    Intended for fast filtering in ticket queries: load once per request,
    test each ticket row's (category, issue_type) against the set.
    """
    try:
        rows = con.execute(
            'SELECT category, issue_type FROM email_controls WHERE muted = 1'
        ).fetchall()
        return {(r[0], r[1]) for r in rows if r[0] and r[1]}
    except sqlite3.OperationalError:
        # Column may not exist on a very old schema — treat as empty set
        return set()


def set_muted(con, category, issue_type, reason=None):
    """Mark a pair as muted. Stamps muted_at = now and records the reason.

    Only updates existing rows — will NOT create a phantom email_controls
    row for an unknown pair. Returns the number of rows affected (0 or 1).

    Caller is responsible for the surrounding transaction and write_copy.
    """
    if not (category and issue_type):
        return 0
    now = datetime.now().isoformat(timespec='seconds')
    cur = con.execute(
        """UPDATE email_controls
              SET muted = 1, muted_at = ?, muted_reason = ?
            WHERE category = ? AND issue_type = ?""",
        (now, (reason or None), category, issue_type),
    )
    return cur.rowcount


def unmute(con, category, issue_type):
    """Clear the muted flag on a pair. Stamps muted_at = now and clears
    muted_reason (the reason describes why something *is* muted, so it
    doesn't survive un-muting).

    Returns the number of rows affected (0 or 1). Caller is responsible
    for the surrounding transaction and write_copy.
    """
    if not (category and issue_type):
        return 0
    now = datetime.now().isoformat(timespec='seconds')
    cur = con.execute(
        """UPDATE email_controls
              SET muted = 0, muted_at = ?, muted_reason = NULL
            WHERE category = ? AND issue_type = ?""",
        (now, category, issue_type),
    )
    return cur.rowcount


# ── cleanup_tickets.py queries ───────────────────────────────────────────────

def get_exclamation_tickets(con, table):
    """Return rows from *table* whose issue_type contains 'exclamation'."""
    return con.execute(f"""
        SELECT ticket_id, category, issue_type
        FROM {table}
        WHERE LOWER(COALESCE(issue_type,'')) LIKE '%exclamation%'
    """).fetchall()


def update_ticket_area_itype(con, table, ticket_id, category, issue_type):
    """Set category and issue_type on a single ticket row."""
    con.execute(f"""
        UPDATE {table} SET category = ?, issue_type = ?
        WHERE ticket_id = ?
    """, (category, issue_type, ticket_id))


def get_exclamation_dupes(con, table, category, issue_type):
    """Return (req_id, keep_id, count) groups where the canonical exclamation
    pair appears more than once per req_id."""
    return con.execute(f"""
        SELECT req_id, MIN(ticket_id) AS keep_id, COUNT(*) AS n
          FROM {table}
         WHERE category = ? AND issue_type = ?
      GROUP BY req_id
        HAVING n > 1
    """, (category, issue_type)).fetchall()


def delete_exclamation_dupes(con, table, category, issue_type, req_id, keep_id):
    """Delete duplicate exclamation tickets for a req_id, keeping *keep_id*."""
    cur = con.execute(f"""
        DELETE FROM {table}
         WHERE category = ? AND issue_type = ?
           AND req_id = ?
           AND ticket_id <> ?
    """, (category, issue_type, req_id, keep_id))
    return cur.rowcount


def get_open_exclamation_tickets(con, category, issue_type):
    """Return open live exclamation tickets (not Closed/Resolved)."""
    return con.execute("""
        SELECT ticket_id, offending_text, issue_summary, status
          FROM tickets
         WHERE category = ? AND issue_type = ?
           AND COALESCE(status,'') NOT LIKE 'Closed%'
           AND COALESCE(status,'') NOT LIKE 'Resolved%'
    """, (category, issue_type)).fetchall()


def close_ticket_rule_updated(con, ticket_id, note_suffix):
    """Close a live ticket as 'Closed \u2014 rule updated' with appended note."""
    con.execute("""
        UPDATE tickets
           SET status = 'Closed \u2014 rule updated',
               notes  = COALESCE(NULLIF(notes,''),'') ||
                        CASE WHEN COALESCE(notes,'') = '' THEN '' ELSE ' | ' END ||
                        ?
         WHERE ticket_id = ?
    """, (note_suffix, ticket_id))


def get_pending_exclamation_tickets(con, category, issue_type):
    """Return all pending exclamation tickets."""
    return con.execute("""
        SELECT ticket_id, offending_text, issue_summary
          FROM pending_tickets
         WHERE category = ? AND issue_type = ?
    """, (category, issue_type)).fetchall()


def delete_pending_ticket(con, ticket_id):
    """Delete a single pending ticket by ID."""
    con.execute("DELETE FROM pending_tickets WHERE ticket_id = ?",
                (ticket_id,))


def get_all_pending_tickets(con):
    """Return all rows from pending_tickets."""
    return con.execute("SELECT * FROM pending_tickets").fetchall()


def get_email_controls_pairs(con):
    """Return set of (category, issue_type) from email_controls where both are non-null."""
    return {
        (r['category'], r['issue_type']) for r in con.execute(
            "SELECT category, issue_type FROM email_controls "
            "WHERE category IS NOT NULL AND issue_type IS NOT NULL"
        ).fetchall()
    }


def register_email_control(con, category, issue_type, notes=''):
    """Insert an email_controls row with sensible defaults (INSERT OR IGNORE)."""
    try:
        con.execute("""
            INSERT OR IGNORE INTO email_controls
              (category, issue_type, email_setting, show_on_community, notes)
            VALUES (?, ?, 'Include in emails', 1, ?)
        """, (category, issue_type, notes))
    except sqlite3.OperationalError:
        pass


def promote_pending_to_live(con, row):
    """Move a pending ticket into the live tickets table and delete the pending row.

    *row* is a sqlite3.Row (or dict-like) from pending_tickets.
    """
    # sqlite3.Row supports [] but not .get(); handle both
    def _g(key, default=''):
        try:
            v = row[key]
            return v if v is not None else default
        except (KeyError, IndexError):
            return default

    con.execute("""
        INSERT OR IGNORE INTO tickets
          (ticket_id, date_flagged, req_id, job_title, community, severity,
           category, issue_type, issue_summary, offending_text, detected_by,
           status, notes, job_url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (row['ticket_id'], row['date_flagged'], row['req_id'],
          row['job_title'], row['community'], row['severity'],
          row['category'] or 'Content', row['issue_type'] or 'Unknown',
          row['issue_summary'], row['offending_text'],
          row['detected_by'] or 'CLAUDE',
          row['status'] or 'Open', row['notes'] or '',
          _g('job_url')))
    con.execute("DELETE FROM pending_tickets WHERE ticket_id = ?",
                (row['ticket_id'],))


def get_used_pairs(con):
    """Return set of (category, issue_type) pairs in tickets OR pending_tickets."""
    used = {(r['category'], r['issue_type']) for r in con.execute(
        "SELECT DISTINCT category, issue_type FROM tickets "
        "WHERE category IS NOT NULL AND category <> '' "
        "AND issue_type IS NOT NULL AND issue_type <> ''"
    ).fetchall()}
    used |= {(r['category'], r['issue_type']) for r in con.execute(
        "SELECT DISTINCT category, issue_type FROM pending_tickets "
        "WHERE category IS NOT NULL AND category <> '' "
        "AND issue_type IS NOT NULL AND issue_type <> ''"
    ).fetchall()}
    return used


def get_alias_canonical_targets(con):
    """Return set of (canonical_area, canonical_issue) from issue_aliases."""
    try:
        return {(r['canonical_area'], r['canonical_issue']) for r in con.execute(
            "SELECT DISTINCT canonical_area, canonical_issue FROM issue_aliases "
            "WHERE canonical_area IS NOT NULL AND canonical_area <> '' "
            "AND canonical_issue IS NOT NULL AND canonical_issue <> ''"
        ).fetchall()}
    except sqlite3.OperationalError:
        return set()


def get_all_email_control_pairs(con):
    """Return list of (category, issue_type) from email_controls."""
    return [(r['category'], r['issue_type']) for r in
            con.execute("SELECT category, issue_type FROM email_controls").fetchall()]


def delete_email_control(con, category, issue_type):
    """Delete one email_controls row. Returns rowcount."""
    cur = con.execute(
        "DELETE FROM email_controls WHERE category = ? AND issue_type = ?",
        (category, issue_type))
    return cur.rowcount


# ── fetch_jobs.py queries ────────────────────────────────────────────────────

def load_all_tickets(con):
    """Return all rows from the tickets table as a list of dicts.

    Handles legacy schemas that may lack category/issue_type columns.

    Round-trip invariant (2026-05-21): every column added to the tickets
    schema MUST appear in this dict AND in upsert_ticket()'s INSERT list,
    or it gets silently wiped on every Standard Check via fetch_jobs
    write_db()'s DELETE+INSERT pattern. See CLAUDE.md "tickets round-trip
    invariant" rule. Columns that are nullable / optional should still
    appear here \u2014 emit None when the row's value is NULL.
    """
    cur = con.cursor()
    cur.execute("SELECT * FROM tickets")
    col_names = [d[0] for d in (cur.description or [])]
    tickets = []
    for row in cur.fetchall():
        category = (row['category'] or '') if 'category' in col_names else ''
        issue_type = (row['issue_type'] or '') if 'issue_type' in col_names else ''
        legacy_ct = (row['check_type'] or '') if 'check_type' in col_names else ''
        if not (category and issue_type) and legacy_ct and '\u2014' in legacy_ct:
            left, _, right = legacy_ct.partition('\u2014')
            category = category or left.strip() or 'Content'
            issue_type = issue_type or right.strip() or 'Unknown'
        check = f'{category} \u2014 {issue_type}' if category and issue_type else legacy_ct
        job_url = ''
        if 'job_url' in col_names:
            job_url = row['job_url'] or ''
        section = row['section'] if 'section' in col_names else None
        # Round-trip preservation (added 2026-05-21): user-attributed and
        # audit-cache columns. All optional / nullable; emit None when the
        # column is absent on a legacy schema or the row's value is NULL.
        reason = row['reason'] if 'reason' in col_names else None
        captured_from = row['captured_from'] if 'captured_from' in col_names else None
        last_action_by = row['last_action_by'] if 'last_action_by' in col_names else None
        last_action_at = row['last_action_at'] if 'last_action_at' in col_names else None
        last_action    = row['last_action']    if 'last_action'    in col_names else None
        tickets.append({
            'ticket_id':    row['ticket_id'] or '',
            'date_flagged': row['date_flagged'] or str(date.today()),
            'req_id':       row['req_id'] or '',
            'job_title':    row['job_title'] or '',
            'community':    row['community'] or '',
            'severity':     row['severity'] or 'MEDIUM',
            'check':        check,
            'summary':      row['issue_summary'] or '',
            'offending':    row['offending_text'] or '',
            'detected_by':  row['detected_by'] or 'AUTO',
            'status':       row['status'] or 'Open',
            'notes':        row['notes'] or '',
            'category':         category,
            'issue_type':   issue_type,
            'job_url':      job_url,
            'section':      section,
            'reason':         reason,
            'captured_from':  captured_from,
            'last_action_by': last_action_by,
            'last_action_at': last_action_at,
            'last_action':    last_action,
        })
    return tickets


def load_all_email_controls(con):
    """Return email controls as a dict keyed by composite check string.

    Legacy format used by fetch_jobs.py: keys like 'Area \u2014 IssueType',
    plus suffix keys like '__notes', '__fix_instruction', etc.
    """
    cur = con.cursor()
    cur.execute("SELECT * FROM email_controls")
    col_names = [d[0] for d in (cur.description or [])]
    controls = {}
    for row in cur.fetchall():
        category = row['category'] if 'category' in col_names else ''
        issue_type = row['issue_type'] if 'issue_type' in col_names else ''
        if (not category or not issue_type) and 'check_type' in col_names:
            ct = row['check_type'] or ''
            if '\u2014' in ct:
                left, _, right = ct.partition('\u2014')
                category = category or left.strip()
                issue_type = issue_type or right.strip()
            else:
                category = category or 'Content'
                issue_type = issue_type or ct
        if not (category and issue_type):
            continue
        check = f'{category} \u2014 {issue_type}'
        controls[check] = row['email_setting'] or 'Include in emails'
        if 'notes' in col_names and row['notes']:
            controls[check + '__notes'] = row['notes']
        if 'fix_instruction' in col_names and row['fix_instruction']:
            controls[check + '__fix_instruction'] = row['fix_instruction']
        if 'show_on_community' in col_names and row['show_on_community'] is not None:
            controls[check + '__show_on_community'] = int(row['show_on_community'])
        controls[check + '__area'] = category
        controls[check + '__issue_type'] = issue_type
        if 'default_severity' in col_names and row['default_severity'] is not None:
            controls[check + '__default_severity'] = row['default_severity']
    return controls


def load_pending_and_rejected(con):
    """Return (pending_list, rejected_list) from the DB.

    Each entry is a plain dict. Handles missing tables gracefully.
    """
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    pending = []
    if 'pending_tickets' in tables:
        for row in con.execute("SELECT * FROM pending_tickets").fetchall():
            pending.append(dict(row))

    rejected = []
    if 'rejected_issues' in tables:
        for row in con.execute("SELECT * FROM rejected_issues").fetchall():
            rejected.append(dict(row))
    elif 'rejected_check_types' in tables:
        for row in con.execute("SELECT * FROM rejected_check_types").fetchall():
            rejected.append(dict(row))

    return pending, rejected


def create_fresh_db_schema(con):
    """Create the full tickets + email_controls schema in an empty DB."""
    con.executescript("""
        CREATE TABLE tickets (
            ticket_id TEXT PRIMARY KEY,
            date_flagged TEXT, req_id TEXT, job_title TEXT, community TEXT,
            severity TEXT,
            category TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            issue_summary TEXT,
            offending_text TEXT, detected_by TEXT, status TEXT, notes TEXT,
            scope TEXT NOT NULL DEFAULT 'ALL',
            job_url TEXT,
            captured_from TEXT
        );
        CREATE TABLE email_controls (
            category TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            email_setting TEXT NOT NULL DEFAULT 'Include in emails',
            show_on_community INTEGER NOT NULL DEFAULT 1,
            default_severity TEXT,
            fix_instruction TEXT,
            notes TEXT,
            scope TEXT DEFAULT 'ALL',
            muted INTEGER NOT NULL DEFAULT 0,
            muted_at TEXT,
            muted_reason TEXT,
            template_pattern TEXT,
            PRIMARY KEY (category, issue_type)
        );
        CREATE INDEX idx_req    ON tickets(req_id);
        CREATE INDEX idx_status ON tickets(status);
        CREATE INDEX idx_comm   ON tickets(community);
        CREATE INDEX idx_category_itype ON tickets(category, issue_type);
        CREATE INDEX idx_det    ON tickets(detected_by);
    """)


# =====================================================================
# Ticket write functions (refactored 2026-05-21, Option B of the
# round-trip wipe bug).
#
# Three functions, two real ones + one wrapper:
#
#   update_existing_ticket(con, t)
#     UPDATE only the columns the Standard Check pipeline can legitimately
#     change for an existing ticket: status, notes, category, issue_type,
#     captured_from. Everything else is preserved untouched. Adding a new
#     column to the tickets schema does NOT require updating this function
#     unless the column is something the pipeline actively modifies.
#
#   insert_new_ticket(con, t)
#     INSERT a brand-new ticket row with every known column populated.
#     Used only for genuinely new ticket IDs that didn't previously exist
#     in the DB. Schema additions should be added here so new rows get
#     a non-NULL value when appropriate.
#
#   upsert_ticket(con, t)
#     Defensive wrapper: looks up whether the ticket_id exists and
#     dispatches to update_existing_ticket or insert_new_ticket. Kept
#     for backwards compatibility with any caller that doesn't know
#     which way to go. The hot fetch_jobs.write_db() loop dispatches
#     inline instead, using a single existence-set query, so it doesn't
#     pay the per-ticket lookup cost.
#
# This replaces the previous DELETE+INSERT pattern that required every
# column to appear in upsert_ticket's INSERT list and load_all_tickets's
# dict, or it would be silently wiped on every Standard Check. That
# invariant is gone — see CLAUDE.md "Tickets Schema Round-Trip Invariant"
# (now retired) and the docs/decisions.md "Round-Trip Wipe + Restore"
# narrative for the history.
# =====================================================================

#: Columns the Standard Check pipeline can modify on an existing ticket.
#: merge_tickets() writes status/notes; write_db()'s template-match step
#: writes category/issue_type/captured_from. Every other column stays at its
#: original detection value across all runs. Adding a column to this
#: tuple means "the pipeline now legitimately changes this column" —
#: requires a matching change in update_existing_ticket's UPDATE SET clause.
PIPELINE_MUTABLE_COLUMNS = (
    'status', 'notes', 'category', 'issue_type', 'captured_from',
)


def update_existing_ticket(con, t):
    """UPDATE only the columns the Standard Check pipeline can change.

    Used by fetch_jobs.write_db() for tickets whose ticket_id is already
    in the DB. Every column NOT in PIPELINE_MUTABLE_COLUMNS is left
    untouched — including reason, last_action_*, severity, offending_text,
    section, and any future column we add. This is the "columns survive
    by default" property that retires the round-trip invariant.
    """
    category = t.get('category', '') or 'Content'
    issue_type = t.get('issue_type', '') or (t.get('check', '') or 'Unknown')
    con.execute("""
        UPDATE tickets
           SET status = ?, notes = ?, category = ?, issue_type = ?, captured_from = ?
         WHERE ticket_id = ?
    """, (
        t.get('status', 'Open') or 'Open',
        t.get('notes', '') or '',
        category, issue_type,
        t.get('captured_from') or None,
        t.get('ticket_id', ''),
    ))


def insert_new_ticket(con, t):
    """INSERT a brand-new ticket with every known column populated.

    Used by fetch_jobs.write_db() for tickets whose ticket_id is NOT
    already in the DB — i.e., genuinely new findings from this run.
    Plain INSERT (not INSERT OR REPLACE) so a buggy caller that tries
    to insert an existing ID raises UNIQUE-constraint loudly instead
    of silently wiping the existing row.
    """
    category = t.get('category', '') or 'Content'
    issue_type = t.get('issue_type', '') or (t.get('check', '') or 'Unknown')
    con.execute("""
        INSERT INTO tickets
          (ticket_id, date_flagged, req_id, job_title, community,
           severity, category, issue_type, issue_summary, offending_text,
           detected_by, status, notes, job_url, section, captured_from,
           reason, last_action_by, last_action_at, last_action)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        t.get('ticket_id', ''), t.get('date_flagged', str(date.today())),
        t.get('req_id', ''), t.get('job_title', ''), t.get('community', ''),
        t.get('severity', 'MEDIUM'),
        category, issue_type,
        t.get('summary', ''), t.get('offending', ''),
        t.get('detected_by', 'AUTO'), t.get('status', 'Open'),
        t.get('notes', ''), t.get('job_url', ''),
        t.get('section') or None,
        t.get('captured_from') or None,
        t.get('reason') or None,
        t.get('last_action_by') if t.get('last_action_by') is not None else None,
        t.get('last_action_at') or None,
        t.get('last_action') or None,
    ))


def upsert_ticket(con, t):
    """Defensive dispatch wrapper. Prefer the direct functions in hot loops.

    Kept for backwards compatibility (one existing caller and any future
    caller that doesn't know whether a ticket is new or existing). Does
    its own existence check, which means it's slower than the inline
    dispatch in fetch_jobs.write_db(). Use the direct functions when
    you've already determined new-vs-existing.
    """
    tid = t.get('ticket_id', '')
    row = con.execute('SELECT 1 FROM tickets WHERE ticket_id = ?', (tid,)).fetchone()
    if row:
        update_existing_ticket(con, t)
    else:
        insert_new_ticket(con, t)


def get_distinct_pairs_from_tickets(con):
    """Return set of (category, issue_type) from the tickets table."""
    rows = con.execute(
        "SELECT DISTINCT category, issue_type FROM tickets "
        "WHERE category IS NOT NULL AND category != '' "
        "AND issue_type IS NOT NULL AND issue_type != ''"
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def upsert_email_control_full(con, category, itype, setting, notes, fix_ins,
                               show_on_comm, default_sev):
    """INSERT OR REPLACE a fully-specified email_controls row."""
    con.execute("""
        INSERT OR REPLACE INTO email_controls
          (category, issue_type, email_setting, notes, fix_instruction,
           show_on_community, default_severity, scope)
        VALUES (?,?,?,?,?,?,?,'ALL')
    """, (category, itype, setting, notes, fix_ins, show_on_comm, default_sev))


def restore_pending_rows(con, pending_rows):
    """Re-insert pending_tickets rows (INSERT OR IGNORE). Creates table if needed."""
    ensure_tables(con)
    for row in pending_rows:
        category = row.get('category', '')
        issue_type = row.get('issue_type', '')
        if not (category and issue_type):
            ct = row.get('check_type', '') or ''
            if '\u2014' in ct:
                left, _, right = ct.partition('\u2014')
                category = category or left.strip() or 'Content'
                issue_type = issue_type or right.strip() or 'Unknown'
            else:
                category = category or 'Content'
                issue_type = issue_type or (ct.strip() or 'Unknown')
        con.execute("""
            INSERT OR IGNORE INTO pending_tickets
              (ticket_id, date_flagged, req_id, job_title, community, severity,
               category, issue_type, issue_summary, offending_text, detected_by,
               status, notes, closest_area, closest_issue_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row.get('ticket_id', ''), row.get('date_flagged', ''),
            row.get('req_id', ''), row.get('job_title', ''),
            row.get('community', ''), row.get('severity', ''),
            category, issue_type,
            row.get('issue_summary', ''), row.get('offending_text', ''),
            row.get('detected_by', ''), row.get('status', 'Pending'),
            row.get('notes', ''), row.get('closest_area', ''),
            row.get('closest_issue_type', ''),
        ))


def restore_rejected_rows(con, rejected_rows):
    """Re-insert rejected_issues rows (INSERT OR IGNORE). Creates table if needed."""
    ensure_tables(con)
    for row in rejected_rows:
        category = row.get('category', '')
        issue_type = row.get('issue_type', '')
        if not (category and issue_type):
            ct = row.get('check_type', '')
            if ct and '\u2014' in ct:
                parts = ct.split('\u2014', 1)
                category = parts[0].strip()
                issue_type = parts[1].strip()
        if category and issue_type:
            con.execute("""
                INSERT OR IGNORE INTO rejected_issues
                  (category, issue_type, rejected_at, notes)
                VALUES (?,?,?,?)
            """, (category, issue_type, row.get('rejected_at', ''),
                  row.get('notes', '')))
