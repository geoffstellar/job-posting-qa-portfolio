"""
taxonomy.py — Single source of truth for business rules and classification.

Every "what counts as X?" question in the QA system should have its answer
here. Categories, auto-approve keywords, exclamation-point rules, aliases,
pair validation, severity ordering, and legacy parsing helpers.

Designed to be imported by: qa_dashboard.py, run_ai_review.py,
cleanup_tickets.py, and any future scripts.

TAXONOMY FLATTEN (2026-04-17): The model was flattened from two-axis
(Area = location-or-category) into single-axis (Category) + display-only
Section metadata. See docs/TAXONOMY_FLATTEN_HANDOFF_2026-04-17.md.

TAXONOMY REFACTOR (2026-05-21+): Categories shrunk from 7 to 5
(dropped 'Job Title' and 'HTML'; their checks redistribute to Structure
and Formatting respectively). Sections expanded from 7 to 11 (renamed
'Job Title' -> 'Title' to remove vocabulary collision; added
'EEO Statement', 'Role Overview', 'How to Apply', 'Cross-cutting'). The
`area` column was renamed to `category` across all tables in the same
refactor. (internal design notes).
"""

from __future__ import annotations
import re


# ── FLAT MODEL — Categories and Sections (2026-04-17, refined 2026-05-21) ─────
#
# Under the refined flat model there are TWO truly orthogonal axes:
#   - Category (5 values): WHAT kind of issue is this?
#   - Section (11 values): WHERE in the posting is the issue located?
# Vocabulary never overlaps between the two axes — every value belongs to
# exactly one of WHAT and WHERE. Section is display-only metadata for
# communities to locate the issue; it's never used for check routing.

CATEGORIES = [
    'Tone',
    'Content',
    'Formatting',
    'Structure',
    # Templates (added 2026-04-21) — the 5th Category. System-managed: tickets
    # are routed here by the routing layer based on exact offending_text match
    # against captured template patterns in email_controls.template_pattern.
    # Claude MUST NOT emit this Category directly (enforced via CRITICAL RULE
    # 19 in run_ai_review.SYSTEM_PROMPT_TEMPLATE and a safety-net reject in
    # make_ticket_row). See CLAUDE.md "Templates Category" standing rule.
    'Templates',
]

CATEGORIES_SET = set(CATEGORIES)

# AI-emittable subset of Categories: Templates is system-managed (assigned
# by the routing layer's template-match step), so Claude must NEVER pick it.
# The AI prompt's category enum should be built from this subset, not CATEGORIES.
CATEGORIES_AI_EMITTABLE = [c for c in CATEGORIES if c != 'Templates']
CATEGORIES_AI_EMITTABLE_SET = set(CATEGORIES_AI_EMITTABLE)

# The 11 Section values. Cross-cutting is the explicit "not tied to one
# specific section" value (replaces NULL post-2026-05-21 refactor; every
# ticket now gets a non-NULL section). Title is the renamed-from-'Job Title'
# section that removes the vocabulary collision with the (now-dropped)
# 'Job Title' Category. EEO Statement, Role Overview, and How to Apply
# were added 2026-05-21 after a real-world audit of live Hireology postings.
# Benefits is NOT a separate Section — Benefits Details checks tag the
# 'What We Offer — General' Section.
SECTIONS = [
    'Title',                              # renamed from 'Job Title' (2026-05-21)
    'Community Intro',
    'Who We Are',
    'Role Overview',                      # added 2026-05-21 (audit: 134 occurrences)
    'Responsibilities',
    'Qualifications',
    'What We Offer \u2014 Pay',           # em-dash U+2014
    'What We Offer \u2014 General',
    'EEO Statement',                      # added 2026-05-21
    'How to Apply',                       # added 2026-05-21 with negative
                                          # semantic: a posting including this
                                          # section gets flagged by a new
                                          # Structure-category check.
    'Cross-cutting',                      # explicit catch-all (replaces NULL)
]

SECTIONS_SET = set(SECTIONS)


