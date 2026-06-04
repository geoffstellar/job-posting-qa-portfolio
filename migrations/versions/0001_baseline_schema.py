"""baseline schema

Single baseline capturing the full QA dashboard schema (tickets,
email_controls, users, audit trail, etc.). The portfolio repo collapses the
original multi-step migration history into this one revision; the DDL below is
generated from the app's own schema builders (db.create_fresh_db_schema +
db.ensure_tables) so it matches the runtime schema exactly.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-04
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# CREATE statements captured from the app schema builders (tables first, then
# indexes). Each is executed in order inside Alembic's migration transaction.
_DDL = [
    "CREATE TABLE audit_events (\n            id                   INTEGER PRIMARY KEY AUTOINCREMENT,\n            entity_type          TEXT NOT NULL,\n            entity_id            TEXT NOT NULL,\n            user_id              INTEGER NOT NULL,\n            actor_email_snapshot TEXT NOT NULL,\n            action               TEXT NOT NULL,\n            payload_json         TEXT,\n            bulk_id              TEXT,\n            created_at           TEXT NOT NULL\n        )",
    "CREATE TABLE custom_areas (\n            category        TEXT PRIMARY KEY,\n            created_at  TEXT,\n            notes       TEXT\n        )",
    "CREATE TABLE discovery_log (\n            id                  INTEGER PRIMARY KEY AUTOINCREMENT,\n            run_date            TEXT NOT NULL,\n            suggested_area      TEXT NOT NULL,\n            suggested_issue_type TEXT NOT NULL,\n            suggested_severity  TEXT,\n            suggested_ownership TEXT,\n            example_text        TEXT,\n            draft_rule          TEXT,\n            decision            TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),\n            notes               TEXT,\n            prompt_version      TEXT\n        )",
    "CREATE TABLE email_controls (\n            category TEXT NOT NULL,\n            issue_type TEXT NOT NULL,\n            email_setting TEXT NOT NULL DEFAULT 'Include in emails',\n            show_on_community INTEGER NOT NULL DEFAULT 1,\n            default_severity TEXT,\n            fix_instruction TEXT,\n            notes TEXT,\n            scope TEXT DEFAULT 'ALL',\n            muted INTEGER NOT NULL DEFAULT 0,\n            muted_at TEXT,\n            muted_reason TEXT,\n            template_pattern TEXT, multi_location_behavior TEXT DEFAULT 'dedup',\n            PRIMARY KEY (category, issue_type)\n        )",
    "CREATE TABLE issue_aliases (\n            alias_area          TEXT NOT NULL,\n            alias_issue_type    TEXT NOT NULL,\n            canonical_area      TEXT NOT NULL,\n            canonical_issue     TEXT NOT NULL,\n            created_at          TEXT,\n            notes               TEXT,\n            PRIMARY KEY (alias_area, alias_issue_type)\n        )",
    "CREATE TABLE pending_tickets (\n            ticket_id      TEXT PRIMARY KEY,\n            date_flagged   TEXT,\n            req_id         TEXT,\n            job_title      TEXT,\n            community      TEXT,\n            severity       TEXT,\n            check_type     TEXT,\n            issue_summary  TEXT,\n            offending_text TEXT,\n            detected_by    TEXT,\n            status         TEXT DEFAULT 'Pending',\n            notes          TEXT,\n            category           TEXT,\n            issue_type     TEXT,\n            closest_area       TEXT,\n            closest_issue_type TEXT,\n            scope          TEXT DEFAULT 'ALL'\n        , job_url TEXT)",
    "CREATE TABLE rejected_issues (\n            category            TEXT NOT NULL,\n            issue_type      TEXT NOT NULL,\n            rejected_at     TEXT,\n            notes           TEXT, archived_at TEXT, retired_settings TEXT,\n            PRIMARY KEY (category, issue_type)\n        )",
    "CREATE TABLE ticket_notes (\n            id          INTEGER PRIMARY KEY AUTOINCREMENT,\n            ticket_id   TEXT NOT NULL,\n            user_id     INTEGER NOT NULL,\n            note_text   TEXT NOT NULL,\n            created_at  TEXT NOT NULL,\n            updated_at  TEXT\n        )",
    "CREATE TABLE tickets (\n            ticket_id TEXT PRIMARY KEY,\n            date_flagged TEXT, req_id TEXT, job_title TEXT, community TEXT,\n            severity TEXT,\n            category TEXT NOT NULL,\n            issue_type TEXT NOT NULL,\n            issue_summary TEXT,\n            offending_text TEXT, detected_by TEXT, status TEXT, notes TEXT,\n            scope TEXT NOT NULL DEFAULT 'ALL',\n            job_url TEXT,\n            captured_from TEXT\n        , section TEXT, reason TEXT, last_action_by INTEGER, last_action_at TEXT, last_action TEXT)",
    "CREATE TABLE users (\n            id               INTEGER PRIMARY KEY AUTOINCREMENT,\n            email            TEXT UNIQUE NOT NULL,\n            name             TEXT,\n            password_hash    TEXT NOT NULL,\n            is_admin         INTEGER NOT NULL DEFAULT 0,\n            active           INTEGER NOT NULL DEFAULT 1,\n            page_permissions TEXT,\n            created_at       TEXT,\n            last_login_at    TEXT\n        )",
    "CREATE INDEX idx_audit_bulk\n            ON audit_events(bulk_id)",
    "CREATE INDEX idx_audit_created_at\n            ON audit_events(created_at)",
    "CREATE INDEX idx_audit_entity\n            ON audit_events(entity_type, entity_id, created_at)",
    "CREATE INDEX idx_audit_user_time\n            ON audit_events(user_id, created_at)",
    "CREATE INDEX idx_category_itype ON tickets(category, issue_type)",
    "CREATE INDEX idx_comm   ON tickets(community)",
    "CREATE INDEX idx_det    ON tickets(detected_by)",
    "CREATE INDEX idx_req    ON tickets(req_id)",
    "CREATE INDEX idx_status ON tickets(status)",
    "CREATE INDEX idx_ticket_notes_ticket_id\n            ON ticket_notes(ticket_id)",
]


def upgrade():
    for stmt in _DDL:
        op.execute(stmt)


def downgrade():
    raise NotImplementedError("The baseline migration is not reversible.")
