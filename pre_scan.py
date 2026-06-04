"""
pre_scan.py -- Fast regex pre-scan of Hireology job postings.

Run before the AI review to catch deterministic issues without an API call.
Results are written directly as AUTO tickets. A structured brief is returned
for inclusion in the Claude prompt so it does not re-flag these items.

AUTO vs CLAUDE ownership (2026-04-16):
  AUTO  — deterministic regex/structural checks live here (or in
          fetch_jobs.py). Fast, cheap, no API cost. Every AUTO-owned pair
          is listed in _AUTO_HANDLED below so the AI knows not to re-flag.
  CLAUDE — contextual/judgment-based checks owned by the AI. Driven by
          the SYSTEM_PROMPT in run_ai_review.py + docs/style_guide.md.
          Unknown AI pairs (not in email_controls) are silently dropped.

To add a new AUTO check: use the Claude qa-rules-maintenance skill, which
registers the (category, issue_type) pair in email_controls, commits the
detection regex into this file, and adds the issue_type string to
_AUTO_HANDLED — all in one pass. The in-dashboard Add Check wizard was
removed 2026-04-24.

New checks added here (not already in fetch_jobs.py AUTO pipeline):
  HTML-FONT    HTML — Inline Font Size
  HTML-COLOR   HTML — Inline Text Color Override
  HTML-ULINE   HTML — Underline Tag
  EXCLAIM      Tone — Excessive Exclamation Points
  BRAND        Missing Brand Boilerplate

Already handled by fetch_jobs.py AUTO (included in brief only, no new ticket):
  EEO Statement, Highlighted/Colored Text, Unfilled Placeholder,
  Raw Email in Description, Inconsistent Wage Figures, Bonus/Pay in Job Title,
  Title/Description Mismatch, Body Text in ALL CAPS

Context gathered for AI brief (no ticket created):
  dollar_amounts      list of $ strings found in plain text
  generic_location    True if known generic-location phrase detected
"""

import re
from html.parser import HTMLParser


# ── HTML stripping ─────────────────────────────────────────────────────────────

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def get_text(self):
        return ' '.join(p.strip() for p in self.parts if p.strip())


def strip_html(html):
    """Return clean readable text with all HTML tags removed."""
    s = _Stripper()
    try:
        s.feed(html or '')
    except Exception:
        pass
    return s.get_text()


# ── Individual checks ──────────────────────────────────────────────────────────

def _check_inline_font_size(html):
    """Flag inline font-size style overrides (copy-paste artifact from Word/Docs)."""
    hits = re.findall(r'font-size\s*:\s*[\d.]+(?:pt|px|em|rem|%)', html, re.I)
    if not hits:
        return None
    return {
        'severity':     'MEDIUM',
        'category':         'Formatting',
        'issue_type':   'Inline Font Size',
        'issue_summary': (
            f'{len(hits)} inline font-size override(s) detected. '
            'Almost always a copy-paste artifact from Word or Google Docs.'
        ),
        'offending_text': hits[0][:120],
    }


def _check_inline_text_color(html):
    """Flag non-black inline text colour on body elements (not background-color)."""
    # Match 'color:' that is NOT preceded by 'background-' and NOT black/default
    # Using a negative lookbehind to exclude background-color
    pattern = re.compile(
        r'(?<![a-z-])color\s*:\s*'
        r'(?!black\b|#000000\b|#000\b|rgb\(0\s*,\s*0\s*,\s*0\b|rgba\(0\s*,\s*0\s*,\s*0\s*,\s*1)'
        r'([^;"\'>]{3,40})',
        re.I
    )
    hits = pattern.findall(html)
    if not hits:
        return None
    return {
        'severity':     'MEDIUM',
        'category':         'Formatting',
        'issue_type':   'Inline Text Color Override',
        'issue_summary': (
            f'{len(hits)} element(s) with non-black inline text color. '
            'All body text should be plain black.'
        ),
        'offending_text': f'color: {hits[0].strip()[:80]}',
    }


def _check_underline_tags(html):
    """Flag <u> tags that are not inside <a> hyperlinks."""
    u_matches = list(re.finditer(r'<u\b[^>]*>(.*?)</u>', html, re.I | re.S))
    non_link = []
    for m in u_matches:
        # Look at the 300 chars before this <u> for an unclosed <a>
        before = html[max(0, m.start() - 300): m.start()]
        open_a  = len(re.findall(r'<a\b', before, re.I))
        close_a = len(re.findall(r'</a>', before, re.I))
        if open_a > close_a:
            continue   # It's inside a link — acceptable
        inner = strip_html(m.group(1))[:60]
        non_link.append(inner)
    if not non_link:
        return None
    return {
        'severity':     'MEDIUM',
        'category':         'Formatting',
        'issue_type':   'Underline Tag',
        'issue_summary': (
            f'{len(non_link)} non-hyperlink underline(s) detected. '
            'Underlines should be reserved for links only.'
        ),
        'offending_text': non_link[0],
    }