# Default Section hint per issue_type. Used by the AI prompt as a nudge and
# by the migration as a backfill heuristic. Sparse by design: most issue
# types are cross-cutting (section=None), so only the section-specific
# issue types have an entry. An entry of None (not present in dict) means
# "no default, leave as None unless the AI provides one."
DEFAULT_SECTION_FOR_ISSUE_TYPE: dict[str, str] = {
    # Title-field issues. Post-2026-05-21 these used to live under the
    # 'Job Title' Category, which has been dropped; checks now route to
    # Structure or Formatting (see CONSOLIDATED_TYPES below). The Section
    # is 'Title' (renamed from 'Job Title' for clarity).
    'Pay/Bonus in Title':         'Title',
    'Title/Description Mismatch': 'Title',
    'Title Formatting':           'Title',
    'Inconsistent Capitalization': None,   # explicitly cross-cutting
    # Section-scoped Content checks
    'Vague Experience Range':     'Qualifications',
    'Compensation Clarity':       'What We Offer \u2014 Pay',
    'Inconsistent Wage Figures':  'What We Offer \u2014 Pay',
    'Single Wage Figure':         'What We Offer \u2014 Pay',
    'Pay Details':                'What We Offer \u2014 Pay',
    'Benefits Details':           'What We Offer \u2014 General',
    # Who-We-Are-specific content
    'Missing Brand Boilerplate':  'Who We Are',
    'Wrong Community Name':       'Who We Are',
    # Community-introduction-specific content
    'Incorrect Role Description': 'Community Intro',
}


def is_valid_category(cat: str) -> bool:
    """True if the string is one of the 5 canonical Categories
    (Tone, Content, Formatting, Structure, Templates)."""
    return (cat or '').strip() in CATEGORIES_SET


def is_valid_section(sec) -> bool:
    """True if the string is one of the 11 canonical Sections.
    None / empty is also accepted for backward compatibility with
    pre-2026-05-21 callers that used NULL to mean cross-cutting;
    new code should use the explicit 'Cross-cutting' section value."""
    if sec is None:
        return True
    s = (sec or '').strip()
    if s == '':
        return True
    return s in SECTIONS_SET


def default_section_for(issue_type: str):
    """Return the default Section string for an issue_type, or None if
    cross-cutting / unknown. Used by the migration and as an AI prompt hint."""
    return DEFAULT_SECTION_FOR_ISSUE_TYPE.get((issue_type or '').strip())


# ── Legacy Area → (Category, Section) mapping ─────────────────────────────────
# Used by Phase 2 migration to backfill tickets.section and rewrite
# email_controls.area. Every pre-flatten Area resolves to (new Category,
# new Section-or-None). The AI never sees this — it's migration-only.

LEGACY_AREA_TO_CATEGORY_SECTION: dict[str, tuple[str, str | None]] = {
    # Categories that survive the 2026-05-21 refactor as-is
    'Tone':             ('Tone',       None),
    'Content':          ('Content',    None),
    'Formatting':       ('Formatting', None),
    'Structure':        ('Structure',  None),
    # Categories dropped in the 2026-05-21 refactor. Their issue-types
    # redistribute via CONSOLIDATED_TYPES; this mapping is a safe default
    # when the issue_type isn't in CONSOLIDATED_TYPES (rare — should not
    # happen for any registered pair).
    'HTML':             ('Formatting', None),      # all HTML checks fold to Formatting
    'Job Title':        ('Structure',  'Title'),   # most Job Title checks are structural; Section is the renamed 'Title'
    'Responsibilities': ('Structure',  'Responsibilities'),   # was a Category for length checks; now Structure

    # Legacy section-areas: the old Area name IS the Section name. Category
    # is Content by default (these were all about content-quality issues in
    # a specific section); specific issue types may need a different Category
    # and the per-row mapping in the migration handles that.
    'Community Introduction':      ('Content',   'Community Intro'),
    'Who We Are':                  ('Content',   'Who We Are'),
    'Responsibilities':            ('Content',   'Responsibilities'),
    'Qualifications':              ('Content',   'Qualifications'),
    'What We Offer \u2014 Pay':    ('Content',   'What We Offer \u2014 Pay'),
    'What We Offer \u2014 General':('Content',   'What We Offer \u2014 General'),
    'What We Offer \u2014 Benefits':('Content',  'What We Offer \u2014 General'),  # Benefits collapses to General Section
    'EEO Statement':               ('Structure', None),   # MRS-style — cross-cutting, section named in offending_text
}


