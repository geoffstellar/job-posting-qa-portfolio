"""
run_ai_review.py -- AI-powered QA review of Cedarline Senior Living job postings.

Pipeline per job:
  1. pre_scan.py  runs regex checks → writes AUTO tickets immediately, builds
                  a structured brief and clean plain text for Claude.
  2. Claude API   receives clean text + brief + focused system prompt.
                  Only evaluates what regex cannot determine.
  3. Result merge known (category, issue_type) pairs (registered in email_controls)
                  → live tickets table.
                  Unknown pairs → silently dropped (logged to console).

Check lifecycle (2026-04-16, updated 2026-04-24): email_controls is the
managed source of truth. New checks are added exclusively through the
Claude qa-rules-maintenance skill (the in-dashboard Add Check wizard was
removed 2026-04-24). Unknown AI-emitted pairs are silently dropped — NOT
sent to pending_tickets (that system is retired). See CLAUDE.md for the
full check lifecycle.

Flags:
  --all          Re-review every job (skips the "already reviewed" guard).
  --batch        Submit to Anthropic's Message Batches API (async, ~50% cheaper).
                 Auto-downgrades to streaming if fewer than BATCH_MIN_JOBS (50) to
                 review — batch has up to a 24hr SLA and is slower for small runs.
  --force-batch  Honour --batch even for small runs (skip the auto-downgrade).
  --dump-prompt  Write the assembled system prompt to _last_prompt.txt and exit
                 without calling the API (useful for inspecting what's sent).

Usage:
    python run_ai_review.py                       # Streaming mode (default)
    python run_ai_review.py --all                 # Re-review all jobs, streaming
    python run_ai_review.py --batch --all         # Full re-run, batch if enough jobs
    python run_ai_review.py --batch --force-batch # Force batch even for small runs
    python run_ai_review.py --dump-prompt         # Dump the system prompt and exit
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date

_JSON_REPAIR_MAX_RETRIES = 1   # number of "fix this JSON" follow-ups before giving up

BASE      = os.path.dirname(os.path.abspath(__file__))

# Env-driven paths (Phase 3, 2026-04-22). Same contract as qa_dashboard.py:
# .env is loaded if present, existing env vars win, and DB/JOBS_RAW paths
# default to the repo folder when nothing is set.
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, '.env'))

# Centralised DB access and taxonomy — see db.py and taxonomy.py.
import db as _db
import taxonomy as _tax

DB        = _db._resolve_sqlite_path(None)
_APP_DATA_DIR = os.environ.get('APP_DATA_DIR') or BASE
JOBS_RAW  = os.environ.get('JOBS_RAW_PATH') or os.path.join(_APP_DATA_DIR, 'jobs_raw.json')
# style_guide.md lives in docs/ now; keep a legacy fallback for older layouts.
_STYLE_CANDIDATES = [
    os.path.join(BASE, 'docs', 'style_guide.md'),
    os.path.join(BASE, 'style_guide.md'),
]
STYLE_MD  = next((p for p in _STYLE_CANDIDATES if os.path.exists(p)),
                 _STYLE_CANDIDATES[0])

#  TESTING: Sonnet streaming for iteration speed. Flip back to
#  'claude-haiku-4-5-20251001' + --batch for production rollout once params are dialed in.
MODEL      = 'claude-sonnet-4-6'             # Sonnet: use during active testing; pair with streaming (no --batch)
MAX_TOKENS = 1024                            # QA findings are short; 1024 is plenty


# ── Structured output (tool_use) ─────────────────────────────────────────────
# 2026-04-21: switched from free-form JSON-in-text to tool_use. The model
# was occasionally preceding the JSON array with a prose preamble ("I'll
# analyze this posting carefully..."), costing a repair round-trip on
# ~25% of jobs. tool_use forces structured output through the API — no
# preamble is possible, no markdown fence is possible, parsing is direct
# json (not regex-spliced text). tool_choice={'type':'tool','name':...}
# pins the choice so Claude is required to call submit_findings.
#
# Schema mirrors the fields the old prompt demanded; see system prompt's
# "Return your findings as a JSON array" section for the field-by-field
# contract those strings should describe.

FINDINGS_TOOL = {
    'name': 'submit_findings',
    'description': (
        'Submit the list of QA findings for the posting. '
        'Always call this tool, even when the list is empty.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'findings': {
                'type': 'array',
                'description': (
                    'Array of QA findings. Empty array if the posting has '
                    'no issues to report.'
                ),
                'items': {
                    'type': 'object',
                    'properties': {
                        # NOTE (2026-05-21): `severity` was previously a
                        # required AI-emitted field, then overridden server-
                        # side by the email_controls.default_severity lock.
                        # The lock is now sole source of truth and the AI
                        # no longer emits severity at all. CRITICAL RULE in
                        # SYSTEM_PROMPT_TEMPLATE forbids re-introducing it.
                        'category': {
                            'type': 'string',
                            'enum': ['Tone', 'Content', 'Formatting', 'Structure'],
                            'description': (
                                'One of the four AI-emittable canonical '
                                'Category values from taxonomy.CATEGORIES_AI_EMITTABLE. '
                                'Templates is system-managed (assigned by the '
                                "routing layer's template-match step) and MUST "
                                'NOT be emitted by the AI.'
                            ),
                        },
                        'section': {
                            'type': 'string',
                            'enum': [
                                'Title',
                                'Community Intro',
                                'Who We Are',
                                'Role Overview',
                                'Responsibilities',
                                'Qualifications',
                                'What We Offer — Pay',
                                'What We Offer — General',
                                'EEO Statement',
                                'How to Apply',
                                'Cross-cutting',
                            ],
                            'description': (
                                'WHERE in the posting the issue is located. '
                                'One of the 11 canonical Section values. Use '
                                "'Cross-cutting' when the issue isn't tied to "
                                'one specific section (typos anywhere, tone '
                                'drift across the posting, formatting issues '
                                "that aren't section-specific). Cross-cutting "
                                'does NOT mean "appears multiple times" — it '
                                'means "not tied to one specific section." '
                                'Every finding must pick exactly one Section.'
                            ),
                        },
                        'issue_type': {
                            'type': 'string',
                            'description': (
                                'Specific problem. When confidence is "matched", '
                                'must exactly match a pair in the Known Issue '
                                'Type Menu from the system prompt.'
                            ),
                        },
                        'issue_summary': {
                            'type': 'string',
                            'minLength': 1,
                            'description': (
                                'One clear sentence explaining the problem. '
                                'MUST NOT be empty — if you cannot describe the '
                                'problem, do not emit the finding.'
                            ),
                        },
                        'offending_text': {
                            'type': 'string',
                            'description': (
                                'Direct quote of the problematic text, max 50 '
                                'words. Empty string when not applicable.'
                            ),
                        },
                        'confidence': {
                            'type': 'string',
                            'enum': ['matched', 'new'],
                        },
                        'closest_category': {
                            'type': 'string',
                            'description': (
                                'When confidence is "new", the closest '
                                'canonical Category (one of Tone, Content, '
                                'Formatting, Structure). Empty string when '
                                'confidence is "matched".'
                            ),
                        },
                        'closest_issue_type': {
                            'type': 'string',
                            'description': (
                                'When confidence is "new", the closest '
                                'issue_type from the menu. Empty string when '
                                'confidence is "matched".'
                            ),
                        },
                    },
                    'required': [
                        'category', 'section', 'issue_type',
                        'issue_summary', 'offending_text', 'confidence',
                    ],
                },
            },
        },
        'required': ['findings'],
    },
}


def _extract_findings(response_or_message, client=None, label=''):
    """Pull the findings list out of a tool_use response.

    Accepts either the full Anthropic response object (from client.messages.create)
    or the .message sub-object (from batch results). Looks for a content block
    of type 'tool_use' with the matching tool name and returns its
    input['findings'] array. If the model fell back to a text response, uses
    the legacy _try_parse_json path as a safety net.
    """
    # Normalize: both Response and MessageBatchResult.result.message have .content
    blocks = getattr(response_or_message, 'content', None) or []
    # Tool-use path (expected happy case)
    for block in blocks:
        if getattr(block, 'type', None) == 'tool_use' \
                and getattr(block, 'name', None) == FINDINGS_TOOL['name']:
            data = getattr(block, 'input', None) or {}
            findings = data.get('findings', [])
            if isinstance(findings, list):
                return findings
            raise ValueError(
                f'tool_use input.findings not a list{f" ({label})" if label else ""}: '
                f'{type(findings)}'
            )
    # Fallback: model returned plain text (shouldn't happen with tool_choice
    # forced, but defense in depth). Concatenate text blocks and re-use the
    # legacy JSON parser with repair.
    text_parts = [getattr(b, 'text', '') for b in blocks if getattr(b, 'type', None) == 'text']
    text = ''.join(text_parts).strip()
    if text:
        return _try_parse_json(text, client=client, label=label)
    raise ValueError(
        f'No tool_use block and no text content in response{f" ({label})" if label else ""}'
    )



# ── API key ────────────────────────────────────────────────────────────────────

def load_api_key():
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if key:
        return key
    env_path = os.path.join(BASE, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('ANTHROPIC_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"\'')
    return None


# ── DB helpers ─────────────────────────────────────────────────────────────────

# ── DB helpers — delegated to db.py ───────────────────────────────────────────
# Thin wrappers so existing call sites (read_db, write_db, etc.) keep working
# while all SQL lives in one file.

def read_db():
    return _db.read_copy()

def write_db(tmp_path):
    _db.write_copy(tmp_path)

def get_reviewed_req_ids(con):
    return _db.get_reviewed_req_ids(con)

def get_max_ticket_num(con):
    return _db.get_max_ticket_num(con)

def ensure_pending_table(con):
    _db.ensure_tables(con)


# ── System prompt ──────────────────────────────────────────────────────────────

# The sections for HTML formatting, EEO, brand boilerplate, and exclamation
# points are intentionally omitted — they are handled by the automated
# pre-scan and do not require AI evaluation.
SYSTEM_PROMPT_TEMPLATE = """You are a QA analyst for Cedarline Senior Living reviewing job postings.

IMPORTANT: An automated pre-scan has already checked for and logged tickets covering:
HTML formatting issues (font sizes, text colors, underlines, highlighted text),
excessive exclamation points, missing brand boilerplate, missing EEO statements,
unfilled template placeholders, raw email addresses, inconsistent wage figures,
pay/bonus in job title, title/description mismatches, and ALL CAPS emphasis.
Do NOT flag any of these. Each user message will include a PRE-SCAN COMPLETE
section listing exactly what was already checked for that specific posting.

Your job is to evaluate only what automated checks cannot determine: missing
required content sections, tone quality, community name accuracy, compensation
clarity, and other contextual issues requiring genuine judgment.

Be conservative — only flag real problems with clear evidence. If a section is
present under an alternate heading, do not flag it as missing. When in doubt,
do not flag.

CRITICAL RULES (learned from reviewer feedback — violations of these will be rejected):

1. FILLER PHRASES ARE ACCEPTABLE. Do NOT flag conversational closing phrases like
   "If you are the right candidate, then we want to hear from you!", "If this is
   how you like to work, then we want to talk to you!", or similar calls-to-action.
   These are intentional and make postings feel human.

2. WARM/EXPRESSIVE LANGUAGE IS ON-BRAND. Cedarline's voice is warm and aspirational.
   Do NOT flag phrases like "cherished senior residents", "nurturing soul", "beacon
   of comfort", "Perfect for students!", or similar emotionally expressive language
   as informal or unprofessional. Only flag language that is genuinely unprofessional
   (slang, text-speak, crude humor) or that obscures the actual job duties.

3. STANDARD ACRONYMS IN ALL CAPS ARE NOT VIOLATIONS. PRN, QMAP, CNA, LPN, RN, OT,
   COTA, DOE, FT, PT, NOC and similar healthcare/industry acronyms are always written
   in all caps. Do not flag these as ALL CAPS issues. Only flag ALL CAPS if the entire
   title or a non-acronym word is unnecessarily capitalized.

4. THE TEXT YOU RECEIVE HAS BEEN STRIPPED OF HTML. Bullet points and formatting may
   not be visible. Do NOT flag missing bullet formatting. Only flag formatting issues
   if content is clearly a run-on paragraph with no logical structure.

