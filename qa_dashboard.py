"""
qa_dashboard.py -- Cedarline Senior Living QA Tools Dashboard
Run with: python qa_dashboard.py
Then open: http://localhost:5000

Architecture notes (2026-04-16, updated 2026-04-24):
- Controls page is where existing checks are managed:
    * Mute toggle       → view-only filter on live/community pages
    * Retire button     → moves check to rejected_issues (saves retired_settings)
    * Retired Checks    → expandable section at bottom; Revive restores checks
    * Severity / visibility / fix_instruction edits (auto-save)
- New checks are added exclusively via the Claude `qa-rules-maintenance` skill
  (the in-dashboard Add Check wizard + AI-Assisted Discovery mode were removed
  2026-04-24 — see archive/add-check-wizard-2026-04-24/).
- email_controls is a MANAGED TABLE — see CLAUDE.md "email_controls Is a
  Managed Table" standing rule. fetch_jobs.write_db() never touches it.
- pending_tickets is RETIRED — its endpoints return stubs (410 Gone / empty)
  for backward compat. Unknown AI pairs are silently dropped in
  run_ai_review.py.
- discovery_log table is HISTORICAL — read-only as of 2026-04-24; kept for
  reference while building the qa-rules-maintenance skill replacement.
"""
from flask import (
    Flask, Response, stream_with_context, jsonify, request, render_template,
    session, redirect, url_for, flash, g, abort,
)
from functools import wraps
from datetime import timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import subprocess, sys, os, webbrowser, sqlite3, shutil, tempfile, json, uuid

BASE = os.path.dirname(os.path.abspath(__file__))

# Load .env before reading any env vars. python-dotenv respects existing
# env vars (os.environ wins over file entries), matching the hand-rolled
# parser this replaces. Absent .env is fine — env-driven deployments
# (Fly.io, Azure) pass vars directly and never ship a .env file.
load_dotenv(os.path.join(BASE, '.env'))

# Centralised business rules and DB helpers — see taxonomy.py and db.py.
# Imported here so APP_DATA_DIR / DATABASE_URL resolution can use db helpers.
import taxonomy as _tax
import db as _db

# ── Paths + deployment config (env-driven, Phase 3 of containerize+DB) ──────
# Every filesystem path and runtime knob is now read from the environment
# with a sensible default. Locally, nothing needs to be set — defaults keep
# `python qa_dashboard.py` behaving exactly as it did before Phase 3.
# In Docker / Fly.io / Azure, the compose file or app-settings panel
# overrides these.
#
# APP_DATA_DIR  — writable data root. Container:/app/data, local:<repo>.
# DATABASE_URL  — primary DB. Default derived from APP_DATA_DIR (see db.py).
# JOBS_RAW_PATH — Hireology cache JSON. Default: <APP_DATA_DIR>/jobs_raw.json.
# HOST / PORT   — Flask dev-server bind address + port. Container sets these.
# FLASK_ENV     — 'production' toggles secure-cookie flags.

APP_DATA_DIR  = os.environ.get('APP_DATA_DIR') or BASE
JOBS_RAW_PATH = os.environ.get('JOBS_RAW_PATH') or os.path.join(APP_DATA_DIR, 'jobs_raw.json')

# DB filesystem path for the few os.path.exists(DB) guards in this file.
# All actual DB access goes through db.connect_readonly / db.read_copy.
DB = _db._resolve_sqlite_path(None)

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '5000'))
FLASK_ENV = os.environ.get('FLASK_ENV', 'development')
IS_PRODUCTION = FLASK_ENV.lower() == 'production'


app  = Flask(__name__)

# Session secret — MUST be set in production. Falls back to a dev-only key with
# a loud warning so the login flow works out of the box on first checkout.
_secret = os.environ.get('SESSION_SECRET_KEY')
if not _secret:
    _secret = 'dev-only-insecure-session-key-change-me'
    print('[!] SESSION_SECRET_KEY not set — using insecure dev fallback. '
          'Set it in .env before sharing the app.')
app.secret_key = _secret
app.permanent_session_lifetime = timedelta(hours=8)

# Production cookie hardening: when running behind HTTPS (Fly.io / Azure /
# any reverse proxy), force secure + httponly + lax SameSite on the session
# cookie. In dev (FLASK_ENV=development) these stay off so localhost HTTP
# logins work.
if IS_PRODUCTION:
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )
# Hot-reload Jinja templates on change (Werkzeug's file-reloader only restarts
# the process on .py changes; without this flag, edits to files in templates/
# stay cached in memory until a full restart). Added 2026-04-17 after a
# template edit appeared lost because the running server had cached the old
# version.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# ── Job URL lookup (req_id → career_site_url) ──────────────────────────────────
# Cached with mtime check so it auto-refreshes when fetch_jobs.py writes new data.
_job_url_cache = {'mtime': 0, 'map': {}}

def _get_job_url_map():
    """Return {req_id_str: career_site_url} from JOBS_RAW_PATH.

    Re-reads the file only when its mtime changes, so this is cheap to call
    on every request but always reflects the latest fetch_jobs.py run.
    """
    path = JOBS_RAW_PATH
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return _job_url_cache['map']
    if mt != _job_url_cache['mtime']:
        try:
            with open(path) as f:
                jobs = json.JSONDecoder().raw_decode(f.read())[0]
            _job_url_cache['map'] = {
                str(j['id']): j.get('career_site_url', '')
                for j in jobs if j.get('career_site_url')
            }
            _job_url_cache['mtime'] = mt
        except Exception:
            pass
    return _job_url_cache['map']

# Email-generator was removed April 2026. The DB columns
# (email_setting, fix_instruction, email_wording) remain for backward compat
# but nothing in the app reads or writes them any more.
_fix_instruction = None


def _read_db_copy():
    """Copy mount DB to a temp file and return (con, tmp_path).

    Delegates to db.read_copy() — single source of truth for safe DB access.
    Caller must close con and call _write_db_copy() or _discard_db_copy().
    """
    return _db.read_copy(DB)


def _write_db_copy(tmp_path):
    """Write the modified temp DB back into the live DB file.

    Delegates to db.write_copy() — single source of truth.
    Uses atomic-rename strategy (staging file + os.replace) to prevent
    partial-write corruption on WSL/FUSE mounts.
    """
    _db.write_copy(tmp_path, DB)


def _discard_db_copy(tmp_path):
    """Discard a temp DB copy without writing back."""
    _db.cleanup_tmp(tmp_path)


# ── Authentication + per-page access control ────────────────────────────────
# Admin-managed users, Flask sessions, and page-level decorators. See CLAUDE.md
# section on "User Accounts" (added 2026-04-20). Writes always go through the
# read_copy → ensure_tables → write_copy pattern per DB-safety rules.

def _current_user():
    """Return the logged-in user dict for this request, or None.

    Cached on flask.g so repeated calls within one request hit the DB once.
    Returns None if no session, no user, or user is inactive.
    """
    if 'user' in g.__dict__:
        return g.user
    g.user = None
    uid = session.get('user_id')
    if not uid:
        return None
    try:
        con = _db.connect_readonly()
        try:
            user = _db.get_user_by_id(con, uid)
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None
    if not user or not user.get('active'):
        session.clear()
        return None
    g.user = user
    return user


def _wants_json():
    """Best-effort detector for whether to respond with JSON vs HTML redirect."""
    if request.path.startswith('/api/'):
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept


def login_required(fn):
    """Require any authenticated active user."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if not user:
            if _wants_json():
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login', next=request.full_path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """Require is_admin=1."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if not user:
            if _wants_json():
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login', next=request.full_path))
        if not user.get('is_admin'):
            if _wants_json():
                return jsonify({'error': 'Admin access required'}), 403
            flash("That page is admin-only.", 'error')
            return redirect(url_for('communities'))
        return fn(*args, **kwargs)
    return wrapper