def category_section_for_legacy_pair(old_area: str, issue_type: str) -> tuple[str, str | None]:
    """Return (new_category, new_section_or_None) for a pre-flatten
    (Area, Issue Type) pair.

    Rules:
      1. If the issue_type is in CONSOLIDATED_TYPES, that determines Category
         (Spelling and Grammar → Content, Missing Required Section → Structure,
         etc.) regardless of old Area. Section defaults from
         DEFAULT_SECTION_FOR_ISSUE_TYPE, or None if not present.
      2. If the issue_type has a default Section in DEFAULT_SECTION_FOR_ISSUE_TYPE,
         use that; combined with the Category from (1) or the Area map.
      3. Otherwise, fall back to LEGACY_AREA_TO_CATEGORY_SECTION for the old Area.
      4. If nothing matches, return ('Content', None) as a safe default.
    """
    a  = (old_area or '').strip()
    it = (issue_type or '').strip()

    # 1. Consolidated issue types win — their Category is fixed.
    if it in CONSOLIDATED_TYPES:
        cat = CONSOLIDATED_TYPES[it]
        sec = default_section_for(it)
        # If no explicit default and the old Area mapped to a Section, use that.
        if sec is None and a in LEGACY_AREA_TO_CATEGORY_SECTION:
            sec = LEGACY_AREA_TO_CATEGORY_SECTION[a][1]
        return (cat, sec)

    # 2. Legacy Area mapping (covers the non-consolidated, section-specific checks).
    if a in LEGACY_AREA_TO_CATEGORY_SECTION:
        cat, sec_from_area = LEGACY_AREA_TO_CATEGORY_SECTION[a]
        # Per-issue-type default Section overrides the area's default if set.
        sec_override = default_section_for(it)
        sec = sec_override if sec_override is not None else sec_from_area
        return (cat, sec)

    # 3. Fallback.
    return ('Content', default_section_for(it))


# ── LEGACY (pre-flatten) constants — kept for backward compat ─────────────────
#
# These are transitional aliases so callers (qa_dashboard.py, run_ai_review.py,
# cleanup_tickets.py) keep working while they migrate to the flat model.
# Once every caller reads CATEGORIES / SECTIONS directly, the names below can
# be removed. Marked with # LEGACY comments so future sessions know to cut them.

CANONICAL_AREAS = [                            # LEGACY — use CATEGORIES
    # Posting sections
    'Job Title',
    'Community Introduction',
    'Who We Are',
    'What We Offer \u2014 Pay',
    'What We Offer \u2014 Benefits',
    'What We Offer \u2014 General',
    'Responsibilities',
    'Qualifications',
    'EEO Statement',
    # Whole-posting / cross-cutting (== new Categories, pre-flatten names)
    'HTML',
    'Tone',
    'Formatting',
    'Content',
    'Structure',
]

CANONICAL_AREAS_SET = set(CANONICAL_AREAS)     # LEGACY — use CATEGORIES_SET

# LEGACY — under the flat model all Categories are "cross-cutting" by definition
# (Category is a category, not a location). Kept non-empty so any caller that
# still asks "is this cross-cutting?" for routing purposes gets the pre-flatten
# answer during the transition window. Post-2026-05-21: HTML dropped (now folds
# into Formatting). Delete after Phase 4/5 callers migrate.
CROSS_CUTTING = {'Tone', 'Formatting', 'Content', 'Structure'}

