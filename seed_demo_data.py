#!/usr/bin/env python
"""Seed a fresh demo database for the Job-Posting QA Dashboard.

This makes the dashboard runnable end-to-end with no live careers API and no
Anthropic API key. It:

  1. Runs Alembic (``alembic upgrade head``) to build the schema on a fresh DB.
  2. Parses the bundled synthetic ``jobs_raw.json`` (a handful of fictional
     postings with QA issues spread deliberately across them).
  3. Runs the deterministic AUTO QA layer (``fetch_jobs.check_job`` +
     ``pre_scan.run_prescan``), writes the resulting tickets, and bootstraps
     the ``email_controls`` table so the Controls page and filters populate.
  4. Optionally (``--include-ai-examples``) writes a few hand-written
     CLAUDE-layer tickets so the AI-review categories show up on the dashboard
     even without an ``ANTHROPIC_API_KEY``.

Usage:
    python seed_demo_data.py                       # AUTO tickets only
    python seed_demo_data.py --include-ai-examples  # + sample AI-review tickets

The demo DB is regenerable — this script always rebuilds it from scratch.
"""
import argparse
import json
import os
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_RAW = os.environ.get("JOBS_RAW_PATH") or os.path.join(BASE, "jobs_raw.json")

import db as _db
import taxonomy as _tax
import fetch_jobs
import pre_scan


# Hand-written examples of the kind of findings the Claude review layer
# produces — the judgment calls the regex layer can't make. Used only with
# --include-ai-examples so visitors can see a populated AI layer without an
# API key. Each references real text in the synthetic postings.
AI_EXAMPLE_TICKETS = [
    {
        "req_id": "4002", "job_title": "Resident Care Aide",
        "community": "Willow Bend Senior Living",
        "severity": "LOW", "category": "Content", "issue_type": "Generic Language",
        "section": "Community Intro",
        "summary": ("Opening describes the role at \"a large senior living community\" and the "
                    "About section names \"Marigold Court\" rather than Willow Bend Senior Living. "
                    "Name the actual community in the opening paragraph."),
        "offending": "a large senior living community … About Marigold Court",
    },
    {
        "req_id": "4004", "job_title": "Caregiver",
        "community": "Marigold Court Retirement Community",
        "severity": "MEDIUM", "category": "Tone", "issue_type": "Resident/Patient Mix",
        "section": "Responsibilities",
        "summary": ("Responsibilities switches to the clinical term \"patient\" while the rest of "
                    "the posting uses \"residents.\" Use \"resident\" consistently in a "
                    "senior-living posting."),
        "offending": "Monitors each patient's well-being",
    },
    {
        "req_id": "4001", "job_title": "Cook",
        "community": "Sage Crossing Retirement Community",
        "severity": "HIGH", "category": "Structure", "issue_type": "Missing Required Section",
        "section": "Cross-cutting",
        "summary": "No \"What We Offer\" section is present — the posting states no pay or benefits.",
        "offending": "Missing section: What We Offer",
    },
    {
        "req_id": "4001", "job_title": "Cook",
        "community": "Sage Crossing Retirement Community",
        "severity": "HIGH", "category": "Content", "issue_type": "Spelling and Grammar",
        "section": "Qualifications",
        "summary": "clear misspelling: \"obtian\" should be \"obtain\".",
        "offending": "obtian",
    },
]


def collect_auto_tickets(jobs):
    """Run the deterministic AUTO layer over each posting (mirrors PHASE 2 of
    fetch_jobs.main: check_job + pre_scan), returning a flat list of ticket
    dicts."""
    all_tickets = []
    for job in jobs:
        tickets = fetch_jobs.check_job(job)
        issues, _brief, _plain = pre_scan.run_prescan(job)
        req_id = str(job.get("id", ""))
        title = (job.get("name") or "").strip()
        community = (job.get("organization", {}) or {}).get("name", "").strip()
        for issue in issues:
            tickets.append({
                "req_id": req_id, "job_title": title, "community": community,
                "severity": issue.get("severity", "MEDIUM"),
                "category": issue.get("category", ""),
                "issue_type": issue.get("issue_type", ""),
                "summary": issue.get("issue_summary", ""),
                "offending": (issue.get("offending_text", "") or "")[:300],
                "detected_by": "AUTO",
            })
        all_tickets.extend(tickets)
    return all_tickets