5. RESPONSIBILITIES LENGTH — FLOOR AND CEILING (2026-04-15 second wave).
   The Responsibilities section has a bullet-count expectation: at least 2
   bullets, at most 15. Enforce it via these two pairs (both live in the
   KNOWN ISSUE TYPE MENU as Responsibilities-category checks):

     - Responsibilities / Responsibilities Too Short — fewer than 2 bullets
       of real duty content. HIGH severity. offending_text should quote the
       truncated content or say "1 bullet present" / "no bullets present".

     - Responsibilities / Responsibilities Too Long — more than 15 bullets
       of real duty content. MEDIUM severity. offending_text should state
       the bullet count, e.g. "22 bullets (maximum 15)".

   There is NO separate "Posting Too Short" check — the whole-posting
   word-count rule was retired 2026-04-15 (second wave). If a posting is
   thin overall, the cause is almost always a missing section and should
   be flagged as Structure / Missing Required Section (rule 13a), not as
   "Posting Too Short".

6. SINGLE WAGE FIGURES ARE ACCEPTABLE. "Starting at $19" is fine. Do not flag the
   absence of a pay range. Only flag compensation if figures genuinely conflict or
   are ambiguous.

7. GENERIC COMMUNITY LANGUAGE IS LOW SEVERITY. If the opening uses "a large senior
   living community" instead of naming the community, flag as LOW severity only.

8. RESIDENT/PATIENT MIX — CLINICAL-DRIFT FLAG, NOT A SYNONYM FLAG (2026-04-21
   rewritten for clarity). "Residents" and "seniors" are interchangeable in
   Cedarline's voice and must NOT trigger this check. The check is ONLY for
   clinical-terminology drift — when a posting otherwise written for a
   senior-living audience slips into clinical language like "patients" or
   "clients" where "residents" (or "seniors") belongs.

   When you DO emit this finding, write the issue_summary so it names the
   drift explicitly: cite the clinical word, cite the senior-living word(s)
   the rest of the posting uses, and frame it as a voice/audience issue.

   Good summary: "Clinical terminology drift: 'patient' appears in the
     Responsibilities section ('Answer patient calls') while the rest of
     the posting uses 'residents' and 'seniors'. Cedarline's voice is senior-
     living; swap 'patient' for 'resident'."
   Bad summary: "The posting mixes terminology." (too vague — doesn't name
     the clinical word, doesn't name the correct alternative, doesn't
     explain why it matters)

   Borderline: "patient calls" or "call lights" are senior-living conventions
     in some care settings. Apply judgment — if the posting is clearly a
     direct-care role (CNA, Med Aide, Caregiver) and the only clinical word
     is a fixed compound like "patient call", prefer NOT to flag unless the
     posting uses "patient" as a general noun elsewhere too.

9. WARM/EMOTIONAL LANGUAGE IS ON-BRAND — see Rule 2. This applies to Informal
   Language flags as well: do NOT flag emotionally warm, aspirational, or
   motivational phrasing ("beacon of comfort", "nurturing soul", "cherished
   residents", "Perfect for students!", "We get to work where people live!")
   as Informal Language. Informal Language should only fire on genuine slang,
   text-speak ("u", "tbh", "lol"), crude humor, or phrasing that undermines
   professionalism (e.g. "gonna wanna hit us up").

10. PRACTICAL ROLES STILL NEED ≥2 RESPONSIBILITIES BULLETS. Cook, Dishwasher,
    Housekeeper, Maintenance Technician / Maintenance Tech, Driver, Server,
    and Dining Room Server roles legitimately have shorter Responsibilities
    and Qualifications sections. 2–4 bullets for these roles is fine. Do NOT
    upflag a 4-bullet Cook posting just because clinical roles have more.
    However, even practical roles must meet the floor of 2 bullets — a
    1-bullet or 0-bullet Responsibilities section for ANY role is still
    flagged as Responsibilities / Responsibilities Too Short (rule 5).
    Qualifications does not have a bullet-count floor/ceiling of its own.

11. ACCEPTABLE ADDENDUM SECTIONS. "Still Undecided?" (or "Still Undecided")
    following a "Why Us?" block is part of Cedarline's standard template. It is
    NOT duplicate content and is NOT a structure issue — ignore it.

12. BENEFITS TEMPLATE BOILERPLATE IS ACCEPTABLE. The Cedarline benefits boilerplate
    line that ends with "…holidays, 401k and more!!!" is part of the approved
    template. Do NOT flag the trailing exclamation points in that line as a tone
    issue. Pre-scan already exempts this line.

13a. "Missing Required Section" AND "Truncated Section Content" ARE
    WHOLE-POSTING (CROSS-CUTTING) CHECKS. Their category is ALWAYS "Structure" —
    NEVER a section-specific category like "Qualifications" or "Responsibilities"
    or "What We Offer — Pay" or "EEO Statement". The specific section being
    flagged goes in `offending_text` (e.g., `"Missing section: What We Offer
    — Pay"`, `"Missing section: EEO Statement"`, `"Missing section: Who We
    Are — Brand Boilerplate"`), not in `category`. This works the same way
    "Spelling and Grammar" always lives under "Content" regardless of which
    section the typo appears in.

    EVERY section-presence check consolidates here. That includes what
    used to be filed as separate section-specific pairs:
      - "EEO Statement / Missing Section"     → Structure / Missing Required Section
      - "Who We Are / Missing Brand Boilerplate" (AUTO) → Structure / Missing Required Section
      - "Responsibilities / Missing Section"  → Structure / Missing Required Section
      - "Qualifications / Missing Section"    → Structure / Missing Required Section
      - "What We Offer — <any> / Missing Section" → Structure / Missing Required Section
    Do not emit any of those section-scoped pairs. Always use Structure /
    Missing Required Section with the section name in offending_text.

    DISTINGUISH the three possible states — pick the one that fits, or skip:

    - Use **Structure / Missing Required Section** ONLY when an entire
      required section (heading AND content) is COMPLETELY ABSENT from the
      posting (e.g., no Responsibilities content anywhere, no Pay information
      whatsoever, no Community Introduction at all). The test is: can you
      find the section's subject matter in the posting text? If the
      Responsibilities content (the actual duties) appears anywhere — even
      under a heading called "Job Description" or with no heading at all —
      the section is PRESENT. Do not flag it as missing. Name the missing
      section in `offending_text` as e.g. `"Missing section: Qualifications"`.
      A posting missing multiple required sections should produce multiple
      Structure / Missing Required Section tickets — one per missing section
      — each with its own `offending_text`, exactly like typos produce
      separate Content / Spelling and Grammar tickets.

      **HEADING MATCHING IS CASE-INSENSITIVE.** "What we offer", "WHAT WE
      OFFER", "What We Offer:", and "What We Offer" all count as the same
      heading. Never flag a section as missing because its heading differs
      in capitalization, trailing punctuation, or spacing from the canonical
      form. The style guide §1a lists accepted variants for each required
      section — consult it before emitting any Missing Required Section.

      **"WHAT WE OFFER — PAY" SPECIFICALLY.** The What We Offer section
      often exists as a single block without a dedicated "Pay" subheading.
      Do NOT flag `"Missing section: What We Offer — Pay"` just because
      the posting lacks a Pay sub-heading. Only flag this when NO pay
      information appears anywhere in the posting — no dollar figures, no
      wage range, no "competitive pay" language, nothing. If a What We
      Offer section exists but omits pay specifics, that is a separate
      concern (see `What We Offer — Pay / Compensation Clarity`), NOT a
      Missing Required Section.

      **RESPONSIBILITIES THRESHOLD (added 2026-04-15):** Do NOT emit
      `"Missing section: Responsibilities"` if the posting contains TWO OR
      MORE bullet points, list items, or duty-describing sentences (sentences
      beginning with an action verb like "Assists", "Provides", "Monitors",
      "Assesses", "Coordinates", "Develops"). Two action-verb items is enough
      signal that the section is present. If the section exists but feels
      thin, that is `Responsibilities / Responsibilities Too Short` — a
      different, section-scoped check. Reserve MRS for the case where there
      is no duty-describing content anywhere in the posting.

    - Use **Structure / Truncated Section Content** when a section IS
      present but its content is incomplete, malformed, ends mid-sentence,
      ends mid-bullet, contains an orphan word fragment, or is otherwise
      visibly cut off (e.g., a Qualifications bullet that reads "Previous A
      love for seniors" with no completion). Do NOT label these as "Missing
      Required Section" — the section exists; it's the content that is
      broken. Name the truncated section in `offending_text` as e.g.
      `"Truncated section: Qualifications"` followed by the offending
      fragment.

    - DO NOT FLAG EITHER when the complaint is really about missing bullet
      formatting, a missing bolded heading, or "the section is there but
      it's a paragraph instead of a bulleted list". The posting text
      delivered to you has been STRIPPED of HTML formatting — bullets, bold,
      headings may not survive the strip even when they exist in the live
      posting (style guide §4b). Examples of what is NOT Missing Required
      Section and NOT Truncated Section Content:
        * "Responsibilities section lacks a proper heading and uses paragraph
          format instead of bulleted list format" — NOT a flag.
        * "Qualifications section lacks a bulleted list format; items are
          presented as paragraph text" — NOT a flag.
        * "No explicit What We Offer heading but the benefits are listed" —
          NOT a flag.
      If the content is clearly present and your ONLY objection is its
      visual/structural formatting, skip the flag entirely.

    Severity is locked at the controller level for both issue types
    (Structure / Missing Required Section = HIGH, Structure / Truncated
    Section Content = MEDIUM) — do not pick severity yourself.

13b. "PARALLEL STRUCTURE" IS A WHOLE-POSTING FORMATTING CHECK. Its category is
    ALWAYS "Formatting" — NEVER "Responsibilities" or "Qualifications". The
    specific section whose bullets lack parallelism goes in `offending_text`
    (e.g., `"Section: Responsibilities — bullets mix verb phrases and noun
    phrases"` or `"Section: Qualifications — bullets alternate between
    sentence fragments and full sentences"`). If two different sections both
    have parallelism problems, emit two separate Formatting / Parallel
    Structure tickets — one per section — each with its own offending_text,
    same pattern as Structure / Missing Required Section. Severity is MEDIUM
    and locked at the controller level.

13. DO NOT FLAG BRAND BOILERPLATE UNDER ANY AREA — IT IS AUTO-OWNED.
    The automated pre-scan is the single source of truth for brand boilerplate
    detection. It looks for the canonical brand markers ("Marian Ellsworth",
    "highest aim", "Cedarline life for every resident") and fires
    "Structure / Missing Required Section" at HIGH severity with
    offending_text "Missing section: Who We Are — Brand Boilerplate" when
    they are missing. If that ticket is NOT in the PRE-SCAN COMPLETE list
    for this posting, the brand boilerplate is present — you MUST NOT flag
    its absence, incompleteness, or structural placement under any category. In
    particular, when you emit a Structure / Missing Required Section ticket
    (per rule 13a), the missing section must NOT be "Who We Are", "Who We
    Are — Brand Boilerplate", or the brand statement itself. Also never flag:
      - A standalone Who We Are category issue of any kind about brand content
      - Structure / Missing Required Section where the `offending_text`
        names "Who We Are", the brand boilerplate, or the CEO quote as
        the missing section
      - Community Introduction issues whose reasoning is really about
        the brand quote, the CEO attribution, or "highest aim"
      - Any novel pair whose summary references the Cedarline brand statement,
        the CEO quote, the Marian Ellsworth attribution, "highest aim",
        "Cedarline life for every resident", or the "Who We Are" block
    If your reasoning for a flag touches any of those phrases, SKIP the flag.
    Structural placement of brand content (e.g. embedded in the opening
    paragraph rather than under a dedicated heading) is a deliberate template
    choice, not a QA issue.

14. SPELLING AND GRAMMAR — GUARDRAILS (2026-04-15, replaces retired AUTO spellcheck).
    Content / Spelling and Grammar is now fully CLAUDE-owned. The prior AUTO regex
    check (Content / Typo — Body Text) was retired because its tokenizer produced
    excessive false positives on possessives, compound words, and brand terms.
    Follow style guide §11 strictly. The key rules:

    a) CATEGORICAL EXCEPTIONS. Do not flag healthcare jargon (ADL, QMAP, CNA, LPN, RN,
       HIPAA, etc.), brand names (Cedarline, Hireology, Marian Ellsworth),
       compound words (onsite, offsite, multidisciplinary), possessives (residents',
       patients', Bachelor's), proper nouns, ALL-CAPS words, words with digits, hyphenated
       compounds, or anything inside quotation marks. These are CATEGORIES, not an
       exact-match list — give benefit of the doubt to words that clearly belong to one
       of them.

    b) SHOW YOUR WORK. Every Spelling and Grammar finding's issue_summary MUST include
       a one-sentence reason naming the specific rule violated (e.g., "clear misspelling:
       'recieve' should be 'receive'", "subject-verb disagreement: 'team are' should be
       'team is'"). If you cannot name a specific grammar or spelling rule, DO NOT emit
       the finding.

    c) REPEATED WORDS. Do NOT treat high-frequency spelling as proof of correctness.
       If a word matches an exemption category (rule 14a), skip it. If it is a clear
       misspelling of a common English word, flag it even if it appears many times —
       repeated template errors are MORE valuable to surface, not less.

    d) ONE TICKET PER ERROR. Emit one Content / Spelling and Grammar ticket per distinct
       error, each with its own offending_text and reason. Do not bundle multiple errors
       into a single ticket.