# Canonical issue types that have been consolidated into whole-posting areas.
# If the AI returns these under a section area, they should be re-homed here.
CONSOLIDATED_TYPES = {
    'Spelling and Grammar':       'Content',
    'Generic Language':            'Content',
    'Resident/Patient Mix':        'Tone',
    # 'Filler Phrase' retired 2026-04-15 — conversational closings / CTAs are
    # part of Cedarline's warm voice. Kept in _EXACT_LOCATION_MAP below so any
    # legacy composite strings still parse, but it is NOT flagged any more.
    'Informal Language':           'Tone',
    'ALL CAPS':                    'Tone',
    # Renamed 2026-04-15 from 'Excessive ALL CAPS' to 'Body Text in ALL CAPS'
    # to distinguish clearly from the (title-level) 'ALL CAPS' slot. The plain
    # 'ALL CAPS' entry above is kept as a Tone safety net for AI emissions;
    # title-level ALL CAPS violations continue to fire as Job Title / Title
    # Formatting (AUTO in fetch_jobs.py). Legacy aliases ('Excessive ALL CAPS',
    # 'Excessive ALL CAPS Emphasis') are handled in _EXACT_LOCATION_MAP below
    # so historical composite strings still coerce correctly.
    'Body Text in ALL CAPS':       'Tone',
    'Excessive ALL CAPS':          'Tone',
    'Formatting Issue':            'Formatting',
    # Added 2026-04-15 — MRS and Truncated Section Content are whole-posting
    # checks, just like Spelling and Grammar. The specific section that is
    # missing / truncated belongs in offending_text, NOT in area. See
    # CLAUDE.md "Missing Required Section Is a Cross-Cutting Check" rule.
    'Missing Required Section':    'Structure',
    'Truncated Section Content':   'Structure',
    # Added 2026-04-15 (second wave) — Parallel Structure is a whole-posting
    # formatting check. Any AI emission like (Responsibilities, Parallel
    # Structure) or (Qualifications, Parallel Structure) is re-homed to
    # (Formatting, Parallel Structure); the specific section goes in
    # offending_text, e.g. "Section: Responsibilities — bullets mix verb and
    # noun phrases".
    'Parallel Structure':          'Formatting',
    # Updated 2026-05-21 — Responsibilities length checks moved from the
    # (now-dropped) 'Responsibilities' Category to 'Structure'. Section is
    # 'Responsibilities' (set via DEFAULT_SECTION_FOR_ISSUE_TYPE or by the
    # AI). The checks are structural (about bullet count); they belong
    # under Structure regardless of which section they target. See CRITICAL
    # RULE 5 in run_ai_review.py for the AI-facing spec.
    'Responsibilities Too Short':  'Structure',
    'Responsibilities Too Long':   'Structure',

    # Added 2026-05-21 — HTML-presentation checks moved from the (now-dropped)
    # 'HTML' Category to 'Formatting'. The checks are about visual presentation
    # of the posting body (font size, color, underline, etc.); they belong
    # under Formatting in the refactored 5-Category model. AI drift emitting
    # them as 'HTML' (the old Category) gets coerced back here.
    'Inline Font Size':            'Formatting',
    'Highlighted/Colored Text':    'Formatting',
    'Inline Text Color Override':  'Formatting',
    'Nested List Structure':       'Formatting',
    'Underline Tag':               'Formatting',

    # Added 2026-05-21 — Title-field checks moved from the (now-dropped)
    # 'Job Title' Category. Placement / identity issues (the title says
    # one thing but the description is for a different role; the title
    # contains a bonus or pay amount that shouldn't be there) are
    # structural concerns: they're about the title field's correctness,
    # not its visual presentation. Routes to Structure.
    'Pay/Bonus in Title':          'Structure',
    'Title/Description Mismatch':  'Structure',
    # Presentation / formatting issues on the title (capitalization style,
    # 'TITLE FORMATTING' as a check name) are about how the title looks.
    # Routes to Formatting.
    'Title Formatting':            'Formatting',
    'Inconsistent Capitalization': 'Formatting',
}