def requires_page(page_name):
    """Require access to a specific gated page.

    Admins bypass. Users without access get redirected to /communities with a
    flash message (HTML) or 403 (JSON/API requests).
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _current_user()
            if not user:
                if _wants_json():
                    return jsonify({'error': 'Authentication required'}), 401
                return redirect(url_for('login', next=request.full_path))
            if not _db.user_has_page_access(user, page_name):
                if _wants_json():
                    return jsonify({'error': f'No access to {page_name}'}), 403
                flash(f"You don't have access to that page.", 'error')
                return redirect(url_for('communities'))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.context_processor
def _inject_user_into_templates():
    """Make current_user available to every Jinja template."""
    return {'current_user': _current_user()}


def _bootstrap_admin_if_empty():
    """On first run, seed a single admin user from env vars.

    Reads BOOTSTRAP_ADMIN_EMAIL + BOOTSTRAP_ADMIN_PASSWORD. If either is missing
    AND the users table is empty, prints a warning — the app still runs, but
    login will be impossible until the maintainer adds a user manually.
    """
    if not os.path.exists(DB):
        return
    try:
        con = _db.connect_readonly()
        try:
            _db.ensure_tables(con)
            con.commit()
            n = _db.count_users(con)
        finally:
            con.close()
    except Exception as e:
        print(f'[!] users bootstrap check failed: {e}')
        return
    if n > 0:
        return
    email = os.environ.get('BOOTSTRAP_ADMIN_EMAIL', '').strip()
    pw    = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD', '')
    if not email or not pw:
        print('[!] users table empty and BOOTSTRAP_ADMIN_EMAIL / '
              'BOOTSTRAP_ADMIN_PASSWORD not set — login will be blocked until '
              'an admin is seeded. Set both in .env and restart.')
        return
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        _db.create_user(
            con,
            email=email,
            name='Admin',
            password_hash=generate_password_hash(pw),
            is_admin=True,
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        print(f'[+] bootstrapped admin user: {email}')
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        _discard_db_copy(tmp)
        print(f'[!] bootstrap failed: {e}')


# ── Shared navigation ─────────────────────────────────────────────────────────

NAV_CSS = """
    /* ── Shared nav header ── */
    header {
      background: #1a1a2e;
      color: white;
      padding: 0 32px;
      display: flex;
      align-items: stretch;
      box-shadow: 0 2px 10px rgba(0,0,0,.2);
    }
    .header-brand {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px 0;
      margin-right: 28px;
      flex-shrink: 0;
    }
    .header-brand h1 {
      font-size: 16px;
      font-weight: 700;
      color: white;
      letter-spacing: 0.3px;
      white-space: nowrap;
    }
    .nav-tabs {
      display: flex;
      align-items: stretch;
    }
    .nav-tab {
      display: flex;
      align-items: center;
      padding: 0 20px;
      font-size: 13px;
      font-weight: 600;
      color: rgba(255,255,255,0.55);
      text-decoration: none;
      border-bottom: 3px solid transparent;
      transition: color 0.15s, border-color 0.15s;
    }
    .nav-tab:hover { color: rgba(255,255,255,0.9); }
    .nav-tab.active { color: white; border-bottom-color: #C89B3C; }
    .nav-user {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: 14px;
      padding-left: 16px;
      font-size: 12px;
      color: rgba(255,255,255,0.75);
    }
    .nav-user .nav-user-name { font-weight: 600; color: #fff; }
    .nav-user .nav-user-role {
      font-size: 10px;
      letter-spacing: .5px;
      text-transform: uppercase;
      background: rgba(200,155,60,0.22);
      color: #f0d483;
      padding: 2px 8px;
      border-radius: 10px;
    }
    .nav-user a {
      color: rgba(255,255,255,0.65);
      text-decoration: none;
      font-weight: 600;
    }
    .nav-user a:hover { color: #fff; }
"""

STAR_SVG = """<svg width="34" height="34" viewBox="0 0 40 40" fill="none">
      <circle cx="20" cy="20" r="20" fill="rgba(255,255,255,0.1)"/>
      <polygon points="20,5 23.5,14.5 34,14.5 25.5,20.5 28.5,30 20,24 11.5,30 14.5,20.5 6,14.5 16.5,14.5" fill="#C89B3C"/>
    </svg>"""

def nav_header(active):
    """Return the shared <header> HTML.

    `active` is 'qa', 'tickets', 'communities', or 'users'. Nav links are
    hidden for users without the corresponding
    page permission. If no user is logged in, a minimal header is
    returned so the login page still renders the brand.
    """
    user = _current_user()
    qa_cls       = ' active' if active == 'qa'          else ''
    tk_cls       = ' active' if active == 'tickets'     else ''
    tmpl_cls     = ' active' if active == 'templates'   else ''
    comm_cls     = ' active' if active == 'communities' else ''
    users_cls    = ' active' if active == 'users'       else ''

    if not user:
        return f"""<header>
  <div class="header-brand">
    {STAR_SVG}
    <h1>Cedarline Senior Living</h1>
  </div>
</header>"""

    links = []
    if _db.user_has_page_access(user, 'qa_tools'):
        links.append(f'<a href="/"            class="nav-tab{qa_cls}">QA Tools</a>')
        links.append(f'<a href="/tickets"     class="nav-tab{tk_cls}">Tickets</a>')
    # Templates nav entry (Phase 3, 2026-04-21). Sits between Tickets and
    # Communities per the maintainer's 2026-04-21 workshop preference. Hidden for
    # users without the 'templates' permission; admins get it via is_admin.
    if _db.user_has_page_access(user, 'templates'):
        links.append(f'<a href="/templates"   class="nav-tab{tmpl_cls}">Templates</a>')
    if _db.user_has_page_access(user, 'communities'):
        links.append(f'<a href="/communities" class="nav-tab{comm_cls}">Communities</a>')
    if user.get('is_admin'):
        links.append(f'<a href="/users"       class="nav-tab{users_cls}">Users</a>')

    display_name = (user.get('name') or user.get('email') or '').strip()
    role_badge = '<span class="nav-user-role">Admin</span>' if user.get('is_admin') else ''
    return f"""<header>
  <div class="header-brand">
    {STAR_SVG}
    <h1>Cedarline Senior Living</h1>
  </div>
  <nav class="nav-tabs">
    {''.join(links)}
  </nav>
  <div class="nav-user">
    <span class="nav-user-name">{display_name}</span>
    {role_badge}
    <a href="/logout">Log out</a>
  </div>
</header>"""


_SCHEMA_VERSION = 4   # bump when adding new migrations below
# v3 (2026-04-17): Taxonomy Flatten Phase 2 — added tickets.section,
#                  email_controls.multi_location_behavior. custom_areas.kind
#                  column is no longer referenced by app code (2026-04-20
#                  cleanup, "Add a New Check Flow" ticket); still present in
#                  legacy DBs as an unused column.
# v4 (2026-04-20): User accounts + feedback infra — added users table,
#                  tickets.reason column, ticket_notes table. Delegates to
#                  db.ensure_tables() for the new schema.

def _ensure_pending_table(force=False):
    """Create pending_tickets and rejected_issues tables if they don't exist.

    Skips the expensive copy cycle when schema_version in _qa_meta.json
    already matches _SCHEMA_VERSION (normal startup fast-path).
    Pass force=True after scripts that rebuild the DB from scratch
    (e.g. fetch_jobs.py) so the migration always runs.
    """
    if not os.path.exists(DB):
        return
    if not force:
        meta = _read_meta()
        if meta.get('schema_version') == _SCHEMA_VERSION:
            return
    con, tmp = _read_db_copy()
    try:
        con.executescript("""
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
            /* User-defined Categories beyond the canonical six. Fed into the
               AI prompt on the next run so Claude learns the extended taxonomy.
               The legacy `kind` column (section vs cross-cutting) is no longer
               populated — the flat model has a single Category axis. */
            CREATE TABLE IF NOT EXISTS custom_areas (
                category        TEXT PRIMARY KEY,
                created_at  TEXT,
                notes       TEXT
            );
            /* AI-drift remapping. When Claude uses a different composite for a
               known issue, we record the redirect here so future runs route
               automatically without a Pending card. */
            CREATE TABLE IF NOT EXISTS issue_aliases (
                alias_area          TEXT NOT NULL,
                alias_issue_type    TEXT NOT NULL,
                canonical_area      TEXT NOT NULL,
                canonical_issue     TEXT NOT NULL,
                created_at          TEXT,
                notes               TEXT,
                PRIMARY KEY (alias_area, alias_issue_type)
            );
        """)
        # closest_area + closest_issue_type on pending_tickets — populated by run_ai_review.py
        pt_cols = [r[1] for r in con.execute('PRAGMA table_info(pending_tickets)').fetchall()]
        if 'closest_area' not in pt_cols:
            con.execute('ALTER TABLE pending_tickets ADD COLUMN closest_area TEXT')
        if 'closest_issue_type' not in pt_cols:
            con.execute('ALTER TABLE pending_tickets ADD COLUMN closest_issue_type TEXT')
        if 'scope' not in pt_cols:
            con.execute('ALTER TABLE pending_tickets ADD COLUMN scope TEXT DEFAULT \'ALL\'')

        # Add show_on_community and scope columns if missing
        cols = [r[1] for r in con.execute('PRAGMA table_info(email_controls)').fetchall()]
        if 'show_on_community' not in cols:
            con.execute('ALTER TABLE email_controls ADD COLUMN show_on_community INTEGER NOT NULL DEFAULT 1')
        if 'scope' not in cols:
            con.execute('ALTER TABLE email_controls ADD COLUMN scope TEXT DEFAULT \'ALL\'')

        # ── Rename location → category (UI consistency) ─────────────────────────────
        for tbl in ('tickets', 'email_controls', 'pending_tickets'):
            tbl_cols = [r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()]
            if 'location' in tbl_cols and 'category' not in tbl_cols:
                con.execute(f'ALTER TABLE {tbl} RENAME COLUMN location TO category')

        # ── category + issue_type migration ──────────────────────────────────────
        # Add columns to all three tables, then backfill from check_type.
        for tbl in ('tickets', 'email_controls', 'pending_tickets'):
            try:
                tbl_cols = [r[1] for r in con.execute(f'PRAGMA table_info({tbl})').fetchall()]
                if 'category' not in tbl_cols:
                    con.execute(f'ALTER TABLE {tbl} ADD COLUMN category TEXT')
                if 'issue_type' not in tbl_cols:
                    con.execute(f'ALTER TABLE {tbl} ADD COLUMN issue_type TEXT')
            except Exception:
                pass

        # Note: backfill code removed April 2026. The new schema enforces NOT NULL
        # on category and issue_type; no stale migration code needed.

        # Add default_severity to email_controls if missing
        ec_cols = [r[1] for r in con.execute('PRAGMA table_info(email_controls)').fetchall()]
        if 'default_severity' not in ec_cols:
            con.execute('ALTER TABLE email_controls ADD COLUMN default_severity TEXT')

        # Add archived_at to rejected_issues for soft-delete (April 2026)
        ri_cols = [r[1] for r in con.execute('PRAGMA table_info(rejected_issues)').fetchall()]
        if 'archived_at' not in ri_cols:
            con.execute('ALTER TABLE rejected_issues ADD COLUMN archived_at TEXT')

        # Delegate to db.ensure_tables() for user accounts + reason column +
        # ticket_notes (schema v4, 2026-04-20). ensure_tables does its own
        # commit, so it goes AFTER the local con.commit() below.
        con.commit()
        _db.ensure_tables(con)
    except Exception as e:
        print(f'[!] _ensure_pending_table migration error: {e}')
    finally:
        con.close()
    # Write back outside the try so errors surface instead of being swallowed,
    # and so con is fully closed before backup opens a second handle to tmp.
    try:
        _write_db_copy(tmp)
        _write_meta({'schema_version': _SCHEMA_VERSION})
    except Exception as e:
        print(f'[!] _ensure_pending_table write-back error: {e}')
        try: os.remove(tmp)
        except OSError: pass


# ── Dirty-state helpers ───────────────────────────────────────────────────────

META_FILE = os.path.join(BASE, '_qa_meta.json')

def _read_meta():
    if os.path.exists(META_FILE):
        try:
            with open(META_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _write_meta(data):
    m = _read_meta()
    m.update(data)
    with open(META_FILE, 'w') as f:
        json.dump(m, f)

# _is_dirty() / _count_dirty() removed April 2026 with the email generator.
# The meta file and _write_meta() remain for forward-compat with any other
# telemetry we might add later.


# ── Ticket data helpers ───────────────────────────────────────────────────────

def _parse_offending(text):
    """Offending text is stored as a plain string or JSON dict."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {'text': text}

def _email_wording(check_type, summary, offending_text):
    # Email generator removed April 2026. Stub kept so any stale callers
    # return an empty string instead of raising.
    return ''

def _severity_order(s):
    return _tax.severity_sort_key(s)

# ── Canonical location list — sourced from taxonomy.py ─────────────────────────
# Post-2026-05-21: CANONICAL_LOCATIONS points at the 5 canonical Categories
# (Tone, Content, Formatting, Structure, Templates). Pre-refactor it pointed
# at the legacy CANONICAL_AREAS union (Categories + Sections). The name
# "CANONICAL_LOCATIONS" is stale — should rename to CANONICAL_CATEGORIES in
# a follow-up sweep.
CANONICAL_LOCATIONS = _tax.CATEGORIES

def _derive_location_and_type(check_type):
    """Return (category, issue_type) for a legacy check_type string. Delegates to taxonomy.py."""
    return _tax.derive_location_and_type(check_type)


def stream_script(commands):
    def generate():
        for cmd in commands:
            proc = subprocess.Popen(
                cmd, cwd=BASE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
            proc.wait()
            if proc.returncode != 0:
                yield f"event: done\ndata: {proc.returncode}\n\n"
                return
            # fetch_jobs.py rebuilds qa_tickets.db from scratch each run with a
            # minimal schema that is missing category / issue_type / default_severity
            # on email_controls (plus the pending_tickets and rejected_check_types
            # tables). Re-run the self-heal migration after every scripted step
            # so the Tickets page doesn't break after a Standard Check.
            if cmd and str(cmd[-1]).endswith(('fetch_jobs.py', 'run_ai_review.py',
                                              'build_data.py')):
                try:
                    _ensure_pending_table(force=True)
                    yield "data: [migrate] schema re-verified (category/issue_type/default_severity)\n\n"
                except Exception as e:
                    yield f"data: [migrate] warning: {e}\n\n"
        yield "event: done\ndata: 0\n\n"
    return generate

@app.route('/api/tickets', methods=['GET'])
@requires_page('qa_tools')
def api_get_tickets():
    """Return tickets with optional filters.
    Query params: status, community, severity, issue_type, detected_by, category
    (check_type still accepted for backward compat — derives category + issue_type)
    All accept comma-separated values for multi-select."""
    if not os.path.exists(DB):
        return jsonify([])
    status      = request.args.get('status',      '').strip()
    community   = request.args.get('community',   '').strip()
    severity    = request.args.get('severity',    '').strip()
    check_type  = request.args.get('check_type',  '').strip()
    issue_type  = request.args.get('issue_type',  '').strip()
    detected_by = request.args.get('detected_by', '').strip()
    category        = request.args.get('category',        '').strip()

    con, tmp = _read_db_copy()
    try:
        where, params = ['1=1'], []
        if status:
            # Support comma-separated OR single value
            vals = [v.strip() for v in status.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('status = ?'); params.append(vals[0])
            elif vals:
                where.append(f'status IN ({",".join("?"*len(vals))})'); params.extend(vals)
        if community:
            vals = [v.strip() for v in community.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('community = ?'); params.append(vals[0])
            elif vals:
                where.append(f'community IN ({",".join("?"*len(vals))})'); params.extend(vals)
        if severity:
            vals = [v.strip().upper() for v in severity.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('severity = ?'); params.append(vals[0])
            elif vals:
                where.append(f'severity IN ({",".join("?"*len(vals))})'); params.extend(vals)
        if check_type:
            # Convert check_type display string to category + issue_type filter
            vals = [v.strip() for v in check_type.split(',') if v.strip()]
            or_conditions = []
            for ct_val in vals:
                ar, it = _derive_location_and_type(ct_val)
                if ar and it:
                    or_conditions.append('(category = ? AND issue_type = ?)')
                    params.extend([ar, it])
            if or_conditions:
                where.append('(' + ' OR '.join(or_conditions) + ')')
        if issue_type:
            vals = [v.strip() for v in issue_type.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('issue_type = ?'); params.append(vals[0])
            elif vals:
                where.append(f'issue_type IN ({",".join("?"*len(vals))})'); params.extend(vals)
        if detected_by:
            vals = [v.strip().upper() for v in detected_by.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('detected_by = ?'); params.append(vals[0])
            elif vals:
                where.append(f'detected_by IN ({",".join("?"*len(vals))})'); params.extend(vals)
        if category:
            vals = [v.strip() for v in category.split(',') if v.strip()]
            if len(vals) == 1:
                where.append('category = ?'); params.append(vals[0])
            elif vals:
                where.append(f'category IN ({",".join("?"*len(vals))})'); params.extend(vals)
        # Section filter (2026-04-17 flatten). Value "(null)" matches rows
        # where section is NULL or empty (cross-cutting tickets).
        section_filter = request.args.get('section', '').strip()
        if section_filter:
            vals = [v.strip() for v in section_filter.split(',') if v.strip()]
            null_match   = any(v == '(null)' for v in vals)
            value_match  = [v for v in vals if v != '(null)']
            or_conds = []
            if null_match:
                or_conds.append("(section IS NULL OR section = '')")
            if value_match:
                ph = ','.join('?' * len(value_match))
                or_conds.append(f'section IN ({ph})')
                params.extend(value_match)
            if or_conds:
                where.append('(' + ' OR '.join(or_conds) + ')')
        # Date range filter (2026-04-16). date_flagged is stored as ISO
        # YYYY-MM-DD text, so plain string comparison works correctly.
        date_from = request.args.get('date_from', '').strip()
        date_to   = request.args.get('date_to',   '').strip()
        if date_from:
            where.append('date_flagged >= ?'); params.append(date_from)
        if date_to:
            where.append('date_flagged <= ?'); params.append(date_to)
        # Exclude muted (category, issue_type) pairs from the Live Tickets view.
        # Muting is view-only — tickets still exist and still appear on the
        # Rejected page (/api/rejected-tickets) and Controls page.
        where.append(
            "NOT EXISTS (SELECT 1 FROM email_controls ec_mute "
            "WHERE ec_mute.muted = 1 "
            "AND ec_mute.category = tickets.category "
            "AND ec_mute.issue_type = tickets.issue_type)"
        )
        sql = f"SELECT * FROM tickets WHERE {' AND '.join(where)} ORDER BY ticket_id DESC"
        rows = [dict(r) for r in con.execute(sql, params)]
        url_map = _get_job_url_map()
        # Resolve last_action_by → name/email in one batch lookup, so the
        # Phase 3 attribution badge ("Resolved by the maintainer · Apr 27") renders
        # without per-row JOINs. Falls back to NULL fields when the user
        # has been deleted; UI handles fallback gracefully.
        umap = _db.get_users_map(
            con, [r.get('last_action_by') for r in rows]
        )
        for r in rows:
            if not r.get('job_url'):
                r['job_url'] = url_map.get(str(r.get('req_id', '')), '')
            # Ensure check_type is set (compose if necessary for backward compat)
            if not r.get('check_type'):
                r['check_type'] = f"{r.get('category', '')} — {r.get('issue_type', '')}"
            uid = r.get('last_action_by')
            u = umap.get(uid) if uid else None
            r['last_action_by_name']  = u['name']  if u else None
            r['last_action_by_email'] = u['email'] if u else None
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/ticket-filters', methods=['GET'])
@requires_page('qa_tools')
def api_ticket_filters():
    """Return unique values for filter dropdowns."""
    if not os.path.exists(DB):
        return jsonify({'communities': [], 'check_types': [], 'categories': [],
                        'severities': [], 'detected_by': [], 'statuses': []})
    con, tmp = _read_db_copy()
    try:
        communities = [r[0] for r in con.execute(
            "SELECT DISTINCT community FROM tickets WHERE community != '' ORDER BY community")]
        # Check types from email_controls (canonical source, stays in sync with Ticket Controls)
        ct_rows = con.execute(
            "SELECT category, issue_type FROM email_controls "
            "WHERE category IS NOT NULL AND category != '' "
            "AND issue_type IS NOT NULL AND issue_type != '' "
            "ORDER BY category, issue_type").fetchall()
        check_types = [f"{r[0]} \u2014 {r[1]}" for r in ct_rows]
        # Issue types: also from email_controls for consistency
        issue_types = sorted({(r[1] or '').strip() for r in ct_rows if r[1] and r[1].strip()})
        detected   = [r[0] for r in con.execute(
            "SELECT DISTINCT detected_by FROM tickets WHERE detected_by != '' ORDER BY detected_by")]
        # Areas: from email_controls (canonical source), in canonical display order
        db_areas = {(r[0] or '').strip() for r in ct_rows if r[0] and r[0].strip()}
        areas = [l for l in CANONICAL_LOCATIONS if l in db_areas]
        return jsonify({
            'communities': communities,
            'check_types': check_types,
            'issue_types': issue_types,
            'categories':  [],
            'areas':       areas,
            'severities':  ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'],
            'detected_by': detected,
            'statuses':    ['Open', 'Resolved', 'Acknowledged'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/tickets/<ticket_id>/flag-incorrect', methods=['POST'])
@login_required
def api_flag_incorrect(ticket_id):
    """Mark a ticket as Flagged Incorrectly.

    Writes the user's reason to the dedicated `reason` column (category +
    optional free text). `notes` is left alone — it's the conversational
    layer, not the queryable feedback signal. Legacy callers that send only
    `reason` (free text) are still accepted and stored verbatim.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    reason_category = (payload.get('reason_category') or '').strip()
    reason_detail   = (payload.get('reason_detail') or '').strip()
    legacy_reason   = (payload.get('reason') or '').strip()
    reason_out = _compose_reason(reason_category, reason_detail, legacy_reason)
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute("SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        prior_status = row['status']
        con.execute(
            "UPDATE tickets SET status='Flagged Incorrectly', reason=? WHERE ticket_id=?",
            (reason_out or None, ticket_id),
        )
        _db.record_ticket_action(
            con, ticket_id=ticket_id, user=user, action='flag_incorrect',
            payload={'prior_status': prior_status, 'new_status': 'Flagged Incorrectly',
                     'reason': reason_out or None},
        )
        con.commit()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({'ok': True, 'ticket_id': ticket_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


def _compose_reason(category, detail, legacy=''):
    """Compose a `reason` column value from a category + optional free text.

    Format: "Category: Detail" when both are present; just one if the other is
    empty. Legacy callers that sent free-text only are stored as-is. Empty
    strings produce None so the DB sees NULL.
    """
    category = (category or '').strip()
    detail   = (detail or '').strip()
    legacy   = (legacy or '').strip()
    if category and detail:
        return f'{category}: {detail}'
    if category:
        return category
    if detail:
        return detail
    return legacy or ''


@app.route('/api/tickets/<ticket_id>/update-notes', methods=['POST'])
@login_required
def api_update_ticket_notes(ticket_id):
    """Update the notes field on a ticket (used by the Rejected view inline editor)."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    notes   = (payload.get('notes') or '').strip()
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute(
            "SELECT ticket_id, notes FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        old_notes = row['notes']
        con.execute("UPDATE tickets SET notes=? WHERE ticket_id=?", (notes, ticket_id))
        _db.write_event(
            con, entity_type='ticket', entity_id=ticket_id, user=user,
            action='notes_update',
            payload={'old_notes': old_notes, 'new_notes': notes},
        )
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/tickets/<ticket_id>/resolve', methods=['POST'])
@login_required
def api_resolve_ticket(ticket_id):
    """Mark a single ticket as Resolved.

    Optionally accepts a `reason_category` / `reason_detail` pair that gets
    written to the `reason` column. `notes` stays reserved for free-text
    commentary.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    reason_out = _compose_reason(
        payload.get('reason_category'),
        payload.get('reason_detail'),
        payload.get('reason'),
    )
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute("SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        if row['status'] == 'Resolved':
            return jsonify({'ok': True, 'already_resolved': True})
        prior_status = row['status']
        if reason_out:
            con.execute("UPDATE tickets SET status='Resolved', reason=? WHERE ticket_id=?",
                        (reason_out, ticket_id))
        else:
            con.execute("UPDATE tickets SET status='Resolved' WHERE ticket_id=?",
                        (ticket_id,))
        _db.record_ticket_action(
            con, ticket_id=ticket_id, user=user, action='resolve',
            payload={'prior_status': prior_status, 'new_status': 'Resolved',
                     'reason': reason_out or None},
        )
        con.commit()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({'ok': True, 'ticket_id': ticket_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/tickets/bulk-resolve', methods=['POST'])
@requires_page('bulk_edit')
def api_bulk_resolve():
    """Resolve multiple tickets at once. Expects JSON: { ticket_ids: [...] }

    Bulk resolve doesn't capture a per-ticket reason (that would defeat the
    purpose of bulk). Individual resolves can still send reason_category /
    reason_detail.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    data = request.get_json(silent=True) or {}
    ids  = data.get('ticket_ids') or []
    if not ids:
        return jsonify({'error': 'No ticket_ids provided'}), 400
    user = _current_user()
    bulk_id = uuid.uuid4().hex[:12]
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        resolved = 0
        for tid in ids:
            row = con.execute("SELECT status FROM tickets WHERE ticket_id = ?", (tid,)).fetchone()
            if not row or row['status'] == 'Resolved':
                continue
            prior_status = row['status']
            con.execute("UPDATE tickets SET status='Resolved' WHERE ticket_id=?", (tid,))
            _db.record_ticket_action(
                con, ticket_id=tid, user=user, action='resolve',
                payload={'prior_status': prior_status, 'new_status': 'Resolved',
                         'reason': None},
                bulk_id=bulk_id,
            )
            resolved += 1
        con.commit()
        _write_db_copy(tmp)
        if resolved:
            import datetime
            _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({'ok': True, 'resolved': resolved, 'total': len(ids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/tickets/<ticket_id>/restore', methods=['POST'])
@login_required
def api_restore_ticket(ticket_id):
    """Restore a Flagged Incorrectly / Acknowledged ticket back to Open.

    Used by the Rejected tab (Flagged Incorrectly → Open) and by admins
    reopening an Acknowledged ticket (Acknowledged → Open).
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute("SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        prior_status = row['status']
        con.execute("UPDATE tickets SET status='Open' WHERE ticket_id = ?", (ticket_id,))
        _db.record_ticket_action(
            con, ticket_id=ticket_id, user=user, action='restore',
            payload={'prior_status': prior_status, 'new_status': 'Open'},
        )
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


# ── Ticket notes (community-facing UI, Migration 2026-04-20) ────────────────

@app.route('/api/tickets/<ticket_id>/notes', methods=['GET'])
@login_required
def api_list_ticket_notes(ticket_id):
    """List notes on a ticket, oldest first, with author attribution."""
    if not os.path.exists(DB):
        return jsonify({'notes': []})
    try:
        con = _db.connect_readonly()
        try:
            _db.ensure_tables(con)
            notes = _db.list_ticket_notes(con, ticket_id)
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'notes': notes})


@app.route('/api/tickets/<ticket_id>/notes', methods=['POST'])
@login_required
def api_add_ticket_note(ticket_id):
    """Add a plain-text note attributed to the current user."""
    user = _current_user()
    payload = request.get_json(silent=True) or {}
    text = (payload.get('note_text') or '').strip()
    if not text:
        return jsonify({'error': 'note_text is required'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'note is too long (max 2000 chars)'}), 400
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        # Verify the ticket exists — otherwise we'd accumulate orphan notes.
        if not con.execute(
            "SELECT 1 FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone():
            con.close()
            _discard_db_copy(tmp)
            return jsonify({'error': 'Ticket not found'}), 404
        con.isolation_level = None
        con.execute('BEGIN')
        new_id = _db.add_ticket_note(con, ticket_id, user['id'], text)
        _db.write_event(
            con, entity_type='ticket', entity_id=ticket_id, user=user,
            action='note_create',
            payload={'note_id': new_id, 'note_text': text},
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        _discard_db_copy(tmp)
        return jsonify({'error': str(e)}), 500


@app.route('/api/tickets/<ticket_id>/notes/<int:note_id>', methods=['PATCH'])
@admin_required
def api_edit_ticket_note(ticket_id, note_id):
    payload = request.get_json(silent=True) or {}
    text = (payload.get('note_text') or '').strip()
    if not text:
        return jsonify({'error': 'note_text is required'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'note is too long (max 2000 chars)'}), 400
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        old_row = con.execute(
            "SELECT note_text FROM ticket_notes WHERE id = ?", (note_id,)
        ).fetchone()
        old_text = old_row['note_text'] if old_row else None
        n = _db.update_ticket_note(con, note_id, text)
        if n:
            _db.write_event(
                con, entity_type='ticket', entity_id=ticket_id, user=user,
                action='note_edit',
                payload={'note_id': note_id, 'old_text': old_text, 'new_text': text},
            )
        con.execute('COMMIT')
        con.close()
        if n:
            _write_db_copy(tmp)
            return jsonify({'ok': True})
        _discard_db_copy(tmp)
        return jsonify({'error': 'note not found'}), 404
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        _discard_db_copy(tmp)
        return jsonify({'error': str(e)}), 500


@app.route('/api/tickets/<ticket_id>/notes/<int:note_id>', methods=['DELETE'])
@admin_required
def api_delete_ticket_note(ticket_id, note_id):
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        old_row = con.execute(
            "SELECT note_text, user_id FROM ticket_notes WHERE id = ?", (note_id,)
        ).fetchone()
        old_text = old_row['note_text'] if old_row else None
        original_author = old_row['user_id'] if old_row else None
        n = _db.delete_ticket_note(con, note_id)
        if n:
            _db.write_event(
                con, entity_type='ticket', entity_id=ticket_id, user=user,
                action='note_delete',
                payload={'note_id': note_id, 'deleted_text': old_text,
                         'original_author_user_id': original_author},
            )
        con.execute('COMMIT')
        con.close()
        if n:
            _write_db_copy(tmp)
            return jsonify({'ok': True})
        _discard_db_copy(tmp)
        return jsonify({'error': 'note not found'}), 404
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        _discard_db_copy(tmp)
        return jsonify({'error': str(e)}), 500


# Severities where Acknowledge is allowed (Part 3, Migration 2026-04-20).
# Critical/High tickets must be resolved; Medium/Low can be either resolved or
# acknowledged-with-reason.
_ACK_ELIGIBLE_SEVERITIES = {'MEDIUM', 'LOW'}


@app.route('/api/tickets/<ticket_id>/acknowledge', methods=['POST'])
@login_required
def api_acknowledge_ticket(ticket_id):
    """Acknowledge a Medium/Low ticket as a judgment-call accepted in context.

    Requires reason_category; reason_detail required only when
    reason_category == 'Other'. Rejects Critical/High tickets at the server
    so a crafted POST can't bypass the UI gate.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    reason_category = (payload.get('reason_category') or '').strip()
    reason_detail   = (payload.get('reason_detail') or '').strip()
    if not reason_category:
        return jsonify({'error': 'reason_category is required'}), 400
    if reason_category == 'Other' and not reason_detail:
        return jsonify({'error': 'reason_detail is required when reason_category is Other'}), 400
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute(
            "SELECT status, severity FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        sev = (row['severity'] or '').strip().upper()
        if sev not in _ACK_ELIGIBLE_SEVERITIES:
            return jsonify({
                'error': f'Acknowledge is not allowed on {sev or "unknown"}-severity tickets. '
                         f'Critical/High must be resolved.'
            }), 400
        reason_out = _compose_reason(reason_category, reason_detail)
        prior_status = row['status']
        con.execute(
            "UPDATE tickets SET status='Acknowledged', reason=? WHERE ticket_id=?",
            (reason_out or None, ticket_id),
        )
        _db.record_ticket_action(
            con, ticket_id=ticket_id, user=user, action='acknowledge',
            payload={'prior_status': prior_status, 'new_status': 'Acknowledged',
                     'reason': reason_out or None},
        )
        con.commit()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({'ok': True, 'ticket_id': ticket_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/tickets/<ticket_id>', methods=['DELETE'])
@requires_page('qa_tools')
def api_delete_ticket(ticket_id):
    """Archive a ticket from the tickets table (set status to 'Archived').

    Used by the Rejected page to remove tickets the user no longer wants
    visible — the row stays in the DB for audit but is hidden from all views."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        row = con.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Ticket not found'}), 404
        prior_status = row['status']
        con.execute(
            "UPDATE tickets SET status = 'Archived' WHERE ticket_id = ?", (ticket_id,)
        )
        _db.record_ticket_action(
            con, ticket_id=ticket_id, user=user, action='archive',
            payload={'prior_status': prior_status, 'new_status': 'Archived'},
        )
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'archived': 1})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


# ── Audit-trail read endpoints (Phase 2, 2026-04-27) ───────────────────────
# Backs the Phase 3 UI: per-ticket history drawer (login-required) and the
# Users-page activity ticker (admin-only). Pure reads — never write.


@app.route('/api/audit-events', methods=['GET'])
@admin_required
def api_audit_events():
    """Paginated feed of recent audit events across all tickets and users.

    Admin-only because it surfaces cross-user activity. Filters are all
    optional and AND-combined: `user_id`, `entity_id`, `entity_type`,
    `action`, `since` (ISO date), `bulk_id`. Pagination via `limit`
    (default 50, capped at 200) + `offset`.

    Synthetic `pre_audit` events (written by
    archive/one-shot-scripts/backfill_audit_events.py for tickets closed
    before 2026-04-27) are excluded from this feed by default — they would
    drown the activity ticker in 1000+ historical placeholders. Pass
    `include_pre_audit=1` to opt in.
    """
    if not os.path.exists(DB):
        return jsonify({'events': []})
    args = request.args
    try:
        limit = max(1, min(200, int(args.get('limit', 50))))
        offset = max(0, int(args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be integers'}), 400

    user_id = args.get('user_id')
    if user_id:
        try:
            user_id = int(user_id)
        except ValueError:
            return jsonify({'error': 'user_id must be integer'}), 400

    include_pre_audit = args.get('include_pre_audit', '').lower() in ('1', 'true', 'yes')

    try:
        con = _db.connect_readonly()
        try:
            _db.ensure_tables(con)
            events = _db.list_audit_events(
                con,
                limit=limit,
                offset=offset,
                user_id=user_id or None,
                entity_id=(args.get('entity_id') or None),
                entity_type=(args.get('entity_type') or None),
                action=(args.get('action') or None),
                since=(args.get('since') or None),
                bulk_id=(args.get('bulk_id') or None),
                exclude_pre_audit=not include_pre_audit,
            )
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'events': events, 'limit': limit, 'offset': offset})


@app.route('/api/tickets/<ticket_id>/history', methods=['GET'])
@login_required
def api_ticket_history(ticket_id):
    """Full event timeline for one ticket, oldest first.

    Authenticated users (not just admins) can see who acted on a ticket
    they're viewing — that's the whole point of the attribution feature.
    Per-ticket history is bounded, so no pagination.
    """
    if not os.path.exists(DB):
        return jsonify({'events': []})
    try:
        con = _db.connect_readonly()
        try:
            _db.ensure_tables(con)
            events = _db.list_ticket_history(con, ticket_id)
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'events': events, 'ticket_id': ticket_id})


# ── Templates Category endpoints (2026-04-21) ──────────────────────────────
# Root Cause Clusters feature (template-pattern routing).
# Phase 2 shipped capture; Phase 3 adds the /templates management page
# endpoints (list / tickets / rename / mute / unmute / retire / resolve-current).


@app.route('/api/templates', methods=['GET'])
@requires_page('templates')
def api_list_templates():
    """Return the list of captured Templates-Category pairs with counts,
    age, mute state, and inherited severity. Backs the /templates page
    list. Read-only; safe to hit with a direct connection \u2014 no write.
    """
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        rows = _db.list_captured_templates(con)
        return jsonify(rows)
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/templates/<path:name>/tickets', methods=['GET'])
@requires_page('templates')
def api_template_tickets(name):
    """Return every ticket linked to the given Templates pair, for the
    inline drawer on each template card.
    """
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        rows = _db.list_template_tickets(con, name)
        url_map = _get_job_url_map()
        for r in rows:
            if not r.get('job_url'):
                r['job_url'] = url_map.get(str(r.get('req_id', '')), '')
        return jsonify(rows)
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/templates/<path:name>/rename', methods=['POST'])
@requires_page('templates')
def api_rename_template(name):
    """Rename a Templates pair (issue_type). Atomic across email_controls +
    tickets. Body: ``{new_name}``. Fails with 400 on collision or empty name.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    new_name = (payload.get('new_name') or '').strip()
    if not new_name:
        return jsonify({'error': 'new_name is required'}), 400
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        try:
            ec_rows, ticket_rows = _db.rename_template(con, name, new_name)
        except ValueError as ve:
            con.execute('ROLLBACK')
            return jsonify({'error': str(ve)}), 400
        _db.record_control_action(
            con, category='Templates', issue_type=new_name, user=user,
            action='rename',
            payload={'before': {'category': 'Templates', 'issue_type': name},
                     'after':  {'category': 'Templates', 'issue_type': new_name},
                     'counts': {'email_controls': ec_rows, 'tickets': ticket_rows}},
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        return jsonify({
            'ok': True,
            'renamed_email_controls': ec_rows,
            'renamed_tickets': ticket_rows,
            'new_name': new_name,
        })
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/<path:name>/mute', methods=['POST'])
@requires_page('templates')
def api_mute_template(name):
    """Re-mute a Templates pair (the "regressions are back" action)."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    reason = (payload.get('reason') or 'Re-muted from Templates page').strip() or None
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        updated = _db.set_muted(con, 'Templates', name, reason=reason)
        if updated:
            _db.record_control_action(
                con, category='Templates', issue_type=name, user=user, action='mute',
                payload={'reason': reason, 'mode': 'single'},
            )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'updated': updated})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/<path:name>/unmute', methods=['POST'])
@requires_page('templates')
def api_unmute_template(name):
    """Unmute a Templates pair (the "Mark Fixed / watch for regressions"
    action). Future matching findings show loud in the admin's Live Tickets
    view until the admin chooses to re-mute."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        updated = _db.unmute(con, 'Templates', name)
        if updated:
            _db.record_control_action(
                con, category='Templates', issue_type=name, user=user, action='unmute',
                payload={'mode': 'single'},
            )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'updated': updated})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/<path:name>/retire', methods=['POST'])
@requires_page('templates')
def api_retire_template(name):
    """Retire a Templates pair (full rejected_issues tombstone).

    Body may include ``{also_resolve_open: bool, notes: str}``. With
    ``also_resolve_open=True``, any currently-Open ticket linked to the
    template is resolved in the same transaction. Tickets are left in
    place with their (Templates, name) pair regardless \u2014 the pair just
    stops appearing in filter dropdowns and auto-linking stops.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload = request.get_json(silent=True) or {}
    also_resolve = bool(payload.get('also_resolve_open'))
    notes = (payload.get('notes') or '').strip()
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        # If we're going to bulk-resolve, snapshot the affected ticket ids
        # BEFORE retire_template runs the UPDATE \u2014 we need them for per-row
        # audit events. retire_template itself doesn't touch audit_events.
        affected_ids = []
        if also_resolve:
            affected_ids = [r['ticket_id'] for r in con.execute(
                "SELECT ticket_id FROM tickets "
                "WHERE category = 'Templates' AND issue_type = ? AND status = 'Open'",
                (name,),
            ).fetchall()]
        try:
            pair, resolved = _db.retire_template(
                con, name, retire_tickets=also_resolve, notes=notes,
            )
        except ValueError as ve:
            con.execute('ROLLBACK')
            return jsonify({'error': str(ve)}), 400
        bulk_id = uuid.uuid4().hex[:12]
        for tid in affected_ids:
            _db.record_ticket_action(
                con, ticket_id=tid, user=user, action='resolve',
                payload={'prior_status': 'Open', 'new_status': 'Resolved',
                         'reason': 'Resolved via template retirement'},
                bulk_id=bulk_id,
            )
        _db.record_control_action(
            con, category='Templates', issue_type=name, user=user, action='retire',
            payload={'notes': notes or None,
                     'tickets_resolved': resolved,
                     'also_resolve_open': also_resolve},
            bulk_id=bulk_id,
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({
            'ok': True,
            'retired_pair': list(pair),
            'tickets_resolved': resolved,
        })
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/<path:name>/resolve-current', methods=['POST'])
@requires_page('templates')
def api_resolve_template_current(name):
    """Bulk-resolve every currently-Open ticket linked to the given
    Templates pair. The template row itself stays untouched \u2014 future
    matching findings continue to auto-link here.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        # Snapshot which tickets the bulk-resolve will hit so we can write
        # per-row audit events (resolve_template_tickets itself does the
        # UPDATE without touching audit_events).
        affected_ids = [r['ticket_id'] for r in con.execute(
            "SELECT ticket_id FROM tickets "
            "WHERE category = 'Templates' AND issue_type = ? AND status = 'Open'",
            (name,),
        ).fetchall()]
        count = _db.resolve_template_tickets(con, name)
        bulk_id = uuid.uuid4().hex[:12] if len(affected_ids) > 1 else None
        for tid in affected_ids:
            _db.record_ticket_action(
                con, ticket_id=tid, user=user, action='resolve',
                payload={'prior_status': 'Open', 'new_status': 'Resolved',
                         'reason': 'Resolved via template bulk-resolve'},
                bulk_id=bulk_id,
            )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({'ok': True, 'tickets_resolved': count})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/templates/capture', methods=['POST'])
@requires_page('templates')
def api_capture_template():
    """Capture a rollup of tickets as a new Templates-Category pair.

    Body: ``{ pattern, template_name, ticket_ids }``.

    Creates a new ``email_controls`` row with ``category='Templates'``,
    ``muted=1``, ``template_pattern=<pattern>``, and rewrites every
    eligible ticket in ``ticket_ids`` (excluding terminal statuses) to
    that pair. The original ``(category, issue_type)`` is recorded in each
    rewritten ticket's ``captured_from`` column for audit.

    Gated by the ``'templates'`` page permission (admins bypass, viewers
    default False). Follows the standard DB write safety pattern —
    read_copy → ensure_tables → modify → write_copy.

    See CLAUDE.md "Templates Category" standing rule.
    """
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    payload       = request.get_json(silent=True) or {}
    pattern       = (payload.get('pattern') or '').strip()
    template_name = (payload.get('template_name') or '').strip()
    ticket_ids    = payload.get('ticket_ids') or []
    if not pattern:
        return jsonify({'error': 'pattern is required'}), 400
    if not template_name:
        return jsonify({'error': 'template_name is required'}), 400
    if not isinstance(ticket_ids, list) or not ticket_ids:
        return jsonify({'error': 'ticket_ids must be a non-empty list'}), 400
    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        con.isolation_level = None
        con.execute('BEGIN')
        try:
            pair, rewritten, inherited_sev = _db.capture_template(
                con, pattern, template_name, ticket_ids,
                created_by_user_id=(user or {}).get('id'),
            )
        except ValueError as ve:
            con.execute('ROLLBACK')
            return jsonify({'error': str(ve)}), 400
        # One audit event for the capture itself. Per-ticket recategorization
        # events would be high-volume and low-value (ticket pair changes are
        # already recoverable via tickets.captured_from); the parent capture
        # event with the ticket-id list in payload is enough.
        _db.record_control_action(
            con, category='Templates', issue_type=template_name, user=user,
            action='capture',
            payload={'pattern': pattern,
                     'ticket_ids': ticket_ids,
                     'rewritten_count': rewritten,
                     'inherited_severity': inherited_sev},
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        import datetime
        _write_meta({'last_ticket_change': datetime.datetime.now().isoformat()})
        return jsonify({
            'ok': True,
            'template_pair': list(pair),
            'rewritten_count': rewritten,
            'inherited_severity': inherited_sev,
        })
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/rejected-tickets/clear', methods=['POST'])
@requires_page('qa_tools')
def api_clear_rejected():
    """Archive every ticket currently in the Rejected view
    (status = 'Flagged Incorrectly') — sets status to 'Archived'.
    Also archives matching rejected_issues rows."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    con, tmp = _read_db_copy()
    try:
        import datetime
        now = datetime.datetime.now().isoformat()
        cur = con.execute(
            "UPDATE tickets SET status = 'Archived' WHERE status = 'Flagged Incorrectly'"
        )
        # Also archive corresponding rejected_issues entries
        con.execute(
            "UPDATE rejected_issues SET archived_at = ? WHERE archived_at IS NULL",
            (now,)
        )
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'archived': cur.rowcount})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/rejected-count')
@requires_page('qa_tools')
def api_rejected_count():
    """Return the number of rejected (Flagged Incorrectly) tickets — used
    so the yellow badge on the Rejected tab is populated on page load,
    not only after the tab is clicked."""
    if not os.path.exists(DB):
        return jsonify({'count': 0})
    try:
        con, tmp = _read_db_copy()
        count = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'Flagged Incorrectly'"
        ).fetchone()[0]
        con.close()
        try: os.remove(tmp)
        except OSError: pass
        return jsonify({'count': count})
    except Exception:
        return jsonify({'count': 0})


@app.route('/api/rejected-tickets')
@requires_page('qa_tools')
def api_rejected_tickets():
    """Return all Flagged Incorrectly tickets grouped by (category, issue_type), newest first."""
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT ticket_id, date_flagged, req_id, job_title, community,
                   severity, category, issue_type, issue_summary, offending_text,
                   detected_by, notes, reason, job_url
            FROM tickets
            WHERE status = 'Flagged Incorrectly'
            ORDER BY category, issue_type, date_flagged DESC
        """).fetchall()]
        url_map = _get_job_url_map()
        for r in rows:
            if not r.get('job_url'):
                r['job_url'] = url_map.get(str(r.get('req_id') or ''), '')
            # Compose check_type for display (backward compat)
            r['check_type'] = f"{r.get('category', '')} \u2014 {r.get('issue_type', '')}"
        # Group by (category, issue_type) pair
        groups = {}
        for r in rows:
            key = (r.get('category', ''), r.get('issue_type', ''))
            ct = r['check_type']  # composed display label
            if key not in groups:
                groups[key] = {'check_type': ct, 'category': key[0], 'issue_type': key[1], 'tickets': []}
            groups[key]['tickets'].append(r)
        return jsonify(list(groups.values()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


# ── Pending-tickets API ───────────────────────────────────────────────────────

@app.route('/api/pending-count')
@login_required
def api_pending_count():
    """DEPRECATED (2026-04-16). pending_tickets system retired.
    Stub returns 0 for backward compatibility with any cached client code."""
    return jsonify({'count': 0})


@app.route('/api/pending-tickets')
@login_required
def api_pending_tickets():
    """DEPRECATED (2026-04-16). pending_tickets system retired.
    Stub returns empty list for backward compatibility."""
    return jsonify([])


@app.route('/api/taxonomy')
@login_required
def api_taxonomy():
    """Return the full taxonomy the Pending Review form needs:
    - canonical + custom areas
    - all known (category, issue_type) pairs (from email_controls)
    - all known issue_types (deduped, from email_controls)
    - all current aliases (with separate category/issue_type fields)
    Used by the approval form to power datalists, merge-warnings, and
    category-picker options.
    """
    if not os.path.exists(DB):
        return jsonify({'canonical_areas': CANONICAL_LOCATIONS, 'custom_areas': [],
                        'check_types': [], 'issue_types': [], 'aliases': []})
    con, tmp = _read_db_copy()
    try:
        # Known check_types with their category/issue_type breakdown from email_controls
        rows = con.execute("""
            SELECT category, issue_type
              FROM email_controls
             WHERE category IS NOT NULL AND category != ''
               AND issue_type IS NOT NULL AND issue_type != ''
             ORDER BY category, issue_type
        """).fetchall()
        check_types = []
        issue_types_set = set()
        for ar, it in rows:
            ct = f"{ar} — {it}"
            check_types.append({'check_type': ct, 'category': ar, 'issue_type': it})
            if it.strip():
                issue_types_set.add(it.strip())
        issue_types = sorted(issue_types_set)

        # Custom Categories (user-defined beyond the canonical six)
        try:
            custom_areas = [
                {'category': r[0]}
                for r in con.execute(
                    "SELECT category FROM custom_areas ORDER BY category"
                ).fetchall()
            ]
        except Exception:
            custom_areas = []

        # Aliases (renamed to issue_aliases in new schema)
        aliases = []
        try:
            alias_rows = con.execute(
                "SELECT alias_area, alias_issue_type, canonical_area, canonical_issue FROM issue_aliases ORDER BY alias_area, alias_issue_type"
            ).fetchall()
            for alias_area, alias_itype, can_area, can_itype in alias_rows:
                aliases.append({
                    'alias': f"{alias_area} \u2014 {alias_itype}",
                    'alias_area': alias_area,
                    'alias_issue_type': alias_itype,
                    'canonical': f"{can_area} \u2014 {can_itype}",
                    'canonical_area': can_area,
                    'canonical_issue_type': can_itype,
                })
        except Exception:
            pass

        return jsonify({
            'canonical_areas': CANONICAL_LOCATIONS,
            'custom_areas':    custom_areas,
            'check_types':     check_types,
            'issue_types':     issue_types,
            'aliases':         aliases,
        })
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/custom-areas', methods=['POST'])
@requires_page('controls')
def api_add_custom_area():
    """Add a new user-defined Category to the taxonomy."""
    data = request.get_json() or {}
    category = (data.get('category') or '').strip()
    if not category:
        return jsonify({'error': 'category required'}), 400
    if category in CANONICAL_LOCATIONS:
        return jsonify({'ok': True, 'note': 'already canonical'})
    con, tmp = _read_db_copy()
    try:
        con.execute("""
            INSERT OR IGNORE INTO custom_areas (category, created_at, notes)
            VALUES (?, datetime('now'), ?)
        """, (category, (data.get('notes') or '').strip()))
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/pending-tickets/approve', methods=['POST'])
@requires_page('qa_tools')
def api_approve_pending():
    """DEPRECATED (2026-04-16). pending_tickets system retired."""
    return jsonify({'error': 'pending_tickets system retired — new checks are added via the qa-rules-maintenance Claude skill'}), 410


@app.route('/api/pending-tickets/discard', methods=['POST'])
@requires_page('qa_tools')
def api_discard_pending_stub():
    """DEPRECATED (2026-04-16). pending_tickets system retired."""
    return jsonify({'error': 'pending_tickets system retired'}), 410


@app.route('/api/pending-tickets/flag-incorrect', methods=['POST'])
@requires_page('qa_tools')
def api_flag_pending_incorrect_stub():
    """DEPRECATED (2026-04-16). pending_tickets system retired."""
    return jsonify({'error': 'pending_tickets system retired'}), 410


@app.route('/api/pending-tickets/<ticket_id>/flag-incorrect', methods=['POST'])
@requires_page('qa_tools')
def api_flag_pending_ticket_stub(ticket_id):
    """DEPRECATED (2026-04-16). pending_tickets system retired."""
    return jsonify({'error': 'pending_tickets system retired'}), 410


# ── Tickets page ──────────────────────────────────────────────────────────────
@app.route('/tickets')
@requires_page('qa_tools')
def tickets_page():
    user = _current_user()
    can_capture_templates = bool(
        user and _db.user_has_page_access(user, 'templates')
    )
    return render_template('tickets.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header('tickets'),
                           can_capture_templates=can_capture_templates)


@app.route('/templates')
@requires_page('templates')
def templates_page():
    """Render the dedicated Templates management page (Phase 3 of the Root
    Cause Clusters feature, 2026-04-21). Lists captured templates with
    mute / rename / retire controls and an inline ticket drawer per template.
    """
    return render_template('templates.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header('templates'))




@app.route('/api/email-controls', methods=['GET'])
@requires_page('controls')
def api_get_email_controls():
    """Return all email_controls rows as JSON. Each row has category + issue_type
    as separate fields; check_type is composed for display/backward compat."""
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        # Post-flatten (2026-04-17): sort by Category (the 6 canonical values)
        # then issue_type. No more section-vs-cross split — there's only one
        # taxonomy axis. Include multi_location_behavior in the projection
        # so the client can render the per-row selector.
        rows = [dict(r) for r in con.execute(
            """SELECT category, issue_type,
                      show_on_community, notes, fix_instruction, default_severity,
                      muted, muted_at, muted_reason,
                      multi_location_behavior
               FROM email_controls
               WHERE category IS NOT NULL AND category != ''
                 AND issue_type IS NOT NULL AND issue_type != ''
               ORDER BY category, issue_type"""
        )]
        # Build a severity-count map from open tickets: (category, issue_type) → {sev: count}
        sev_rows = con.execute("""
            SELECT category, issue_type, severity, COUNT(*) as cnt
            FROM tickets
            WHERE status NOT IN ('Resolved', 'Flagged Incorrectly', 'Archived', 'Acknowledged')
            GROUP BY category, issue_type, severity
        """).fetchall()
        sev_map = {}   # (category, issue_type) → {severity: count, ...}
        for sr in sev_rows:
            key = (sr['category'], sr['issue_type'])
            if key not in sev_map:
                sev_map[key] = {}
            sev_map[key][sr['severity']] = sr['cnt']
        # Also build a per-pair open-ticket-count map so the client can show
        # "N tickets held back" alongside muted checks.
        open_rows = con.execute("""
            SELECT category, issue_type, COUNT(*) as cnt
            FROM tickets
            WHERE status NOT IN ('Resolved','Flagged Incorrectly','Archived','Acknowledged')
            GROUP BY category, issue_type
        """).fetchall()
        open_map = {(r['category'], r['issue_type']): r['cnt'] for r in open_rows}
        # Compose check_type for display and backward compat
        for r in rows:
            category = r.get('category', '')
            itype = r.get('issue_type', '')
            r['check_type'] = f"{category} — {itype}" if category and itype else (itype or category)
            r['ticket_severities'] = sev_map.get((category, itype), {})
            r['open_ticket_count']  = open_map.get((category, itype), 0)
            # Normalize muted to a plain boolean for the client (SQLite gives int)
            r['muted'] = bool(r.get('muted'))
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/email-controls', methods=['POST'])
@requires_page('controls')
def api_save_email_controls():
    """Persist the ticket-controls editor payload: severity, show_on_community,
    notes, and the fix_instruction text (used as the community-page explanation).

    Endpoint name is `/api/email-controls` for backward compat only. The
    email generator was removed April 2026; fix_instruction now drives the
    community-page explanation block, not emails.

    The endpoint expects rows with 'check_type', 'category', 'issue_type', etc.
    We extract category+issue_type and insert using the new schema."""
    if not os.path.exists(DB):
        return jsonify({'error': 'Database not found'}), 404
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({'error': 'Expected a JSON array'}), 400
    user = _current_user()
    # Single shared bulk_id for multi-row saves. Lets the activity ticker
    # collapse "the maintainer edited 5 controls at once" into one logical action.
    bulk_id = uuid.uuid4().hex[:12] if len(data) > 1 else None
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        events_written = 0
        for row in data:
            category    = (row.get('category') or row.get('category') or '').strip()
            itype   = (row.get('issue_type')       or '').strip()
            notes   = (row.get('notes')            or '').strip()
            fix_ins = (row.get('fix_instruction')  or '').strip()
            show_comm = 1 if row.get('show_on_community', 1) else 0
            default_sev = (row.get('default_severity') or '').strip().upper()
            mlb = (row.get('multi_location_behavior') or 'dedup').strip().lower()
            if mlb not in ('dedup', 'per_section'):
                mlb = 'dedup'
            if not (category and itype):
                continue
            if default_sev and default_sev not in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
                default_sev = ''
            # Snapshot the existing row BEFORE write so the audit payload can
            # carry a clean before/after diff. New rows have before=None.
            existing = con.execute(
                "SELECT show_on_community, notes, fix_instruction, "
                "default_severity, multi_location_behavior "
                "FROM email_controls WHERE category = ? AND issue_type = ?",
                (category, itype),
            ).fetchone()
            before = dict(existing) if existing else None
            after = {
                'show_on_community': show_comm,
                'notes': notes or None,
                'fix_instruction': fix_ins or None,
                'default_severity': default_sev or None,
                'multi_location_behavior': mlb,
            }
            con.execute("""
                INSERT INTO email_controls
                  (category, issue_type, show_on_community, notes, fix_instruction,
                   default_severity, multi_location_behavior, scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ALL')
                ON CONFLICT(category, issue_type) DO UPDATE SET
                    show_on_community       = excluded.show_on_community,
                    notes                   = excluded.notes,
                    fix_instruction         = excluded.fix_instruction,
                    default_severity        = excluded.default_severity,
                    multi_location_behavior = excluded.multi_location_behavior
            """, (category, itype, show_comm, notes or None, fix_ins or None,
                  default_sev or None, mlb))
            # Only audit when something actually changed. Auto-save fires this
            # endpoint on every focus-out; without the diff guard we'd write a
            # no-op event every time the user tabs through the form.
            if before != after:
                _db.record_control_action(
                    con, category=category, issue_type=itype, user=user, action='edit',
                    payload={'before': before, 'after': after},
                    bulk_id=bulk_id,
                )
                events_written += 1
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'updated': len(data), 'audited': events_written})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


# ── Check Management wizard (2026-04-16, Phase 3) ────────────────────────────
# New checks come exclusively through this wizard. email_controls is the
# managed source of truth; write_db() never touches it.

def _get_anthropic_client():
    """Load Anthropic API key and return a client, or (None, error_msg)."""
    try:
        from run_ai_review import load_api_key
        import anthropic
        key = load_api_key()
        if not key:
            return None, 'No API key found — set ANTHROPIC_API_KEY in .env'
        return anthropic.Anthropic(api_key=key), None
    except Exception as e:
        return None, str(e)


@app.route('/api/checks/explanation-preview', methods=['POST'])
@requires_page('controls')
def api_explanation_preview():
    """AI-summarized preview of the Community-Page Explanation for a check.

    Takes the current (possibly-unsaved) fix_instruction text plus a small
    sample of recent open tickets for the (category, issue_type) pair, and returns
    a plain-English summary describing what a community reader would see when
    they land on a ticket of this type.

    Payload: { "category": str, "issue_type": str, "fix_instruction": str }
    Response: { "preview": str } or { "error": str }
    """
    data = request.get_json() or {}
    category  = (data.get('category') or '').strip()
    itype = (data.get('issue_type') or '').strip()
    fix   = (data.get('fix_instruction') or '').strip()
    if not category or not itype:
        return jsonify({'error': 'category and issue_type are required'}), 400

    client, err = _get_anthropic_client()
    if not client:
        return jsonify({'error': err}), 500

    # Gather up to 3 example offending_text samples from real tickets so the AI
    # can describe what the check actually catches, not just what the rule says.
    samples = []
    con, tmp = _read_db_copy()
    try:
        rows = con.execute(
            'SELECT issue_summary, offending_text, severity FROM tickets '
            "WHERE category = ? AND issue_type = ? AND status = 'Open' "
            'ORDER BY date_flagged DESC LIMIT 3',
            (category, itype),
        ).fetchall()
        for r in rows:
            txt = r['offending_text'] or ''
            # offending_text is sometimes stored as a JSON object
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    txt = parsed.get('text', '') or str(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
            samples.append({
                'summary': r['issue_summary'] or '',
                'offending': txt,
                'severity': r['severity'] or '',
            })
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass

    sample_block = '\n'.join(
        f'  - [{s["severity"]}] {s["summary"]}\n    offending: "{s["offending"][:180]}"'
        for s in samples
    ) or '  (no open tickets for this check right now)'

    fix_block = fix if fix else '(none set — the explanation block would be hidden on the community page)'

    prompt = (
        f'You are summarizing the Community-Page Explanation for a QA check so a '
        f'community manager can see what a reader at their location would actually '
        f'see when this check fires on one of their postings.\n\n'
        f'Check:         {category} / {itype}\n\n'
        f'Stored fix_instruction text (what the community page will render):\n'
        f'  """{fix_block}"""\n\n'
        f'Recent real tickets for this check:\n{sample_block}\n\n'
        f'Write a 2-4 sentence plain-English summary describing:\n'
        f'1. What this check is flagging (the pattern or issue it catches).\n'
        f'2. What the community reader sees on their community page when this fires '
        f'(based on the stored fix_instruction text, if any).\n'
        f'3. If the fix_instruction is empty or too generic, say so plainly — do NOT '
        f'invent an explanation.\n\n'
        f'Return ONLY the summary text. No preamble, no bullets, no markdown.'
    )
    try:
        resp = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}],
        )
        return jsonify({'preview': resp.content[0].text.strip()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Issue Type management ─────────────────────────────────────────────────────

@app.route('/api/issue-types/rename', methods=['POST'])
@requires_page('controls')
def api_rename_issue_type():
    """Rename an issue type across all tables. Expects JSON:
    { category, old_issue_type, new_issue_type }
    Updates: email_controls, tickets, pending_tickets, issue_aliases."""
    data = request.get_json() or {}
    category     = (data.get('category') or '').strip()
    old_it   = (data.get('old_issue_type') or '').strip()
    new_it   = (data.get('new_issue_type') or '').strip()
    if not category or not old_it or not new_it:
        return jsonify({'error': 'category, old_issue_type, and new_issue_type are required'}), 400
    if old_it == new_it:
        return jsonify({'ok': True, 'renamed': 0, 'message': 'No change'})

    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        counts = {}
        # Check the target doesn't already exist (would create a duplicate)
        existing = con.execute(
            "SELECT 1 FROM email_controls WHERE category = ? AND issue_type = ?",
            (category, new_it)).fetchone()
        if existing:
            return jsonify({'error': f'Issue type "{new_it}" already exists under {category}. '
                           f'Use merge to combine them.'}), 409

        # Rename in email_controls
        cur = con.execute(
            "UPDATE email_controls SET issue_type = ? WHERE category = ? AND issue_type = ?",
            (new_it, category, old_it))
        counts['email_controls'] = cur.rowcount

        # Rename in tickets
        cur = con.execute(
            "UPDATE tickets SET issue_type = ? WHERE category = ? AND issue_type = ?",
            (new_it, category, old_it))
        counts['tickets'] = cur.rowcount

        # Rename in pending_tickets
        cur = con.execute(
            "UPDATE pending_tickets SET issue_type = ? WHERE category = ? AND issue_type = ?",
            (new_it, category, old_it))
        counts['pending_tickets'] = cur.rowcount

        # Rename in issue_aliases (both alias and canonical sides)
        try:
            con.execute(
                "UPDATE issue_aliases SET alias_issue_type = ? WHERE alias_area = ? AND alias_issue_type = ?",
                (new_it, category, old_it))
            con.execute(
                "UPDATE issue_aliases SET canonical_issue = ? WHERE canonical_area = ? AND canonical_issue = ?",
                (new_it, category, old_it))
        except Exception:
            pass  # aliases table may not exist on older DBs

        # Audit on the NEW pair (entity_id reflects post-rename identity).
        # before/after captures both names; the per-table counts let an
        # auditor see how widely the rename rippled.
        _db.record_control_action(
            con, category=category, issue_type=new_it, user=user, action='rename',
            payload={
                'before': {'category': category, 'issue_type': old_it},
                'after':  {'category': category, 'issue_type': new_it},
                'counts': counts,
            },
        )

        con.commit()
        _write_db_copy(tmp)
        total = sum(counts.values())
        return jsonify({'ok': True, 'renamed': total, 'counts': counts})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/issue-types/mute', methods=['POST'])
@requires_page('controls')
def api_mute_issue_type():
    """Mute a check — view-only filter. The check keeps firing and tickets
    keep being generated; they're just hidden from the Live Tickets page
    and Community pages (Rejected page still shows them). See the internal issue tracker → "Mute / pause toggle" for the original design.

    Two modes, chosen by payload shape:
      - Single pair:  { "category": "Tone", "issue_type": "Resident/Patient Mix", "reason": "..." }
      - Whole category:   { "category": "Tone", "reason": "..." }   (no issue_type)

    Whole-category muting loads all pairs in `email_controls` for the given category
    and mutes them in a single atomic transaction. Unknown pairs / unknown
    areas return 404 without writing.

    Response: { ok: true, muted: N }  (N = number of pairs actually flipped)
    """
    data = request.get_json() or {}
    category   = (data.get('category') or '').strip()
    itype  = (data.get('issue_type') or '').strip()
    reason = (data.get('reason') or '').strip() or None
    if not category:
        return jsonify({'error': 'category is required'}), 400

    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        if itype:
            targets = [(category, itype)]
        else:
            # Whole-category mute: fetch every pair in this category from email_controls
            rows = con.execute(
                'SELECT category, issue_type FROM email_controls WHERE category = ?',
                (category,)).fetchall()
            targets = [(r['category'], r['issue_type']) for r in rows]
            if not targets:
                return jsonify({'error': f'No pairs found for category {category!r}'}), 404

        # Whole-category mutes get a shared bulk_id so the activity ticker can
        # collapse "muted Tone (8 pairs)" instead of listing every pair.
        bulk_id = uuid.uuid4().hex[:12] if not itype and len(targets) > 1 else None
        total = 0
        for a, it in targets:
            rc = _db.set_muted(con, a, it, reason=reason)
            if rc == 0 and itype:
                # Single-pair mode: caller referenced a pair that doesn't exist
                return jsonify({'error': 'Issue type not found in controls'}), 404
            if rc:
                _db.record_control_action(
                    con, category=a, issue_type=it, user=user, action='mute',
                    payload={'reason': reason,
                             'mode': 'whole_area' if not itype else 'single'},
                    bulk_id=bulk_id,
                )
            total += rc
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'muted': total})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/issue-types/unmute', methods=['POST'])
@requires_page('controls')
def api_unmute_issue_type():
    """Un-mute a check. Restores it to the Live Tickets and Community views.
    Previously-generated tickets that were hidden reappear automatically
    (no archive on mute → no un-archive on unmute).

    Two modes, same shape as /api/issue-types/mute:
      - Single pair:  { "category": "Tone", "issue_type": "Resident/Patient Mix" }
      - Whole category:   { "category": "Tone" }   (no issue_type)

    Response: { ok: true, unmuted: N }
    """
    data = request.get_json() or {}
    category   = (data.get('category') or '').strip()
    itype  = (data.get('issue_type') or '').strip()
    if not category:
        return jsonify({'error': 'category is required'}), 400

    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        if itype:
            targets = [(category, itype)]
        else:
            rows = con.execute(
                'SELECT category, issue_type FROM email_controls WHERE category = ?',
                (category,)).fetchall()
            targets = [(r['category'], r['issue_type']) for r in rows]
            if not targets:
                return jsonify({'error': f'No pairs found for category {category!r}'}), 404

        bulk_id = uuid.uuid4().hex[:12] if not itype and len(targets) > 1 else None
        total = 0
        for a, it in targets:
            rc = _db.unmute(con, a, it)
            if rc == 0 and itype:
                return jsonify({'error': 'Issue type not found in controls'}), 404
            if rc:
                _db.record_control_action(
                    con, category=a, issue_type=it, user=user, action='unmute',
                    payload={'mode': 'whole_area' if not itype else 'single'},
                    bulk_id=bulk_id,
                )
            total += rc
        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'unmuted': total})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/issue-types/retire', methods=['POST'])
@requires_page('controls')
def api_retire_issue_type():
    """Retire an issue type. Full Recipe 2 transaction (2026-04-16 Phase 4):
    1. Save current email_controls settings to retired_settings JSON on the
       rejected_issues tombstone (for clean revive later).
    2. DELETE from email_controls.
    3. INSERT tombstone into rejected_issues with reason and date.
    4. Archive all Open + Flagged Incorrectly tickets for this pair.

    Expects JSON: { category, issue_type, reason? }
    Returns: { ok, archived_tickets }
    """
    data = request.get_json() or {}
    category   = (data.get('category') or '').strip()
    itype  = (data.get('issue_type') or '').strip()
    reason = (data.get('reason') or '').strip()
    if not category or not itype:
        return jsonify({'error': 'category and issue_type are required'}), 400

    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        # 1. Read the current email_controls row to preserve settings
        ec_row = con.execute(
            'SELECT * FROM email_controls WHERE category=? AND issue_type=?',
            (category, itype)
        ).fetchone()
        if not ec_row:
            return jsonify({'error': 'Issue type not found in controls'}), 404

        # Build retired_settings JSON
        import json as _json
        settings = {k: ec_row[k] for k in ec_row.keys()
                    if k not in ('category', 'issue_type')}
        settings_json = _json.dumps(settings)

        # 2. DELETE from email_controls
        con.execute(
            'DELETE FROM email_controls WHERE category=? AND issue_type=?',
            (category, itype))

        # 3. INSERT tombstone
        from datetime import datetime
        now = datetime.now().isoformat(timespec='seconds')
        note = f'Retired {now[:10]} via Controls page.'
        if reason:
            note += f' Reason: {reason}'
        con.execute("""
            INSERT OR REPLACE INTO rejected_issues
              (category, issue_type, rejected_at, notes, retired_settings)
            VALUES (?, ?, ?, ?, ?)
        """, (category, itype, now, note, settings_json))

        # 4. Archive Open + Flagged Incorrectly tickets — and audit each one
        # under a shared bulk_id tied to the parent retire event so the trail
        # explains "these N tickets were archived because their check retired."
        archive_note = f'Archived {now[:10]}: {category} / {itype} retired.'
        if reason:
            archive_note += f' ({reason})'
        bulk_id = uuid.uuid4().hex[:12]
        # Snapshot the tickets we're about to archive so we can write per-row
        # audit events with prior_status before the bulk UPDATE clobbers it.
        affected_rows = con.execute(
            "SELECT ticket_id, status FROM tickets "
            "WHERE category = ? AND issue_type = ? "
            "  AND status IN ('Open', 'Flagged Incorrectly')",
            (category, itype),
        ).fetchall()
        cur = con.execute("""
            UPDATE tickets
               SET status = 'Archived',
                   notes  = COALESCE(notes, '') ||
                            CASE WHEN COALESCE(notes, '') = '' THEN '' ELSE ' | ' END ||
                            ?
             WHERE category = ? AND issue_type = ?
               AND status IN ('Open', 'Flagged Incorrectly')
        """, (archive_note, category, itype))
        archived = cur.rowcount
        for ar in affected_rows:
            _db.record_ticket_action(
                con, ticket_id=ar['ticket_id'], user=user, action='archive',
                payload={'prior_status': ar['status'], 'new_status': 'Archived',
                         'reason': f'Retired check: {category} / {itype}'},
                bulk_id=bulk_id,
            )

        # Parent retire event — same bulk_id ties it to the per-ticket archives.
        _db.record_control_action(
            con, category=category, issue_type=itype, user=user, action='retire',
            payload={'reason': reason or None,
                     'archived_tickets': archived,
                     'retired_settings': settings},
            bulk_id=bulk_id,
        )

        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'archived_tickets': archived})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/issue-types/revive', methods=['POST'])
@requires_page('controls')
def api_revive_issue_type():
    """Revive a retired check. Removes the rejected_issues tombstone and
    re-creates the email_controls row from retired_settings JSON (or defaults).
    Does NOT un-archive old tickets — the revived check generates new tickets
    on the next run.

    Expects JSON: { category, issue_type }
    """
    data = request.get_json() or {}
    category  = (data.get('category') or '').strip()
    itype = (data.get('issue_type') or '').strip()
    if not category or not itype:
        return jsonify({'error': 'category and issue_type are required'}), 400

    user = _current_user()
    con, tmp = _read_db_copy()
    try:
        _db.ensure_tables(con)
        # Read the tombstone
        row = con.execute(
            'SELECT retired_settings FROM rejected_issues WHERE category=? AND issue_type=?',
            (category, itype)
        ).fetchone()
        if not row:
            return jsonify({'error': 'Issue type not found in retired checks'}), 404

        # Parse retired_settings if available
        import json as _json
        settings = {}
        if row['retired_settings']:
            try:
                settings = _json.loads(row['retired_settings'])
            except _json.JSONDecodeError:
                pass

        # Re-create email_controls row
        con.execute("""
            INSERT OR IGNORE INTO email_controls
              (category, issue_type, email_setting, show_on_community,
               default_severity, fix_instruction, notes, scope, muted)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ALL', 0)
        """, (
            category, itype,
            settings.get('email_setting', 'Include in emails'),
            settings.get('show_on_community', 1),
            settings.get('default_severity'),
            settings.get('fix_instruction'),
            settings.get('notes', ''),
        ))

        # Remove the tombstone
        con.execute(
            'DELETE FROM rejected_issues WHERE category=? AND issue_type=?',
            (category, itype))

        _db.record_control_action(
            con, category=category, issue_type=itype, user=user, action='revive',
            payload={'restored_settings': settings},
        )

        con.commit()
        _write_db_copy(tmp)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/retired-checks')
@requires_page('controls')
def api_retired_checks():
    """Return all retired checks from rejected_issues, with ticket counts
    and retired_settings for the Retired Checks section on the Controls page."""
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT ri.category, ri.issue_type, ri.rejected_at, ri.notes, ri.retired_settings,
                   (SELECT COUNT(*) FROM tickets t
                    WHERE t.category = ri.category AND t.issue_type = ri.issue_type
                    AND t.status = 'Archived') AS archived_ticket_count,
                   (SELECT COUNT(*) FROM tickets t
                    WHERE t.category = ri.category AND t.issue_type = ri.issue_type
                    AND t.status NOT IN ('Archived','Resolved')) AS open_ticket_count
            FROM rejected_issues ri
            ORDER BY ri.rejected_at DESC, ri.category, ri.issue_type
        """)]
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


# ── Community pages ────────────────────────────────────────────────────────────

@app.route('/api/communities')
@requires_page('communities')
def api_communities():
    """Return [{community, total, critical, high, medium, low}]
    sorted by severity weight (most critical/high open tickets first)."""
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        rows = con.execute("""
            SELECT community,
                   COUNT(*) AS total,
                   SUM(CASE WHEN severity='CRITICAL' THEN 1 ELSE 0 END) AS critical,
                   SUM(CASE WHEN severity='HIGH'     THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN severity='MEDIUM'   THEN 1 ELSE 0 END) AS medium,
                   SUM(CASE WHEN severity='LOW'      THEN 1 ELSE 0 END) AS low
            FROM tickets
            WHERE status NOT IN ('Resolved','Flagged Incorrectly','Archived','Acknowledged')
              AND community IS NOT NULL AND community != ''
              AND NOT EXISTS (
                SELECT 1 FROM email_controls ec_mute
                WHERE ec_mute.muted = 1
                  AND ec_mute.category = tickets.category
                  AND ec_mute.issue_type = tickets.issue_type
              )
            GROUP BY community
            ORDER BY critical DESC, high DESC, medium DESC, low DESC, community ASC
        """).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/api/community/<path:name>/tickets')
@requires_page('communities')
def api_community_tickets(name):
    """Return open tickets for one community, grouped by (category, issue_type),
    filtered to only types that are show_on_community=1."""
    if not os.path.exists(DB):
        return jsonify([])
    con, tmp = _read_db_copy()
    try:
        # Fetch tickets (open only) for this community
        rows = con.execute("""
            SELECT t.ticket_id, t.category, t.issue_type, t.community, t.job_title, t.req_id,
                   t.severity, t.issue_summary, t.offending_text, t.status, t.notes,
                   t.job_url,
                   t.last_action_by, t.last_action_at, t.last_action,
                   ec.fix_instruction, ec.show_on_community, ec.muted
            FROM tickets t
            LEFT JOIN email_controls ec ON ec.category = t.category AND ec.issue_type = t.issue_type
            WHERE t.community = ?
              AND t.status NOT IN ('Resolved','Flagged Incorrectly','Archived','Acknowledged')
            ORDER BY
              CASE t.severity WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
              t.category, t.issue_type, t.ticket_id
        """, (name,)).fetchall()

        # Filter to only show_on_community types (NULL defaults to show) and
        # exclude muted pairs. ec.muted is NULL when the pair isn't in
        # email_controls at all; treat NULL/0 as not-muted.
        #
        # Templates carve-out (2026-04-21, Phase 2 of Root Cause Clusters):
        # tickets in `category='Templates'` are exempt from the mute filter on
        # community-facing pages. Admin captures a template to suppress noise
        # in the Live Tickets view, but the community still needs to see and
        # fix its posting. See CLAUDE.md "Mute Is a Separate Lifecycle State"
        # standing rule, Templates carve-out clause.
        visible = [dict(r) for r in rows
                   if r['show_on_community'] != 0
                   and (not r['muted'] or r['category'] == 'Templates')]

        # Attach job URLs: prefer DB-stored value, fall back to JSON map.
        # Also enrich last_action_by with the user's display name/email so
        # the Phase 3 attribution badge renders without a per-row JOIN.
        url_map = _get_job_url_map()
        umap = _db.get_users_map(
            con, [r.get('last_action_by') for r in visible]
        )
        note_counts = _db.get_note_counts(con, [r.get('ticket_id') for r in visible])
        for r in visible:
            if not r.get('job_url'):
                r['job_url'] = url_map.get(str(r.get('req_id') or ''), '')
            r['check_type'] = f"{r.get('category', '')} — {r.get('issue_type', '')}"
            r['note_count'] = note_counts.get(r.get('ticket_id'), 0)
            uid = r.get('last_action_by')
            u = umap.get(uid) if uid else None
            r['last_action_by_name']  = u['name']  if u else None
            r['last_action_by_email'] = u['email'] if u else None

        # Group by category → issue_type → tickets
        # Prefer category/issue_type from ticket row
        area_groups  = {}   # category → {issue_type → group_dict}
        CROSS        = {'Tone', 'Formatting', 'Content', 'Structure'}  # post-2026-05-21: HTML folded into Formatting
        _sev_rank    = {'CRITICAL': 1, 'HIGH': 2, 'MEDIUM': 3, 'LOW': 4}

        for r in visible:
            ar    = r.get('category') or ''
            itype = r.get('issue_type') or ''
            r['category']        = ar
            r['issue_type']  = itype
            if ar not in area_groups:
                area_groups[ar] = {}
            if itype not in area_groups[ar]:
                area_groups[ar][itype] = {
                    'category':             ar,
                    'issue_type':       itype,
                    'check_type':       r['check_type'],
                    'severity':         r['severity'],
                    'fix_instruction':  r['fix_instruction'] or '',
                    'tickets':          [],
                }
            area_groups[ar][itype]['tickets'].append(r)
            # Track highest severity seen for this group
            existing_rank = _sev_rank.get(area_groups[ar][itype]['severity'], 5)
            new_rank      = _sev_rank.get(r['severity'], 5)
            if new_rank < existing_rank:
                area_groups[ar][itype]['severity'] = r['severity']

        # Build ordered result: sections first (by worst severity), then cross-cutting
        sections_areas = sorted(
            [a for a in area_groups if a not in CROSS],
            key=lambda a: min(_sev_rank.get(g['severity'], 5) for g in area_groups[a].values())
        )
        cross_areas = sorted(
            [a for a in area_groups if a in CROSS],
            key=lambda a: min(_sev_rank.get(g['severity'], 5) for g in area_groups[a].values())
        )
        result = []
        for ar in sections_areas + cross_areas:
            issue_types = sorted(
                area_groups[ar].values(),
                key=lambda g: (_sev_rank.get(g['severity'], 5), g['issue_type'])
            )
            result.append({
                'category':         ar,
                'cross':        ar in CROSS,
                'issue_types':  issue_types,
            })

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        con.close()
        try: os.remove(tmp)
        except OSError: pass


@app.route('/communities')
@requires_page('communities')
def communities():
    return render_template('communities.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header('communities'))


@app.route('/community/<path:name>')
@requires_page('communities')
def community_detail(name):
    return render_template('community_detail.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header('communities'))


@app.route('/')
@login_required
def index():
    user = _current_user()
    # Route users to the first page they have access to. Non-admin viewers
    # typically land on /communities; power users / admins on QA Tools.
    if _db.user_has_page_access(user, 'qa_tools'):
        return render_template('qa_tools.html',
                               nav_css=NAV_CSS,
                               nav_html=nav_header('qa'))
    return redirect(url_for('communities'))

@app.route('/run/full')
@requires_page('ai_check')
def run_full():
    # Full refresh: re-fetch jobs from Hireology. The dashboard reads tickets
    # directly from qa_tickets.db; no downstream build step required.
    cmds = [
        [sys.executable, '-m', 'pip', 'install', 'requests', '-q'],
        [sys.executable, 'fetch_jobs.py'],
    ]
    return Response(stream_with_context(stream_script(cmds)()), mimetype='text/event-stream')

@app.route('/run/ai')
@requires_page('ai_check')
def run_ai():
    # AI Check: fresh fetch → AI review → tickets written straight to DB.
    # run_ai_review.py auto-downgrades --batch to streaming for small runs
    # (< 50 jobs). Pass --force-batch inline to lock it to batch mode.
    cmds = [
        [sys.executable, '-m', 'pip', 'install', 'requests', 'anthropic', '-q'],
        [sys.executable, 'fetch_jobs.py'],
        [sys.executable, 'run_ai_review.py'],
    ]
    return Response(stream_with_context(stream_script(cmds)()), mimetype='text/event-stream')

# ── Auth routes + /users admin page (Migration 2026-04-20) ────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Email + password login. On success redirects to ?next= or /communities."""
    if _current_user():
        return redirect(request.args.get('next') or url_for('communities'))
    error = None
    email = ''
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        pw    = request.form.get('password') or ''
        try:
            con = _db.connect_readonly()
            try:
                user = _db.get_user_by_email(con, email)
            finally:
                con.close()
        except sqlite3.OperationalError:
            user = None
        if not user or not user.get('active') or not check_password_hash(user['password_hash'], pw):
            error = 'Incorrect email or password.'
        else:
            # Touch last_login_at via the safe write path.
            try:
                con, tmp = _read_db_copy()
                _db.ensure_tables(con)
                con.isolation_level = None
                con.execute('BEGIN')
                _db.touch_last_login(con, user['id'])
                con.execute('COMMIT')
                con.close()
                _write_db_copy(tmp)
            except Exception as e:
                print(f'[!] touch_last_login failed: {e}')
            session.clear()
            session.permanent = True
            session['user_id'] = user['id']
            nxt = request.args.get('next') or request.form.get('next')
            if nxt and nxt.startswith('/') and not nxt.startswith('//'):
                return redirect(nxt)
            return redirect(url_for('communities'))
    return render_template(
        'login.html',
        nav_css=NAV_CSS,
        nav_html=nav_header(''),
        error=error,
        email=email,
        next_url=request.args.get('next', ''),
    )


@app.route('/logout')
def logout():
    session.clear()
    flash("You've been logged out.", 'info')
    return redirect(url_for('login'))


@app.route('/users')
@admin_required
def users_page():
    """Admin-only user management page."""
    return render_template(
        'users.html',
        nav_css=NAV_CSS,
        nav_html=nav_header('users'),
        gated_pages=_db.GATED_PAGES,
    )


@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    try:
        con = _db.connect_readonly()
        try:
            _db.ensure_tables(con)
            users = _db.list_users(con)
        finally:
            con.close()
    except sqlite3.OperationalError as e:
        return jsonify({'error': str(e)}), 500
    # Never leak password_hash to the client.
    safe = []
    for u in users:
        u = dict(u)
        u.pop('password_hash', None)
        safe.append(u)
    return jsonify({'users': safe, 'gated_pages': list(_db.GATED_PAGES)})


@app.route('/api/users', methods=['POST'])
@admin_required
def api_create_user():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    name  = (data.get('name') or '').strip()
    pw    = data.get('password') or ''
    is_admin = bool(data.get('is_admin'))
    perms = data.get('page_permissions') or None
    if not email or not pw:
        return jsonify({'error': 'email and password are required'}), 400
    if '@' not in email:
        return jsonify({'error': 'invalid email'}), 400
    if len(pw) < 8:
        return jsonify({'error': 'password must be at least 8 characters'}), 400
    try:
        con, tmp = _read_db_copy()
        _db.ensure_tables(con)
        existing = _db.get_user_by_email(con, email)
        if existing:
            con.close()
            _discard_db_copy(tmp)
            return jsonify({'error': 'a user with that email already exists'}), 409
        con.isolation_level = None
        con.execute('BEGIN')
        new_id = _db.create_user(
            con,
            email=email,
            name=name,
            password_hash=generate_password_hash(pw),
            is_admin=is_admin,
            permissions=perms,
        )
        con.execute('COMMIT')
        con.close()
        _write_db_copy(tmp)
        return jsonify({'ok': True, 'id': new_id})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        _discard_db_copy(tmp)
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>', methods=['PATCH'])
@admin_required
def api_update_user(user_id):
    """Update is_admin / permissions, reset password, or activate/deactivate."""
    data = request.get_json(silent=True) or {}
    me = _current_user()
    try:
        con, tmp = _read_db_copy()
        _db.ensure_tables(con)
        target = _db.get_user_by_id(con, user_id)
        if not target:
            con.close()
            _discard_db_copy(tmp)
            return jsonify({'error': 'user not found'}), 404
        con.isolation_level = None
        con.execute('BEGIN')
        touched = 0
        if 'active' in data:
            new_active = bool(data['active'])
            # Guard: admin can't deactivate themselves (would lock out).
            if me and me['id'] == user_id and not new_active:
                con.execute('ROLLBACK')
                con.close()
                _discard_db_copy(tmp)
                return jsonify({'error': "you can't deactivate yourself"}), 400
            _db.set_user_active(con, user_id, new_active)
            touched += 1
        if 'is_admin' in data or 'page_permissions' in data:
            is_admin = data.get('is_admin', target['is_admin'])
            perms    = data.get('page_permissions', target['page_permissions'])
            # Guard: admin can't demote themselves (would lock out of /users).
            if me and me['id'] == user_id and target['is_admin'] and not is_admin:
                con.execute('ROLLBACK')
                con.close()
                _discard_db_copy(tmp)
                return jsonify({'error': "you can't remove your own admin role"}), 400
            _db.update_user_permissions(
                con, user_id, is_admin=is_admin, permissions=perms,
            )
            touched += 1
        if 'password' in data:
            pw = data['password'] or ''
            if len(pw) < 8:
                con.execute('ROLLBACK')
                con.close()
                _discard_db_copy(tmp)
                return jsonify({'error': 'password must be at least 8 characters'}), 400
            _db.set_user_password(con, user_id, generate_password_hash(pw))
            touched += 1
        con.execute('COMMIT')
        con.close()
        if touched:
            _write_db_copy(tmp)
        else:
            _discard_db_copy(tmp)
        return jsonify({'ok': True, 'touched': touched})
    except Exception as e:
        try: con.execute('ROLLBACK')
        except Exception: pass
        try: con.close()
        except Exception: pass
        try: _discard_db_copy(tmp)
        except Exception: pass
        return jsonify({'error': str(e)}), 500


@app.route('/healthz')
def healthz():
    """Liveness + DB reachability probe for load balancers and orchestrators.

    Unauthenticated by design — Fly.io, Azure App Service, and a Postgres
    sidecar's compose healthcheck all need to hit it without credentials.
    Returns 200 {"status":"ok"} when a trivial SELECT succeeds on the
    configured DATABASE_URL, else 503 {"status":"degraded","error":...}
    so the platform can pull a bad instance out of rotation.

    Intentionally minimal — no session touches, no `ensure_tables`, no
    user lookup. This endpoint must stay fast even if the rest of the
    app is sick.
    """
    try:
        con = _db.connect_readonly()
        try:
            con.execute('SELECT 1').fetchone()
        finally:
            con.close()
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'status': 'degraded', 'error': str(e)}), 503


@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header(''),
                           error_code=404,
                           error_message="This page doesn't exist. It may have been moved or the URL might be wrong."), 404

@app.errorhandler(500)
def internal_error(e):
    return render_template('error.html',
                           nav_css=NAV_CSS,
                           nav_html=nav_header(''),
                           error_code=500,
                           error_message="Something went wrong on our end. Try refreshing, or head back to QA Tools."), 500

# Run the admin-seed check at module-import time so it fires under gunicorn
# too — not just when `python qa_dashboard.py` executes the __main__ block.
# Before this was lifted out of __main__, containerized deploys booted with
# an empty users table and nobody could log in. Idempotent: no-op if the DB
# doesn't exist yet (dev pre-stamp) or if users already has rows.
#
# Runs once per worker process. With gunicorn's default fork-without-preload,
# multiple workers may race — but create_user() hits a UNIQUE constraint on
# email so only the first wins; subsequent races hit the try/except in
# _bootstrap_admin_if_empty and log a warning, which is harmless.
_ensure_pending_table()
_bootstrap_admin_if_empty()


if __name__ == '__main__':
    # Bring schema to head. On an existing pre-Alembic DB this stamps the
    # baseline revision without replaying it; on a fresh DB it runs the
    # initial revision to create every table. See db.stamp_or_upgrade().
    # Only the parent reloader process runs this — the child reload cycle
    # re-imports the module and we don't need to re-stamp on every reload.
    # In containers, Alembic runs via the release_command + entrypoint.sh,
    # so this block never fires there.
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        try:
            _db.stamp_or_upgrade()
        except Exception as _e:
            print(f'[!] alembic stamp_or_upgrade failed: {_e}')
    # Re-seed on dev first-boot: stamp_or_upgrade above just created the DB,
    # so the module-import-time bootstrap above was a no-op. Call again.
    _bootstrap_admin_if_empty()
    # Flask reloader architecture:
    #   Parent process  — monitors files for changes, never restarts.
    #   Child process   — the actual server; killed & respawned on every file change.
    #                     Identified by WERKZEUG_RUN_MAIN='true'.
    #
    # We open the browser from the PARENT (runs once) so file-change reloads
    # never spawn new tabs. A short Timer delay lets the child start listening
    # before the browser tries to connect.
    # Auto-open the browser only in dev AND only from the parent reloader
    # process. Production servers (gunicorn in Docker) never hit this block.
    if not IS_PRODUCTION and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        import threading
        browser_host = 'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST
        threading.Timer(1.5, lambda: webbrowser.open(f'http://{browser_host}:{PORT}')).start()
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=True)
