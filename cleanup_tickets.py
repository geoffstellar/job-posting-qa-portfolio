"""One-shot cleanup for qa_tickets.db after the April 2026 rule changes.

Run this ONCE on your Windows machine from the QA v2 folder:
    python cleanup_tickets.py           # dry run — shows what WOULD change
    python cleanup_tickets.py --apply   # actually writes the changes

What it does
------------
1. Collapses every exclamation-point variant in both `tickets` and
   `pending_tickets` to the canonical cross-cutting type
   `Tone — Excessive Exclamation Points` (category=`Tone`,
   issue_type=`Excessive Exclamation Points`). Deduplicates any resulting
   duplicates per (req_id, check_type).

2. Re-evaluates every LIVE exclamation-point ticket against the new rule
   (any sentence with 2+ "!", or more than 2 sentences ending in "!").
   Tickets that no longer violate are closed with status
   `Closed — rule updated`. This is the "delete existing tickets that
   should not have been flagged" step.

3. Auto-approves grammar-class pending tickets. Any pending_tickets row
   whose issue_type contains one of:
     grammar, spelling, typo, punctuation, syntax, capitalization
   …is:
     (a) registered in email_controls (if its check_type isn't there yet),
     (b) promoted to the live `tickets` table,
     (c) removed from `pending_tickets`.

4. Drops orphaned `email_controls` rows — any check_type that has zero
   matches in either `tickets` or `pending_tickets`. Prevents the AI menu
   from bloating with long-dead types.

5. Prints a summary report.

Safe to re-run. All writes happen inside one transaction; if anything
raises, the DB is left untouched.
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

# Centralised business rules and DB access — see taxonomy.py and db.py.
import db as _db
import taxonomy as _tax

DB_PATH = _db.DB

# ── Helpers — delegated to taxonomy.py ─────────────────────────────────────────

CANONICAL_EXCL_CT    = _tax.CANONICAL_EXCL_CT
CANONICAL_EXCL_AREA  = _tax.CANONICAL_EXCL_AREA
CANONICAL_EXCL_ITYPE = _tax.CANONICAL_EXCL_ITYPE

def _is_excl(issue_type, category=''):
    return _tax.is_exclamation(issue_type, category)

def _is_auto_approve(issue_type, category=''):
    return _tax.is_auto_approve(issue_type, category)

def _violates_new_excl_rule(offending_text, issue_summary=''):
    return _tax.violates_exclamation_rule(offending_text, issue_summary)

def _derive_location_and_type(check_type):
    return _tax.derive_location_and_type(check_type)


# ── Cleanup steps ───────────────────────────────────────────────────────────────

def collapse_exclamation_tickets(con, report):
    """Rewrite every exclamation-point variant in tickets + pending_tickets
    to the canonical cross-cutting type."""
    for table in ('tickets', 'pending_tickets'):
        rows = _db.get_exclamation_tickets(con, table)
        changed = 0
        for r in rows:
            if (r['category'] == CANONICAL_EXCL_AREA
                    and r['issue_type'] == CANONICAL_EXCL_ITYPE):
                continue  # already canonical
            _db.update_ticket_area_itype(con, table, r['ticket_id'],
                                         CANONICAL_EXCL_AREA, CANONICAL_EXCL_ITYPE)
            changed += 1
        report[f'collapsed_excl_{table}'] = changed


def dedupe_exclamation_tickets(con, report):
    """After collapsing, there can be multiple canonical-excl tickets for the
    same req_id. Keep the oldest (lowest ticket_id) and delete the rest."""
    for table in ('tickets', 'pending_tickets'):
        dups = _db.get_exclamation_dupes(con, table,
                                          CANONICAL_EXCL_AREA, CANONICAL_EXCL_ITYPE)
        deleted = 0
        for d in dups:
            deleted += _db.delete_exclamation_dupes(
                con, table, CANONICAL_EXCL_AREA, CANONICAL_EXCL_ITYPE,
                d['req_id'], d['keep_id'])
        report[f'deduped_excl_{table}'] = deleted


def close_no_longer_violating_excl(con, report):
    """Close live exclamation tickets whose offending_text no longer violates
    the new rule. Pending tickets for the same condition are DELETED (they
    were AI-generated and don't need a paper trail)."""
    _CLOSE_NOTE = ('Auto-closed by cleanup_tickets.py: no longer violates '
                   'updated exclamation-point rule (1 "!" ending a sentence '
                   'is allowed; limit 2 per posting).')

    # Live: mark as closed rather than delete (preserve history)
    live_rows = _db.get_open_exclamation_tickets(
        con, CANONICAL_EXCL_AREA, CANONICAL_EXCL_ITYPE)
    closed = 0
    for r in live_rows:
        if not _violates_new_excl_rule(r['offending_text'], r['issue_summary']):
            _db.close_ticket_rule_updated(con, r['ticket_id'], _CLOSE_NOTE)
            closed += 1
    report['excl_live_closed_as_no_violation'] = closed

    # Pending: delete outright (they haven't been approved yet)
    pend_rows = _db.get_pending_exclamation_tickets(
        con, CANONICAL_EXCL_AREA, CANONICAL_EXCL_ITYPE)
    deleted = 0
    for r in pend_rows:
        if not _violates_new_excl_rule(r['offending_text'], r['issue_summary']):
            _db.delete_pending_ticket(con, r['ticket_id'])
            deleted += 1
    report['excl_pending_deleted_as_no_violation'] = deleted


def auto_approve_grammar_pending(con, report):
    """For every pending ticket whose issue class is grammar/spelling/etc,
    ensure its check_type is in email_controls, then promote the pending
    ticket to the live tickets table."""
    rows = _db.get_all_pending_tickets(con)
    grammar_rows = [r for r in rows if _is_auto_approve(
        r['issue_type'], r['category'])]
    if not grammar_rows:
        report['grammar_pending_promoted'] = 0
        report['grammar_check_types_registered'] = 0
        return

    # Register any novel check_types in email_controls
    existing_ec = _db.get_email_controls_pairs(con)
    new_cts = set()
    for r in grammar_rows:
        category = r.get('category', '') if hasattr(r, 'get') else (r['category'] or '')
        itype = r.get('issue_type', '') if hasattr(r, 'get') else (r['issue_type'] or '')
        if not category or not itype:
            area_derived, itype_derived = _derive_location_and_type(
                r.get('check_type', '') if hasattr(r, 'get') else '')
            if not category:
                category = area_derived
            if not itype:
                itype = itype_derived
        if category and itype and (category, itype) not in existing_ec:
            new_cts.add((category, itype))

    for category, itype in sorted(new_cts):
        _db.register_email_control(
            con, category, itype,
            notes='Auto-approved grammar/spelling class \u2014 added by cleanup_tickets.py')
    report['grammar_check_types_registered'] = len(new_cts)

    # Promote pending → live
    promoted = 0
    for r in grammar_rows:
        _db.promote_pending_to_live(con, r)
        promoted += 1
    report['grammar_pending_promoted'] = promoted


def drop_orphaned_email_controls(con, report):
    """Remove email_controls rows whose (category, issue_type) has zero live or pending
    tickets. Keeps the AI menu from bloating with dead entries.

    Conservative: we only drop rows where NEITHER tickets NOR pending_tickets
    have ever used the (category, issue_type) pair. We also skip any row marked as an
    alias target (i.e. referenced by issue_aliases.canonical_*), because approvers
    may want to restore them via alias lookups."""
    used = _db.get_used_pairs(con)
    used |= _db.get_alias_canonical_targets(con)

    ec_rows = _db.get_all_email_control_pairs(con)
    orphans = [(a, i) for a, i in ec_rows
               if a and i and (a, i) not in used]

    dropped = 0
    for category, itype in orphans:
        dropped += _db.delete_email_control(con, category, itype)
    report['email_controls_orphans_dropped'] = dropped
    report['_orphan_sample'] = [f"{a} \u2014 {i}" for a, i in orphans[:15]]
    report['_orphan_total']  = len(orphans)


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true',
                    help='Actually write changes. Without this flag it is a dry run.')
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f'ERROR: {DB_PATH} not found', file=sys.stderr)
        sys.exit(1)

    # Use read_copy/write_copy to avoid WSL/FUSE mount corruption
    con, tmp_path = _db.read_copy(DB_PATH)
    con.isolation_level = None  # manual transaction control
    con.execute('BEGIN')

    report = {}
    try:
        collapse_exclamation_tickets(con, report)
        dedupe_exclamation_tickets(con, report)
        close_no_longer_violating_excl(con, report)
        auto_approve_grammar_pending(con, report)
        # drop_ghost_email_controls removed April 2026: the schema now prevents
        # synthetic dict keys in the category/issue_type columns, so the bug cannot occur.
        drop_orphaned_email_controls(con, report)
    except Exception:
        con.execute('ROLLBACK')
        con.close()
        _db.cleanup_tmp(tmp_path)
        raise

    if args.apply:
        con.execute('COMMIT')
        con.close()
        _db.write_copy(tmp_path, DB_PATH)
        mode = 'APPLIED'
    else:
        con.execute('ROLLBACK')
        con.close()
        _db.cleanup_tmp(tmp_path)
        mode = 'DRY RUN (no changes written — re-run with --apply)'

    print('─' * 60)
    print(f'cleanup_tickets.py — {mode}')
    print('─' * 60)
    for k in (
        'collapsed_excl_tickets',
        'collapsed_excl_pending_tickets',
        'deduped_excl_tickets',
        'deduped_excl_pending_tickets',
        'excl_live_closed_as_no_violation',
        'excl_pending_deleted_as_no_violation',
        'grammar_check_types_registered',
        'grammar_pending_promoted',
        'email_controls_orphans_dropped',
    ):
        print(f'  {k:45s}  {report.get(k, 0)}')
    # Show a preview of ghost rows (suffix-polluted check_types) if any
    if report.get('ghost_email_controls_dropped', 0):
        print('─' * 60)
        print(f'  Ghost check_types purged (sample):')
        for ct in report.get('_ghost_sample', []):
            print(f'    • {ct}')
    # Show a preview of orphaned check_types so you can sanity-check before --apply
    if report.get('_orphan_total', 0):
        print('─' * 60)
        print(f'  Orphaned check_types (preview, {len(report["_orphan_sample"])} of '
              f'{report["_orphan_total"]}):')
        for ct in report['_orphan_sample']:
            print(f'    • {ct}')
    print('─' * 60)


if __name__ == '__main__':
    main()
