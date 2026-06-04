# Cedarline Senior Living — Job Posting Style Guide

> **How this file is used:** The AI review layer (`run_ai_review.py`) reads this
> document into its system prompt before analyzing a posting, and several
> deterministic checks in `fetch_jobs.py` / `pre_scan.py` enforce the concrete
> phrases called out below. Edit a rule here and the next review run reflects
> it — but if you change a phrase that a regex check keys on (the brand
> boilerplate markers in §5, the generic-location phrase in §10, the careers
> email domain in §8), update the matching check too. See the project
> `CLAUDE.md` note "Style guide = AI configuration."

This guide is a generic, illustrative standard for a fictional senior-living
operator. It demonstrates the *kinds* of rules a careers-page QA tool enforces
without reproducing any real employer's proprietary language.

---

## 1. Required Sections

Every job posting **must** contain all of the following. Headings need not match
the canonical names exactly — use the **Accepted Headings** in §1a to decide
whether a section is present before flagging it as missing.

| Section | Purpose |
|---|---|
| **Community / Company Introduction** | Brief overview of the specific community or Cedarline Senior Living as a whole |
| **Who We Are** | Standard Cedarline brand statement (see §5) |
| **What We Offer** | Compensation, benefits, and perks |
| **Job Description / Role Overview** *(optional heading)* | Summary of the day-to-day work |
| **Responsibilities** | Bulleted list of key duties |
| **Qualifications** | Required credentials, experience, and skills |
| **Equal Opportunity Employer Statement** | Standard EEO disclaimer (see §6) |

**Issue type to use.** Both of the following are **whole-posting** (cross-cutting)
issue types whose canonical category is always `Structure`. The specific section
that is missing or truncated goes in the `offending_text` field (e.g.
`"Missing section: What We Offer — Pay"`), never in the `category` field.

- **`Structure / Missing Required Section`** — the section (and all of its
  accepted headings) is entirely absent. Locked at HIGH severity. The test is:
  can the section's subject matter be found *anywhere* in the posting? If
  Responsibilities content (actual duties) appears under a "Job Description"
  heading — or with no heading at all — the section is PRESENT, not missing. A
  posting missing multiple required sections produces multiple tickets, one per
  missing section.

  **Responsibilities threshold.** Do not flag `"Missing section: Responsibilities"`
  when the posting contains two or more bullet points or duty-describing
  sentences (those starting with an action verb like "Assists", "Provides",
  "Monitors", "Coordinates"). Thin-but-present content is a different check
  (§4c), not a missing section.
