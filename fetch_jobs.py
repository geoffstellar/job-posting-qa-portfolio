"""
Hireology API Fetcher + Programmatic QA Pre-Check
==================================================
Step 1 of the QA v2 pipeline.

- Pulls all live job postings from the Hireology public API
- Preserves the raw HTML job_description for Claude's deeper analysis
- Runs fast programmatic QA checks (no AI required)
- Saves two output files:
    jobs_raw.json    -- full job data including raw HTML, consumed by the QA skill
    qa_tickets.db    -- SQLite database with ticket history and email controls

Architecture notes (2026-04-16):
- `email_controls` is a MANAGED TABLE — this script never writes to it.
  New checks are added exclusively through the Claude qa-rules-maintenance
  skill (the in-dashboard Add Check wizard was removed 2026-04-24).
- `write_db()` MERGES tickets into the existing DB (read_copy → DELETE tickets
  → re-insert merged list → write_copy). Managed tables (email_controls,
  rejected_issues, issue_aliases, custom_areas) persist across runs untouched.
- The old load_email_controls() → write_db() → restore_pending_tickets()
  backup/restore cycle is ELIMINATED. Those helpers are marked DEPRECATED
  for reference; they're not called from main().
- See CLAUDE.md for the full check lifecycle (Active / Muted / Retired).

Usage:
    python fetch_jobs.py
    python fetch_jobs.py --dry-run    # fetch and check but don't write files
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date
from html.parser import HTMLParser

import requests
from dotenv import load_dotenv

# Centralised DB access — see db.py
import db as _db
import taxonomy as _tax

# pre_scan.py lives alongside this file — import it for Standard Check coverage.
try:
    from pre_scan import run_prescan as _run_prescan
    _PRESCAN_AVAILABLE = True
except ImportError:
    _PRESCAN_AVAILABLE = False


# -- Configuration -------------------------------------------------------------
# Env-driven paths (Phase 3, 2026-04-22). `.env` is loaded so running
# `python fetch_jobs.py` directly respects the same config qa_dashboard.py
# does. Local dev with nothing set → JSON + DB default to the repo folder.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

API_URL   = "https://api.hireology.com/v2/public/careers/cedarlineseniorliving"
OUT_DIR   = os.path.dirname(os.path.abspath(__file__))
_APP_DATA_DIR = os.environ.get('APP_DATA_DIR') or OUT_DIR
JSON_OUT  = os.environ.get('JOBS_RAW_PATH') or os.path.join(_APP_DATA_DIR, "jobs_raw.json")
# DB path extracted from DATABASE_URL (SQLite) so fetch_jobs and qa_dashboard
# agree on the live DB file regardless of how either was launched.
DB_OUT    = _db._resolve_sqlite_path(None)


# -- HTML helpers --------------------------------------------------------------

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []
    def handle_data(self, data):
        self._parts.append(data)
    def get_text(self):
        return re.sub(r"\s{2,}", " ", " ".join(self._parts)).strip()

def strip_html(html):
    if not html:
        return ""
    s = _Stripper()
    s.feed(html)
    return s.get_text()


# -- API fetch -----------------------------------------------------------------

def _parse_response(data):
    if isinstance(data, list):
        return data, len(data)
    if isinstance(data, dict):
        batch = data.get("jobs") or data.get("data") or []
        total = data.get("count") or data.get("total") or 0
        return batch, int(total)
    return [], 0

def fetch_all_jobs():
    PAGE_SIZE = 100
    all_jobs, page = [], 1

    while True:
        print(f"  Fetching page {page} ...", end="", flush=True)
        resp = requests.get(API_URL, params={"page": page, "page_size": PAGE_SIZE}, timeout=15)
        resp.raise_for_status()
        batch, total = _parse_response(resp.json())
        if not batch:
            print()
            break
        existing = {j["id"] for j in all_jobs}
        new = [j for j in batch if j.get("id") not in existing]
        if not new:
            print(f" (no new -- done)")
            break
        all_jobs.extend(new)
        print(f" {len(all_jobs)}/{total or '?'}")
        if total and len(all_jobs) >= total:
            break
        if len(batch) < PAGE_SIZE:
            break
        page += 1

    return all_jobs


# -- Programmatic QA checks ----------------------------------------------------

# Known safe acronyms -- not flagged as ALL CAPS
SAFE_ACRONYMS = {
    "CNA", "LPN", "RN", "ADL", "ADLs", "CEO", "EEO", "EEOC", "EOE",
    "COVID", "QMAP", "DON", "DOE", "PTO", "PRN", "HTML", "OK",
    "AZ", "CO", "MT", "NV", "OR", "WA", "CA", "NM", "WY", "ID", "UT",
    "USD", "TBD", "POC", "HR", "401K", "LCSW", "LSW", "MST", "UTC",
    "BOM", "RN", "IV", "CPR", "BLS", "ACLS", "CMA",
    "HIPAA", "COTA",   # added 2026-04-15 — healthcare industry acronyms surfaced in rejected-ticket review
    # Added 2026-04-21 after rejected-ticket review — therapy / clinical
    # acronyms seen in real postings (QA-1802 / QA-1936 false-positives).
    "APTA", "ADLS", "DPT", "PTA", "ADON", "MDS",
    # Section-heading-shape words that Hireology renders in ALL CAPS as
    # section markers, not as body-text emphasis. Added alongside the
    # section-heading filter in _check_body_caps below — the filter
    # catches them in position (start of line / before colon), this
    # list catches them anywhere for safety.
    "ABOUT", "BENEFITS", "OVERVIEW", "REQUIREMENTS", "QUALIFICATIONS",
    "RESPONSIBILITIES", "DUTIES", "SUMMARY", "ESSENTIAL", "FUNCTION",
    "FUNCTIONS", "EXPERIENCE", "EDUCATION", "SKILLS", "SCHEDULE",
    "HOURS", "COMPENSATION", "SALARY",
}

def check_job(job):
    """Run all programmatic checks against one job. Returns list of ticket dicts."""
    tickets = []
    html   = job.get("job_description", "") or ""
    plain  = strip_html(html)
    title  = (job.get("name") or "").strip()
    org    = (job.get("organization") or {})
    community = (org.get("name") or "").strip()
    req_id = str(job.get("id", ""))
    job_url = (job.get("career_site_url") or "").strip()

    def flag(severity, category, issue_type, summary, offending=""):
        tickets.append({
            "req_id":    req_id,
            "job_title": title,
            "community": community,
            "severity":  severity,
            "category":      category,
            "issue_type": issue_type,
            "summary":   summary,
            "offending": offending[:300],
            "detected_by": "AUTO",
            "job_url":   job_url,
        })

    # 1 -- Unfilled template placeholder
    matches = re.findall(r'\[([^\]]{1,60})\]', plain)
    if matches:
        flag("CRITICAL", "Content", "Unfilled Placeholder",
             "Replace the template placeholder text with real content before this posting ships.",
             " | ".join(f"[{m}]" for m in matches[:3]))

    # 2 -- Raw email address in description
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', plain)
    # filter out generic domains that might appear legitimately
    emails = [e for e in emails if "cedarline" in e.lower() or "hireology" in e.lower()]
    if emails:
        flag("HIGH", "Content", "Raw Email Address",
             f"Remove the {len(emails)} recruiter email address(es) from the description. Candidates apply through the Apply button.",
             ", ".join(emails[:3]))

    # 3 -- Missing EEO statement
    # As of 2026-04-15 (second wave) this lives under the cross-cutting
    # Structure / Missing Required Section pair, same pattern as every
    # other missing-section check. The specific section (EEO Statement)
    # is named in offending_text, not in category. See CLAUDE.md standing
    # rule "Missing Required Section Is a Cross-Cutting Check".
    if "equal opportunity" not in plain.lower():
        flag("HIGH", "Structure", "Missing Required Section",
             "Add the standard Equal Opportunity Employer disclaimer to the posting.",
             "Missing section: EEO Statement")

    # 4 -- Highlighted / colored text in raw HTML
    if re.search(r'background[-_]color\s*:|background-color\s*=|<mark', html, re.I):
        # Extract a short snippet of context
        m = re.search(r'.{0,30}(?:background[-_]color|<mark).{0,30}', html, re.I)
        flag("HIGH", "Formatting", "Highlighted/Colored Text",
             "Remove the HTML highlight or background-color style. It renders visibly on the careers page.",
             m.group() if m else "")

    # 5 -- Inconsistent wage figures (more than 2 distinct $ amounts)
    # Exclude dollar amounts that appear within 100 chars of bonus/sign-on language
    _bonus_ctx = re.compile(
        r'(?:sign.?on|signing|referral|hiring|retention|relocation|bonus|incentive|stipend|one.?time)\s*(?:bonus|pay|payment|award|incentive)?.{0,100}\$[\d,]+'
        r'|\$[\d,]+.{0,100}(?:sign.?on|signing|referral|hiring|retention|relocation|bonus|incentive|stipend|one.?time)',
        re.I
    )
    _bonus_amounts = set()
    for _m in _bonus_ctx.finditer(plain):
        for _w in re.findall(r'\$[\d,]+(?:\.\d{2})?(?:/hr|/hour)?', _m.group()):
            _bonus_amounts.add(_w)
    wages = re.findall(r'\$[\d,]+(?:\.\d{2})?(?:/hr|/hour)?', plain)
    wages_filtered = [w for w in wages if w not in _bonus_amounts]
    distinct = list(dict.fromkeys(wages_filtered))   # dedupe preserving order
    if len(distinct) > 2:
        flag("MEDIUM", "Content", "Inconsistent Wage Figures",
             f"{len(distinct)} distinct dollar amounts found. Verify these are intentional.",
             " | ".join(distinct[:5]))

    # 6 -- Body Text in ALL CAPS (multiple non-acronym caps words used for emphasis)
    # Renamed 2026-04-15 from "Excessive ALL CAPS" to disambiguate from the
    # title-level "ALL CAPS" check (Job Title / Title Formatting).
    #
    # Section-heading filter (2026-04-21): ignore single-word all-caps that
    # are positioned as section headings — either followed by a colon (with
    # optional whitespace) or appearing at the start of a line. Hireology
    # renders section markers in all-caps, and those are not "body-text
    # emphasis bursts"; the check is meant for emphasis sprinkled mid-
    # paragraph. Surfaced 2026-04-21 on a Physical Therapy posting where
    # "ABOUT / BENEFITS / ESSENTIAL / FUNCTION(S)" were all section
    # markers, not emphasis. Safe-acronyms list above also catches these
    # as a belt-and-suspenders layer.
    heading_pattern = re.compile(r'(?:^|\n)\s*([A-Z]{2,}(?:\s+[A-Z]{2,})*)\s*:?\s*(?:\n|$)', re.MULTILINE)
    heading_tokens = set()
    for m in heading_pattern.finditer(plain):
        for w in m.group(1).split():
            heading_tokens.add(w)
    # Also catch single-word all-caps followed directly by a colon anywhere
    # (e.g. "RESPONSIBILITIES: Manage...")
    for m in re.finditer(r'\b([A-Z]{2,})\s*:', plain):
        heading_tokens.add(m.group(1))

    caps = re.findall(r'\b[A-Z]{4,}\b', plain)
    caps_filtered = [w for w in caps
                     if w not in SAFE_ACRONYMS and w not in heading_tokens]
    if len(set(caps_filtered)) >= 3:
        flag("MEDIUM", "Tone", "Body Text in ALL CAPS",
             "Multiple ALL CAPS words used for emphasis in the body text. Consider standard formatting instead.",
             " | ".join(sorted(set(caps_filtered))[:6]))

    # 7 -- Missing community name or address
    locs = job.get("locations") or []
    loc  = locs[0] if locs else {}
    address_parts = [loc.get("address",""), loc.get("city",""), loc.get("state","")]
    if not community:
        flag("MEDIUM", "Structure", "Missing Community Name", "Community name (organization.name) is blank.")
    if not any(address_parts):
        flag("MEDIUM", "Structure", "Missing Address", "No address data found for this posting.")

    # 8 -- Posted title vs. description role heading mismatch
    #
    # Intent: catch cases like posted title "Cook" but description opens with
    #   "Executive Chef — Fine Dining" — substantively different roles.
    #
    # Reality of Hireology HTML (measured April 2026):
    #   * ~99% of postings are copy-pasted from Word, so real <h1>-<h3> tags
    #     are almost never present. Headings come through as <strong>/<b>.
    #   * The FIRST <strong> in a posting is usually either (a) the bolded
    #     community name / greeting, or (b) a section label like
    #     "Responsibilities" — NOT the job title heading itself.
    #   * Many postings have no dedicated role heading at all; that is not
    #     a violation on its own.
    #
    # Strategy: scan every <strong>/<b> (and any real <h1>-<h3>) in the
    # first ~2000 chars of the HTML. Skip any that are section labels or
    # clearly community/greeting text. If we find ANY candidate that looks
    # like a role name, compare it to the posted title. Only flag when we
    # have a candidate and it has zero word-overlap with the title — this
    # is a conservative "same role or not" check, not a strict match.
    _SECTION_LABELS = {
        'whoweare', 'aboutus', 'aboutthecompany', 'aboutthecommunity',
        'whatweoffer', 'whyus', 'whyyoulllove', 'whyyoulllloveworkinghere',
        'stillundecided', 'responsibilities', 'keyresponsibilities',
        'primaryresponsibilities', 'essentialduties', 'essentialfunctions',
        'essentialfunctionsandresponsibilities',
        'varied', 'variedresponsibilities',
        'whatyoulldo', 'whatyouwilldo', 'whatyouown', 'whatmakesthisroledistinct',
        'qualifications', 'minimumqualifications', 'minimumrequirements',
        'requirements', 'whoyouare', 'education', 'educationrequired',
        'description', 'jobdescription', 'positionsummary', 'jobsummary',
        'abouttherole', 'abouttheopportunity', 'roleoverview', 'yourrole',
        'function', 'benefits', 'ourbenefits', 'compensationandbenefits',
        'eeostatement', 'equalopportunityemployer', 'joinus', 'joinourteam',
        'daytoday', 'daytodayresponsibilities', 'compensation',
        'schedule', 'hours', 'location', 'shift', 'shifts',
    }
    # Words that strongly suggest a candidate IS a job-role heading.
    _ROLE_WORDS = {
        'caregiver', 'cna', 'lpn', 'rn', 'qmap', 'med', 'tech', 'nurse',
        'cook', 'chef', 'dishwasher', 'housekeeper', 'maintenance',
        'driver', 'server', 'receptionist', 'concierge', 'aide', 'assistant',
        'manager', 'director', 'coordinator', 'supervisor', 'administrator',
        'therapist', 'therapy', 'analyst', 'specialist', 'dietary',
        'wellness', 'activities', 'marketing', 'sales', 'engagement',
        'executive', 'lead', 'associate', 'intern',
    }
    _STOP = {'a','an','the','of','and','or','for','at','to','in','on','with',
             'our','your','we','us','is','are',
             'per','hour','day','week','month','year'}
    def _words(s):
        """Tokenise a title/heading into meaningful words.

        - Lowercase, strip non-letters to spaces.
        - Keep 2+ letter tokens so acronyms like 'RN', 'OT', 'PT' are included.
        - Drop stop-words.
        """
        toks = re.findall(r'[a-z]{2,}', s.lower())
        return {w for w in toks if w not in _STOP}

    # Tokenise the full posted title — do NOT strip a trailing "- <phrase>"
    # suffix, because many real titles include hyphenated words ("Part-Time",
    # "Sign-on") that the greedy regex used to consume.
    title_for_compare = title or ''
    opening_html = html[:2000] if html else ''
    # Collect all candidate heading-ish snippets from the opening
    candidates = []
    for m in re.finditer(
        r'<(?:h[1-3]|strong|b)[^>]*>(.*?)</(?:h[1-3]|strong|b)>',
        opening_html, re.I | re.S
    ):
        txt = strip_html(m.group(1)).strip()
        if not txt or not (4 < len(txt) < 100):
            continue
        norm = re.sub(r'\W', '', txt.lower())
        if norm in _SECTION_LABELS:
            continue
        candidates.append(txt)
        # Only look at the first few — later <strong> tags are almost always
        # section labels or inline emphasis.
        if len(candidates) >= 4:
            break

    # Filter candidates to those that look role-like: contain at least one
    # known role word. If none do, we treat the posting as having no
    # distinct role heading — do not flag.
    role_candidates = [c for c in candidates if _words(c) & _ROLE_WORDS]
    if role_candidates and title_for_compare:
        t_words = _words(title_for_compare)
        norm_title = re.sub(r'\W', '', title_for_compare.lower())
        # Pass if ANY role-like candidate shares at least one role word with
        # the title. This is a conservative "same role or not" check, not a
        # strict match — generic overlap on non-role words doesn't count.
        def _matches_title(c):
            norm_c = re.sub(r'\W', '', c.lower())
            if (norm_title and norm_c and
                    (norm_title in norm_c or norm_c in norm_title)):
                return True
            c_words = _words(c)
            return bool(t_words & c_words & _ROLE_WORDS) or bool(t_words & c_words)
        if t_words and not any(_matches_title(c) for c in role_candidates):
            first = role_candidates[0]
            flag("HIGH", "Structure", "Title/Description Mismatch",
                 f"The posting title names a different role than the description "
                 f"body introduces. The posted title is \"{title}\", but the first "
                 f"role named inside the description is \"{first}\". Applicants see "
                 f"the title first and the description second; when they name "
                 f"different roles, candidates don't know which position they'd "
                 f"be applying to. Either change the posted title to match the "
                 f"role described in the body, or change the body's opening "
                 f"heading to match the posted title.",
                 f"Posted: \"{title}\" | Heading: \"{first}\"")

    # 9 -- Bonus / pay incentive language in job title
    TITLE_BONUS_PATTERN = re.compile(
        r'sign.?on\s*bonus|signing\s*bonus|\$[\d,]+|\bbonus\b|up\s+to\s+\$',
        re.I
    )
    if TITLE_BONUS_PATTERN.search(title):
        flag("HIGH", "Structure", "Pay/Bonus in Title",
             "Remove the signing bonus or pay amount from the job title and move it into the description body. "
             "Indeed demotes postings that put compensation in the title.",
             title)

    return tickets


def check_file_lock(path):
    """Exit early with a clear message if the Excel file is open in another program."""
    if not os.path.exists(path):
        return  # file doesn't exist yet -- no lock issue
    try:
        with open(path, 'r+b'):
            pass  # just testing write access
    except PermissionError:
        print(f"\n  [X]  Permission denied: {path}")
        print(f"     qa_tickets.xlsx appears to be open in Excel.")
        print(f"     Please close it and run the script again.\n")
        sys.exit(1)


def load_previous_tickets(db_path):
    """Load existing tickets from the SQLite database. Returns list of dicts."""
    if not os.path.exists(db_path):
        return []
    try:
        con, tmp_path = _db.read_copy(db_path)
        tickets = _db.load_all_tickets(con)
        con.close()
        _db.cleanup_tmp(tmp_path)
        return tickets
    except Exception as e:
        print(f"  [!] Could not load previous tickets from DB: {e}")
        return []


def merge_tickets(previous, new_tickets, live_req_ids=None):
    """
    Merge new run results with previous ticket history.

    Rules:
    - Previous Open tickets (any detected_by) whose req_id is NOT in
      live_req_ids -> mark Resolved with note "job no longer in Hireology
      feed". This is the "closed posting" branch — once the whole posting
      is gone from the Hireology API, every ticket on it should close
      regardless of who detected it. Pass live_req_ids=None to disable
      this branch (legacy callers / dry-run scripts).
    - Previous Open AUTO tickets whose (req_id, category, issue_type) fingerprint
      is GONE from this run -> mark Resolved. This handles the live-posting
      case where an AUTO check stopped firing.
    - Previous Resolved AUTO tickets whose fingerprint REAPPEARS in this run
      -> re-open in place (with a "Re-flagged YYYY-MM-DD" note). This prevents
      creating a brand-new ticket every time the same issue resurfaces.
    - Previous Archived / Flagged Incorrectly tickets are NEVER touched here:
      they represent a deliberate user decision and must stay out of the way.
    - Previous tickets already Open whose fingerprint still fires -> carry
      forward unchanged.
    - Genuinely new AUTO fingerprints (no previous ticket exists) -> add as
      new Open tickets with a fresh QA-XXXX id.
    - CLAUDE Open tickets on STILL-LIVE postings are not touched (the LLM
      is non-deterministic; a clean pass on a live posting doesn't mean
      the finding is gone). They only auto-resolve via the live_req_ids
      branch above when the whole posting leaves the feed.
    """
    today = str(date.today())

    def _fp(t):
        a = t.get("category", "") or ""
        i = t.get("issue_type", "") or ""
        if not (a and i):
            # Legacy tickets may only have "check" composite
            c = t.get("check", "") or ""
            if "\u2014" in c:
                left, _, right = c.partition("\u2014")
                a = a or left.strip()
                i = i or right.strip()
            else:
                a = a or "Content"
                i = i or c
        return (t.get("req_id", ""), a, i)

    new_fps = {_fp(t) for t in new_tickets}

    # Index ALL previous AUTO tickets by fingerprint so we can find candidates
    # to re-open. If multiple historical rows share a fingerprint (legacy data),
    # prefer Open > Resolved > Archived > Flagged Incorrectly so we never
    # accidentally re-promote a deliberately-archived ticket.
    _STATUS_RANK = {'Open': 0, 'Resolved': 1, 'Archived': 2,
                    'Flagged Incorrectly': 3}
    prev_auto_index = {}
    for idx, t in enumerate(previous):
        if t.get("detected_by") != "AUTO":
            continue
        fp = _fp(t)
        rank = _STATUS_RANK.get(t.get("status", ""), 9)
        cur = prev_auto_index.get(fp)
        if cur is None or rank < cur[0]:
            prev_auto_index[fp] = (rank, idx)

    prev_open_auto_fps = {fp for fp, (rank, _) in prev_auto_index.items()
                          if rank == 0}

    # Walk previous tickets:
    #   - Auto-resolve Open tickets (any source) whose req_id has left the feed
    #   - Auto-resolve Open AUTO tickets whose fp is gone (live-posting case)
    #   - Re-open Resolved AUTO tickets whose fp is back
    merged = []
    reopened = 0
    auto_resolved = 0
    vanished_resolved = 0
    reopened_indices = set()
    for idx, t in enumerate(previous):
        fp = _fp(t)
        req_id = t.get("req_id", "")
        status = t.get("status", "")
        det    = t.get("detected_by", "")

        if (live_req_ids is not None
                and status == "Open"
                and req_id
                and req_id not in live_req_ids):
            t = dict(t)
            t["status"] = "Resolved"
            t["notes"]  = (t.get("notes", "") +
                           f" | Auto-resolved {today}: job no longer in Hireology feed"
                          ).strip(" |")
            vanished_resolved += 1

        elif det == "AUTO" and status == "Open" and fp not in new_fps:
            t = dict(t)
            t["status"] = "Resolved"
            t["notes"]  = (t.get("notes", "") + f" | Auto-resolved {today}").strip(" |")
            auto_resolved += 1

        elif det == "AUTO" and status == "Resolved" and fp in new_fps:
            # Only re-open the *preferred* survivor for this fingerprint
            # (avoids re-opening every historical duplicate row).
            preferred_idx = prev_auto_index.get(fp, (None, None))[1]
            if idx == preferred_idx:
                t = dict(t)
                t["status"] = "Open"
                t["notes"]  = (t.get("notes", "") + f" | Re-flagged {today}").strip(" |")
                reopened += 1
                reopened_indices.add(fp)

        merged.append(t)

    # Determine next ticket number (ignore timestamp-style IDs > 99999)
    max_num = 0
    for t in previous:
        tid = str(t.get("ticket_id", ""))
        m = re.search(r'(\d+)$', tid)
        if m:
            num = int(m.group(1))
            if num <= 99999:
                max_num = max(max_num, num)

    # Add genuinely new AUTO tickets:
    #   - Skip if already Open in the previous data
    #   - Skip if we just re-opened a Resolved row for this fingerprint
    #   - Skip if a previous Archived / Flagged Incorrectly row exists
    #     (deliberate user decision — do NOT recreate)
    added = 0
    for t in new_tickets:
        fp = _fp(t)
        if fp in prev_open_auto_fps:
            continue
        if fp in reopened_indices:
            continue
        # Was there ANY previous AUTO ticket for this fingerprint? If so,
        # the indexing above picked the best-status survivor. If that survivor
        # was Archived or Flagged Incorrectly, respect the user decision and
        # do not recreate.
        existing = prev_auto_index.get(fp)
        if existing is not None:
            existing_rank = existing[0]
            if existing_rank >= _STATUS_RANK['Archived']:
                continue   # user explicitly archived/flagged-incorrect — skip

        max_num += 1
        _, category, itype = fp
        merged.append({
            "ticket_id":   f"QA-{max_num:04d}",
            "date_flagged": today,
            "req_id":      t["req_id"],
            "job_title":   t["job_title"],
            "community":   t["community"],
            "severity":    t["severity"],
            "check":       f"{category} \u2014 {itype}",
            "category":        category,
            "issue_type":  itype,
            "summary":     t.get("summary", ""),
            "offending":   t.get("offending", ""),
            "detected_by": t.get("detected_by", "AUTO"),
            "status":      "Open",
            "notes":       "",
            "job_url":     t.get("job_url", ""),
        })
        added += 1

    resolved   = sum(1 for t in merged if t.get("status") == "Resolved")
    still_open = sum(1 for t in merged if t.get("status") == "Open")
    print(f"  {added} new  |  {reopened} re-opened  |  "
          f"{auto_resolved} auto-resolved  |  "
          f"{vanished_resolved} vanished-req auto-resolved  |  "
          f"{still_open} open total\n")
    return merged


def load_email_controls(db_path):
    """DEPRECATED (2026-04-16). No longer called from main().

    Was part of the backup/restore cycle. email_controls is now a managed
    table that persists across runs. Kept for reference."""
    if not os.path.exists(db_path):
        return {}
    try:
        con, tmp_path = _db.read_copy(db_path)
        controls = _db.load_all_email_controls(con)
        con.close()
        _db.cleanup_tmp(tmp_path)
        return controls
    except Exception:
        return {}


def load_pending_tickets(db_path):
    """DEPRECATED (2026-04-16). No longer called from main().

    Was part of the backup/restore cycle that pre-dated the merge-based
    write_db(). Kept for reference; will be removed in a future cleanup.

    Returns (list[dict], list[dict]) for pending_tickets and rejected_issues.
    """
    if not os.path.exists(db_path):
        return [], []
    try:
        con, tmp_path = _db.read_copy(db_path)
        pending, rejected = _db.load_pending_and_rejected(con)
        con.close()
        _db.cleanup_tmp(tmp_path)
        return pending, rejected
    except Exception as e:
        print(f"  [!] Could not load pending/rejected tables: {e}")
        return [], []


def restore_pending_tickets(db_path, pending_rows, rejected_rows):
    """DEPRECATED (2026-04-16). No longer called from main().

    Was part of the backup/restore cycle. write_db() now merges into the
    existing DB, so managed tables persist across runs untouched.
    """
    if not pending_rows and not rejected_rows:
        return
    try:
        con, tmp_path = _db.read_copy(db_path)
        if pending_rows:
            _db.restore_pending_rows(con, pending_rows)
            print(f"  Preserved {len(pending_rows)} pending review ticket(s) across DB rebuild.")
        if rejected_rows:
            _db.restore_rejected_rows(con, rejected_rows)
            print(f"  Preserved {len(rejected_rows)} retired check tombstone(s) across DB rebuild.")
        con.commit()
        con.close()
        _db.write_copy(tmp_path, db_path)
    except Exception as e:
        print(f"  [!] Could not restore pending/rejected tables: {e}")


CHECK_DESCRIPTIONS = {
    "Missing Community Name":
        "Community name is blank in the API response. The posting may not be properly attributed to a community.",
    "Missing Address":
        "No address, city, or state data found for this posting in the API.",
    "HTML -- Inline Font Size":
        "Custom font sizes embedded in HTML, almost always from copying out of Word. "
        "Example: a heading styled with font-size:14pt instead of just bold.",
    "Tone -- Excessive Exclamation Points":
        "5 or more exclamation points in a single posting. "
        'Example: "Join our team today! Great pay! Fun environment! Apply now!!!"',
    "Missing Required Section":
        "One or more of the 7 required sections is completely absent: Community Intro, Who We Are, "
        "What We Offer, Job Description, Responsibilities, Qualifications, EEO Statement.",
    "Title / Description Mismatch":
        "Posted job title doesn't match the first heading inside the description. "
        'Example: title says "Cook" but description opens with "Food Service Worker - Part Time".',
    "Tone -- Resident/Patient Mix":
        'Uses "residents" in some places and "patients" or "clients" in others. '
        'Example: "We care for our residents... patient care is our top priority."',
    "Highlighted / Colored Text":
        "Background-color or highlight styles carried over from Word -- visible as colored blocks "
        "on the live careers page. Usually yellow fill-in cues that were never removed.",
    "Raw Email in Description":
        "A @cedarline.example.com address appears in the posting body. "
        'Example: "Send your resume to hiring@willowbend.cedarline.example.com"',
    "Missing Brand Boilerplate":
        'CEO quote or Cedarline founding story absent from "Who We Are". '
        'Must include: "Our highest aim..." -- Marian Ellsworth, CEO, and the Founded in 2012 language.',
    "Generic Location Language":
        "Opening paragraph doesn't name the specific community. "
        'Example: "We are looking for a Cook at a large senior living community in Bountiful."',
    "Missing EEO Statement":
        '"Equal opportunity employer" phrase not found. Required on every posting for legal compliance.',
    "Body Text in ALL CAPS":
        "Multiple words in full capitals used for emphasis in the body text. "
        'Example: "MUST HAVE experience. GREAT benefits. APPLY NOW."',
    # Legacy recipe key kept so any pre-rename tickets still render email copy.
    "Excessive ALL CAPS Emphasis":
        "Multiple words in full capitals used for emphasis in the body text. "
        'Example: "MUST HAVE experience. GREAT benefits. APPLY NOW."',
    "Inconsistent Wage Figures":
        "More than 2 distinct dollar amounts -- suggests conflicting pay info. "
        'Example: "$18/hr" in the intro and "$20/hr" in What We Offer.',
    "HTML -- Colored Text Span":
        "Non-black text color via inline span styles. "
        'Example: <span style="color:#FF0000">Important requirement</span>',
    "Bonus/Pay in Job Title":
        "Dollar amounts or bonus language in the job title. Indeed demotes these in search results. "
        'Example: "CNA -- $2,000 Sign On Bonus, Night Shift"',
    "HTML -- Underline Tag":
        "<u> tags on plain text (not links). Looks like a broken hyperlink to candidates. "
        "Example: <u>Must hold a valid CNA license</u>",
    "Posting Too Short":
        "Under 200 words -- almost certainly missing required sections.",
    "Unfilled Placeholder":
        "Template fill-in brackets still present in a live posting. "
        'Example: "[INSERT COMMUNITY NAME]", "[ADD SHIFT DETAILS HERE]"',
    "Posting Too Long":
        "Over 1,200 words -- may have duplicate boilerplate from two templates merged together.",
    "Compensation -- Single Wage Figure":
        "A single wage listed rather than a range. A range (e.g. $18-$22/hr) is preferred.",
    "HTML -- Inline Font-Size Override":
        "Inline font-size on body text causing inconsistent sizing on the live page.",
    "HTML -- Inline Text Color Override":
        "Non-default text color applied inline. All body text should be plain black.",
    "Title Formatting -- ALL CAPS":
        'Job title written in ALL CAPITALS. Should use Title Case. '
        'Example: "CERTIFIED NURSING ASSISTANT" -> "Certified Nursing Assistant"',
    "Tone -- Inconsistent Resident Terminology":
        'Alternates between "residents," "patients," and "clients". "Residents" is the Cedarline standard.',
}


def write_db(tickets, db_path):
    """Merge tickets into the existing database.

    Rearchitected 2026-04-16 (Phase 1 of Check Management feature). The old
    implementation created a fresh empty DB on every run and rebuilt everything
    from scratch — including re-deriving email_controls rows from ticket data,
    which resurrected retired pairs. The new implementation opens the existing
    DB and refreshes only the tickets table. Managed tables are never touched:

        - email_controls   — managed via Controls page UI
        - rejected_issues  — managed via Retire/Revive UI
        - issue_aliases    — managed via Cowork sessions
        - custom_areas     — managed via Cowork sessions

    If the DB doesn't exist yet (first run), a fresh schema is created and
    email_controls is bootstrapped from the ticket data so the app isn't
    empty on first launch.
    """
    first_run = not os.path.exists(db_path)

    if first_run:
        # First run: create the DB from scratch
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        con = sqlite3.connect(tmp.name)
        con.row_factory = sqlite3.Row
        _db.create_fresh_db_schema(con)
    else:
        # Subsequent runs: open the existing DB
        con, tmp = _db.read_copy(db_path)

    _db.ensure_tables(con)  # idempotent — adds any missing auxiliary tables/columns

    # Template-match step (Templates Category, 2026-04-21). Before writing
    # tickets, redirect any finding whose offending_text exactly matches a
    # captured template pattern to the Templates pair. The original pair is
    # preserved in captured_from for audit. Retired templates (no row in
    # email_controls) contribute no entries to template_map, so their
    # findings fall through to their natural pair — the "regressions stay
    # loud" behavior the maintainer signed off on (2026-04-21). See CLAUDE.md
    # "Templates Category" standing rule.
    template_map = _db.get_template_patterns(con)
    if template_map:
        rewrites = 0
        for t in tickets:
            hit = _tax.match_template(t.get('offending_text', '') or t.get('offending', ''),
                                      template_map)
            if not hit:
                continue
            new_category, new_itype = hit
            old_category = t.get('category', '') or 'Content'
            old_itype    = t.get('issue_type', '') or 'Unknown'
            if (new_category, new_itype) == (old_category, old_itype):
                continue
            t['captured_from'] = f'{old_category} / {old_itype}'
            t['category']      = new_category
            t['issue_type']    = new_itype
            rewrites += 1
        if rewrites:
            print(f"  [template-match] {rewrites} AUTO ticket(s) redirected to "
                  f"Templates-Category pairs (captured_from preserved for audit).")

    # Refresh the tickets table: UPDATE existing rows in place + INSERT
    # genuinely new rows. Replaces the previous DELETE-everything-then-
    # re-INSERT pattern (refactored 2026-05-21, "Refactor write_db to
    # UPDATE existing tickets instead of DELETE+INSERT" internal ticket).
    #
    # The merged list from merge_tickets() always contains every previous
    # ticket (sometimes status-modified, sometimes carried forward
    # unchanged) PLUS any genuinely new findings from this run. So:
    #
    #   - Any ticket_id already in the DB is an "existing" row to update.
    #     update_existing_ticket() writes ONLY the columns the pipeline
    #     can change (status, notes, category, issue_type, captured_from).
    #     Every other column — reason, last_action_*, severity, etc. —
    #     stays exactly as it was.
    #   - Any ticket_id NOT already in the DB is genuinely new.
    #     insert_new_ticket() writes a full row.
    #
    # This eliminates the round-trip wipe failure mode: adding a new
    # column to the tickets schema no longer requires updating two
    # other functions. Columns survive by default.
    existing_ids = {row[0] for row in con.execute('SELECT ticket_id FROM tickets')}
    new_inserts = 0
    updates = 0
    for t in tickets:
        # Section backfill (2026-04-22): when a ticket has no Section
        # set yet, derive it from the issue_type via the taxonomy default.
        # Cross-cutting types resolve to None and stay NULL. Section-
        # scoped types like 'Title/Description Mismatch' pick up
        # 'Job Title', 'Inconsistent Wage Figures' picks up
        # 'What We Offer — Pay', etc. Preserves any section previously
        # set by run_ai_review.py (AI-driven nuance wins over the
        # static default).
        #
        # Post-Option-B note: this backfill effectively only takes effect
        # at INSERT time for new tickets, because update_existing_ticket()
        # does NOT include `section` in its UPDATE list (decision: keep
        # the UPDATE set narrow to the columns the pipeline actively
        # modifies). Existing tickets' section values are preserved
        # exactly as-is.
        if not t.get('section'):
            t['section'] = _tax.default_section_for(t.get('issue_type', ''))
        tid = t.get('ticket_id', '')
        if tid in existing_ids:
            _db.update_existing_ticket(con, t)
            updates += 1
        else:
            _db.insert_new_ticket(con, t)
            new_inserts += 1
    print(f"  Tickets table: {updates} updated, {new_inserts} newly inserted")

    if first_run:
        # Bootstrap email_controls from ticket data (one-time only).
        # This ensures a fresh install has all discovered check types
        # registered. Subsequent runs never touch email_controls here —
        # it's managed exclusively through the Controls page UI.
        all_pairs = _db.get_distinct_pairs_from_tickets(con)
        for category, itype in sorted(all_pairs):
            if category and itype:
                con.execute("""
                    INSERT OR IGNORE INTO email_controls
                      (category, issue_type, email_setting, show_on_community, scope)
                    VALUES (?, ?, 'Include in emails', 1, 'ALL')
                """, (category, itype))
        print(f"  First run: bootstrapped {len(all_pairs)} email_controls "
              f"pair(s) from ticket data.")

    con.commit()
    con.close()

    if first_run:
        _db.write_copy(tmp, db_path)
    else:
        _db.write_copy(tmp, db_path)
    print(f"  Saved: {db_path}  ({len(tickets)} tickets)")



# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch Hireology jobs and run programmatic QA checks.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and check but don't write files.")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Hireology QA v2 -- Fetch & Pre-Check")
    print(f"{'='*60}\n")

    # Note (2026-04-16): email_controls, rejected_issues, issue_aliases, and
    # custom_areas are now persistent managed tables. write_db() merges into
    # the existing DB instead of rebuilding from scratch. No backup/restore
    # cycle needed — those tables survive across runs untouched.

    # -- Fetch -----------------------------------------------------------------
    print("PHASE 1: Fetching jobs from API ...\n")
    jobs = fetch_all_jobs()
    print(f"\n  {len(jobs)} jobs fetched.\n")

    if not jobs:
        print("  No jobs returned. Check the API URL.")
        return

    # -- Programmatic checks ---------------------------------------------------
    print("PHASE 2: Running programmatic QA checks ...\n")
    if _PRESCAN_AVAILABLE:
        print("  pre_scan.py found — running extended HTML/tone/boilerplate checks.\n")
    else:
        print("  WARNING: pre_scan.py not found — skipping extended checks.\n")

    all_tickets = []
    flagged = 0
    for job in jobs:
        t = check_job(job)

        # Also run pre_scan checks (inline HTML, exclamation points, boilerplate, spelling)
        if _PRESCAN_AVAILABLE:
            prescan_issues, _brief, _plain = _run_prescan(job)
            req_id    = str(job.get('id', ''))
            title     = (job.get('name') or '').strip()
            community = (job.get('organization', {}) or {}).get('name', '').strip()
            for issue in prescan_issues:
                # prescan now returns category + issue_type separately
                category = issue.get('category', '')
                issue_type = issue.get('issue_type', '')
                t.append({
                    'req_id':      req_id,
                    'job_title':   title,
                    'community':   community,
                    'severity':    issue.get('severity', 'MEDIUM'),
                    'category':        category,
                    'issue_type':  issue_type,
                    'summary':     issue.get('issue_summary', ''),
                    'offending':   (issue.get('offending_text', '') or '')[:300],
                    'detected_by': 'AUTO',
                })

        all_tickets.extend(t)
        if t:
            flagged += 1

    print(f"  {flagged}/{len(jobs)} jobs flagged  ->  {len(all_tickets)} total tickets\n")

    # Print breakdown
    from collections import Counter
    display_checks = []
    for t in all_tickets:
        category = t.get("category", "")
        issue_type = t.get("issue_type", "")
        if category and issue_type:
            display_checks.append(f"{category} — {issue_type}")
        else:
            display_checks.append(t.get("check", ""))
    for check, count in Counter(display_checks).most_common():
        print(f"    {count:>3}  {check}")
    print()

    if args.dry_run:
        print("  DRY RUN -- no files written.\n")
        return

    # -- Save raw JSON (for Claude skill) --------------------------------------
    print("PHASE 3: Saving output files ...\n")
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {JSON_OUT}  ({len(jobs)} jobs)")

    # -- Merge with previous tickets (resolution tracking) ---------------------
    print("PHASE 4: Merging with previous ticket history ...\n")
    previous = load_previous_tickets(DB_OUT)
    if previous:
        print(f"  Loaded {len(previous)} previous tickets from {DB_OUT}")
    else:
        print(f"  No previous ticket history found -- starting fresh.")
    # The Hireology fetch returned `jobs`; its ids are the authoritative set
    # of still-live req_ids. Any previous Open ticket whose req_id isn't here
    # belongs to a posting that's been removed from Hireology and should
    # auto-resolve regardless of detected_by. The early-return at line ~835
    # on `if not jobs:` guarantees we never reach here with an empty fetch,
    # so this set is never spuriously empty.
    live_req_ids = {str(j.get('id', '')) for j in jobs if j.get('id')}
    merged = merge_tickets(previous, all_tickets, live_req_ids=live_req_ids)

    # -- Write SQLite database -------------------------------------------------
    write_db(merged, DB_OUT)

    print(f"\n{'='*60}")
    print(f"  Done. Ticket history updated with resolution tracking.")
    print(f"  Next step: open Cowork and run the QA Skill")
    print(f"  for AI-level analysis (style guide, HTML formatting, etc.)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()