def _next_id_after(tickets):
    """Highest numeric suffix among QA-#### ids, +1."""
    nums = [0]
    for t in tickets:
        tid = t.get("ticket_id", "")
        if tid.startswith("QA-") and tid[3:].isdigit():
            nums.append(int(tid[3:]))
    return max(nums) + 1


def main():
    ap = argparse.ArgumentParser(description="Seed a fresh demo DB for the QA dashboard.")
    ap.add_argument("--include-ai-examples", action="store_true",
                    help="Also write sample CLAUDE-layer tickets (no API key needed).")
    args = ap.parse_args()

    db_path = _db._resolve_sqlite_path(None)

    print("=" * 64)
    print("  Seeding demo database")
    print("=" * 64)

    # The demo DB is regenerable — always rebuild from scratch.
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"  Removed existing DB: {db_path}")

    # 1. Schema via Alembic.
    action = _db.stamp_or_upgrade()
    print(f"  alembic upgrade head -> {action}")

    # 2. Parse synthetic postings.
    with open(JOBS_RAW, encoding="utf-8") as f:
        jobs = json.load(f)
    print(f"  Parsed {len(jobs)} synthetic postings from {os.path.basename(JOBS_RAW)}")

    # 3. AUTO layer + merge (assigns QA-#### ids).
    auto = collect_auto_tickets(jobs)
    live = {str(j.get("id", "")) for j in jobs if j.get("id")}
    merged = fetch_jobs.merge_tickets([], auto, live_req_ids=live)

    # 4. Optional CLAUDE examples (ids continue after the AUTO ones).
    rows = list(merged)
    if args.include_ai_examples:
        start = _next_id_after(merged)
        for offset, ex in enumerate(AI_EXAMPLE_TICKETS):
            row = dict(ex)
            row["ticket_id"] = f"QA-{start + offset:04d}"
            row["detected_by"] = "CLAUDE"
            row["status"] = "Open"
            row["date_flagged"] = str(date.today())
            rows.append(row)

    # 5. Write tickets + bootstrap email_controls (read-copy -> write-copy).
    con, tmp = _db.read_copy(db_path)
    try:
        _db.ensure_tables(con)
        for t in rows:
            if not t.get("section"):
                t["section"] = _tax.default_section_for(t.get("issue_type", ""))
            _db.insert_new_ticket(con, t)
        for category, itype in sorted(_db.get_distinct_pairs_from_tickets(con)):
            con.execute(
                "INSERT OR IGNORE INTO email_controls "
                "(category, issue_type, email_setting, show_on_community, scope) "
                "VALUES (?, ?, 'Include in emails', 1, 'ALL')",
                (category, itype),
            )
        con.commit()
    finally:
        con.close()
    _db.write_copy(tmp, db_path)

    # 6. Summary.
    from collections import Counter
    by_cat = Counter(t.get("category", "?") for t in rows)
    by_src = Counter(t.get("detected_by", "?") for t in rows)
    print(f"\n  Wrote {len(rows)} tickets to {db_path}")
    print("  By category: " + ", ".join(f"{c}={n}" for c, n in sorted(by_cat.items())))
    print("  By source:   " + ", ".join(f"{s}={n}" for s, n in sorted(by_src.items())))
    if not args.include_ai_examples:
        print("\n  Tip: re-run with --include-ai-examples to populate the AI-review layer too.")
    print("\n  Next: python qa_dashboard.py  ->  http://localhost:5000")
    print("=" * 64)


if __name__ == "__main__":
    main()