def _check_nested_lists(html):
    """Flag <ul>/<ol> tags that open while a <li> is still open (nested lists).

    Uses a lightweight tag-order scan so it works reliably on the imperfect HTML
    typical of Hireology postings, without needing a full DOM parser.

    A nested list is one where a <ul> or <ol> opens *inside* a <li> item rather
    than at the same level — the copy-paste artifact that produces indented sub-bullets
    that collapse or flatten on job boards.
    """
    tag_pattern = re.compile(r'<(/?(?:ul|ol|li))\b[^>]*>', re.I)
    matches = list(tag_pattern.finditer(html))

    li_depth    = 0
    nested_hits = []   # char positions of nested list opens

    for m in matches:
        raw     = m.group(1)
        name    = raw.lower().lstrip('/')
        opening = not raw.lower().startswith('/')

        if name == 'li':
            li_depth = max(0, li_depth + (1 if opening else -1))
        elif name in ('ul', 'ol') and opening and li_depth > 0:
            nested_hits.append(m.start())

    if not nested_hits:
        return None

    # Find the <li> tag that immediately precedes the first nested list and
    # extract its readable text content as the offending snippet.
    pos    = nested_hits[0]
    before = html[:pos]
    li_m   = list(re.finditer(r'<li\b[^>]*>', before, re.I))
    if li_m:
        li_start = li_m[-1].end()           # character after the opening <li> tag
        inner    = html[li_start:pos]       # content between <li> and the nested <ul>/<ol>
        snippet  = strip_html(inner)[:100].strip()
        if not snippet:
            # Parent <li> had no text before the sub-list — show the first nested item
            after    = html[pos:pos + 400]
            first_li = re.search(r'<li\b[^>]*>(.*?)</li>', after, re.I | re.S)
            if first_li:
                snippet = strip_html(first_li.group(1))[:80].strip()
    else:
        # Fallback: plain text of a window around the hit
        snippet = strip_html(html[max(0, pos - 200): pos + 100])[:100].strip()

    return {
        'severity':      'MEDIUM',
        'category':          'Formatting',
        'issue_type':    'Nested List Structure',
        'issue_summary': (
            f'{len(nested_hits)} nested sub-list(s) detected inside bullet items. '
            'Nested bullets lose their indentation when pasted into Hireology '
            'and often collapse to a flat list on job boards.'
        ),
        'offending_text': snippet,
    }


def _check_exclamation_points(plain):
    """Flag overuse of exclamation points under a single cross-cutting
    issue type. A single exclamation point ending a sentence is fine.

    Trigger conditions (updated 2026-04-15 to be less militant):
      1. Any sentence ends with THREE OR MORE exclamation points (e.g. "!!!").
         Two "!!" is borderline-warm and no longer flagged on its own.
      2. More than THREE sentences in the posting end with an exclamation
         point. (Previously: more than two. Warm CTA language is on-brand.)

    Approved boilerplate exemption: the Cedarline benefits template line that
    ends "...holidays, 401k and more!!!" is part of the standard template
    and does not count toward either trigger.

    Consolidating everything under one cross-cutting "Tone" type prevents the
    AI from inventing section-specific variants like
    "Benefits — Exclamation Points".
    """
    # Exempt the approved benefits boilerplate line from both counts. We strip
    # the approximate phrase before counting so it cannot contribute to a
    # violation. The match is intentionally loose to survive NBSPs and minor
    # punctuation drift across postings.
    _BOILERPLATE_EXEMPT = re.compile(
        r'(?:holidays?|holiday)[,\s\u00a0]+401\s*k[^.!?\n]*?!{1,}',
        re.I
    )
    cleaned = _BOILERPLATE_EXEMPT.sub('', plain)

    # Match any run of 3+ exclamation points. Two in a row is borderline-warm
    # and no longer flagged on its own (Cedarline tone is warm/aspirational).
    multi_bang_matches = re.findall(r'[^.!?\n]{0,60}!{3,}[!?]*', cleaned)

    # Match sentence endings that terminate with a single "!". A sentence-end
    # "!" is one not followed immediately by another "!" (so multi-bangs don't
    # double-count here; those are handled above).
    single_bang_sentences = re.findall(r'[^.!?\n]{0,60}!(?!!)', cleaned)
    single_bang_count = len(single_bang_sentences)

    multi_violation  = len(multi_bang_matches) > 0
    single_violation = single_bang_count > 3

    if not (multi_violation or single_violation):
        return None

    # Build offending_text from the actual violating snippets.
    examples = []
    if multi_violation:
        examples.extend(multi_bang_matches[:3])
    if single_violation:
        # Only include single-bang examples if THEY are what triggered the rule.
        examples.extend(single_bang_sentences[:3])
    snippet = ' … '.join(e.strip() for e in examples if e.strip())

    # Build a summary message that names the specific violation(s).
    parts = []
    if multi_violation:
        parts.append(
            f'{len(multi_bang_matches)} sentence(s) end with three or more '
            'exclamation points in a row — tone down to one or two per sentence.'
        )
    if single_violation:
        parts.append(
            f'{single_bang_count} sentences in the posting end with an '
            'exclamation point — limit is 3 per posting.'
        )
    summary = ' '.join(parts)

    return {
        'severity':       'MEDIUM',
        'category':           'Tone',
        'issue_type':     'Excessive Exclamation Points',
        'issue_summary':  summary,
        'offending_text': snippet,
    }