15. COMPENSATION CLARITY — ANTI-HALLUCINATION GUARDRAILS (2026-04-16, added after
    QA-1793). This check is prone to AI hallucinations where Claude claims a character
    is missing that is actually present in the source. Strict rules:

    a) QUOTE THE EXACT CHARACTER YOU CLAIM IS MISSING. A vague summary like "missing
       dollar sign on upper bound" is NOT acceptable. Write it as
       `"missing '$' before '19hr'"` or `"missing '/hr' unit after '$22'"`. The
       issue_summary must name the exact character(s) and the exact neighboring text.

    b) RE-READ BEFORE EMITTING. Before writing a Compensation Clarity ticket that
       claims a character is missing, SCAN the `offending_text` one more time,
       character-by-character, to confirm the claimed-missing character is actually
       absent. If the character you claim is missing is visible in the quoted
       offending_text, DO NOT EMIT THE TICKET. Example:
       - Input: "$15- $19hr DOE"
       - WRONG finding: "missing $ on upper bound" (both $ are clearly present)
       - RIGHT finding: "hyphen has inconsistent spacing: '$15-' has no space
         after hyphen but '$19hr' follows a space"

    c) ONE CONCERN PER TICKET. If a single pay string has multiple distinct issues
       (e.g., spacing + missing /hr unit), emit SEPARATE Compensation Clarity tickets
       — one per concern — each with its own exact-character offending_text. This
       prevents a single valid finding from being polluted by an adjacent hallucinated
       one. A candidate reviewing the tickets should be able to verify each one
       independently.

    d) COSMETIC FORMATTING IS NOT A VIOLATION (per style guide §7). Missing $ on the
       upper bound of an obvious range (e.g. "$18–22/hr") is cosmetic, not a
       Compensation Clarity finding. A single starting wage ("starting at $19") is
       acceptable. Only flag when a candidate genuinely cannot tell what the pay is.

16. TONE OF issue_summary AND suggested_fix MATCHES SEVERITY (2026-04-21, paired with
    severity recalibration + Acknowledge UX). The text you write for each ticket
    must match the severity tier:

    a) CRITICAL / HIGH — IMPERATIVE VOICE. These are hard correctness or compliance
       items. Use direct commands. Good: "Add the JAFA disclosure.", "Remove the
       recruiter email address.", "Fix the broken HTML tag.". Bad (too soft for this
       tier): "Consider adding…", "You might want to…", "This could be improved…".

    b) MEDIUM / LOW — SUGGESTIVE VOICE. These are judgment calls the community can
       accept or acknowledge-with-reason. Use softer framing. Good: "Consider
       rephrasing the opening.", "The hook could be stronger.", "This reads as
       generic — an anecdote would land better.". Bad (too forceful for this tier):
       "You must rewrite this.", "This is wrong.".

    The severity you pick is what decides the tier — write the summary and fix text
    in a voice that matches. A reader skimming the ticket list should be able to
    tell at a glance which items are mandatory vs. advisory, even without looking
    at the severity pill.

