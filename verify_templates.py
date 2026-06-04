"""
verify_templates.py — End-to-end smoke test for the Templates Category
plumbing (Root Cause Clusters feature, 2026-04-21).

Runs in a throwaway temp SQLite DB. Does NOT touch the live qa_tickets.db.
Safe to run anytime. Prints pass/fail for each check; exits non-zero on any
failure so CI-style invocation works.

Phase 1 checks (routing plumbing):
  1. Schema migration — create_fresh_db_schema + ensure_tables produce both
     new columns (email_controls.template_pattern, tickets.captured_from).
  2. Normalizer — taxonomy.normalize_template_pattern trims + collapses
     whitespace deterministically.
  3. Matcher — taxonomy.match_template returns the canonical pair on exact
     match, None on miss, None on empty input.
  4. db.get_template_patterns — round-trips a captured template from
     email_controls into the routing map.
  5. CLAUDE path — run_ai_review.route_and_write rewrites a matching finding
     to the Templates pair and sets captured_from; non-matching findings
     route normally.
  6. AUTO path — run_ai_review.write_prescan_tickets rewrites a matching
     AUTO finding the same way.
  7. Retirement fall-through — when the template_pattern is cleared from
     email_controls, the previously-matched offending_text falls through to
     its natural routing (no Templates rewrite).
  8. AI-safety reject — make_ticket_row called with category='Templates'
     coerces to 'Content' (CRITICAL RULE 19 belt-and-suspenders).

Phase 2 checks (capture flow):
  9. db.capture_template — creates email_controls row (muted=1), rewrites
     eligible tickets, skips terminal-status tickets, populates
     captured_from, severity inherits highest-wins.
 10. Name collision — a second capture with the same template_name raises
     ValueError.
 11. Severity inheritance — highest-wins ranking across mixed severities
     (LOW + MEDIUM + HIGH -> HIGH; MEDIUM + CRITICAL -> CRITICAL).
 12. Community carve-out filter — replicates the /api/community/<name>/tickets
     inline filter: muted Templates rows do NOT hide their tickets from
     community pages, even though muted non-Templates rows do.

Phase 3 checks (templates management page):
 13. list_captured_templates — returns one dict per Templates-Category row
     with correct open/resolved/total counts + age_days.
 14. rename_template — atomic across email_controls + tickets; empty
     new_name / same-name / collision / missing source all raise
     ValueError.
 15. retire_template — snapshots email_controls row into
     rejected_issues.retired_settings JSON; DELETEs the live row; tickets
     stay with their (Templates, name) pair by default; with
     retire_tickets=True, Open tickets are resolved in the same
     transaction.
 16. resolve_template_tickets — resolves only Open tickets for the named
     template; leaves Resolved / Acknowledged / Flagged / Archived alone.

Usage:
    python -X utf8 verify_templates.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date


# Keep these imports AFTER sys.path manipulation so running from elsewhere works.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db as _db              # noqa: E402
import taxonomy as _tax       # noqa: E402
import run_ai_review as _rar  # noqa: E402


_PASS = 0
_FAIL = 0


def _check(label: str, ok: bool, detail: str = '') -> None:
    global _PASS, _FAIL
    mark = 'PASS' if ok else 'FAIL'
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    print(f'  [{mark}] {label}' + (f'  --  {detail}' if detail else ''))


def _fresh_db() -> tuple[sqlite3.Connection, str]:
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.row_factory = sqlite3.Row
    _db.create_fresh_db_schema(con)
    _db.ensure_tables(con)
    return con, tmp.name


def _seed_registered_pair(con: sqlite3.Connection, category: str, itype: str,
                          severity: str = 'MEDIUM',
                          template_pattern: str | None = None) -> None:
    con.execute("""
        INSERT OR IGNORE INTO email_controls
          (category, issue_type, email_setting, show_on_community,
           default_severity, fix_instruction, notes, scope, template_pattern)
        VALUES (?, ?, 'Include in emails', 1, ?, '', '', 'ALL', ?)
    """, (category, itype, severity, template_pattern))


# ─── Checks ──────────────────────────────────────────────────────────────────

def check_1_schema() -> None:
    print('\n[1] Schema migration')
    con, path = _fresh_db()
    try:
        ec_cols = {r[1] for r in con.execute('PRAGMA table_info(email_controls)').fetchall()}
        t_cols  = {r[1] for r in con.execute('PRAGMA table_info(tickets)').fetchall()}
        _check('email_controls.template_pattern present', 'template_pattern' in ec_cols)
        _check('tickets.captured_from present',           'captured_from' in t_cols)
    finally:
        con.close()
        os.unlink(path)


def check_2_normalizer() -> None:
    print('\n[2] Normalizer (taxonomy.normalize_template_pattern)')
    n = _tax.normalize_template_pattern
    _check('trims leading/trailing whitespace',
           n('   APPLY NOW!   ') == 'APPLY NOW!',
           f"got {n('   APPLY NOW!   ')!r}")
    _check('collapses internal whitespace runs',
           n('APPLY    NOW!') == 'APPLY NOW!',
           f"got {n('APPLY    NOW!')!r}")
    _check('handles tabs + newlines',
           n('APPLY\t\nNOW!') == 'APPLY NOW!',
           f"got {n('APPLY\t\nNOW!')!r}")
    _check('case-sensitive (no lowercasing)',
           n('Apply Now!') == 'Apply Now!',
           f"got {n('Apply Now!')!r}")
    _check('empty input returns empty string',
           n('') == '' and n(None) == '',
           f"got {n('')!r} and {n(None)!r}")


def check_3_matcher() -> None:
    print('\n[3] Matcher (taxonomy.match_template)')
    tmap = {'APPLY NOW!': ('Templates', 'Apply Now greeting')}
    _check('exact match returns pair',
           _tax.match_template('APPLY NOW!', tmap) == ('Templates', 'Apply Now greeting'))
    _check('whitespace variation still matches (normalized)',
           _tax.match_template('  APPLY   NOW!  ', tmap) == ('Templates', 'Apply Now greeting'))
    _check('non-match returns None',
           _tax.match_template('Hire us today!', tmap) is None)
    _check('empty input returns None',
           _tax.match_template('', tmap) is None and _tax.match_template(None, tmap) is None)
    _check('empty map returns None',
           _tax.match_template('APPLY NOW!', {}) is None)
    _check('case difference is NOT a match (case-sensitive per the maintainer 2026-04-21)',
           _tax.match_template('apply now!', tmap) is None)


def check_4_get_template_patterns() -> None:
    print('\n[4] db.get_template_patterns round-trip')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Templates', 'Apply Now greeting',
                              severity='MEDIUM', template_pattern='APPLY NOW!')
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points',
                              severity='MEDIUM', template_pattern=None)
        con.commit()
        tmap = _db.get_template_patterns(con)
        _check('map contains the captured pattern',
               'APPLY NOW!' in tmap and tmap['APPLY NOW!'] == ('Templates', 'Apply Now greeting'),
               f'got {tmap!r}')
        _check('non-template rows are excluded',
               all(v[0] == 'Templates' for v in tmap.values()),
               f'got {tmap!r}')
    finally:
        con.close()
        os.unlink(path)


def check_5_claude_path_rewrite() -> None:
    print('\n[5] CLAUDE path (run_ai_review.route_and_write)')
    con, path = _fresh_db()
    try:
        # Register the natural pair and the Templates pair both.
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        _seed_registered_pair(con, 'Templates', 'Apply Now greeting',
                              'MEDIUM', template_pattern='APPLY NOW!')
        con.commit()

        _rar.set_template_map(_db.get_template_patterns(con))

        today = str(date.today())
        match_issue = {
            'category':     'Tone',
            'issue_type':   'Excessive Exclamation Points',
            'severity':     'MEDIUM',
            'issue_summary':'Excessive use of exclamation in apply-now line.',
            'offending_text':'APPLY NOW!',
        }
        nonmatch_issue = {
            'category':     'Tone',
            'issue_type':   'Excessive Exclamation Points',
            'severity':     'MEDIUM',
            'issue_summary':'Different exclamation issue.',
            'offending_text':'Some unrelated exclamation text!!!',
        }
        rows = [
            _rar.make_ticket_row('QA-T001', today, 'req1', 'Cook', 'Test Community', match_issue),
            _rar.make_ticket_row('QA-T002', today, 'req2', 'Cook', 'Test Community', nonmatch_issue),
        ]
        known = {('Tone', 'Excessive Exclamation Points'),
                 ('Templates', 'Apply Now greeting')}
        _rar.route_and_write(con, rows, known)
        con.commit()

        fetched = {r[0]: r for r in con.execute(
            "SELECT ticket_id, category, issue_type, captured_from "
            "FROM tickets ORDER BY ticket_id"
        ).fetchall()}

        t001 = fetched.get('QA-T001')
        t002 = fetched.get('QA-T002')
        _check('matching ticket routed to Templates',
               t001 is not None and t001['category'] == 'Templates'
                 and t001['issue_type'] == 'Apply Now greeting',
               f'got {dict(t001) if t001 else None}')
        _check('matching ticket has captured_from set',
               t001 is not None and t001['captured_from'] == 'Tone / Excessive Exclamation Points',
               f'got {t001["captured_from"] if t001 else None!r}')
        _check('non-matching ticket retains natural pair',
               t002 is not None and t002['category'] == 'Tone'
                 and t002['issue_type'] == 'Excessive Exclamation Points'
                 and (t002['captured_from'] is None or t002['captured_from'] == ''),
               f'got {dict(t002) if t002 else None}')
    finally:
        _rar.set_template_map({})
        con.close()
        os.unlink(path)


def check_6_auto_path_rewrite() -> None:
    print('\n[6] AUTO path (run_ai_review.write_prescan_tickets)')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Body Text in ALL CAPS', 'MEDIUM')
        _seed_registered_pair(con, 'Templates', 'Urgency Caps Template',
                              'MEDIUM', template_pattern='MUST APPLY IMMEDIATELY')
        con.commit()

        _rar.set_template_map(_db.get_template_patterns(con))

        today = str(date.today())
        issues = [
            {'category':'Tone', 'issue_type':'Body Text in ALL CAPS', 'severity':'MEDIUM',
             'issue_summary':'ALL CAPS emphasis.',
             'offending_text':'MUST APPLY IMMEDIATELY'},
            {'category':'Tone', 'issue_type':'Body Text in ALL CAPS', 'severity':'MEDIUM',
             'issue_summary':'Different ALL CAPS emphasis.',
             'offending_text':'GREAT OPPORTUNITY'},
        ]
        _rar.write_prescan_tickets(con, today, 'req3', 'Nurse', 'Test Community',
                                   issues, 0, job_url='http://example.test')
        con.commit()

        rows = list(con.execute(
            "SELECT ticket_id, category, issue_type, captured_from "
            "FROM tickets ORDER BY ticket_id"
        ).fetchall())
        _check('AUTO matcher rewrote first finding to Templates',
               any(r['category'] == 'Templates' and r['issue_type'] == 'Urgency Caps Template'
                   and r['captured_from'] == 'Tone / Body Text in ALL CAPS'
                   for r in rows),
               f'got {[dict(r) for r in rows]}')
        _check('non-matching AUTO finding retains natural pair',
               any(r['category'] == 'Tone' and r['issue_type'] == 'Body Text in ALL CAPS'
                   and (r['captured_from'] is None or r['captured_from'] == '')
                   for r in rows),
               f'got {[dict(r) for r in rows]}')
    finally:
        _rar.set_template_map({})
        con.close()
        os.unlink(path)


def check_7_retirement_fallthrough() -> None:
    print('\n[7] Retirement fall-through (template removed -> natural routing)')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        _seed_registered_pair(con, 'Templates', 'Apply Now greeting',
                              'MEDIUM', template_pattern='APPLY NOW!')
        con.commit()

        # Simulate retirement: clear the template_pattern. (Deletion of the
        # row has the same effect since get_template_patterns only returns
        # rows with non-null pattern.)
        con.execute("""UPDATE email_controls
                         SET template_pattern = NULL
                       WHERE category = 'Templates'""")
        con.commit()

        _rar.set_template_map(_db.get_template_patterns(con))

        today = str(date.today())
        issue = {
            'category':     'Tone',
            'issue_type':   'Excessive Exclamation Points',
            'severity':     'MEDIUM',
            'issue_summary':'APPLY NOW pattern that USED to match a template.',
            'offending_text':'APPLY NOW!',
        }
        row = _rar.make_ticket_row('QA-T900', today, 'req9', 'Cook', 'Test Community', issue)
        known = {('Tone', 'Excessive Exclamation Points'),
                 ('Templates', 'Apply Now greeting')}
        _rar.route_and_write(con, [row], known)
        con.commit()

        r = con.execute("SELECT category, issue_type, captured_from "
                        "FROM tickets WHERE ticket_id = 'QA-T900'").fetchone()
        _check('former-match ticket routes to natural pair',
               r is not None and r['category'] == 'Tone'
                 and r['issue_type'] == 'Excessive Exclamation Points',
               f'got {dict(r) if r else None}')
        _check('former-match ticket has NO captured_from',
               r is not None and (r['captured_from'] is None or r['captured_from'] == ''),
               f'got {r["captured_from"] if r else None!r}')
    finally:
        _rar.set_template_map({})
        con.close()
        os.unlink(path)


def _seed_ticket(con, ticket_id, category, issue_type, severity='MEDIUM',
                 status='Open', offending_text='', community='Test Community',
                 req_id='req-test', job_title='Tester'):
    con.execute("""
        INSERT OR REPLACE INTO tickets
          (ticket_id, date_flagged, req_id, job_title, community,
           severity, category, issue_type, issue_summary, offending_text,
           detected_by, status, notes, scope, job_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'AUTO', ?, '', 'ALL', '')
    """, (ticket_id, str(date.today()), req_id, job_title, community,
          severity, category, issue_type, 'Synthetic finding', offending_text,
          status))


def check_9_capture_helper() -> None:
    print('\n[9] db.capture_template round-trip')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        for i in range(5):
            _seed_ticket(con, f'QA-C{i:03d}', 'Tone', 'Excessive Exclamation Points',
                         severity='MEDIUM', offending_text='APPLY NOW!')
        _seed_ticket(con, 'QA-C999', 'Tone', 'Excessive Exclamation Points',
                     severity='MEDIUM', status='Resolved', offending_text='APPLY NOW!')
        con.commit()

        pair, rewritten, sev = _db.capture_template(
            con, pattern='APPLY NOW!', template_name='Apply Now greeting',
            ticket_ids=[f'QA-C{i:03d}' for i in range(5)] + ['QA-C999'],
            created_by_user_id=42,
        )
        con.commit()

        _check('capture returns correct pair',
               pair == ('Templates', 'Apply Now greeting'), f'got {pair!r}')
        _check('rewrites only the 5 eligible tickets (Resolved skipped)',
               rewritten == 5, f'got {rewritten}')
        _check('inherited_severity is MEDIUM (all eligible were MEDIUM)',
               sev == 'MEDIUM', f'got {sev!r}')

        ec = con.execute(
            "SELECT default_severity, muted, muted_reason, template_pattern, "
            "       fix_instruction, show_on_community, email_setting "
            "FROM email_controls WHERE category='Templates' AND issue_type=?",
            ('Apply Now greeting',),
        ).fetchone()
        _check('email_controls row created', ec is not None)
        if ec:
            _check('row is muted=1',
                   ec['muted'] == 1, f"got muted={ec['muted']}")
            _check('muted_reason set',
                   (ec['muted_reason'] or '').startswith('Captured'),
                   f"got muted_reason={ec['muted_reason']!r}")
            _check('template_pattern stored raw',
                   ec['template_pattern'] == 'APPLY NOW!',
                   f"got template_pattern={ec['template_pattern']!r}")
            _check('default_severity = MEDIUM (highest-wins across eligible)',
                   ec['default_severity'] == 'MEDIUM',
                   f"got default_severity={ec['default_severity']!r}")
            _check('show_on_community = 1 (community still sees these)',
                   ec['show_on_community'] == 1,
                   f"got show_on_community={ec['show_on_community']}")

        rewritten_rows = list(con.execute(
            "SELECT ticket_id, category, issue_type, captured_from, status "
            "FROM tickets WHERE ticket_id LIKE 'QA-C%' ORDER BY ticket_id"
        ).fetchall())
        open_rows     = [r for r in rewritten_rows if r['status'] == 'Open']
        resolved_rows = [r for r in rewritten_rows if r['status'] == 'Resolved']
        _check('5 open tickets now on Templates pair',
               all(r['category'] == 'Templates' and r['issue_type'] == 'Apply Now greeting'
                   for r in open_rows) and len(open_rows) == 5,
               f'got {[dict(r) for r in open_rows]}')
        _check('5 open tickets have captured_from set',
               all(r['captured_from'] == 'Tone / Excessive Exclamation Points'
                   for r in open_rows),
               f'got {[r["captured_from"] for r in open_rows]}')
        _check('1 Resolved ticket left on original pair',
               len(resolved_rows) == 1
                 and resolved_rows[0]['category'] == 'Tone'
                 and resolved_rows[0]['issue_type'] == 'Excessive Exclamation Points'
                 and (resolved_rows[0]['captured_from'] is None
                      or resolved_rows[0]['captured_from'] == ''),
               f'got {[dict(r) for r in resolved_rows]}')
    finally:
        con.close()
        os.unlink(path)


def check_10_name_collision() -> None:
    print('\n[10] db.capture_template name-collision rejection')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        _seed_ticket(con, 'QA-X001', 'Tone', 'Excessive Exclamation Points',
                     offending_text='APPLY NOW!')
        con.commit()

        _db.capture_template(con, pattern='APPLY NOW!',
                             template_name='Greeting X',
                             ticket_ids=['QA-X001'])
        con.commit()

        raised = False
        msg = ''
        try:
            _db.capture_template(con, pattern='SOMETHING ELSE',
                                 template_name='Greeting X',
                                 ticket_ids=['QA-X001'])
        except ValueError as e:
            raised = True
            msg = str(e)
        _check('collision raises ValueError', raised,
               'second capture did not raise')
        _check('ValueError mentions duplicate name',
               raised and 'already exists' in msg, f'got {msg!r}')
    finally:
        con.close()
        os.unlink(path)


def check_11_severity_inheritance() -> None:
    print('\n[11] Severity inheritance (highest-wins)')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Content', 'Pay Details', 'MEDIUM')
        _seed_ticket(con, 'QA-S001', 'Content', 'Pay Details',
                     severity='LOW', offending_text='PAY TEMPLATE')
        _seed_ticket(con, 'QA-S002', 'Content', 'Pay Details',
                     severity='HIGH', offending_text='PAY TEMPLATE')
        _seed_ticket(con, 'QA-S003', 'Content', 'Pay Details',
                     severity='MEDIUM', offending_text='PAY TEMPLATE')
        con.commit()

        _pair, _n, sev = _db.capture_template(
            con, pattern='PAY TEMPLATE', template_name='Pay block v1',
            ticket_ids=['QA-S001', 'QA-S002', 'QA-S003'],
        )
        con.commit()
        _check('LOW + MEDIUM + HIGH inherits HIGH',
               sev == 'HIGH', f'got {sev!r}')

        _seed_ticket(con, 'QA-S004', 'Content', 'Pay Details',
                     severity='CRITICAL', offending_text='PAY TEMPLATE 2')
        _seed_ticket(con, 'QA-S005', 'Content', 'Pay Details',
                     severity='MEDIUM', offending_text='PAY TEMPLATE 2')
        con.commit()
        _pair, _n, sev = _db.capture_template(
            con, pattern='PAY TEMPLATE 2', template_name='Pay block v2',
            ticket_ids=['QA-S004', 'QA-S005'],
        )
        _check('MEDIUM + CRITICAL inherits CRITICAL',
               sev == 'CRITICAL', f'got {sev!r}')
    finally:
        con.close()
        os.unlink(path)


def check_12_community_carve_out() -> None:
    print('\n[12] Community-page carve-out (muted Templates still visible)')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        _seed_registered_pair(con, 'Tone', 'Other Muted Check', 'MEDIUM')
        con.execute("UPDATE email_controls SET muted = 1 "
                    "WHERE category='Tone' AND issue_type='Other Muted Check'")
        _seed_ticket(con, 'QA-CO001', 'Tone', 'Excessive Exclamation Points',
                     offending_text='APPLY NOW!', community='Alpha')
        _seed_ticket(con, 'QA-CO002', 'Tone', 'Other Muted Check',
                     offending_text='GREAT PLACE', community='Alpha')
        con.commit()

        _db.capture_template(con, pattern='APPLY NOW!',
                             template_name='Community test template',
                             ticket_ids=['QA-CO001'])
        con.commit()

        rows = list(con.execute("""
            SELECT t.ticket_id, t.category, t.issue_type, ec.muted,
                   ec.show_on_community
              FROM tickets t
              LEFT JOIN email_controls ec
                ON ec.category = t.category AND ec.issue_type = t.issue_type
             WHERE t.community = 'Alpha'
               AND t.status NOT IN ('Resolved','Flagged Incorrectly','Archived','Acknowledged')
        """).fetchall())
        visible = [r for r in rows
                   if (r['show_on_community'] or 0) != 0
                   and (not r['muted'] or r['category'] == 'Templates')]
        visible_ids = {r['ticket_id'] for r in visible}
        _check('captured-into-Templates ticket visible on community page '
               '(despite muted row)',
               'QA-CO001' in visible_ids, f'visible={visible_ids}')
        _check('non-Templates muted ticket hidden on community page',
               'QA-CO002' not in visible_ids, f'visible={visible_ids}')
    finally:
        con.close()
        os.unlink(path)


def check_13_list_templates() -> None:
    print('\n[13] db.list_captured_templates round-trip')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        # Capture a template with 3 Open + 1 Resolved (resolved is ineligible
        # at capture time, so it stays on the original pair).
        for i in range(3):
            _seed_ticket(con, f'QA-L{i:03d}', 'Tone', 'Excessive Exclamation Points',
                         offending_text='APPLY NOW!')
        con.commit()
        _db.capture_template(con, pattern='APPLY NOW!', template_name='Apply Now',
                             ticket_ids=[f'QA-L{i:03d}' for i in range(3)])
        con.commit()
        # Now resolve one of the rewritten tickets to exercise open/resolved counts
        con.execute("UPDATE tickets SET status = 'Resolved' WHERE ticket_id = 'QA-L000'")
        con.commit()

        rows = _db.list_captured_templates(con)
        _check('returns one row per template', len(rows) == 1, f'got {len(rows)}')
        r = rows[0] if rows else {}
        _check('name matches',        r.get('name') == 'Apply Now', f'got {r.get("name")!r}')
        _check('pattern matches',     r.get('pattern') == 'APPLY NOW!', f'got {r.get("pattern")!r}')
        _check('severity inherited',  r.get('severity') == 'MEDIUM', f'got {r.get("severity")!r}')
        _check('muted true',          r.get('muted') is True, f'got {r.get("muted")!r}')
        _check('open_count correct',  r.get('open_count') == 2, f'got {r.get("open_count")!r}')
        _check('resolved_count correct', r.get('resolved_count') == 1, f'got {r.get("resolved_count")!r}')
        _check('total_count correct', r.get('total_count') == 3, f'got {r.get("total_count")!r}')
        _check('age_days is 0 (captured today)',
               r.get('age_days') == 0, f'got {r.get("age_days")!r}')
    finally:
        con.close()
        os.unlink(path)


def check_14_rename_template() -> None:
    print('\n[14] db.rename_template atomic + validation')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        _seed_ticket(con, 'QA-R001', 'Tone', 'Excessive Exclamation Points',
                     offending_text='APPLY NOW!')
        con.commit()
        _db.capture_template(con, pattern='APPLY NOW!', template_name='Greeting v1',
                             ticket_ids=['QA-R001'])
        con.commit()

        # Happy path: rename to a new name
        ec_rows, t_rows = _db.rename_template(con, 'Greeting v1', 'Greeting v2')
        con.commit()
        _check('rename updates 1 email_controls row',
               ec_rows == 1, f'got {ec_rows}')
        _check('rename updates 1 ticket row',
               t_rows == 1, f'got {t_rows}')
        check_row = con.execute(
            "SELECT 1 FROM email_controls WHERE category='Templates' AND issue_type='Greeting v2'"
        ).fetchone()
        _check('email_controls row exists at new name',
               check_row is not None)
        ticket_row = con.execute(
            "SELECT issue_type FROM tickets WHERE ticket_id = 'QA-R001'"
        ).fetchone()
        _check('ticket issue_type updated to new name',
               ticket_row and ticket_row['issue_type'] == 'Greeting v2',
               f'got {ticket_row["issue_type"] if ticket_row else None!r}')

        # Error path: empty new_name
        raised = False
        try: _db.rename_template(con, 'Greeting v2', '')
        except ValueError: raised = True
        _check('empty new_name raises', raised)

        # Error path: same-name no-op
        raised = False
        try: _db.rename_template(con, 'Greeting v2', 'Greeting v2')
        except ValueError: raised = True
        _check('same-name raises', raised)

        # Error path: collision
        _seed_registered_pair(con, 'Tone', 'Other check', 'MEDIUM')
        _seed_ticket(con, 'QA-R002', 'Tone', 'Other check',
                     offending_text='GREAT TEAM')
        con.commit()
        _db.capture_template(con, pattern='GREAT TEAM', template_name='Other Template',
                             ticket_ids=['QA-R002'])
        con.commit()
        raised = False
        msg = ''
        try: _db.rename_template(con, 'Greeting v2', 'Other Template')
        except ValueError as e:
            raised = True
            msg = str(e)
        _check('collision raises', raised)
        _check('collision msg mentions duplicate',
               raised and 'already exists' in msg, f'got {msg!r}')

        # Error path: missing source
        raised = False
        try: _db.rename_template(con, 'Does Not Exist', 'Newest Name')
        except ValueError: raised = True
        _check('missing source raises', raised)
    finally:
        con.close()
        os.unlink(path)


def check_15_retire_template() -> None:
    print('\n[15] db.retire_template + retired_settings snapshot')
    con, path = _fresh_db()
    try:
        import json as _json

        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        for i in range(4):
            # First 3 are Open, 4th is Resolved (already terminal)
            status = 'Open' if i < 3 else 'Resolved'
            _seed_ticket(con, f'QA-Z{i:03d}', 'Tone', 'Excessive Exclamation Points',
                         severity='HIGH', status=status, offending_text='STAR PATTERN')
        con.commit()
        _db.capture_template(con, pattern='STAR PATTERN', template_name='Star block',
                             ticket_ids=[f'QA-Z{i:03d}' for i in range(4)])
        con.commit()
        # Note: only the 3 Open ones get rewritten into Templates (per capture
        # eligibility filter); the Resolved one stays on Tone / Excl Exc Pts.

        # Simple retire (no bulk-resolve)
        pair, resolved = _db.retire_template(con, 'Star block')
        con.commit()
        _check('retire returns the correct pair',
               pair == ('Templates', 'Star block'), f'got {pair!r}')
        _check('no tickets resolved when retire_tickets=False',
               resolved == 0, f'got {resolved}')
        ec_row = con.execute(
            "SELECT 1 FROM email_controls WHERE category='Templates' AND issue_type='Star block'"
        ).fetchone()
        _check('email_controls row deleted', ec_row is None)
        ri_row = con.execute(
            "SELECT rejected_at, notes, retired_settings FROM rejected_issues "
            "WHERE category='Templates' AND issue_type='Star block'"
        ).fetchone()
        _check('rejected_issues tombstone present', ri_row is not None)
        _check('retired_settings JSON present + parseable',
               ri_row and ri_row['retired_settings']
                 and _json.loads(ri_row['retired_settings']).get('template_pattern') == 'STAR PATTERN',
               f'got {ri_row["retired_settings"] if ri_row else None!r}')
        ticket_check = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE category='Templates' AND issue_type='Star block'"
        ).fetchone()[0]
        _check('tickets preserved on (Templates, Star block) pair',
               ticket_check == 3, f'got {ticket_check} rows')

        # Second retire with retire_tickets=True
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        for i in range(3):
            _seed_ticket(con, f'QA-ZZ{i:03d}', 'Tone', 'Excessive Exclamation Points',
                         severity='HIGH', offending_text='HIRE NOW')
        con.commit()
        _db.capture_template(con, pattern='HIRE NOW', template_name='Hire block',
                             ticket_ids=[f'QA-ZZ{i:03d}' for i in range(3)])
        con.commit()

        _pair, resolved = _db.retire_template(con, 'Hire block', retire_tickets=True)
        con.commit()
        _check('retire_tickets=True resolves the open tickets',
               resolved == 3, f'got {resolved}')
        open_after = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE category='Templates' "
            "AND issue_type='Hire block' AND status='Open'"
        ).fetchone()[0]
        _check('no Open tickets remain after retire_tickets=True',
               open_after == 0, f'got {open_after}')

        # Error path: retire non-existent
        raised = False
        try: _db.retire_template(con, 'No Such Template')
        except ValueError: raised = True
        _check('retire non-existent raises', raised)
    finally:
        con.close()
        os.unlink(path)


def check_16_resolve_template_tickets() -> None:
    print('\n[16] db.resolve_template_tickets — only Open statuses')
    con, path = _fresh_db()
    try:
        _seed_registered_pair(con, 'Tone', 'Excessive Exclamation Points', 'MEDIUM')
        # Mixed: 3 Open + 1 Resolved + 1 Acknowledged + 1 Flagged Incorrectly
        statuses = [('Open', 3), ('Resolved', 1), ('Acknowledged', 1),
                    ('Flagged Incorrectly', 1)]
        tid = 0
        for status, n in statuses:
            for _ in range(n):
                _seed_ticket(con, f'QA-RT{tid:03d}', 'Tone', 'Excessive Exclamation Points',
                             status=status, offending_text='MIX PATTERN')
                tid += 1
        con.commit()
        # Capture only picks Open (3) per eligibility filter
        _db.capture_template(con, pattern='MIX PATTERN', template_name='Mix block',
                             ticket_ids=[f'QA-RT{i:03d}' for i in range(tid)])
        con.commit()

        # Now call resolve_template_tickets
        n = _db.resolve_template_tickets(con, 'Mix block')
        con.commit()
        _check('resolves exactly the 3 Open tickets',
               n == 3, f'got {n}')
        open_after = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE category='Templates' AND "
            "issue_type='Mix block' AND status='Open'"
        ).fetchone()[0]
        _check('no Open tickets remain', open_after == 0, f'got {open_after}')
        # Non-Open tickets stayed on their original pair (capture's eligibility
        # filter skipped them); that's the correct behavior \u2014 no rewrite.
        resolved_after = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE status='Resolved'"
        ).fetchone()[0]
        _check('Resolved bucket grew by the 3 we just resolved',
               resolved_after >= 3, f'got {resolved_after}')
    finally:
        con.close()
        os.unlink(path)


def check_8_ai_safety_reject() -> None:
    print('\n[8] AI-safety reject (make_ticket_row coerces AI-emitted Templates)')
    today = str(date.today())
    issue = {
        'category':     'Templates',               # Claude mis-emits this
        'issue_type':   'Made Up Template',
        'severity':     'MEDIUM',
        'issue_summary':'Pretending to classify a finding as Templates.',
        'offending_text':'whatever',
    }
    row = _rar.make_ticket_row('QA-T800', today, 'req8', 'Cook', 'Test Community', issue)
    category = row[6]
    _check("AI-emitted 'Templates' is coerced away",
           category != 'Templates', f'got category={category!r}')
    _check('coerced target is Content (safe fallback)',
           category == 'Content', f'got category={category!r}')


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print('=' * 66)
    print('Templates Category Phase 1 — verification smoke test')
    print('=' * 66)
    check_1_schema()
    check_2_normalizer()
    check_3_matcher()
    check_4_get_template_patterns()
    check_5_claude_path_rewrite()
    check_6_auto_path_rewrite()
    check_7_retirement_fallthrough()
    check_8_ai_safety_reject()
    check_9_capture_helper()
    check_10_name_collision()
    check_11_severity_inheritance()
    check_12_community_carve_out()
    check_13_list_templates()
    check_14_rename_template()
    check_15_retire_template()
    check_16_resolve_template_tickets()
    print()
    print('=' * 66)
    print(f'Summary: {_PASS} passed, {_FAIL} failed')
    print('=' * 66)
    return 0 if _FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