- **`Structure / Truncated Section Content`** — the section IS present but its
  content is cut off: ends mid-sentence, ends mid-bullet, or contains an orphan
  word fragment (e.g. a Qualifications bullet reading "Previous A love for
  seniors" with no completion). Locked at MEDIUM severity.
- **Neither** — if the content is clearly present and the only complaint is
  missing bullets, missing bold, or "it's a paragraph not a list," do **not**
  flag. HTML formatting is stripped from the text the AI sees (§4b), so
  surviving formatting is not a reliable signal.

These are three distinct states — don't conflate them.

### 1a. Accepted Heading Aliases

When checking for a required section, accept **any** of the following as
satisfying it. Matching is case-insensitive and ignores trailing punctuation.

**Community / Company Introduction**
Any opening paragraph that names the specific community and describes what it
offers residents counts — no heading required. Also accept:
- About Us / About the Company / About [Community Name]
- Join Us / Join Our Team

**Who We Are**
- Who We Are / About Us / About the Company / About [Community Name] / ABOUT US

**What We Offer**
- What We Offer (any capitalization, with or without a colon)
- Why Us? / Why You'll Love Working Here
- Benefits / Our Benefits / Benefits Include
- Compensation & Benefits / Compensation and Benefits / What We Provide

**Job Description / Role Overview**
- Job Description / Role Overview / Your Role / Position Summary / Job Summary
- Description / About the Opportunity / About the Role

**Responsibilities**
- Responsibilities / Key Responsibilities / Primary Responsibilities
- Essential Duties / Essential Duties and Responsibilities
- What You'll Do / What You Will Do / Day-to-Day

**Qualifications**
- Qualifications / Minimum Qualifications / Requirements / Minimum Requirements
- Who You Are / Education / Education Required

**Equal Opportunity Employer Statement**
No heading required. Flag as missing only if the phrase
"equal opportunity employer" does not appear anywhere in the posting body.

**Heading matching is case-INSENSITIVE.** Do not flag a section as missing
because of capitalization or punctuation differences.

---

## 2. Job Title Standards

- The **posted job title** should match or closely align with the **first
  heading** inside the description body.
- Acceptable variations: abbreviations (`CNA` vs `Certified Nursing Assistant`)
  or added shift/location detail (`CNA – Day Shift`).
- **Not acceptable:** a posted title of `"Cook"` opening with
  `"Executive Chef – Fine Dining"` — substantively different roles.
- Titles should use **Title Case** (`Certified Nursing Assistant`, not
  `CERTIFIED NURSING ASSISTANT` or `certified nursing assistant`).
- **Standard healthcare and industry acronyms are always acceptable in ALL
  CAPS.** Do NOT flag: PRN, QMAP, CNA, LPN, RN, OT, COTA, DOE, FT, PT, NOC,
  PTA, OTR, DPT, SNF, AL, IL, MC, Med Tech. Any well-known abbreviation
  conventionally written in caps is treated the same way.

**Flag if:** posted title and the first description heading differ by more than
one meaningful word, or the **entire title** is ALL CAPS or all lowercase.
Individual standard-acronym caps words are not violations.

---

## 3. Tone and Language

- **Warm and welcoming** — postings should feel inviting, not like a compliance
  form. Warm, aspirational, emotionally expressive language is part of
  Cedarline's brand voice. Phrases like "cherished residents," "a nurturing
  heart," and "Perfect for students!" are **acceptable and on-brand**. Do NOT
  flag these as informal or unprofessional.
- **Second person** — address the candidate as "you" and "your."
- **Active voice** — prefer `"You will provide care"` over
  `"Care will be provided by the employee."`
- **Consistent vocabulary.** "Residents" and "seniors" are **synonyms** for this
  check and may be used interchangeably — do NOT flag a posting that uses both.
  Treat it as a violation only when the posting drifts into *clinical*
  terminology like "patients" or "clients" in a way that blurs the care model
  (e.g., a senior-living posting that switches to "patients" as if it were a
  hospital).
- **Conversational closings are acceptable** — "If this sounds like you, we'd
  love to hear from you!" and similar calls to action make postings feel human.
  Do NOT flag these as filler.
- **Exclamation points.** Up to **three** sentence-ending exclamation points per
  posting is acceptable; **four or more** is a flag. A single sentence ending in
  `!!` is acceptable; `!!!` or more is a flag. The standard benefits boilerplate
  line ending "…holidays, 401k and more!!!" is approved template language and is
  **exempt** from the check. (Enforced by `pre_scan.py` as the cross-cutting
  `Tone / Excessive Exclamation Points` issue type — do not create
  section-specific exclamation variants.)
- **Informal Language** should flag only **genuinely unprofessional** wording —
  text-speak ("u", "tbh", "lol"), crude humor, or slang that undercuts
  professionalism ("gonna wanna", "hit us up", "swing by"). Warm, expressive,
  motivational phrasing is **never** an Informal Language violation, even when
  florid.

**Flag if:** the posting alternates between `residents`/`seniors` and *clinical*
terms (`patients`/`clients`) in a way that blurs the care model, uses genuinely
unprofessional slang, or is so informal it obscures what the job entails.

---

## 4. Formatting Rules

### 4a. Text Formatting
- **No highlighted or colored text.** All body text should be plain black.
  Background-color styles and `<mark>` tags are almost always copy-paste
  artifacts from Word or Google Docs.
- **No inline font-size overrides.** Section headers may be bold; they should
  not carry custom font sizes set via inline HTML styles.
- **No underlined text** (except hyperlinks).
- Bold is for section headings and key terms only — not scattered emphasis.

### 4b. Lists
- Responsibilities and Qualifications should use **bulleted lists**, not numbered
  lists (numbering implies a required sequence).
- Bullets should be **parallel in structure** — all noun phrases or all verb
  phrases within a single list. Parallelism issues are the cross-cutting
  `Formatting / Parallel Structure` type (MEDIUM); the affected section goes in
  `offending_text`.
- **No nested sub-lists** — sub-bullets lose their indentation when pasted into
  the careers system and often flatten on job boards.
- **Note:** the posting text the AI receives is HTML-stripped. Bullets and bold
  may not be visible. Do NOT flag missing bullet formatting unless the content
  is clearly a run-on paragraph with no logical list structure.

### 4c. Spacing and Length
- Postings should generally run **300–800 words** of meaningful content for
  clinical / office / leadership roles. Practical roles (Cook, Housekeeper,
  Driver, etc.) may be shorter.
- **Responsibilities bullet count** is enforced by two locked pairs:
  - `Responsibilities / Responsibilities Too Short` — HIGH. Fires when the
    section has **fewer than 2 bullets** of real duty content. Applies to every
    role.
  - `Responsibilities / Responsibilities Too Long` — MEDIUM. Fires above **15
    bullets**.
  - The canonical list of short-section roles lives in
    `taxonomy.py → SHORT_SECTION_ROLES` — keep that list and this paragraph in
    sync.

---

## 5. Required Brand Boilerplate

### "Who We Are" Block
Every posting must include a version of the following (exact wording may vary
slightly — it is the *substance* that matters):

> *"Our highest aim is to do and be the best in all we undertake, and to provide
> a Cedarline life for every resident, their families and our employees."
> — Marian Ellsworth, CEO*
>
> Cedarline Senior Living is a premier assisted living and memory care provider
> in the Western United States. Founded in 2012…

**Single source of truth.** Brand-boilerplate detection is owned exclusively by
the AUTO pre-scan (`pre_scan.py → _check_brand_boilerplate`), which looks for the
canonical markers **"Marian Ellsworth"**, **"highest aim"**, and **"Cedarline
life for every resident"** and fires `Structure / Missing Required Section` at
HIGH severity with offending_text `"Missing section: Who We Are — Brand
Boilerplate"` when they are absent. The AI must **not** flag this itself. If the
markers are present, the boilerplate is fulfilled — even if it lives in the
opening paragraph rather than under a dedicated "Who We Are" heading.

---

## 6. Equal Opportunity Employer Statement

Every posting must end with a version of the standard EEO statement. It must
include at minimum the phrase **"equal opportunity employer"** and reference
protection from discrimination based on race, color, religion, age, sex, and
national origin.

**Flag if:** the phrase `"equal opportunity employer"` does not appear anywhere
in the description. This is a section-presence check and consolidates under
`Structure / Missing Required Section` with offending_text
`"Missing section: EEO Statement"` (AUTO-owned by `fetch_jobs.py`).

---

## 7. Compensation Mentions

- If a wage or salary is stated, it should appear **once**, in the "What We
  Offer" section.
- A pay range (e.g., `$18.00–$22.00/hr`) is preferred over a single figure, but a
  single starting wage ("starting at $19") is **acceptable** and should NOT be
  flagged. Minor formatting variations (a missing dollar sign on a range's upper
  bound) are cosmetic, not violations.
- A salary range listed alongside a bonus ("$65,000–$75,000 base + quarterly
  bonus") is clear and acceptable.
- **Performance-based, discretionary, sales-incentive, and "discussed at
  interview" bonuses are EXEMPT from missing-amount flags.** Phrasing like
  "performance-based incentive eligibility" or "bonus discussed at interview"
  does NOT need to disclose a dollar amount.

**Flag if:** more than two distinct dollar amounts appear with genuinely
conflicting values, or figures are so ambiguous a candidate can't tell what the
pay actually is. Do NOT flag single wage figures, minor formatting issues,
clearly labeled salary-plus-bonus combinations, or vague bonus references.

---

## 8. Contact and Application Instructions

- Candidates should always be directed to apply via the **Apply button** on the
  careers page.
- **Recruiter email addresses should not appear in the posting body.** They
  expose staff contact information publicly and bypass applicant tracking.

**Flag if:** any company careers-domain email address (e.g.
`hiring@willowbend.cedarline.example.com`) appears in the description body.
AUTO-owned by `fetch_jobs.py` as `Content / Raw Email in Description`.

---

## 9. Template / Draft Artifacts

- All template placeholders must be filled before a posting goes live. Patterns
  like `[INSERT LOCATION]`, `[COMMUNITY NAME]`, `[HIRING MANAGER]`, and any text
  in square brackets that reads like a fill-in instruction must never appear in
  a live posting.

**Flag as CRITICAL if:** any `[...]` placeholder pattern is detected.

---

## 10. Community-Specific vs. Generic Postings

- Postings should reference the **specific community** by name at least once in
  the opening paragraph.
- Generic postings that say "a large senior living community" without naming the
  location are acceptable only as temporary placeholders.

**Flag as LOW severity if:** the opening paragraph contains the phrase
`"large senior living community"` (or similar generic phrasing) without naming
the actual community. This is cosmetic — the name usually appears elsewhere.

---

## 11. Spelling and Grammar (Content / Spelling and Grammar)

This check is **AI-owned** (no AUTO/regex layer — a regex tokenizer can't handle
possessives, compound words, or brand terms cleanly). Issue type:
`Content / Spelling and Grammar` (HIGH, locked). **Category is always
`Content`**, regardless of which section the error appears in.

### 11a. Categorical exceptions — do NOT flag
The list is illustrative, not exhaustive — give a word the benefit of the doubt
if it clearly belongs to one of these categories:

- **Healthcare / senior-living jargon:** ADL, QMAP, LPN, CNA, RN, CMA, MDS,
  HIPAA, AL, IL, MC, SNF, PT, OT, COTA, med aide, med tech, PRN, DON, NOC, BLS,
  rehab, dementia, Alzheimer's, hospice.
- **Software / tech terms:** HTTPS, URL, API, PDF, Wi-Fi, iPhone, iPad.
- **Brand names and Cedarline vocabulary:** Cedarline, Marian Ellsworth.
- **Common compound words:** onsite, offsite, online, multi-state, multi-site.
- **Proper nouns:** community names, city names, person names.
- **Possessive forms** ending in `'s` or `'` (`residents'`, `Bachelor's`,
  `Alzheimer's`).
- **Casual-but-valid hyphenated compounds:** team-oriented, detail-oriented.
- **ALL CAPS words** (likely acronyms), **words containing digits** (`401k`,
  `24/7`), **hyphenated compounds** (`full-time`, `live-in`), and **anything
  inside quotation marks**.

### 11b. Show your work
Every finding's `issue_summary` must name the **specific rule violated** — e.g.
"missing article before 'assisted living community'", "subject-verb
disagreement: 'team are' should be 'team is'", "clear misspelling: 'recieve'
should be 'receive'". If you cannot name a specific rule, **do not emit the
finding.** "This looks wrong" is not a valid reason.

### 11c. One finding per error
Emit **one** ticket per spelling or grammar error. `offending_text` holds the
specific word or phrase; `issue_summary` holds the reason (§11b). Do not bundle
multiple errors into one ticket.

---

*Illustrative portfolio style guide — review periodically or after any brand
guideline change.*
