"""Smoke test for the audit_events table + write_event / record_ticket_action.

Phase 1 of the pilot tester activity audit trail (internal ticket
34fdcd6e-4de2-8158-96a9-f3c97390d607). Mirrors the verify_templates.py
pattern: spins up a temp SQLite DB, runs every code path that should
produce an audit row, asserts the rows look right, prints a pass/fail
summary.

Safe to run against a fresh checkout:
    python -X utf8 verify_audit_events.py

Never touches the live DB — the temp DB is created in the system temp
directory and removed on exit.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as _db  # noqa: E402


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


def _seed_user(con, *, user_id=1, email='admin@cedarline.test',
               name='Admin', is_admin=1) -> dict:
    con.execute(
        "INSERT INTO users (id, email, name, password_hash, is_admin, active, "
        "page_permissions, created_at) "
        "VALUES (?, ?, ?, '', ?, 1, NULL, '2026-04-27T00:00:00')",
        (user_id, email, name, is_admin),
    )
    con.commit()
    return {'id': user_id, 'email': email, 'name': name,
            'is_admin': bool(is_admin), 'active': True,
            'page_permissions': {}}


def _seed_ticket(con, *, ticket_id='QA-0001', status='Open') -> None:
    con.execute(
        "INSERT INTO tickets (ticket_id, category, issue_type, status, severity) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticket_id, 'Content', 'Spelling and Grammar', status, 'HIGH'),
    )
    con.commit()


def _events(con, ticket_id=None) -> list[dict]:
    if ticket_id is None:
        rows = con.execute(
            "SELECT * FROM audit_events ORDER BY id"
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM audit_events WHERE entity_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Tests ──────────────────────────────────────────────────────────────────

def test_schema_round_trip() -> None:
    print('\n[1] Schema round-trip — ensure_tables creates audit_events + indexes')
    con, path = _fresh_db()
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        _check('audit_events table exists', 'audit_events' in tables)
        cols = {r[1] for r in con.execute('PRAGMA table_info(audit_events)').fetchall()}
        expected = {'id', 'entity_type', 'entity_id', 'user_id',
                    'actor_email_snapshot', 'action', 'payload_json',
                    'bulk_id', 'created_at'}
        _check('audit_events has all expected columns', expected <= cols,
               f'missing: {expected - cols}' if expected - cols else '')

        idxs = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='audit_events'"
        ).fetchall()}
        for needed in ('idx_audit_entity', 'idx_audit_user_time',
                       'idx_audit_created_at', 'idx_audit_bulk'):
            _check(f'index {needed} exists', needed in idxs)

        tcols = {r[1] for r in con.execute('PRAGMA table_info(tickets)').fetchall()}
        for needed in ('last_action_by', 'last_action_at', 'last_action'):
            _check(f'tickets.{needed} column exists', needed in tcols)
    finally:
        con.close()
        os.unlink(path)


def test_ensure_tables_idempotent() -> None:
    print('\n[2] ensure_tables is idempotent — calling twice does not error')
    con, path = _fresh_db()
    try:
        try:
            _db.ensure_tables(con)
            _db.ensure_tables(con)
            _check('second + third ensure_tables call succeed', True)
        except Exception as e:
            _check('second + third ensure_tables call succeed', False, repr(e))
    finally:
        con.close()
        os.unlink(path)


def test_write_event_basic() -> None:
    print('\n[3] write_event — writes one row with expected columns')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        eid = _db.write_event(
            con,
            entity_type='ticket',
            entity_id='QA-0001',
            user=user,
            action='resolve',
            payload={'prior_status': 'Open', 'reason': None},
        )
        con.commit()
        _check('write_event returns new id', isinstance(eid, int) and eid > 0)
        evs = _events(con, 'QA-0001')
        _check('exactly one event row written', len(evs) == 1, f'got {len(evs)}')
        ev = evs[0]
        _check('entity_type set', ev['entity_type'] == 'ticket')
        _check('entity_id set', ev['entity_id'] == 'QA-0001')
        _check('user_id set', ev['user_id'] == user['id'])
        _check('actor_email_snapshot captures email at write time',
               ev['actor_email_snapshot'] == user['email'])
        _check('action set', ev['action'] == 'resolve')
        payload = json.loads(ev['payload_json'])
        _check('payload round-trips through JSON',
               payload == {'prior_status': 'Open', 'reason': None})
        _check('bulk_id NULL when not provided', ev['bulk_id'] is None)
        _check('created_at populated', bool(ev['created_at']))
    finally:
        con.close()
        os.unlink(path)


def test_write_event_validation() -> None:
    print('\n[4] write_event — validation catches missing user fields')
    con, path = _fresh_db()
    try:
        try:
            _db.write_event(con, entity_type='ticket', entity_id='QA-0001',
                            user=None, action='resolve')
            _check('rejects None user', False, 'no exception raised')
        except ValueError:
            _check('rejects None user', True)
        except Exception as e:
            _check('rejects None user', False, f'wrong exception: {e!r}')

        try:
            _db.write_event(con, entity_type='ticket', entity_id='QA-0001',
                            user={'id': 1}, action='resolve')
            _check('rejects user dict missing email', False)
        except ValueError:
            _check('rejects user dict missing email', True)
    finally:
        con.close()
        os.unlink(path)


def test_record_ticket_action_state_change() -> None:
    print('\n[5] record_ticket_action — state-change action updates last_action_*')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        _db.record_ticket_action(
            con, ticket_id='QA-0001', user=user, action='acknowledge',
            payload={'prior_status': 'Open', 'reason': 'Correct in context'},
        )
        con.commit()
        evs = _events(con, 'QA-0001')
        _check('event written', len(evs) == 1)
        _check('event action recorded', evs[0]['action'] == 'acknowledge')

        row = con.execute(
            "SELECT last_action_by, last_action_at, last_action "
            "FROM tickets WHERE ticket_id = 'QA-0001'"
        ).fetchone()
        _check('last_action_by populated', row['last_action_by'] == user['id'])
        _check('last_action_at populated', bool(row['last_action_at']))
        _check('last_action populated', row['last_action'] == 'acknowledge')
    finally:
        con.close()
        os.unlink(path)


def test_record_ticket_action_note_no_denorm() -> None:
    print('\n[6] record_ticket_action — note actions do NOT bump last_action_*')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        _db.record_ticket_action(
            con, ticket_id='QA-0001', user=user, action='note_create',
            payload={'note_id': 1, 'note_text': 'hello'},
        )
        con.commit()
        row = con.execute(
            "SELECT last_action_by, last_action_at, last_action "
            "FROM tickets WHERE ticket_id = 'QA-0001'"
        ).fetchone()
        _check('last_action_by stays NULL for note action',
               row['last_action_by'] is None)
        _check('last_action_at stays NULL for note action',
               row['last_action_at'] is None)
        _check('last_action stays NULL for note action',
               row['last_action'] is None)
        evs = _events(con, 'QA-0001')
        _check('event still written for note action', len(evs) == 1)
    finally:
        con.close()
        os.unlink(path)


def test_record_ticket_action_unknown_rejected() -> None:
    print('\n[7] record_ticket_action — unknown action raises ValueError')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        try:
            _db.record_ticket_action(
                con, ticket_id='QA-0001', user=user, action='magic_undo',
            )
            _check('unknown action rejected', False, 'no exception')
        except ValueError:
            _check('unknown action rejected', True)
        evs = _events(con)
        _check('no event written for rejected action', len(evs) == 0)
    finally:
        con.close()
        os.unlink(path)


def test_bulk_id_ties_events() -> None:
    print('\n[8] bulk_id — shared across events from a single bulk action')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        for tid in ('QA-0001', 'QA-0002', 'QA-0003'):
            _seed_ticket(con, ticket_id=tid, status='Open')
        bulk_id = 'abc123def456'
        for tid in ('QA-0001', 'QA-0002', 'QA-0003'):
            _db.record_ticket_action(
                con, ticket_id=tid, user=user, action='resolve',
                payload={'prior_status': 'Open'},
                bulk_id=bulk_id,
            )
        con.commit()
        rows = con.execute(
            "SELECT entity_id, bulk_id FROM audit_events ORDER BY id"
        ).fetchall()
        _check('three events written', len(rows) == 3)
        _check('all share same bulk_id',
               all(r['bulk_id'] == bulk_id for r in rows))
        ids = {r['entity_id'] for r in rows}
        _check('all three tickets represented',
               ids == {'QA-0001', 'QA-0002', 'QA-0003'})
    finally:
        con.close()
        os.unlink(path)


def test_query_by_user() -> None:
    print('\n[9] Query shape — recent activity for one user uses idx_audit_user_time')
    con, path = _fresh_db()
    try:
        user_a = _seed_user(con, user_id=1, email='a@cedarline.test')
        user_b = _seed_user(con, user_id=2, email='b@cedarline.test')
        _seed_ticket(con, ticket_id='QA-0001')
        _seed_ticket(con, ticket_id='QA-0002')
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user_a, action='resolve')
        _db.record_ticket_action(con, ticket_id='QA-0002', user=user_b, action='resolve')
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user_a, action='restore')
        con.commit()
        rows = con.execute(
            "SELECT entity_id, action FROM audit_events "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_a['id'],),
        ).fetchall()
        _check('user_a has two events', len(rows) == 2)
        actions = {r['action'] for r in rows}
        _check('user_a actions correct', actions == {'resolve', 'restore'})
    finally:
        con.close()
        os.unlink(path)


def test_actor_email_immutable_snapshot() -> None:
    print('\n[10] Self-contained event — renaming user does not rewrite history')
    con, path = _fresh_db()
    try:
        user = _seed_user(con, email='old@cedarline.test')
        _seed_ticket(con)
        _db.record_ticket_action(
            con, ticket_id='QA-0001', user=user, action='resolve',
        )
        con.commit()
        # Rename the user post-write
        con.execute("UPDATE users SET email = 'new@cedarline.test' WHERE id = 1")
        con.commit()
        ev = _events(con, 'QA-0001')[0]
        _check('actor_email_snapshot still says old@cedarline.test',
               ev['actor_email_snapshot'] == 'old@cedarline.test',
               f"got {ev['actor_email_snapshot']}")
    finally:
        con.close()
        os.unlink(path)


def test_list_audit_events_filters() -> None:
    print('\n[11] list_audit_events — filters compose correctly')
    con, path = _fresh_db()
    try:
        a = _seed_user(con, user_id=1, email='a@cedarline.test', name='Alice')
        b = _seed_user(con, user_id=2, email='b@cedarline.test', name='Bob')
        for tid in ('QA-0001', 'QA-0002'):
            _seed_ticket(con, ticket_id=tid)
        _db.record_ticket_action(con, ticket_id='QA-0001', user=a, action='resolve')
        _db.record_ticket_action(con, ticket_id='QA-0002', user=b, action='resolve')
        _db.record_ticket_action(con, ticket_id='QA-0001', user=b, action='restore')
        _db.write_event(con, entity_type='ticket', entity_id='QA-0001', user=a,
                        action='note_create', payload={'note_id': 1, 'note_text': 'hi'})
        con.commit()

        all_events = _db.list_audit_events(con)
        _check('all events returned newest-first', len(all_events) == 4)
        _check('events sorted DESC by id',
               all_events[0]['id'] > all_events[-1]['id'])
        _check('payload parsed to dict, not string',
               isinstance(all_events[0]['payload'], dict))
        _check('actor_name resolved via JOIN',
               all_events[0]['actor_name'] in ('Alice', 'Bob'))

        only_alice = _db.list_audit_events(con, user_id=a['id'])
        _check('user_id filter narrows correctly', len(only_alice) == 2)
        _check('all alice events have her id',
               all(e['user_id'] == a['id'] for e in only_alice))

        only_resolve = _db.list_audit_events(con, action='resolve')
        _check('action filter narrows correctly', len(only_resolve) == 2)

        only_qa1 = _db.list_audit_events(con, entity_id='QA-0001')
        _check('entity_id filter narrows correctly', len(only_qa1) == 3)

        paged = _db.list_audit_events(con, limit=2, offset=0)
        _check('limit/offset pagination works', len(paged) == 2)
        page2 = _db.list_audit_events(con, limit=2, offset=2)
        _check('second page returns remainder', len(page2) == 2)
        _check('pages do not overlap',
               {e['id'] for e in paged}.isdisjoint({e['id'] for e in page2}))
    finally:
        con.close()
        os.unlink(path)


def test_list_ticket_history_chronological() -> None:
    print('\n[12] list_ticket_history — returns oldest-first for one ticket')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        _seed_ticket(con, ticket_id='QA-9999')
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user, action='resolve')
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user, action='restore')
        _db.record_ticket_action(con, ticket_id='QA-9999', user=user, action='resolve')
        con.commit()
        history = _db.list_ticket_history(con, 'QA-0001')
        _check('only events for the requested ticket', len(history) == 2)
        _check('all entries belong to QA-0001',
               all(e['entity_id'] == 'QA-0001' for e in history))
        _check('chronological (oldest first)',
               history[0]['id'] < history[1]['id'])
        _check('first action is resolve', history[0]['action'] == 'resolve')
        _check('second action is restore', history[1]['action'] == 'restore')
    finally:
        con.close()
        os.unlink(path)


def test_get_users_map() -> None:
    print('\n[13] get_users_map — bulk lookup, ignores NULLs and unknowns')
    con, path = _fresh_db()
    try:
        _seed_user(con, user_id=1, email='a@cedarline.test', name='Alice')
        _seed_user(con, user_id=2, email='b@cedarline.test', name='Bob')
        umap = _db.get_users_map(con, [1, 2, None, 999, 1])
        _check('returns the two real users', set(umap.keys()) == {1, 2})
        _check('alice email correct', umap[1]['email'] == 'a@cedarline.test')
        _check('alice name correct', umap[1]['name'] == 'Alice')
        empty = _db.get_users_map(con, [None, None])
        _check('empty input returns empty map', empty == {})
    finally:
        con.close()
        os.unlink(path)


def test_actor_name_falls_back_when_user_deleted() -> None:
    print('\n[14] list_audit_events — actor_name is NULL after user deleted')
    con, path = _fresh_db()
    try:
        user = _seed_user(con, name='Admin')
        _seed_ticket(con)
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user, action='resolve')
        con.commit()
        # Hard-delete the user to simulate account removal
        con.execute("DELETE FROM users WHERE id = 1")
        con.commit()
        events = _db.list_audit_events(con, entity_id='QA-0001')
        _check('event still exists after user deletion', len(events) == 1)
        _check('actor_name is NULL after deletion',
               events[0]['actor_name'] is None)
        _check('actor_email_snapshot still readable',
               events[0]['actor_email_snapshot'] == 'admin@cedarline.test')
    finally:
        con.close()
        os.unlink(path)


def test_record_control_action_basic() -> None:
    print('\n[15] record_control_action — writes email_controls event with composite entity_id')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        eid = _db.record_control_action(
            con, category='Content', issue_type='Spelling and Grammar', user=user,
            action='edit',
            payload={'before': {'default_severity': 'MEDIUM'},
                     'after':  {'default_severity': 'HIGH'}},
        )
        con.commit()
        _check('record_control_action returns new id', isinstance(eid, int) and eid > 0)
        ev = _db.list_audit_events(con, entity_type='email_controls')[0]
        _check('entity_type set to email_controls',
               ev['entity_type'] == 'email_controls')
        _check('entity_id encoded as category||issue_type',
               ev['entity_id'] == 'Content||Spelling and Grammar')
        _check('control_entity_id helper round-trips',
               _db.control_entity_id('Content', 'Spelling and Grammar') == ev['entity_id'])
        _check('payload before/after preserved',
               ev['payload']['before']['default_severity'] == 'MEDIUM' and
               ev['payload']['after']['default_severity'] == 'HIGH')
    finally:
        con.close()
        os.unlink(path)


def test_record_control_action_unknown_rejected() -> None:
    print('\n[16] record_control_action — unknown verb rejected, ticket verbs rejected too')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        try:
            _db.record_control_action(con, category='Content', issue_type='X',
                                      user=user, action='destroy_world')
            _check('unknown control verb rejected', False, 'no exception')
        except ValueError:
            _check('unknown control verb rejected', True)
        # A ticket-only verb (resolve) must NOT be accepted as a control action
        try:
            _db.record_control_action(con, category='Content', issue_type='X',
                                      user=user, action='resolve')
            _check('ticket verb rejected for control action', False)
        except ValueError:
            _check('ticket verb rejected for control action', True)
    finally:
        con.close()
        os.unlink(path)


def test_pre_audit_filter() -> None:
    print('\n[17] list_audit_events — exclude_pre_audit filter')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user, action='resolve')
        # Synthesize a pre_audit event the same way the backfill script does
        # (raw INSERT — pre_audit isn't part of write_event's gating set).
        con.execute(
            "INSERT INTO audit_events (entity_type, entity_id, user_id, "
            "actor_email_snapshot, action, payload_json, bulk_id, created_at) "
            "VALUES ('ticket', 'QA-0001', 0, '(pre-audit)', 'pre_audit', "
            "'{\"status_at_backfill\": \"Resolved\"}', 'backfillxyz', "
            "'2026-04-27T00:00:00')"
        )
        con.commit()

        all_events = _db.list_audit_events(con)
        _check('default returns both real + synthetic events', len(all_events) == 2)

        filtered = _db.list_audit_events(con, exclude_pre_audit=True)
        _check('exclude_pre_audit drops the synthetic row', len(filtered) == 1)
        _check('the surviving event is the real resolve',
               filtered[0]['action'] == 'resolve')

        history = _db.list_ticket_history(con, 'QA-0001')
        _check('per-ticket history keeps pre_audit rows by default', len(history) == 2)
    finally:
        con.close()
        os.unlink(path)


def test_control_actions_share_table_with_tickets() -> None:
    print('\n[18] Control + ticket events coexist in audit_events without collision')
    con, path = _fresh_db()
    try:
        user = _seed_user(con)
        _seed_ticket(con)
        _db.record_ticket_action(con, ticket_id='QA-0001', user=user, action='resolve')
        _db.record_control_action(con, category='Tone', issue_type='ALL CAPS',
                                  user=user, action='mute', payload={'reason': 'noisy'})
        con.commit()

        ticket_events = _db.list_audit_events(con, entity_type='ticket')
        control_events = _db.list_audit_events(con, entity_type='email_controls')
        _check('one ticket event', len(ticket_events) == 1)
        _check('one control event', len(control_events) == 1)
        _check('ticket event has tickets entity_id',
               ticket_events[0]['entity_id'] == 'QA-0001')
        _check('control event has composite entity_id',
               control_events[0]['entity_id'] == 'Tone||ALL CAPS')
    finally:
        con.close()
        os.unlink(path)


def main() -> int:
    print('=== verify_audit_events.py — Phases 1 + 2 + 4 smoke test ===')
    tests = [
        test_schema_round_trip,
        test_ensure_tables_idempotent,
        test_write_event_basic,
        test_write_event_validation,
        test_record_ticket_action_state_change,
        test_record_ticket_action_note_no_denorm,
        test_record_ticket_action_unknown_rejected,
        test_bulk_id_ties_events,
        test_query_by_user,
        test_actor_email_immutable_snapshot,
        test_list_audit_events_filters,
        test_list_ticket_history_chronological,
        test_get_users_map,
        test_actor_name_falls_back_when_user_deleted,
        test_record_control_action_basic,
        test_record_control_action_unknown_rejected,
        test_pre_audit_filter,
        test_control_actions_share_table_with_tickets,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            global _FAIL
            _FAIL += 1
            print(f'  [FAIL] {t.__name__} raised:')
            traceback.print_exc()
    print(f'\n=== {_PASS} passed, {_FAIL} failed ===')
    return 0 if _FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