# ── Acceptable ALL CAPS acronyms ──────────────────────────────────────────────
# Standard healthcare and industry abbreviations that are always written in
# all caps. These should never be flagged as ALL CAPS title violations.
ACCEPTABLE_ACRONYMS = {
    'PRN', 'QMAP', 'CNA', 'LPN', 'RN', 'OT', 'COTA', 'OTR', 'DPT', 'PTA',
    'DOE', 'FT', 'PT', 'NOC', 'SNF', 'AL', 'IL', 'MC', 'AZ', 'CO', 'WA',
    'MUST', 'SAT', 'SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI',
}

# Roles where shorter Responsibilities sections (3-4 bullets) are acceptable.
SHORT_SECTION_ROLES = {
    'cook', 'dishwasher', 'housekeeper', 'maintenance technician',
    'maintenance tech', 'driver', 'server', 'dining room server',
}


# ── Category aliases (AI drift → canonical) ──────────────────────────────────
# When the AI returns a near-miss on category, coerce rather than dropping the
# ticket. Keys are lowercase, with dashes normalised from em/en-dash. Values
# are always one of the 5 canonical Categories (Tone, Content, Formatting,
# Structure, Templates).

CATEGORY_ALIASES = {
    # Case-folded variants of canonical Category names \u2014 without these, an
    # AI emission of 'tone' (lowercase) would fall through to the 'Content'
    # fallback instead of being coerced to 'Tone'.
    'tone':                           'Tone',
    'content':                        'Content',
    'formatting':                     'Formatting',
    'structure':                      'Structure',
    'templates':                      'Templates',
    # Dropped Categories from the 2026-05-21 refactor \u2014 fold to new homes
    'html':                           'Formatting',
    'job title':                      'Structure',  # most title issues are structural
    'responsibilities':               'Structure',  # length checks now under Structure
    # Section-name drift mapped to plausible Categories (last-resort coercion;
    # CONSOLIDATED_TYPES will override based on issue_type when applicable).
    # Pre-2026-05-21 these mapped to Section values (which made sense in the
    # pre-flatten Area model). Post-flatten + post-refactor: these are
    # legitimate Section names, not Category names, so when AI emits them
    # AS a category, the only sensible coercion is to a best-fit Category.
    'pay':                            'Content',
    'compensation':                   'Content',
    'benefits':                       'Content',
    'what we offer':                  'Content',
    'qualifications':                 'Content',
    'who we are':                     'Content',
    'community intro':                'Content',
    'community introduction':         'Content',
    'eeo':                            'Structure',
    'eeo statement':                  'Structure',
    'role overview':                  'Content',
    'how to apply':                   'Structure',  # the new check is Structure-category
    'title':                          'Structure',  # title-field issues default to Structure
}

# AREA_ALIASES kept as a deprecated alias for any caller that hasn't been
# updated yet. Same contents as CATEGORY_ALIASES post-refactor. Remove after
# the code sweep completes and no callers reference the old name.
AREA_ALIASES = CATEGORY_ALIASES


# ── Custom areas (populated at runtime) ───────────────────────────────────────
# User-defined areas from custom_areas table. Filled once near startup so the
# validator recognises them for the remainder of the run.

_custom_areas: set[str] = set()


def register_custom_areas(areas):
    """Extend the accepted-areas set with user-defined custom areas."""
    _custom_areas.update(a for a in (areas or set()) if a)


def get_custom_areas():
    """Return the currently registered custom areas (read-only copy)."""
    return set(_custom_areas)


# ── Category validation / coercion ───────────────────────────────────────────────

def validate_and_coerce_category(category: str) -> tuple[str, bool]:
    """Return (canonical_category, was_coerced).

    If ``category`` is already one of the 5 canonical Categories (or a
    registered custom category), returns it unchanged. Otherwise tries the
    alias table (case-insensitive, en-dash tolerant); if still no match,
    falls back to 'Content'.

    Post-2026-05-21: returns ONLY canonical Category values (Tone, Content,
    Formatting, Structure, Templates). Pre-refactor it could return Section-
    style values like 'What We Offer -- Pay'; that behavior is gone -- those
    are now Section values, not Categories.
    """
    a = (category or '').strip()
    if a in CATEGORIES_SET or a in _custom_areas:
        return a, False
    key = a.lower().replace('—', '-').replace('–', '-').strip()
    key = ' '.join(key.split())
    if key in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[key], True
    return 'Content', True