def _check_brand_boilerplate(plain):
    """Flag if the Cedarline CEO brand statement is absent.

    As of 2026-04-15 (second wave), the canonical home for this check is
    Structure / Missing Required Section — the same cross-cutting home used
    for every missing-section check. The specific section identity (Who We
    Are brand boilerplate) lives in offending_text, NOT in category. See CLAUDE.md
    standing rule "Missing Required Section Is a Cross-Cutting Check".
    """
    markers = ['marian ellsworth', 'highest aim', 'cedarline life for every resident']
    if any(m in plain.lower() for m in markers):
        return None   # Present — no ticket needed
    return {
        'severity':     'HIGH',
        'category':         'Structure',
        'issue_type':   'Missing Required Section',
        'issue_summary': (
            'Add the "Who We Are" brand boilerplate (CEO Marian Ellsworth quote) to this posting.'
        ),
        'offending_text': 'Missing section: Who We Are \u2014 Brand Boilerplate',
    }


# ── Spell-check ───────────────────────────────────────────────────────────────
#
# Retired 2026-04-15. The AUTO regex spellcheck lived here until it was
# retired in favor of the Claude-owned `Content / Spelling and Grammar`
# check. The regex tokenizer could not reliably handle possessives
# (residents', patients'), compound words (onsite), or brand terms
# (cedarline) without a brittle hand-curated allowlist. Claude handles
# these contextually. See rejected_issues for the tombstone and
# docs/style_guide.md for the replacement check's guardrails.


# ── Context helpers (AI brief only, no ticket) ─────────────────────────────────

def _get_dollar_amounts(plain):
    """Return deduplicated list of dollar strings found in plain text."""
    found = re.findall(
        r'\$[\d,]+(?:\.\d{2})?(?:\s*/\s*(?:hr|hour|year|yr|annual))?',
        plain, re.I
    )
    return list(dict.fromkeys(found))   # dedupe, preserve order


def _generic_location_detected(plain):
    """Return True if the known generic-location phrase appears verbatim."""
    return bool(re.search(r'large senior living community', plain, re.I))


# ── Main entry point ───────────────────────────────────────────────────────────

# Checks handled by the AUTO pipeline (fetch_jobs.py + pre_scan.py) — included
# in the Claude brief so it knows not to re-flag any of these.
_AUTO_HANDLED = [
    # fetch_jobs.py
    'Unfilled Placeholder',
    'Raw Email in Description',
    'Missing EEO Statement',
    'Highlighted / Colored Text',
    'Inconsistent Wage Figures',
    'Bonus/Pay in Job Title',
    'Title / Description Mismatch',
    'Body Text in ALL CAPS',   # renamed 2026-04-15 (was "Excessive ALL CAPS Emphasis")
    'Missing Community Name',
    'Missing Address',
    # pre_scan.py (also run as part of Standard Check via fetch_jobs.py)
    'HTML — Inline Font Size',
    'HTML — Inline Text Color Override',
    'HTML — Underline Tag',
    'HTML — Nested List Structure',
    'Tone — Excessive Exclamation Points',
    # Brand boilerplate, EEO, and similar section-presence checks now all
    # emit under the cross-cutting Structure / Missing Required Section
    # pair (2026-04-15 second wave). The specific section lives in
    # offending_text, e.g. "Missing section: Who We Are — Brand Boilerplate"
    # or "Missing section: EEO Statement".
    'Structure / Missing Required Section (Who We Are — Brand Boilerplate)',
    'Structure / Missing Required Section (EEO Statement)',
    # 'Typo — Body Text' removed 2026-04-15 — AUTO regex spellcheck retired;
    # spelling/grammar now CLAUDE-owned via Content / Spelling and Grammar.
]