17. NEVER EMIT A FINDING WITH EMPTY CONTENT (2026-04-21, learned from a run
    that produced five unusable tickets on one Harbor Light posting). Every
    finding MUST have a non-empty `issue_summary`. If you cannot describe the
    problem in one concrete sentence naming the specific issue, DO NOT emit
    the finding — there is no point in a ticket that says nothing. The tool
    schema enforces minLength: 1 on issue_summary so the API will reject
    attempts to emit an empty one, but you should not rely on the schema to
    catch you — decide up front whether you have enough signal to write a
    useful summary before adding the finding to the array.

    `offending_text` MAY be empty in narrow cases where the finding is about
    ABSENCE (e.g., "missing section: EEO Statement" — the text that should
    be there isn't there to quote). In those cases the issue_summary must
    carry the full signal. For any finding about text that IS present (tone,
    spelling, formatting, wage inconsistency, etc.), offending_text MUST
    contain a direct quote — if you can't find one, you don't have a real
    finding.

18. MISSING COMPENSATION GOES UNDER Content / Pay Details, NOT Content /
    Generic Language (2026-04-21). If a posting has NO compensation info at
    all (no wage, no range, no "competitive pay" language), that is a Pay
    Details concern. `Generic Language` is for community-naming issues
    ("a large senior living community" without naming the community), not
    for compensation gaps. Getting this wrong buries real pay concerns in
    the wrong severity (Generic Language is LOW; Pay Details is MEDIUM).

18a. PERFORMANCE-BASED / DISCRETIONARY BONUSES ARE EXEMPT FROM Content /
    Pay Details (2026-04-27, learned from QA-2070). When a posting mentions
    a bonus, incentive, or commission whose phrasing indicates the structure
    is performance-based, discretionary, target-based, sales-incentive, or
    "discussed at interview", do NOT emit a Content / Pay Details flag for
    "missing bonus amount" or "missing bonus structure". Examples that
    should NOT fire Pay Details:
      - "Bonus: Performance-based incentive eligibility"
      - "Performance based bonuses on top of commissions"
      - "Discretionary annual bonus"
      - "Quarterly performance bonus"
      - "Sales commission structure" (without dollar/percentage)
      - "Bonus structure discussed at interview"
    These bonus structures are often complex (multi-tier, role-specific, or
    individually negotiated) and the hiring process — not the job ad — is
    the appropriate place for the mechanics. Pay Details remains in scope
    for: missing base wage/salary entirely, conflicting wage figures,
    ambiguous wage figures, and bonus figures that ARE concrete but listed
    alongside conflicting bases. A separate LOW-severity Content / Bonus
    Disclosure check (forthcoming) may surface a soft suggestion that the
    posting tell candidates where they'll learn the structure — that is a
    different concern from missing-pay-details.

19. NEVER EMIT `Templates` AS YOUR CATEGORY (2026-04-21). Templates is a
    7th, system-managed Category used to group tickets that share the same
    boilerplate text across many postings. Tickets reach Templates ONLY via
    the routing layer, which redirects a finding whose offending_text
    exactly matches a captured template pattern. You have no way to know
    which patterns are captured, and your job is to classify what the
    problem IS — Tone, Content, Formatting, or Structure. If the routing
    layer decides a finding belongs on Templates, it will redirect it
    after you emit; you should always classify the underlying issue
    honestly. If you see a pair like `Templates / X` in the known issue
    type menu (you shouldn't — it is filtered out), do not select it.
    Always pick from the four AI-emittable canonical Categories.

CANONICAL CATEGORY VALUES — the "category" field must be EXACTLY one of these
four strings. Do not paraphrase, abbreviate, or invent a variant. Pick the
best fit based on WHAT KIND of issue this is.

    Tone | Content | Formatting | Structure

  (Templates is a fifth Category but is system-managed and is NEVER
  emitted by you — see above.)

  CONSOLIDATION RULES — these issue types ALWAYS go under their designated
  Category, regardless of where in the posting the problematic text appears:
    Spelling and Grammar, Generic Language, Pay Details, Benefits Details,
      Vague Experience Range, Compensation Clarity → Content
    Resident/Patient Mix, Informal Language, ALL CAPS, Body Text in ALL CAPS,
      Excessive Exclamation Points, Informal Abbreviations → Tone
    Formatting Issue, Parallel Structure → Formatting
    Inline Font Size, Inline Text Color Override, Nested List Structure,
      Highlighted/Colored Text, Underline Tag → Formatting
    Title Formatting, Inconsistent Capitalization → Formatting
    Missing Required Section, Truncated Section Content → Structure
    Pay/Bonus in Title, Title/Description Mismatch → Structure
    Responsibilities Too Short, Responsibilities Too Long → Structure
  (Filler Phrase was retired — see Rule 1.)
  (Posting Too Short was retired 2026-04-15 second wave — Responsibilities
   length is now covered by Responsibilities Too Short / Too Long.)
  (HTML, Job Title, and Responsibilities are NO LONGER Categories as of
   2026-05-21 — see consolidation rules above for where each old Category's
   checks now route.)

CANONICAL SECTION VALUES — the "section" field describes WHERE in the
posting the issue is located. It is display-only metadata for community
readers; it NEVER affects routing, Category selection, or which check
fires. Pick EXACTLY ONE of these 11 values — no null, no empty string:

    Title | Community Intro | Who We Are | Role Overview |
    Responsibilities | Qualifications | What We Offer — Pay |
    What We Offer — General | EEO Statement | How to Apply |
    Cross-cutting

  SECTION RULES:
    - Use "Cross-cutting" when the issue isn't tied to one specific
      section — typos that could be anywhere, tone drift across the
      whole posting, formatting issues that aren't section-specific.
      "Cross-cutting" does NOT mean "appears multiple times" — it
      means "not tied to one specific section."
    - For checks about the job title string itself (Pay/Bonus in Title,
      Title/Description Mismatch, Title Formatting, Inconsistent
      Capitalization on a title) → section = "Title".
    - For pay-specific content → section = "What We Offer — Pay".
    - For benefits-specific content → section = "What We Offer — General"
      (Benefits is a sub-topic of General, not its own Section).
    - For Missing Required Section tickets where a specific section is
      missing, set section to the name of the missing section (e.g., a
      missing EEO statement gets section = "EEO Statement"). For cases
      where you can't pin a single section, set section = "Cross-cutting"
      and describe the gap in offending_text.
    - For the new "How to Apply" check (postings that include an
      application-instructions block — Cedarline treats this as a problem)
      → section = "How to Apply".
    - If the posting uses an unrecognized heading variant, fall back to
      section = "Cross-cutting". Do NOT invent new Section values.

KNOWN ISSUE TYPE MENU — these are the ONLY established (category, issue_type) pairs.
When the issue you observe matches one of these, you MUST use the exact category and
issue_type shown. Do not paraphrase, rename, or create a variant of an existing pair.
{check_type_menu}

DO NOT FLAG — these (category, issue_type) pairs have been reviewed and determined to be
invalid or unnecessary. Never flag any of these, regardless of what you observe:
{rejected_types_list}

STYLE GUIDE (sections relevant to your review):
{style_guide}

CATEGORIZATION WALKTHROUGH — for every finding, before committing to a Category
and issue_type, you MUST perform this three-step check. The menu is your source
of truth; do not reach for a new label if an existing one already covers the
problem.

  Step 1 — Scan the Known Issue Type Menu (grouped by Category). Ask: "Is my
           finding describing the same underlying problem as any existing pair,
           even if the specific text differs?" Most findings are instances of
           existing pairs with different wording — prefer matches over novel
           labels. Apply the CONSOLIDATION RULES above before anything else.

  Step 2 — If Step 1 surfaces a match, set confidence = "matched" and use the
           EXACT Category and issue_type from the menu — do not rephrase them.
           Then pick the appropriate Section (or null) for display metadata.

  Step 3 — If no menu item covers the problem, set confidence = "new", pick the
           best canonical Category, propose a short issue_type, and ALSO set
           closest_category and closest_issue_type to the menu pair that came
           closest (even if it's not a true match). Both values must be copied
           verbatim from the menu, or left as "" if nothing is remotely similar.
           Still emit the best-fit Section (or null) even for novel findings.

When in doubt between "matched" and "new", prefer "matched". Most novel-looking
findings are actually instances of existing pairs with different wording.

Submit your findings by calling the `submit_findings` tool. The tool takes
a single `findings` argument — an array of finding objects. Each finding
must have exactly these fields:
- "category": the taxonomy category — MUST be exactly one of the four
  AI-emittable Canonical Category Values: Tone, Content, Formatting,
  Structure. (Templates is a fifth Category but is system-managed —
  it's assigned by the server-side routing layer when a finding's
  offending_text matches a captured template pattern. You MUST NEVER
  emit "Templates" as a category yourself.)
- "section": WHERE in the posting the issue is located. MUST be exactly one
  of the 11 Canonical Section Values: Title, Community Intro, Who We Are,
  Role Overview, Responsibilities, Qualifications, What We Offer — Pay,
  What We Offer — General, EEO Statement, How to Apply, Cross-cutting.
  Use "Cross-cutting" when the issue isn't tied to one specific section
  (typos anywhere, tone drift across the posting, formatting issues that
  aren't section-specific). "Cross-cutting" does NOT mean "appears multiple
  times" — it means "not tied to one specific section." Every finding
  must pick exactly one Section value (no null, no empty string).
  Section NEVER affects which check fires; it is metadata for community
  readers only.
- "issue_type": the specific problem — MUST match the Known Issue Type Menu when
  confidence is "matched". When confidence is "new", use a short descriptive phrase.
- "issue_summary": one clear sentence explaining the problem
- "offending_text": a direct quote of the problematic text, 50 words max
  (use empty string if not applicable)
- "confidence": either "matched" (issue maps cleanly to an existing menu pair) or
  "new" (genuinely novel problem not covered by any menu pair)
- "closest_category": when confidence is "new", the Category of the menu pair
  that came closest (verbatim from Tone / Content / Formatting / Structure),
  or "" if nothing is remotely similar. When confidence is "matched", this
  field should be "".
- "closest_issue_type": when confidence is "new", the issue_type of the menu pair
  that came closest (verbatim from the menu), or "" if nothing is remotely similar.
  When confidence is "matched", this field should be "".

IMPORTANT: You do NOT emit a "severity" field. Severity is determined
server-side by a lock on each (category, issue_type) pair and is not
something the AI should attempt to choose. Earlier versions of this
prompt asked you to emit severity; that requirement is removed.

Always call `submit_findings`, even when the posting has no issues — in that
case, pass `findings: []`. Do NOT emit free-form text; the tool call is the
only expected response."""


def build_system_prompt(style_guide, known_pairs=None, rejected_pairs=None,
                        custom_areas=None, aliases=None,
                        fire_counts=None, max_per_area=30):
    """Build the system prompt, trimming HTML/EEO/boilerplate sections Claude no longer needs.

    Parameters
    ----------
    style_guide : str
        Raw contents of style_guide.md.
    known_pairs : iterable[tuple[str, str]] | None
        All (category, issue_type) pairs currently in email_controls. Claude must
        prefer these exact pairs over ad-hoc labels.
    rejected_pairs : iterable[tuple[str, str]] | None
        (category, issue_type) pairs that have been flagged as invalid. Claude
        must never flag these regardless of what it observes.
    custom_areas : iterable[str] | None
        User-defined areas beyond the canonical list.
    aliases : dict[(str, str), (str, str)] | None
        Alias (category, issue_type) -> canonical (category, issue_type) redirects.
    fire_counts : dict[(str, str), int] | None
        Historical fire counts keyed by pair, used to prune/sort the menu.
    max_per_area : int
        Cap on issue types listed per category in the prompt.
    """
    # Remove style guide sections now handled by pre-scan:
    # §4a Text Formatting (HTML), §5 Brand Boilerplate, §6 EEO Statement
    trimmed = style_guide

    sections_to_remove = [
        (r'### 4a\. Text Formatting.*?(?=### 4b)', re.DOTALL),
        (r'## 5\. Required Brand Boilerplate.*?(?=## 6)', re.DOTALL),
        (r'## 6\. Equal Opportunity Employer Statement.*?(?=## 7)', re.DOTALL),
    ]
    for pattern, flags in sections_to_remove:
        trimmed = re.sub(pattern, '', trimmed, flags=flags)

    # Clean up any resulting double blank lines
    trimmed = re.sub(r'\n{3,}', '\n\n', trimmed)

    # Build the issue-type menu — grouped by Category under the flat model.
    # Each row is a (category, issue_type) pair with an optional fire count.
    # No cross-cutting vs section-specific split — Category IS the only axis.
    pairs = list(known_pairs or [])
    if pairs:
        fc = fire_counts or {}
        # Prune to pairs that have actually fired at least once. Approved but
        # never-used pairs stay in known_pairs for fuzzy matching, but don't
        # bloat the prompt.
        active = [p for p in pairs if fc.get(p, 0) > 0] or list(pairs)

        cat_map = {}   # {category: [(issue_type, fire_count), ...]}
        for (cat, itype) in active:
            if not cat or not itype:
                continue
            # Templates Category (2026-04-21) is system-managed — the routing
            # layer redirects findings to it based on exact offending_text
            # match. Claude must never emit Templates directly (CRITICAL
            # RULE 19), so it is filtered out of the prompt menu entirely.
            if cat == 'Templates':
                continue
            cat_map.setdefault(cat, []).append((itype, fc.get((cat, itype), 0)))

        def _fmt_cat(cat, entries):
            by_type = {}
            for itype, n in entries:
                if itype not in by_type or n > by_type[itype]:
                    by_type[itype] = n
            ordered = sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
            truncated = ordered[:max_per_area]
            parts = [f'{t} ({n})' if n else t for t, n in truncated]
            suffix = f' [+{len(ordered) - len(truncated)} more]' if len(ordered) > len(truncated) else ''
            return f'  {cat}: ' + ', '.join(parts) + suffix

        # Render Categories in canonical order (Tone, Content, ...), then any
        # lingering non-canonical ones at the end (shouldn't happen post-flatten
        # but tolerated for safety during transition).
        menu_lines = []
        canonical_in_use = [c for c in _tax.CATEGORIES if c in cat_map]
        other = sorted(c for c in cat_map if c not in _tax.CATEGORIES_SET)
        for cat in canonical_in_use:
            menu_lines.append(_fmt_cat(cat, cat_map[cat]))
        for cat in other:
            menu_lines.append(_fmt_cat(cat, cat_map[cat]))
        check_type_menu = '\n'.join(menu_lines)
    else:
        check_type_menu = '  (none defined yet — choose the closest Category from the canonical list of six)'

    # Build rejected pairs list — grouped by Category so Claude sees them the
    # same way as the live menu.
    if rejected_pairs:
        rej_map = {}
        for (cat, it) in rejected_pairs:
            if cat and it:
                rej_map.setdefault(cat, []).append(it)
        rej_lines = []
        for cat in sorted(rej_map):
            rej_lines.append(f'  {cat}: ' + ', '.join(sorted(rej_map[cat])))
        rejected_types_list = '\n'.join(rej_lines) if rej_lines else '  (none)'
    else:
        rejected_types_list = '  (none)'

    # Under the flat model, there are no user-defined "areas" — Categories are a
    # fixed set of six. This block is kept as a no-op for backward compatibility
    # with callers that still pass custom_areas (they'll eventually stop).
    # If non-empty, custom values are surfaced so Claude sees them but treated
    # the same as canonical Categories.
    custom_areas_block = ''
    if custom_areas:
        lines = [f'  \u2022 {a}' for a in sorted(custom_areas)]
        custom_areas_block = (
            '\nCUSTOM CATEGORIES (legacy — treat as canonical):\n'
            + '\n'.join(lines) + '\n'
        )

    # Active aliases. Keys and values are (category, issue_type) tuples; we
    # render each side as "Category / Issue Type" (two fields, slash separator —
    # never a glued composite) so the two-field model is unambiguous.
    aliases_block = ''
    if aliases:
        lines = []
        for (a_area, a_it), (c_area, c_it) in sorted(aliases.items()):
            if (a_area, a_it) == (c_area, c_it):
                continue
            lines.append(f'  \u2022 "{a_area} / {a_it}"  \u2192  "{c_area} / {c_it}"')
        if lines:
            aliases_block = (
                '\nALIAS REDIRECTS (previously-proposed labels that map to canonical pairs — '
                'DO NOT use the left-hand pair; always use the right-hand canonical):\n'
                + '\n'.join(lines) + '\n'
            )

    return SYSTEM_PROMPT_TEMPLATE.format(
        style_guide=trimmed.strip(),
        check_type_menu=check_type_menu + custom_areas_block + aliases_block,
        rejected_types_list=rejected_types_list,
    )


# ── Fuzzy pair matching ────────────────────────────────────────────────────────

def fuzzy_match_pair(category, issue_type, known_pairs, threshold=0.75):
    """Return the best-matching known (category, issue_type) pair if similarity
    >= threshold, using a joint score over both fields.

    Area matches carry more weight — we require an exact (case-insensitive)
    category match OR a very strong issue_type match to collapse across areas.

    Returns
    -------
    ((best_area, best_itype), score) where best pair is None if no match met
    the threshold.
    """
    from difflib import SequenceMatcher
    if not known_pairs:
        return None, 0.0
    a_lower = (category or '').lower().strip()
    it_lower = (issue_type or '').lower().strip()
    best_pair = None
    best_score = 0.0
    for (ka, kit) in known_pairs:
        ka_l = (ka or '').lower().strip()
        kit_l = (kit or '').lower().strip()
        area_score = 1.0 if a_lower == ka_l else SequenceMatcher(None, a_lower, ka_l).ratio()
        itype_score = SequenceMatcher(None, it_lower, kit_l).ratio()
        # Weight issue_type more heavily — category drift is common, issue_type
        # carries the actual semantic meaning.
        score = 0.3 * area_score + 0.7 * itype_score
        if score > best_score:
            best_score = score
            best_pair = (ka, kit)
    if best_score >= threshold:
        return best_pair, best_score
    return None, best_score


def fuzzy_match_issue_type_only(issue_type, known_pairs, threshold=0.75):
    """Match on the issue_type portion alone, ignoring category. Returns the full
    (category, issue_type) pair of the closest known issue_type.

    Lets "Exclamation Points" and "Excessive Exclamation Points" collapse
    regardless of which category the AI attached them to.
    """
    from difflib import SequenceMatcher
    if not known_pairs or not issue_type:
        return None, 0.0
    cand = issue_type.lower().strip()
    best_pair = None
    best_score = 0.0
    for (ka, kit) in known_pairs:
        kit_l = (kit or '').lower().strip()
        score = SequenceMatcher(None, cand, kit_l).ratio()
        if score > best_score:
            best_score = score
            best_pair = (ka, kit)
    if best_score >= threshold:
        return best_pair, best_score
    return None, best_score


def apply_fuzzy_matching(issues, known_pairs, threshold=0.75, verbose=True,
                         aliases=None):
    """Normalise each issue's (category, issue_type) to a known canonical pair
    when close enough. ``known_pairs`` and ``aliases`` are tuple-keyed — no
    composite strings anywhere.

    Issues with no match pass through unchanged; route_and_write() will send
    them to pending review.

    Parameters
    ----------
    issues : list[dict]
        Each dict has separate 'category' and 'issue_type' fields.
    known_pairs : set[tuple[str, str]] | list[tuple[str, str]]
    threshold : float
    verbose : bool
    aliases : dict[(str, str), (str, str)] | None

    Returns
    -------
    list[dict] with category + issue_type normalised where possible.
    """
    known_set = set(known_pairs or [])
    result = []
    for issue in issues:
        issue = dict(issue)  # don't mutate caller's dict

        # Flat model (2026-04-17): AI now emits 'category' + 'section'. During
        # the transition window we accept legacy 'category' as a fallback so any
        # in-flight response shapes still work. Section is display-only
        # metadata — it does not participate in fuzzy matching.
        category = (issue.get('category') or issue.get('category') or '').strip()
        itype    = (issue.get('issue_type') or '').strip()
        # Normalize: Section may come through as 'null', 'None', or the actual
        # JSON null. All three should collapse to None internally.
        raw_section = issue.get('section')
        if isinstance(raw_section, str):
            raw_section = raw_section.strip()
            if raw_section.lower() in ('null', 'none', ''):
                raw_section = None
        section = raw_section if _tax.is_valid_section(raw_section) else None
        # Keep both legacy 'category' and new 'category' populated so downstream
        # code that still reads 'category' (pre-Phase 4/5 consumers) continues to
        # work unchanged. Section is persisted for the first time here.
        issue['category'] = category
        issue['category']     = category   # legacy mirror
        issue['section']  = section

        # Blanket collapse: any AI-flagged exclamation-point variant is forced
        # to the canonical (Tone, Excessive Exclamation Points) pair.
        if _tax.is_exclamation(itype, category):
            category, itype = _tax.CANONICAL_EXCL_AREA, _tax.CANONICAL_EXCL_ITYPE
            issue['category'], issue['category'], issue['issue_type'] = category, category, itype
            if verbose:
                print(f'    [collapse] exclamation-point variant \u2192 "{category} / {itype}"')

        # Consolidated-type pass: cross-cutting issue types (Generic Language,
        # Informal Language, Spelling and Grammar, etc.) must always live under
        # their canonical Category regardless of which Category the AI picked.
        # Coerce BEFORE fuzzy matching so the fuzzy matcher can't accidentally
        # overwrite them with a section-specific pair.
        coerced_cat, coerced_it = _tax.coerce_consolidated_type(category, itype)
        if coerced_cat != category:
            if verbose:
                print(f'    [consolidated] "{category} / {itype}"  \u2192  "{coerced_cat} / {coerced_it}"')
            category, itype = coerced_cat, coerced_it
            issue['category'], issue['category'], issue['issue_type'] = category, category, itype

        # Rename local var for the rest of the loop (rest of the code below
        # still expects `category` — keep the name to minimize delta).
        category = category

        # Alias pass — honour explicit "when AI says pair X, treat as pair Y"
        # rules recorded during Pending Review approvals.
        if aliases and (category, itype) in aliases:
            can_area, can_it = aliases[(category, itype)]
            if verbose:
                print(f'    [alias] "{category} / {itype}"  \u2192  "{can_area} / {can_it}"')
            issue['category'] = can_area
            issue['category']     = can_area   # legacy mirror
            issue['issue_type'] = can_it
            result.append(issue)
            continue

        if (category, itype) in known_set:
            result.append(issue)
            continue

        matched, score = fuzzy_match_pair(category, itype, known_set, threshold)
        if not matched and itype:
            matched, score = fuzzy_match_issue_type_only(itype, known_set, threshold)
            if matched and verbose:
                print(f'    [fuzzy:itype] "{itype}"  (Category ignored)')
        if matched:
            new_area, new_it = matched
            if verbose:
                print(f'    [fuzzy] "{category} / {itype}"')
                print(f'         \u2192 "{new_area} / {new_it}"  ({score:.0%})')
            issue['category'] = new_area
            issue['category']     = new_area   # legacy mirror
            issue['issue_type'] = new_it

        result.append(issue)
    return result


# ── Claude analysis (streaming mode) ──────────────────────────────────────────

def _sleep_for_retry(err, attempt):
    """Pick a wait duration for a rate-limit retry. Prefer the server's
    `retry-after` header when we can find it; otherwise use exponential
    backoff with a cap. Returns seconds to sleep."""
    try:
        resp = getattr(err, 'response', None)
        if resp is not None:
            ra = resp.headers.get('retry-after') or resp.headers.get('Retry-After')
            if ra:
                return max(1.0, float(ra)) + 0.5
    except Exception:
        pass
    return min(60.0, (2 ** attempt) * 5.0)


def _try_parse_json(raw, client=None, label=''):
    """Attempt to parse *raw* as a JSON array. On failure, ask Claude to
    repair the malformed JSON once before giving up. Returns the parsed
    list on success; raises on unrecoverable failure.

    Robust to common non-fatal response shapes:
      - empty / whitespace-only responses (clearer error than "char 0")
      - markdown fences anywhere in the response (not just at the start),
        including when preceded by preamble like "Here is the JSON:"
      - leading preamble or trailing postamble around the JSON array
        (e.g. "<thinking>...</thinking>\n[ ... ]" or
        "Here are the issues:\n\n[ ... ]\n\nLet me know if you need more.")
    """
    cleaned = (raw or '').strip()

    # Empty response — fail fast with a clearer error than the cryptic
    # "Expecting value: line 1 column 1 (char 0)" that json.loads gives.
    if not cleaned:
        raise ValueError(
            f'Empty response from model{f" ({label})" if label else ""} '
            '\u2014 nothing to parse as JSON.'
        )

    # Strip a markdown code fence anywhere in the string. The old code only
    # handled fences at the very start; some responses prefix them with
    # preamble like "Here is the JSON:\n```json\n[...]\n```".
    fence = re.search(r'```(?:json|JSON)?\s*\n?(.*?)\n?\s*```',
                      cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    # Slice to the outermost [...] span — handles both leading preamble
    # ("Here are the issues:\n[...]") AND trailing postamble
    # ("[...]\n\nLet me know if you want more."), as well as stray
    # <thinking> blocks on either side. Safe no-op when `cleaned` is
    # already exactly an array. Runs AFTER fence-stripping so the fence
    # branch wins when both patterns apply.
    first = cleaned.find('[')
    last  = cleaned.rfind(']')
    if first != -1 and last > first:
        cleaned = cleaned[first:last + 1]

    try:
        issues = json.loads(cleaned)
        if not isinstance(issues, list):
            raise ValueError(f'Expected JSON array, got: {type(issues)}')
        return issues
    except (json.JSONDecodeError, ValueError) as first_err:
        if client is None:
            raise  # no client to attempt repair
        # Save the error before the except block exits (Python 3 deletes
        # the variable after the block, causing UnboundLocalError later).
        original_err = first_err

    # ── Repair attempt ──────────────────────────────────────────────
    # Short sample of the offending response so the log is debuggable next
    # time this fires (empty string? thinking block? markdown?). Cap at 200
    # chars — we don't want the full response spammed into the ticket history.
    raw_sample = (raw or '')[:200].replace('\n', ' ')
    raw_len    = len(raw or '')
    if label:
        print(f'    [json-repair] {label}: {original_err}', flush=True)
    else:
        print(f'    [json-repair] {original_err}', flush=True)
    print(f'    [json-repair] raw response ({raw_len} chars): {raw_sample!r}', flush=True)

    # If the raw response is effectively empty (< 5 chars of non-whitespace
    # content), a repair call won't help — there's nothing TO repair. Skip
    # straight to re-raising so the caller can retry the full query instead
    # of wasting a second API call on an empty payload.
    if raw_len < 5 or not raw.strip():
        print('    [json-repair] raw is empty/tiny \u2014 skipping repair, '
              'caller should retry the full query.', flush=True)
        raise original_err

    print('    [json-repair] Asking Claude to fix the JSON\u2026', flush=True)

    try:
        # Reuse the caller's authenticated `client` so the repair call picks
        # up the same API key the main call used. Constructing a fresh
        # `Anthropic()` here fails in environments where the key is loaded
        # from .env and passed explicitly to the caller's client (seen on
        # 2026-04-17: every repair returned "Could not resolve authentication
        # method"). Using the same MODEL also avoids silent empty responses
        # from hardcoding a different model that may be unavailable.
        repair_resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{
                'role': 'user',
                'content': (
                    'The following text was supposed to be a valid JSON array '
                    'of objects, but it has a syntax error. Return ONLY the '
                    'repaired JSON array \u2014 no explanation, no markdown fences.\n\n'
                    + raw
                ),
            }],
        )
        repaired = repair_resp.content[0].text.strip()
        if not repaired:
            raise ValueError('Repair model returned empty response too')
        issues = json.loads(repaired)
        if not isinstance(issues, list):
            raise ValueError(f'Repair returned {type(issues)}, not list')
        print('    [json-repair] Success \u2014 repaired JSON accepted.', flush=True)
        return issues
    except Exception as repair_err:
        print(f'    [json-repair] Repair failed: {repair_err}', flush=True)
        raise original_err   # re-raise the original so caller sees real cause


def analyze_job_streaming(client, system_prompt, job, brief, clean_text,
                          max_retries=4):
    """Call Claude for one job using standard (streaming-compatible) API.
    Sends the pre-scan brief + clean text instead of raw HTML.
    Transparently retries on 429 rate-limit errors with server-suggested
    wait time (or exponential backoff)."""
    req_id    = str(job['id'])
    title     = job.get('name', '')
    community = (job.get('organization') or {}).get('name', '')

    user_msg = f"""{brief}
=== JOB POSTING ===
Req ID:    {req_id}
Title:     {title}
Community: {community}

Description (plain text):
{clean_text}"""

    # Two retry loops in one function:
    #   1. API-level retries (existing) — 429 rate-limit backoff.
    #   2. Parse-level retries (2026-04-17, new) — if the model returns an
    #      empty / non-JSON / unparseable response, retry the whole call
    #      with a short backoff. This survives transient failures where the
    #      model returns nothing or a bare thinking block; previously those
    #      were silently abandoned even though a second attempt usually
    #      succeeds. Capped at PARSE_MAX_RETRIES to bound API spend.
    PARSE_MAX_RETRIES = 2     # up to 3 total attempts per job
    parse_attempt = 0
    while True:
        attempt = 0
        while True:
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=[
                        {
                            'type': 'text',
                            'text': system_prompt,
                            'cache_control': {'type': 'ephemeral'},   # ← prompt caching
                        }
                    ],
                    messages=[{'role': 'user', 'content': user_msg}],
                    tools=[FINDINGS_TOOL],
                    tool_choice={'type': 'tool', 'name': 'submit_findings'},
                )
                break
            except Exception as e:
                status = getattr(e, 'status_code', None)
                is_429 = status == 429 or '429' in str(e) or 'rate_limit' in str(e).lower()
                if not is_429 or attempt >= max_retries:
                    raise
                wait = _sleep_for_retry(e, attempt)
                print(f'    [rate-limit] waiting {wait:.0f}s before retry {attempt+1}/{max_retries}\u2026',
                      flush=True)
                time.sleep(wait)
                attempt += 1

        # Structured output via tool_use (2026-04-21): the model is forced
        # to call the submit_findings tool, whose JSON Schema defines the
        # array-of-findings contract. No text parsing, no preamble risk, no
        # markdown fences. _try_parse_json stays in place as a belt-and-
        # suspenders fallback for text blocks that might precede the tool
        # use on some models.
        issues = _extract_findings(response, client=client, label=f'req {req_id}')
        raw = ''   # kept for error-logging paths that reference raw below
        try:
            # Validate we got a list (_extract_findings always returns one
            # but keep the same control flow so the retry loop below works).
            if not isinstance(issues, list):
                raise ValueError(f'Expected list, got {type(issues)}')
            break   # parsed cleanly (possibly via repair) — exit parse loop
        except (json.JSONDecodeError, ValueError) as parse_err:
            if parse_attempt >= PARSE_MAX_RETRIES:
                print(f'    [parse-retry] giving up after {parse_attempt+1} attempt(s) '
                      f'\u2014 last error: {parse_err}', flush=True)
                raise
            wait = 2 ** parse_attempt   # 1s, 2s
            print(f'    [parse-retry] attempt {parse_attempt+1}/{PARSE_MAX_RETRIES} '
                  f'failed ({parse_err}); retrying in {wait}s\u2026', flush=True)
            time.sleep(wait)
            parse_attempt += 1

    # Log cache usage if available
    usage = getattr(response, 'usage', None)
    cache_read  = getattr(usage, 'cache_read_input_tokens',  0) or 0
    cache_write = getattr(usage, 'cache_creation_input_tokens', 0) or 0
    return issues, cache_read, cache_write


# ── Claude analysis (batch mode) ──────────────────────────────────────────────

def build_batch_requests(system_prompt, jobs_with_context):
    """Build the list of MessageBatchRequestParam dicts for the Batches API."""
    import anthropic
    requests = []
    for job, brief, clean_text in jobs_with_context:
        req_id    = str(job['id'])
        title     = job.get('name', '')
        community = (job.get('organization') or {}).get('name', '')

        user_msg = f"""{brief}
=== JOB POSTING ===
Req ID:    {req_id}
Title:     {title}
Community: {community}

Description (plain text):
{clean_text}"""

        requests.append(
            anthropic.types.message_create_params.MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{'type': 'text', 'text': system_prompt,
                          'cache_control': {'type': 'ephemeral'}}],
                messages=[{'role': 'user', 'content': user_msg}],
                tools=[FINDINGS_TOOL],
                tool_choice={'type': 'tool', 'name': 'submit_findings'},
            )
        )
    return requests


def run_batch_mode(client, system_prompt, jobs_to_review, jobs_with_context):
    """Submit all jobs as a single batch and poll until complete."""
    import anthropic

    print(f'Submitting {len(jobs_to_review)} jobs to Anthropic Batch API...')

    batch_requests = []
    for (job, brief, clean_text), job_obj in zip(jobs_with_context, jobs_to_review):
        req_id = str(job_obj['id'])
        title     = job_obj.get('name', '')
        community = (job_obj.get('organization') or {}).get('name', '')

        user_msg = f"""{brief}
=== JOB POSTING ===
Req ID:    {req_id}
Title:     {title}
Community: {community}

Description (plain text):
{clean_text}"""

        batch_requests.append(
            anthropic.types.message_create_params.MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[{'type': 'text', 'text': system_prompt,
                          'cache_control': {'type': 'ephemeral'}}],
                messages=[{'role': 'user', 'content': user_msg}],
                tools=[FINDINGS_TOOL],
                tool_choice={'type': 'tool', 'name': 'submit_findings'},
            )
        )

    # Build proper batch request params
    batch_params = [
        {'custom_id': str(job['id']), 'params': req}
        for job, req in zip(jobs_to_review, batch_requests)
    ]

    batch = client.messages.batches.create(requests=batch_params)
    batch_id = batch.id
    print(f'Batch submitted: {batch_id}')
    print('Polling for results (this may take several minutes)...')

    # Poll until processing_status == 'ended'
    poll_interval = 30
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        total    = counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired
        done     = counts.succeeded + counts.errored + counts.canceled + counts.expired
        print(f'  Status: {batch.processing_status}  |  {done}/{total} complete', flush=True)
        if batch.processing_status == 'ended':
            break
        time.sleep(poll_interval)

    # Collect results keyed by custom_id (req_id)
    results = {}   # req_id → list of issue dicts
    errors  = {}   # req_id → error string
    for result in client.messages.batches.results(batch_id):
        cid = result.custom_id
        if result.result.type == 'succeeded':
            # Structured output via tool_use (2026-04-21): extract findings
            # from the tool_use content block. _try_parse_json stays as a
            # belt-and-suspenders fallback for edge cases where the model
            # returns a text block instead (shouldn't happen with
            # tool_choice forced, but defense in depth).
            try:
                issues = _extract_findings(result.result.message,
                                           client=client, label=f'batch {cid}')
                results[cid] = issues
            except Exception as e:
                errors[cid] = f'JSON parse error: {e}'
        else:
            errors[cid] = str(result.result)

    return results, errors


# ── Ticket helpers ─────────────────────────────────────────────────────────────

# ── Taxonomy — delegated to taxonomy.py ───────────────────────────────────────
# Canonical areas, category aliases/coercion, pair validation, and custom areas
# now live in one place. Thin wrappers preserve call-site names.

# Post-2026-05-21: _CANONICAL_AREAS now points at the new CATEGORIES_SET
# (the 5 canonical Categories), not the legacy pre-flatten CANONICAL_AREAS_SET
# which included Section-style values. CANONICAL_AREAS_SET is kept in
# taxonomy.py as a deprecated alias for any reader still on the old name.
_CANONICAL_AREAS = _tax.CATEGORIES_SET  # used by build_system_prompt's CROSS_CUTTING check

def register_custom_areas(areas):
    _tax.register_custom_areas(areas)

def _validate_and_coerce_area(category):
    return _tax.validate_and_coerce_category(category)

def _is_valid_pair(category, issue_type):
    return _tax.is_valid_pair(category, issue_type)


# ── Structural dedup (Plan D, 2026-04-15) ─────────────────────────────────────
# Issue types that should fire AT MOST ONCE per (req_id, category, issue_type)
# within a single posting. Instance-based types (Spelling and Grammar,
# Resident/Patient Mix, Generic Language, Unfilled Placeholder, etc.) are
# NOT in this set — distinct occurrences are legitimate separate findings.
#
# NOTE (2026-04-15): Missing Required Section and Truncated Section Content
# are NOT in this set any more. They became cross-cutting (Structure category)
# and can legitimately fire multiple times per posting — one per missing or
# truncated section — exactly like typos produce multiple Content / Spelling
# and Grammar tickets. Distinct offending_text values are what separate them.
#
# NOTE (2026-04-15 second wave): Parallel Structure is also NOT in this set.
# It's cross-cutting now (Formatting category) and can fire per-section, same
# pattern as MRS. Posting Too Short was retired entirely — whole-posting
# word-count is no longer a check.
STRUCTURAL_ONCE_PER_POSTING = frozenset({
    'Compensation Clarity',
    'Single Wage Figure',
    'Inconsistent Wage Figures',
    'Vague Experience Range',
    # Responsibilities length is AI-judged, section-scoped, and fires at
    # most once per side — gated here so a single posting cannot produce
    # duplicate too-short/too-long tickets.
    'Responsibilities Too Short',
    'Responsibilities Too Long',
})

_SEV_RANK = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1, '': 0}


def dedupe_structural_findings(issues, verbose=True):
    """Collapse multiple findings of the same structural (category, issue_type)
    in a single posting down to ONE — the highest-severity occurrence (ties
    keep the first). Non-structural issue types pass through untouched so
    instance-based duplicates (e.g. multiple typos, multiple patient/resident
    mixings) remain separate findings.

    Flat-model refinement (2026-04-17, Q7 Decision 3): if a pair's
    multi_location_behavior is 'per_section', the dedup key additionally
    includes Section so findings in different sections stay separate.
    Default behavior ('dedup') keeps the original one-per-posting semantics.

    Operates on AI-output dicts (not finalized ticket tuples), so it must run
    BEFORE make_ticket_row.
    """
    best = {}        # key -> issue dict
    out = []
    dropped = 0
    for iss in issues:
        category = (iss.get('category') or iss.get('category') or '').strip()
        itype    = (iss.get('issue_type') or '').strip()
        if itype not in STRUCTURAL_ONCE_PER_POSTING:
            out.append(iss)
            continue
        mlb = _mlb_for(category, itype)
        if mlb == 'per_section':
            # Per-section mode: include Section in the dedup key so findings
            # in different Sections survive as separate tickets.
            sec = iss.get('section')
            if isinstance(sec, str) and sec.strip().lower() in ('null', 'none', ''):
                sec = None
            key = (category, itype, sec)
        else:
            key = (category, itype)
        cur = best.get(key)
        if cur is None:
            best[key] = iss
            out.append(('__placeholder__', key))
            continue
        # Duplicate of a structural finding — keep the higher-severity one.
        cur_rank = _SEV_RANK.get((cur.get('severity') or '').upper(), 0)
        new_rank = _SEV_RANK.get((iss.get('severity') or '').upper(), 0)
        if new_rank > cur_rank:
            best[key] = iss
        dropped += 1
    if dropped and verbose:
        print(f'  [structural-dedupe] dropped {dropped} duplicate structural finding(s).')
    # Resolve placeholders into the surviving best-of-group
    return [best[item[1]] if isinstance(item, tuple) and item and item[0] == '__placeholder__' else item
            for item in out]


# ── Severity lock (Plan B, 2026-04-15) ─────────────────────────────────────────
# Populated once in main() from email_controls.default_severity. Both AUTO
# (write_prescan_tickets) and CLAUDE (make_ticket_row) lookups read this.
# Empty dict = no overrides; legacy behavior (AI/pre-scan picks freely).
_DEFAULT_SEVERITIES = {}


def set_default_severities(mapping):
    """Install the severity-override map for this run. Idempotent; safe to call
    repeatedly. ``mapping`` is {(category, issue_type): 'HIGH'|'MEDIUM'|'LOW'|...}.
    """
    global _DEFAULT_SEVERITIES
    _DEFAULT_SEVERITIES = dict(mapping or {})


def _override_severity(category, issue_type, current):
    """Return locked severity for this pair if configured, else ``current``."""
    locked = _DEFAULT_SEVERITIES.get((category, issue_type))
    if locked:
        return locked
    return (current or 'MEDIUM').upper()


# ── Multi-location behavior (Flatten Q7 Decision 3, 2026-04-17) ───────────────
# Populated once in main() from email_controls.multi_location_behavior.
# Keys are (category, issue_type) pairs; values are 'dedup' (default) or
# 'per_section'. Used by dedupe_structural_findings to decide whether two
# findings for the same check in different Sections collapse to one ticket
# or stay as separate tickets.
_MULTI_LOCATION_BEHAVIOR = {}


def set_multi_location_behavior(mapping):
    """Install the multi_location_behavior map for this run. ``mapping`` is
    {(category, issue_type): 'dedup' | 'per_section'}. Missing entries default
    to 'dedup'."""
    global _MULTI_LOCATION_BEHAVIOR
    _MULTI_LOCATION_BEHAVIOR = dict(mapping or {})


def _mlb_for(category, issue_type):
    """Return the multi_location_behavior for a pair. Defaults to 'dedup'."""
    return _MULTI_LOCATION_BEHAVIOR.get((category, issue_type), 'dedup')


# ── Template pattern map (Templates Category, 2026-04-21) ────────────────────
# Populated once in main() from db.get_template_patterns(). Keys are normalized
# offending_text strings (trim + collapse internal whitespace); values are
# (category, issue_type) for the Templates-Category pair to route to. Empty dict
# = no captured templates yet (the normal state during Phase 1 before the
# capture UI ships). Read by both the CLAUDE path (route_and_write) and the
# AUTO path (write_prescan_tickets). See taxonomy.match_template() and
# CLAUDE.md "Templates Category" standing rule.
_TEMPLATE_MAP = {}


def set_template_map(mapping):
    """Install the template-pattern map for this run. ``mapping`` is
    ``{normalized_pattern: (category, issue_type)}``. Idempotent; safe to call
    repeatedly."""
    global _TEMPLATE_MAP
    _TEMPLATE_MAP = dict(mapping or {})


def _template_match_for(offending_text):
    """Return ``(category, issue_type)`` if ``offending_text`` matches a captured
    template pattern exactly, else ``None``. Thin wrapper so callers don't
    need to import taxonomy directly. Case-sensitive; whitespace-normalized
    per the maintainer's 2026-04-21 decision (exact match keeps the admin as the
    reliability layer for what counts as a template)."""
    return _tax.match_template(offending_text, _TEMPLATE_MAP)


def make_ticket_row(ticket_id, today, req_id, title, community, issue,
                    job_url=''):
    """Build a ticket tuple for routing. Tuple layout is documented on
    route_and_write(). Category (aka category, legacy name), issue_type, and
    Section are separate fields end-to-end.
    """
    raw_area   = (issue.get('category') or issue.get('category') or '').strip()
    issue_type = (issue.get('issue_type') or '').strip()

    # Hard Category validation — coerce non-canonical values before they poison
    # downstream routing, filters, or the AI menu on the next run.
    category, coerced = _validate_and_coerce_area(raw_area)
    if coerced and raw_area:
        print(f'  [category-validator] "{raw_area}" \u2192 "{category}"  (issue_type: {issue_type})')
        issue['category'] = category
        issue['category']     = category   # legacy mirror

    # Templates-Category AI-safety reject (2026-04-21). The Templates Category
    # is system-managed — the routing layer assigns it based on offending_text
    # pattern matching, NEVER from AI output. CRITICAL RULE 19 in the system
    # prompt tells Claude not to emit it, but we coerce here as belt-and-
    # suspenders in case of drift. See CLAUDE.md "Templates Category".
    if category == 'Templates':
        print(f'  [templates-guard] rejected AI-emitted "Templates" Category on '
              f'issue_type "{issue_type}" \u2192 coerced to "Content". The routing '
              f'layer is the ONLY path to Templates.')
        category = 'Content'
        issue['category'] = 'Content'
        issue['category']     = 'Content'

    # Consolidated-type coercion — if the AI puts a consolidated type
    # (e.g. Spelling and Grammar) under the wrong Category, re-home it.
    area_before = category
    category, issue_type = _tax.coerce_consolidated_type(category, issue_type)
    if category != area_before:
        print(f'  [consolidation] "{area_before} / {issue_type}" \u2192 Category = "{category}"')
        issue['category'] = category
        issue['category']     = category

    # Section: use AI-supplied Section if valid, else fall back to the
    # issue-type default, else None (cross-cutting).
    raw_section = issue.get('section')
    if isinstance(raw_section, str):
        raw_section = raw_section.strip()
        if raw_section.lower() in ('null', 'none', ''):
            raw_section = None
    section = raw_section if _tax.is_valid_section(raw_section) else None
    if section is None:
        section = _tax.default_section_for(issue_type)  # may still be None

    # closest_category preferred; fall back to closest_area for backward compat
    closest_area       = (issue.get('closest_category') or issue.get('closest_area') or '').strip()
    closest_issue_type = (issue.get('closest_issue_type') or '').strip()
    confidence         = (issue.get('confidence') or '').strip().lower()

    return (
        ticket_id,                                           # 0
        today,                                               # 1
        req_id,                                              # 2
        title,                                               # 3
        community,                                           # 4
        _override_severity(category, issue_type,
                           issue.get('severity')),           # 5  (severity-lock — Plan B)
        category,                                                # 6
        issue_type,                                          # 7
        (issue.get('issue_summary') or '').strip(),          # 8
        str(issue.get('offending_text') or '').strip(),      # 9
        'CLAUDE',                                            # 10
        'Open',                                              # 11
        '',                                                  # 12
        job_url,                                             # 13 — stored in tickets
        closest_area,                                        # 14 — pending only
        closest_issue_type,                                  # 15 — pending only
        confidence,                                          # 16 — routing only
        section,                                             # 17 — Section (display-only metadata)
        None,                                                # 18 — captured_from (default NULL;
                                                             #      route_and_write sets this when
                                                             #      a template-match rewrites the
                                                             #      pair to Templates / <name>)
    )


def _is_auto_approve_issue(issue_type: str, category: str = '') -> bool:
    """Return True if the (category, issue_type) is a grammar-class auto-approve.

    Passes category through to ``taxonomy.is_auto_approve`` so wordings like
    ``Formatting / Grammar`` match the same keyword set as ``Content /
    Spelling Error``. Delegates to taxonomy.py."""
    return _tax.is_auto_approve(issue_type, category)


def _auto_register_pair(con, category, issue_type):
    """Insert a grammar-class pair into email_controls. Delegates to db.py."""
    _db.auto_register_pair(con, category, issue_type)


def route_and_write(con, ticket_rows, known_pairs):
    """Split 19-value ticket rows into live/pending and insert both.

    Tuple layout (index):
      0  ticket_id
      1  date_flagged
      2  req_id
      3  job_title
      4  community
      5  severity
      6  category (Category under flat model) ← part of routing key
      7  issue_type                        ← part of routing key
      8  issue_summary
      9  offending_text
      10 detected_by
      11 status
      12 notes
      13 job_url                           ← stored in tickets
      14 closest_area                      ← pending only
      15 closest_issue_type                ← pending only
      16 confidence                        ← routing only; not stored
      17 section                           ← stored in tickets.section (2026-04-17 flatten)
      18 captured_from                     ← stored in tickets.captured_from;
                                              the template-match step below
                                              sets this when a finding is
                                              redirected from its natural pair
                                              to a Templates-Category pair
                                              (2026-04-21)
    """
    known_set = set(known_pairs or ())

    # Validation gate: reject rows with malformed category/issue_type before routing.
    valid   = [t for t in ticket_rows if _is_valid_pair(t[6], t[7])]
    dropped = len(ticket_rows) - len(valid)
    if dropped:
        print(f'  [validation gate] Dropped {dropped} ticket(s) with malformed (category, issue_type).')

    # Empty-content guardrail (2026-04-21): reject rows where the AI returned
    # a shell of a finding with no actual content. Observed 2026-04-21 on a
    # Harbor Light Housekeeper posting — Claude emitted multiple findings
    # with empty issue_summary AND empty offending_text, leaving the maintainer with
    # unactionable tickets. With the tool_use schema now enforcing
    # minLength: 1 on issue_summary this shouldn't fire often, but belt-and-
    # suspenders. A legitimate finding MUST have non-empty issue_summary.
    # offending_text may be empty if the finding genuinely can't be quoted
    # (e.g. "missing section" — the section isn't there to quote) — in that
    # case issue_summary alone carries the signal.
    def _has_content(t):
        summary = (t[8] or '').strip()
        return bool(summary)
    empty_content = [t for t in valid if not _has_content(t)]
    valid = [t for t in valid if _has_content(t)]
    if empty_content:
        print(f'  [empty-content guardrail] Dropped {len(empty_content)} ticket(s) '
              f'with empty issue_summary. Pairs: '
              f'{sorted({(t[6], t[7]) for t in empty_content})}')

    # Confidence-aware remap: if the AI said "matched" but the pair isn't in the
    # known menu AND it nominated a closest_area/closest_issue_type pair that IS
    # in the menu, redirect the ticket to that canonical pair.
    remapped = 0
    for i, t in enumerate(valid):
        pair  = (t[6], t[7])
        close = (t[14], t[15])
        conf  = t[16]
        if pair in known_set:
            continue
        if close in known_set and close != ('', '') and conf == 'matched':
            row = list(valid[i])
            row[6], row[7] = close
            valid[i] = tuple(row)
            remapped += 1
    if remapped:
        print(f'  [confidence-remap] {remapped} ticket(s) redirected to closest canonical pairs.')

    # Template-match step (Templates Category, 2026-04-21). If a finding's
    # offending_text exactly matches a captured template pattern, rewrite the
    # ticket's (category, issue_type) to the Templates pair and record the
    # original pair in captured_from for audit. Runs BEFORE the grammar-class
    # auto-approve loop so a typo captured as a template stays on Templates
    # and doesn't bounce to Content / Spelling and Grammar. When the template
    # is later retired (rejected_issues removes the email_controls row and
    # thus its template_pattern), the match returns None and the finding
    # falls through to the normal routing below — exactly the behavior
    # the maintainer signed off on (2026-04-21). See CLAUDE.md "Templates Category".
    template_rewrites = 0
    for i, t in enumerate(valid):
        hit = _template_match_for(t[9])   # offending_text at index 9
        if not hit:
            continue
        new_area, new_itype = hit
        old_area, old_itype = t[6], t[7]
        if (new_area, new_itype) == (old_area, old_itype):
            continue   # already on this Templates pair (unlikely no-op)
        row = list(t)
        row[6] = new_area
        row[7] = new_itype
        # captured_from at index 18 (19-tuple post-2026-04-21). Older 18-tuples
        # from legacy callers are padded defensively.
        while len(row) < 19:
            row.append(None)
        row[18] = f'{old_area} / {old_itype}'
        valid[i] = tuple(row)
        template_rewrites += 1
    if template_rewrites:
        print(f'  [template-match] {template_rewrites} ticket(s) redirected to '
              f'Templates-Category pairs (captured_from preserved for audit).')

    # Auto-approve grammar/spelling/punctuation-class tickets: register the
    # pair so it becomes "known" and this + future runs route live.
    #
    # Grammar-class drift guard (2026-04-21): the ticket's own category/issue_type
    # is ALSO rewritten to the single canonical pair ('Content', 'Spelling and
    # Grammar') when it matches the auto-approve keyword set. Without this,
    # AI phrasing drift (Spelling Error / Grammar Error / Formatting Grammar /
    # etc.) produced orphan email_controls rows and mis-bucketed tickets.
    # db.auto_register_pair applies the same coercion on the controls side;
    # this loop also patches the ticket tuple in place so the tickets table
    # row lands on the canonical pair. See CLAUDE.md "Grammar-class drift
    # guard" standing rule.
    CANONICAL_GRAMMAR = ('Content', 'Spelling and Grammar')
    # Snapshot canonical registration before we auto-register, so we can
    # distinguish "a brand-new pair was registered" from "we routed to an
    # already-registered pair". Matters for log honesty — previously the
    # log always said "added" even when INSERT OR IGNORE was a no-op.
    grammar_already_registered = CANONICAL_GRAMMAR in known_set
    grammar_coerced = 0
    grammar_emissions = 0
    for i, t in enumerate(valid):
        pair = (t[6], t[7])
        if pair in known_set:
            continue
        if _is_auto_approve_issue(t[7], t[6]):
            grammar_emissions += 1
            if pair != CANONICAL_GRAMMAR:
                row = list(t)
                row[6], row[7] = CANONICAL_GRAMMAR
                valid[i] = tuple(row)
                grammar_coerced += 1
            _auto_register_pair(con, *CANONICAL_GRAMMAR)
            known_set.add(CANONICAL_GRAMMAR)
    if grammar_emissions and not grammar_already_registered:
        print(f'  [auto-approve] canonical grammar-class pair registered in '
              f'email_controls: {CANONICAL_GRAMMAR}')
    elif grammar_emissions:
        print(f'  [auto-approve] {grammar_emissions} grammar-class ticket(s) '
              f'routed to existing canonical pair {CANONICAL_GRAMMAR}.')
    if grammar_coerced:
        print(f'  [grammar-coerce] {grammar_coerced} ticket(s) rewritten to '
              f'canonical Content / Spelling and Grammar (was drifted AI wording).')

    live    = [t for t in valid if (t[6], t[7]) in known_set]
    # Silent-drop unknown pairs (2026-04-16, Phase 2 of Check Management).
    # With email_controls as a managed table, unknown pairs are not errors —
    # they're just pairs that haven't been registered via the Claude
    # qa-rules-maintenance skill. Silently dropping them prevents phantom
    # pending tickets and keeps the system clean. To add a new check type,
    # use the qa-rules-maintenance skill (the in-dashboard Add Check wizard
    # and AI-Assisted Discovery mode were removed 2026-04-24).
    dropped = [t for t in valid if (t[6], t[7]) not in known_set]
    if dropped:
        dropped_pairs = sorted({(t[6], t[7]) for t in dropped})
        print(f'  [silent-drop] {len(dropped)} ticket(s) from {len(dropped_pairs)} '
              f'unregistered pair(s) — not in email_controls, skipped:')
        for a, it in dropped_pairs:
            print(f'    • {a} / {it}')

    if live:
        # 16-column rows: section at index 14 (post-flatten 2026-04-17),
        # captured_from at index 15 (post-Templates 2026-04-21). Tolerates
        # older 17- / 18-element tuples by defaulting missing fields to None.
        live_rows = [
            (t[0], t[1], t[2], t[3], t[4], t[5],
             t[6], t[7], t[8], t[9], t[10], t[11], t[12], t[13],
             t[17] if len(t) > 17 else None,
             t[18] if len(t) > 18 else None)
            for t in live
        ]
        _db.insert_live_tickets(con, live_rows)

    return len(live), len(dropped)


def write_prescan_tickets(con, today, req_id, title, community, issues, ticket_num,
                          job_url=''):
    """Write pre-scan findings as AUTO tickets. Returns updated ticket_num.

    pre_scan.py returns ``category`` and ``issue_type`` separately. Under the flat
    model (2026-04-17) the old ``category`` value IS the Category name, and Section
    is derived from DEFAULT_SECTION_FOR_ISSUE_TYPE (mostly None for AUTO checks,
    which tend to be cross-cutting HTML / Tone / Content issues).

    Templates Category (2026-04-21): before building each row, we check
    whether offending_text matches a captured template pattern via
    ``_template_match_for()``. On match, the row's ``(category, issue_type)`` is
    rewritten to the Templates pair and the original pair is recorded in
    ``captured_from`` for audit. Retired templates return None from the
    matcher \u2192 falls through to the normal AUTO routing. See CLAUDE.md
    "Templates Category" standing rule.
    """
    rows = []
    template_rewrites = 0
    for issue in issues:
        ticket_num += 1
        category = issue.get('category', '') or 'Content'
        issue_type = issue.get('issue_type', '') or 'Unknown'
        offending_text = issue.get('offending_text', '') or ''
        # Template-match BEFORE severity-lock and Section lookup \u2014 a template
        # rewrite changes (category, issue_type), so those lookups need the
        # post-rewrite values to hit the right row in email_controls.
        captured_from = None
        hit = _template_match_for(offending_text)
        if hit:
            new_area, new_itype = hit
            if (new_area, new_itype) != (category, issue_type):
                captured_from = f'{category} / {issue_type}'
                category, issue_type = new_area, new_itype
                template_rewrites += 1
        section = _tax.default_section_for(issue_type)  # None if cross-cutting
        rows.append((
            f'QA-{ticket_num:04d}',
            today,
            req_id,
            title,
            community,
            _override_severity(category, issue_type, issue['severity']),  # severity-lock (Plan B)
            category,
            issue_type,
            issue['issue_summary'],
            offending_text,
            'AUTO',
            'Open',
            '',
            job_url,
            section,
            captured_from,
        ))
    if template_rewrites:
        print(f'  [template-match] {template_rewrites} AUTO ticket(s) redirected '
              f'to Templates-Category pairs (captured_from preserved for audit).')
    _db.insert_live_tickets(con, rows)
    return ticket_num


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    recheck_all = '--all'   in sys.argv
    batch_mode  = '--batch' in sys.argv
    force_batch = '--force-batch' in sys.argv  # Skip the auto-downgrade; always honour --batch.
    dump_prompt = '--dump-prompt' in sys.argv  # Write assembled system prompt to _last_prompt.txt and exit.

    # Auto-downgrade threshold: below this count, batch mode is slower than streaming
    # (batch has up to 24hr SLA; for a handful of jobs it wastes wall-clock time).
    BATCH_MIN_JOBS = 50

    # -- Prerequisites
    api_key = load_api_key()
    if not api_key:
        print('ERROR: No API key found.')
        print('  Set ANTHROPIC_API_KEY in environment or create a .env file with:')
        print('  ANTHROPIC_API_KEY=sk-ant-...')
        sys.exit(1)

    for path, label in [(JOBS_RAW, 'jobs_raw.json'), (DB, 'qa_tickets.db'), (STYLE_MD, 'style_guide.md')]:
        if not os.path.exists(path):
            print(f'ERROR: {label} not found. Run fetch_jobs.py first.')
            sys.exit(1)

    # -- Install anthropic if needed
    try:
        import anthropic
    except ImportError:
        print('Installing anthropic package...')
        import subprocess
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', 'anthropic', '-q',
             '--break-system-packages'],
            stdout=subprocess.DEVNULL
        )
        import anthropic

    # -- Import pre_scan
    try:
        from pre_scan import run_prescan
    except ImportError:
        print('ERROR: pre_scan.py not found in the same folder as this script.')
        sys.exit(1)

    # -- Load inputs
    with open(JOBS_RAW) as f:
        all_jobs = json.load(f)
    with open(STYLE_MD) as f:
        style_guide = f.read()

    client = anthropic.Anthropic(api_key=api_key)

    # -- Determine which jobs to review
    con, tmp = read_db()
    ensure_pending_table(con)
    reviewed_ids   = get_reviewed_req_ids(con)
    max_ticket_num = get_max_ticket_num(con)

    # Load taxonomy data from DB via centralised queries (db.py)
    known_pairs    = _db.get_known_pairs(con)
    rejected_pairs = _db.get_rejected_pairs(con)
    custom_areas   = _db.get_custom_areas(con)
    aliases        = _db.get_aliases(con)
    fire_counts    = _db.get_fire_counts(con)
    sev_overrides  = _db.get_default_severities(con)
    mlb_map        = _db.get_multi_location_behaviors(con)
    template_map   = _db.get_template_patterns(con)

    # Register custom areas so the per-finding validator recognises them.
    register_custom_areas(custom_areas)

    # Install severity-lock map (Plan B). Read by both make_ticket_row (CLAUDE)
    # and write_prescan_tickets (AUTO) via _override_severity().
    set_default_severities(sev_overrides)
    if sev_overrides:
        print(f'Severity lock: {len(sev_overrides)} pair(s) will have severity '
              f'overridden by email_controls.default_severity.')

    # Install multi_location_behavior map (Flatten Q7 Decision 3). Pairs set
    # to 'per_section' retain separate tickets across Sections; 'dedup'
    # (default) collapses to one ticket per posting.
    set_multi_location_behavior(mlb_map)
    per_section_n = sum(1 for v in mlb_map.values() if v == 'per_section')
    if per_section_n:
        print(f'Multi-location: {per_section_n} pair(s) configured for per_section '
              f'dedup; others default to posting-level dedup.')

    # Install template-pattern map (Templates Category, 2026-04-21). Read by
    # both the CLAUDE path (route_and_write) and the AUTO path
    # (write_prescan_tickets) to redirect findings whose offending_text
    # exactly matches a captured template to the Templates pair.
    set_template_map(template_map)
    if template_map:
        print(f'Templates: {len(template_map)} captured template pattern(s) '
              f'will match incoming findings and route to Templates.')

    system_prompt = build_system_prompt(
        style_guide,
        known_pairs=known_pairs,
        rejected_pairs=rejected_pairs,
        custom_areas=custom_areas,
        aliases=aliases,
        fire_counts=fire_counts,
    )
    live_menu_n = sum(1 for p in known_pairs if fire_counts.get(p, 0) > 0)
    print(f'Issue type menu: {live_menu_n} active / {len(known_pairs)} total pair(s)  |  '
          f'{len(rejected_pairs)} rejected  |  '
          f'{len(custom_areas)} custom category(s)  |  '
          f'{len(aliases)} alias(es) injected into system prompt.')

    if dump_prompt:
        # Extract the raw text from the structured system prompt (list of blocks)
        blocks = system_prompt if isinstance(system_prompt, list) else [system_prompt]
        parts = []
        for b in blocks:
            if isinstance(b, dict):
                parts.append(b.get('text', ''))
            else:
                parts.append(str(b))
        dump_path = os.path.join(BASE, '_last_prompt.txt')
        with open(dump_path, 'w', encoding='utf-8') as f:
            f.write('\n\n---\n\n'.join(parts))
        print(f'System prompt written to {dump_path} ({sum(len(p) for p in parts):,} chars). Exiting without API call.')
        con.close()
        try: os.remove(tmp)
        except OSError: pass
        sys.exit(0)

    if recheck_all:
        jobs_to_review = all_jobs
        print(f'Re-reviewing all {len(jobs_to_review)} jobs (--all flag).')
    else:
        jobs_to_review = [j for j in all_jobs if str(j['id']) not in reviewed_ids]
        skipped = len(all_jobs) - len(jobs_to_review)
        print(f'Jobs to review: {len(jobs_to_review)}  (skipping {skipped} already reviewed)')

    if not jobs_to_review:
        print('Nothing to do — all jobs already reviewed.')
        con.close()
        try: os.remove(tmp)
        except OSError: pass
        sys.exit(0)

    # Auto-downgrade batch → streaming for small runs (unless user forced it).
    if batch_mode and not force_batch and len(jobs_to_review) < BATCH_MIN_JOBS:
        print(f'NOTE: --batch requested but only {len(jobs_to_review)} job(s) to review '
              f'(< {BATCH_MIN_JOBS}). Auto-downgrading to streaming mode for faster turnaround.')
        print(f'      Use --force-batch to override if you really want batch pricing.')
        batch_mode = False

    print(f'Model: {MODEL}  |  Mode: {"batch" if batch_mode else "streaming"}')
    print()

    # ── Phase 1: Pre-scan all jobs ─────────────────────────────────────────────
    today = str(date.today())
    print('Running pre-scan on all jobs...')
    prescan_total = 0
    jobs_context  = []   # list of (job, brief, clean_text)

    for job in jobs_to_review:
        req_id    = str(job['id'])
        title     = job.get('name', '')
        community = (job.get('organization') or {}).get('name', '')
        issues, brief, clean_text = run_prescan(job)
        job_url = (job.get('career_site_url') or '').strip()
        max_ticket_num = write_prescan_tickets(
            con, today, req_id, title, community, issues, max_ticket_num,
            job_url=job_url,
        )
        prescan_total += len(issues)
        jobs_context.append((job, brief, clean_text))

    if prescan_total:
        con.commit()
        print(f'Pre-scan complete: {prescan_total} AUTO ticket(s) written.')
    else:
        print('Pre-scan complete: no issues found.')
    print()

    # ── Phase 2: AI review ─────────────────────────────────────────────────────
    new_tickets = []
    errors      = []
    cache_reads = 0
    cache_writes = 0

    if batch_mode:
        # ── Batch mode ──────────────────────────────────────────────────────
        batch_results, batch_errors = run_batch_mode(
            client, system_prompt, jobs_to_review, jobs_context
        )
        for job, brief, clean_text in jobs_context:
            req_id    = str(job['id'])
            title     = job.get('name', '')
            community = (job.get('organization') or {}).get('name', '')
            if req_id in batch_errors:
                errors.append((req_id, title, batch_errors[req_id]))
                continue
            raw_issues = apply_fuzzy_matching(
                batch_results.get(req_id, []), known_pairs, verbose=True,
                aliases=aliases,
            )
            raw_issues = dedupe_structural_findings(raw_issues, verbose=True)
            job_url = (job.get('career_site_url') or '').strip()
            for issue in raw_issues:
                max_ticket_num += 1
                new_tickets.append(
                    make_ticket_row(
                        f'QA-{max_ticket_num:04d}', today, req_id, title, community, issue,
                        job_url=job_url,
                    )
                )
    else:
        # ── Streaming mode ────────────────────────────────────────────────
        total = len(jobs_to_review)
        # Inter-request pacing to stay under Tier-1 limits (5 RPM, 10K ITPM on
        # Sonnet). 13s/request = ~4.6 RPM, well under the ceiling. With prompt
        # caching, subsequent requests are ~1K input tokens (cache read), so
        # ITPM isn't the binding constraint after the first call.
        min_interval = float(os.environ.get('QA_MIN_INTERVAL_SEC', '13'))
        last_call_ts = 0.0
        for idx, (job, brief, clean_text) in enumerate(jobs_context, 1):
            req_id    = str(job['id'])
            title     = job.get('name', '')
            community = (job.get('organization') or {}).get('name', '')

            print(f'[{idx:>3}/{total}] {title[:50]:<50}  ({community})', end='  ', flush=True)
            # Pace requests to avoid tripping RPM limits on Tier-1 Sonnet.
            elapsed = time.time() - last_call_ts
            if last_call_ts and elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            try:
                last_call_ts = time.time()
                issues, cr, cw = analyze_job_streaming(
                    client, system_prompt, job, brief, clean_text
                )
                cache_reads  += cr
                cache_writes += cw
                issues = apply_fuzzy_matching(issues, known_pairs, verbose=True,
                                               aliases=aliases)
                issues = dedupe_structural_findings(issues, verbose=True)
                # ASCII-only marker so the cp1252 Windows console doesn't choke
                # on piped stdout. Previously used \u2713 (checkmark) which
                # crashed via UnicodeEncodeError when stdout wasn't a tty.
                cache_note = f'  [cache hit {cr}t]' if cr else ''
                print(f'{len(issues)} issue(s){cache_note}')
                job_url = (job.get('career_site_url') or '').strip()
                for issue in issues:
                    max_ticket_num += 1
                    new_tickets.append(
                        make_ticket_row(
                            f'QA-{max_ticket_num:04d}', today, req_id, title, community, issue,
                            job_url=job_url,
                        )
                    )
            except Exception as e:
                print(f'ERROR — {e}')
                errors.append((req_id, title, str(e)))

    # ── Phase 3: Write CLAUDE tickets ──────────────────────────────────────────
    if new_tickets:
        live_n, drop_n = route_and_write(con, new_tickets, known_pairs)
        con.commit()
        write_db(tmp)
        print()
        if live_n:
            print(f'  {live_n} CLAUDE ticket(s) added to live DB.')
        if drop_n:
            print(f'  {drop_n} ticket(s) silently dropped (unregistered pairs).')
    else:
        con.commit()
        write_db(tmp)
        print()
        print('Done. No new AI issues found.')

    # Also flush prescan tickets to mount even if no CLAUDE tickets
    if prescan_total and not new_tickets:
        write_db(tmp)

    if errors:
        print(f'\n{len(errors)} job(s) could not be analyzed:')
        for req_id, title, err in errors:
            print(f'  Req {req_id} ({title[:40]}): {err}')

    # Summaries
    all_tickets = new_tickets
    if all_tickets:
        from collections import Counter
        sev = Counter(t[5] for t in all_tickets)
        print()
        print('AI findings by severity:')
        for level in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW'):
            if sev[level]:
                print(f'  {level}: {sev[level]}')

    if not batch_mode and (cache_reads or cache_writes):
        # Report cache effectiveness: hit rate (% of input tokens served from
        # cache) and how many jobs paid the full-write cost. Prompt caching
        # typically writes once per ~5 minute window, then reads on every
        # subsequent job — so a healthy run shows 1-2 writes and many reads.
        total_input = cache_reads + cache_writes
        hit_rate = (cache_reads / total_input * 100) if total_input else 0
        print()
        print(f'Prompt cache: {cache_reads:,} read / {cache_writes:,} written '
              f'({hit_rate:.0f}% hit rate across {len(jobs_to_review)} job(s)).')
        # Warn when the cache is actually underperforming. The previous
        # heuristic (2026-04-21 audit) compared cache_writes — a TOKEN count —
        # against successful_jobs // 5, a COUNT quotient. On a healthy
        # 1-write / N-hits run that single write is ~12K tokens, which
        # tripped the warning every time despite the cache doing its job.
        # Switched to hit_rate. Also gated on job count: with N jobs the
        # theoretical max hit rate is (N-1)/N, so 70% isn't reachable until
        # N >= 4. For tiny runs (2-3 jobs), suppress the warning since it's
        # mathematically impossible to hit the threshold.
        error_rate = len(errors) / max(1, len(jobs_to_review))
        if (error_rate < 0.5
                and cache_writes > 0
                and len(jobs_to_review) >= 4
                and hit_rate < 70):
            print(f'  NOTE: cache hit rate is only {hit_rate:.0f}% — the 5-min '
                  'TTL may be expiring between jobs, or prompt drift is '
                  'invalidating the cache. Consider running jobs closer '
                  'together or check for system-prompt churn mid-run.')

    total_new = prescan_total + len(new_tickets)
    print()
    print(f'Done. {total_new} total ticket(s) created '
          f'({prescan_total} pre-scan AUTO + {len(new_tickets)} CLAUDE AI).')

    con.close()
    try: os.remove(tmp)
    except OSError: pass


if __name__ == '__main__':
    main()