# Deprecated alias for backward compatibility with pre-2026-05-21 callers.
# Remove after the code sweep migrates every call site to the new name.
validate_and_coerce_area = validate_and_coerce_category


def coerce_consolidated_type(area: str, issue_type: str) -> tuple[str, str]:
    """If the (area, issue_type) is a consolidated type that should live under
    a specific whole-posting area, return the corrected (area, issue_type).
    Otherwise return the inputs unchanged.

    This ensures that even if the AI puts 'Spelling and Grammar' under
    'Qualifications', it gets re-homed to 'Content' automatically.
    """
    it = (issue_type or '').strip()
    if it in CONSOLIDATED_TYPES:
        return CONSOLIDATED_TYPES[it], it
    return (area or '').strip(), it


# ── Template pattern matching (2026-04-21) ────────────────────────────────────
# Templates is the 7th Category. Unlike the other 6, a ticket is routed here
# by matching its offending_text against a captured pattern — not by issue-
# type logic. Patterns live in email_controls.template_pattern; the routing
# layer compares each incoming finding's offending_text against every active
# pattern BEFORE the grammar-class drift guard and BEFORE auto_register_pair.
#
# Matching is exact after whitespace normalization (trim + collapse internal
# runs). Case-sensitive. Per a 2026-04-21 design decision, the admin is the reliability layer for
# what counts as a template — the system facilitates the decision via
# point-and-click capture, not by fuzzy judgment. Loosening the matcher
# would push that judgment into code, which is exactly the wrong direction.


def normalize_template_pattern(s) -> str:
    """Return the canonical form of a template-match string.

    Trims leading/trailing whitespace and collapses internal whitespace runs
    to single spaces. Case-sensitive. The same normalization is applied to
    both the stored template_pattern values AND to incoming offending_text
    at match time, so mismatches can only come from genuine content
    differences, not formatting drift.
    """
    if not s:
        return ''
    return ' '.join(str(s).split())


def match_template(offending_text, template_map):
    """Return the canonical (area, issue_type) for the template whose pattern
    exactly matches this offending_text, or None if no match.

    ``template_map`` is ``{normalized_pattern: (area, issue_type)}`` — built
    once per run via ``db.get_template_patterns()``.
    """
    if not offending_text or not template_map:
        return None
    key = normalize_template_pattern(offending_text)
    if not key:
        return None
    return template_map.get(key)


# ── Pair validation ───────────────────────────────────────────────────────────

_MALFORMED_PATTERNS = [
    '__show_on_community', '__email_setting', '__fix_instruction',
    '__notes', '__default_severity',
]


def is_valid_pair(area: str, issue_type: str) -> bool:
    """Return False for (area, issue_type) pairs that look like schema artifacts
    or are empty/too long."""
    if not area or not area.strip() or not issue_type or not issue_type.strip():
        return False
    for val in (area.strip(), issue_type.strip()):
        for pat in _MALFORMED_PATTERNS:
            if pat in val:
                return False
        if len(val) > 120:
            return False
    return True


# ── Auto-approve rules ────────────────────────────────────────────────────────
# Grammar/spelling issues are low-judgment and high-volume — no real value in
# the maintainer manually approving every new phrasing variant the AI invents.

AUTO_APPROVE_KEYWORDS = (
    'grammar',
    'spelling',
    'typo',
    'punctuation',
    'syntax',
    'capitalization',
)


def is_auto_approve(issue_type: str, area: str = '') -> bool:
    """Return True if the issue belongs to the auto-approve grammar class.

    Checks both issue_type and area so that either "Spelling Error" or a future
    "Spelling" area would match.
    """
    hay = f'{area or ""} {issue_type or ""}'.lower()
    return any(kw in hay for kw in AUTO_APPROVE_KEYWORDS)