def run_prescan(job):
    """
    Run all pre-scan checks on a job posting dict.

    Parameters
    ----------
    job : dict
        Raw Hireology job object with keys: id, name, organization, job_description.

    Returns
    -------
    issues : list[dict]
        Issue dicts with keys: severity, category, issue_type, issue_summary, offending_text.
        Write these directly as AUTO tickets.

    brief : str
        A structured text block to prepend to the Claude user message.
        Tells Claude what has already been checked so it focuses on
        what only it can evaluate.

    clean_text : str
        HTML-stripped plain text of the job description.
        Pass this to Claude instead of raw HTML to reduce token count.
    """
    html      = job.get('job_description', '') or ''
    plain     = strip_html(html)

    # --- Run new regex checks -------------------------------------------------
    issues = []
    for fn in (
        lambda: _check_inline_font_size(html),
        lambda: _check_inline_text_color(html),
        lambda: _check_underline_tags(html),
        lambda: _check_nested_lists(html),
        lambda: _check_exclamation_points(plain),
        lambda: _check_brand_boilerplate(plain),
    ):
        result = fn()
        if result:
            issues.append(result)

    # --- Gather context for AI brief ------------------------------------------
    dollar_amounts   = _get_dollar_amounts(plain)
    generic_location = _generic_location_detected(plain)

    # --- Build structured tee-up brief ----------------------------------------
    lines = []

    # 1. What's already handled
    lines.append('PRE-SCAN COMPLETE — do NOT re-flag any of the following:')
    for item in _AUTO_HANDLED:
        lines.append(f'  • {item} (handled by automated pipeline)')
    for issue in issues:
        category = issue.get('category', '')
        itype = issue.get('issue_type', '')
        display = f'{category} — {itype}' if category and itype else (itype or category)
        lines.append(f'  • {display} (detected by pre-scan, ticket already logged)')
    lines.append('')

    # 2. Compensation context
    lines.append('COMPENSATION CONTEXT:')
    if dollar_amounts:
        lines.append(f'  Dollar amounts found: {", ".join(dollar_amounts)}')
        lines.append(
            '  Evaluate: are these clearly labeled, consistent, and professionally presented? '
            'Flag if amounts appear to conflict, are duplicated with different values, or the '
            'section is missing context.'
        )
    else:
        lines.append(
            '  No dollar amounts detected. Evaluate whether compensation information is '
            'present and sufficient.'
        )
    lines.append('')

    # 3. Location language context
    lines.append('LOCATION LANGUAGE:')
    if generic_location:
        lines.append(
            '  ⚑ Exact phrase "large senior living community" detected in opening paragraph.'
        )
        lines.append(
            '  Also check for any other forms of generic location language that do not name '
            'the specific community.'
        )
    else:
        lines.append(
            '  Known generic phrase not detected. Still check whether the opening paragraph '
            'names the specific community clearly.'
        )
    lines.append('')

    # 4. What Claude should focus on
    lines.append('FOCUS YOUR REVIEW ON (only these areas):')
    lines.append('  - Missing required sections: Who We Are, What We Offer, Responsibilities, Qualifications')
    lines.append('  - Tone issues: filler phrases, resident/patient/client terminology mix, second-person voice')
    lines.append('  - Wrong community name in boilerplate text')
    lines.append('  - Title formatting (ALL CAPS full words, not acronyms like CNA/RN/LPN)')
    lines.append('  - Compensation quality (see context above)')
    lines.append('  - Generic location language (see context above)')
    lines.append('  - Spelling and grammar errors not already caught by pre-scan')
    lines.append('  - Any other genuine issue not already handled above')
    lines.append('')
    lines.append(
        'DO NOT flag exclamation points under any new check_type. '
        'Pre-scan owns the exclamation-point rule: it flags sentences ending '
        'in more than one "!" and postings with more than 2 sentence-ending '
        '"!" — all under the single cross-cutting type '
        '"Tone — Excessive Exclamation Points". Single exclamation points '
        'within the allowed count are acceptable. Do not create section-'
        'specific variants like "Benefits — Exclamation Points".'
    )
    lines.append('')

    brief = '\n'.join(lines)
    return issues, brief, plain