# ── Exclamation-point rules ──────────────────────────────────────────────────

CANONICAL_EXCL_AREA  = 'Tone'
CANONICAL_EXCL_ITYPE = 'Excessive Exclamation Points'
CANONICAL_EXCL_CT    = f'{CANONICAL_EXCL_AREA} \u2014 {CANONICAL_EXCL_ITYPE}'


def is_exclamation(issue_type: str, area: str = '') -> bool:
    """Return True if the (area, issue_type) pair is any exclamation variant."""
    hay = f'{area or ""} {issue_type or ""}'.lower()
    return 'exclamation' in hay


def violates_exclamation_rule(offending_text: str, issue_summary: str = '') -> bool:
    """True if text still violates the current exclamation rule.

    Current rule: any sentence ending in 2+ "!" OR more than 2 sentences
    ending in a single "!".
    """
    text = f'{offending_text or ""}\n{issue_summary or ""}'
    if re.search(r'!{2,}', text):
        return True
    single = re.findall(r'!(?!!)', text)
    return len(single) > 2


# ── Severity helpers ──────────────────────────────────────────────────────────

SEVERITY_ORDER = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}


def severity_sort_key(s: str) -> int:
    """Return a numeric sort key for severity strings (lower = more severe)."""
    return SEVERITY_ORDER.get((s or '').upper(), 4)


# ── Legacy parsing helpers ────────────────────────────────────────────────────
# Used to derive (area, issue_type) from old-format composite check_type strings.

# Maps legacy prefix (before ' — ') → canonical area
_PREFIX_TO_LOCATION = {
    'HTML':             'HTML',
    'Tone':             'Tone',
    'Formatting':       'Formatting',
    'Compensation':     'What We Offer \u2014 Pay',
    'Title Formatting': 'Job Title',
    'Qualifications':   'Qualifications',
    'Responsibilities': 'Responsibilities',
    'Draft Artifact':   'Content',
    'Typo':             'Content',   # Retired 2026-04-15 — 'Typo — Body Text' AUTO check removed; kept for legacy string parsing
}

# Exact-match overrides for check_types without a clean separator.
# Updated April 14, 2026 to reflect issue-type audit renames.
_EXACT_LOCATION_MAP = {
    'Bonus/Pay in Job Title':               ('Job Title',              'Pay/Bonus in Title'),
    # Renamed 2026-04-15: 'Excessive ALL CAPS' / 'Excessive ALL CAPS Emphasis'
    # both coerce to the new canonical name 'Body Text in ALL CAPS' (Tone).
    'Excessive ALL CAPS Emphasis':          ('Tone',                   'Body Text in ALL CAPS'),
    'Excessive ALL CAPS':                   ('Tone',                   'Body Text in ALL CAPS'),
    'Body Text in ALL CAPS':                ('Tone',                   'Body Text in ALL CAPS'),
    'Generic Location Language':            ('Content',                'Generic Language'),
    'Generic Language':                     ('Content',                'Generic Language'),
    'Highlighted / Colored Text':           ('HTML',                   'Highlighted/Colored Text'),
    'Highlighted Text':                     ('HTML',                   'Highlighted/Colored Text'),
    'Inconsistent Wage Figures':            ('What We Offer \u2014 Pay', 'Inconsistent Wage Figures'),
    'Incorrect Role Description in Opening':('Community Introduction', 'Incorrect Role Description'),
    # Brand boilerplate and Missing EEO are now cross-cutting Structure/MRS.
    # The legacy composite strings still parse here so old data remains
    # parseable, but the canonical home is Structure/Missing Required Section.
    'Missing Brand Boilerplate':            ('Structure',              'Missing Required Section'),
    'Missing EEO Statement':               ('Structure',               'Missing Required Section'),
    'Missing Required Section':            ('Structure',               'Missing Required Section'),
    # Posting Too Short (whole-posting <200 words) retired 2026-04-15.
    # Responsibilities length is now covered by Responsibilities Too Short /
    # Responsibilities Too Long under the Responsibilities area. Legacy
    # string still parses to Content/Posting Too Short so rejected_issues
    # can match it; it should never appear in live output.
    'Posting Too Short':                   ('Content',                 'Posting Too Short'),
    'Posting Too Long':                    ('Structure',               'Posting Too Long'),
    'Raw Email in Description':            ('Content',                 'Raw Email Address'),
    'Email Address in Posting':            ('Content',                 'Raw Email Address'),
    'Spelling and Grammar':                ('Content',                 'Spelling and Grammar'),
    'Resident/Patient Mix':                ('Tone',                    'Resident/Patient Mix'),
    'Inconsistent Resident Terminology':   ('Tone',                    'Resident/Patient Mix'),
    'Filler Phrase':                        ('Tone',                   'Filler Phrase'),
    'Informal Language':                    ('Tone',                   'Informal Language'),
    'Formatting Issue':                     ('Formatting',             'Formatting Issue'),
    'Compensation Clarity':                 ('What We Offer \u2014 Pay', 'Compensation Clarity'),
    'Title / Description Mismatch':        ('Job Title',               'Title/Description Mismatch'),
    'Title Formatting':                    ('Job Title',               'Title Formatting'),
    'Unfilled Placeholder':                ('Content',                 'Unfilled Placeholder'),
    'Wrong Community Name in Boilerplate': ('Who We Are',              'Wrong Community Name'),
}

# Maps section names in 'Missing Required Section — <name>' patterns.
#
# NOTE (2026-04-15 second wave): Missing Required Section is now a cross-cutting
# check whose canonical area is always 'Structure' (see CONSOLIDATED_TYPES).
# This table is only used by the legacy parser derive_location_and_type() for
# composite check_type strings like "Missing Required Section — EEO" that
# historical data still carries. The values here reflect the old per-section
# home for parsing old records; modern MRS tickets always live under Structure
# regardless of what section is named in offending_text.
_MISSING_SECTION_LOC = {
    'who we are':                     'Who We Are',
    'what we offer':                  'What We Offer \u2014 General',
    'community introduction':         'Community Introduction',
    'community/company introduction': 'Community Introduction',
    'community / company introduction':'Community Introduction',
    'responsibilities':               'Responsibilities',
    'qualifications':                 'Qualifications',
    'eeo':                            'EEO Statement',
    'brand boilerplate':              'Who We Are',
}


def derive_location_and_type(check_type: str) -> tuple[str, str]:
    """Return (area, issue_type) for a legacy check_type composite string.

    Priority:
      1. Exact match in _EXACT_LOCATION_MAP
      2. 'Missing Required Section — <section>' pattern
      3. '<prefix> — <issue>' split where prefix is in _PREFIX_TO_LOCATION
      4. Fallback: treat the whole string as the issue_type under 'Content'
    """
    ct = (check_type or '').strip()
    if not ct:
        return ('Content', '')

    # 1. Exact match
    if ct in _EXACT_LOCATION_MAP:
        return _EXACT_LOCATION_MAP[ct]

    # 2. Missing Required Section variants
    if ct.startswith('Missing Required Section') or ct.startswith('Missing Section'):
        tag = 'Missing Required Section' if ct.startswith('Missing Required Section') else 'Missing Section'
        remainder = ct[len(tag):].lstrip(' \u2014\u2013-').strip()
        if not remainder:
            return ('Structure', 'Missing Required Section')
        rl = remainder.lower()
        for key, loc in _MISSING_SECTION_LOC.items():
            if key in rl:
                return (loc, 'Missing Required Section')
        return ('Structure', f'Missing Required Section')

    # 3. Prefix split on em-dash or double-dash
    for sep in (' \u2014 ', ' -- ', ' \u2014 '):
        if sep in ct:
            prefix, suffix = ct.split(sep, 1)
            prefix = prefix.strip()
            suffix = suffix.strip()
            if prefix in _PREFIX_TO_LOCATION:
                return (_PREFIX_TO_LOCATION[prefix], suffix)
            return (prefix, suffix)

    # 4. Fallback
    return ('Content', ct)
