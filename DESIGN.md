# ePepper — design

This file is the design source-of-truth for the web app. It captures the
**concept** and the **rules** every view is meant to follow, so the next
person editing a template knows when they're nudging things in the right
direction and when they're fighting the design.

It started as a chosen direction from a five-direction parallel exploration
(captured historically on the `claude/epepper-design-exploration-yVw1f`
branch — not part of this repo's tree — including the four alternatives
that lost). The text below has been updated to reflect the decisions made
during round 1–5 review.

---

## Concept

ePepper is treated as a **personal cooking journal** — the kind of slim,
well-set monthly magazine you'd keep on a kitchen shelf. The redesign
borrows from print: strict typographic hierarchy, generous margins,
hairline rules, small-caps section labels, and a single warm accent
(deep paprika) used sparingly enough that it actually means something
when it appears.

## Mood

Quiet, confident, a little austere. Closer to *Cherry Bombe*, *Apartment
Folio*, or a Phaidon cookbook than to a SaaS dashboard. The page is
mostly white space; the type does the heavy lifting.

## Intended user

A home cook who has spent real money on an e-ink kitchen display and
wants the companion app to feel like a thing, not a tool. Someone who
reads recipes the way other people read essays.

---

## Typography

| Family | Role | Used for |
|---|---|---|
| **Fraunces** (Google Fonts, OFL) | Display | Recipe titles (`h1`), page titles, the `ePepper` wordmark, the on-display recipe headline on the status page, instruction step numerals |
| **Inter** (Google Fonts, OFL) | UI + body | Paragraphs, lists, form controls, navigation, captions, section labels |
| **JetBrains Mono** (Google Fonts, OFL) | Mono | Only the `API_KEY` chip on the login hint copy — nowhere else |

**Rules**
- Section labels (`INGREDIENTS`, `INSTRUCTIONS`, `DISPLAY`,
  `TOMORROW`, etc.) are uppercase tracked **Inter**, never serif. The
  deliberate sans/serif contrast against the Fraunces title is the
  design.
- Big titles use Fraunces with `font-variation-settings: "opsz" 96+`
  and a SOFT axis around 50.
- Step numerals on recipe instructions are Fraunces *italic* — the
  italic glyph adds the only flourish the page allows itself.

## Color

A two-mode palette: bone-white paper and graphite ink. One accent.

| Role | Light | Dark |
|---|---|---|
| `--bg` | `#FBFAF7` | `#121110` |
| `--text` | `#1A1A1A` | `#F1ECE2` |
| `--muted` | `#807A70` | `#8E867A` |
| `--border` | `#E4DFD4` | `#2C2A26` |
| `--accent` (paprika) | `#B83227` | `#E5614F` |
| `--accent-soft` | `#F7E4E1` | `#3A1C18` |
| `--danger` | `#9B1C1C` | `#F0817E` |
| `--warn` | `#B07A0F` | `#D9A742` |

**Rules**
- One accent (paprika) carries the entire brand load: the italic `e`
  in the wordmark, the on-air dot, the favicon, the section h2 icons,
  the focused input ring, and the link-style primary actions.
- **No green** — the original wellness-y green was replaced because
  green pulls toward "garden / spa" while paprika pulls toward
  "cooking". This is a load-bearing identity decision.
- Hairlines are always `--border`, never `--border-strong`. There is
  no `--border-strong` in the production stylesheet anymore.

## Identity

The brand mark is **text only** — italic paprika `e` followed by upright
bold `Pepper`. No pepper SVG, no pictographic accompaniment. The
favicon collapses the wordmark to just the italic `e`.

If you find yourself reaching for a pictogram next to the wordmark,
that's the wrong instinct — the typography is the mark.

---

## Layout

### Masthead — magazine, not toolbar

The top bar is a **masthead**, not a labelled-icon dashboard toolbar.
Brand wordmark flush left; on the right a single row of small-caps
Inter text links (`ADD · STATUS`) separated by middle dots.
No icons next to the link labels. The theme follows the OS — there
is no in-app toggle, by design.

This is the rule the redesign quietly violated for a while —
labelled-icon-buttons read as "Notion clone", which is the SaaS-
dashboard composition the concept explicitly rejects. The masthead
treatment is the fix.

### Repertoire — magazine contents page

The repertoire reads as a **tiered magazine contents page**, not a
database table dump.

- Recipes are grouped by recency into named tiers — **This week /
  This month / Earlier this year / Older** — each introduced by a
  small-caps date eyebrow with a hairline on top.
- Inside each tier, rows still use the editorial vocabulary: serif
  title, tracked small-caps source, right-aligned `cooked X days ago`
  (or `never cooked`) in muted body type. Hairlines between rows.
- The currently-on-display recipe is **not** promoted to a separate
  lead row at the top. The on-air dot inside its list row is enough
  of a signal — a dedicated lead made the page feel like a
  newsletter, and the row-level indicator already does the job
  without competing with the tier rhythm.
- The on-air badge (`on display`) sits in its **own grid cell on
  the far right** of the row, pulled out of `.card-meta` so it
  pins to the row's right edge regardless of the cooked-text
  length. On mobile the badge stays on the title's row (top-right);
  the cooked-text drops to a row below. The paprika dot pulses
  via a 1.6 s scale + opacity loop so the "on air" reading is
  unmissable from across the room.
- The filter controls sit above the first tier with no extra
  divider — the first tier's own top hairline carries the gap. Order
  is fixed (most-recently-cooked first, or FTS5 relevance when
  searching) — there's no manual sort toggle. The controls cover the
  source + `#tag` filters only.

The bucketing uses string matching against `fmt_saved()`'s
deterministic phrases (`"days ago"`, `"last week"`, `"weeks ago"`,
`"last month"`, `"months ago"`, `"last year"`) so it stays in Jinja
without a Python helper.

### Recipe — editorial two-column

Desktop recipe pages are a magazine layout: a narrow ingredients
sidebar pinned on the left, the instructions column flowing on the
right. Instruction steps are numbered with **oversized italic Fraunces
numerals** (`01`, `02`, `03` …). Sub-headings inside instructions
restart the count per section.

**The numerals mark a sequence. A section of one doesn't have a
sequence to mark.** When a sub-heading has only a single step, the
template renders it as a plain `<p class="step-solo">` instead of
`<ol><li>` — the `<h3>` already carries the section identity, and a
solitary `01` stamped next to one instruction reads as label, not
flourish. The italic numeral is the page's only flourish; spending
it on a non-sequence burns the brand accent on nothing. The **e-ink
renderer mirrors this rule**: single-step sections drop the inline
`1.` prefix and render as plain body text under the italic-serif
sub-heading.

Meta line under the title (`from X · saved Y · cooked Z`) wraps
between segments but never inside one — each phrase is an
inline-block no-break span. The bullet separator is a `::after`
pseudo on every non-last segment so that, when the meta wraps to
two lines, the `·` stays at the end of the previous line and never
dangles at the start of the new one. The hairline below the meta
is the same `--border` as every other separator on the page.

**Ingredient lines do not get paprika bullet squares.** They get a
thin leading hairline indent and that's it. The brand accent is
already doing real work on the step numerals; doubling it up on every
ingredient line dilutes the numerals and breaks the concept's
"means something when it appears" rule.

The `Push to display` action is a **quiet link-style button**
(`button.link.accent`), centered under the recipe meta and above the
two-column body. It matches `Delete this recipe` in the footer, just
paprika instead of danger red. (A previous round tried it as a
small-caps coda at the very foot of the recipe; that buried the most
important verb on the page, so it's back to its placement under the
title.)

Mobile collapses the two-column to one, the ingredients section
becomes a top block, instructions flow below, the back chip tightens.

### Add — flat list of methods

`LINK` and `SNAP` are two **rows separated by hairlines**, not
rounded cards. Each row carries a **small-caps section eyebrow**
(same `.status-card-h-label` treatment used by `DISPLAY` / `REPERTOIRE`
on the Status page — Inter 12 px, 700, 0.18 em tracking, paprika
icon, text-dim text). Below the eyebrow sits a quiet muted help
line ("Paste a recipe link to add it to the repertoire." / "Snap a
cookbook page to add it to the repertoire.") in body type, then the
input affordance.

The earlier round used a serif-ish `h2` with sentence-case ("Paste a
recipe link") — but it competed with the page `h1` for weight and
didn't reuse the Status page's section vocabulary. Re-using the
small-caps eyebrow lets Add read as the same kind of structured
list of sections that Status uses, and frees the body type for
explanatory help text instead of restating the action verb.

### Status — flat list with the e-ink frame nested

Status sections (`DISPLAY`, `TOMORROW`, `REPERTOIRE`, `DEVICE`) are
the same hairline-separated rows. Each section's h2 has an optional
meta slot on the right (right-aligned, muted) — used by **Display**
for `Updated 26 min ago`, by **Device** for `Last seen X` plus the
overdue chip, and by **Repertoire** for `Backed up X` (so the backup
freshness reads with the same vocabulary as the device freshness
instead of being a separate body line below).

The **e-ink preview is nested inside the Display section**, between
the title row and the page-nav / Clear controls. There's no caption /
eyebrow above it — the Display section above is its caption. The page
nav and Clear button live below the preview, centered.

Recipe-on-display title uses the same `.display-title` Fraunces
treatment as the Tomorrow card so they read with identical weight.

### Login — single column, generous

The wordmark fills the top at `clamp(48px, 6vw, 72px)`. Directly under
it sits the access-code field with the merged paste/submit button
(`.input-action-btn`) inline. **No tagline above the form, no hint
below it.** The placeholder ("Access code") names the field; the
wordmark establishes which app you're signing into.

The access-code input is rendered as a **single bottom hairline**,
not a rounded-rectangle border. Same reasoning as the rest of the
system: rounded surfaces compete with the editorial paper, hairlines
sit in it. The merged paste/submit button keeps its outline because
it's a button, not an input.

The only paragraph that ever shows up next to the form is the inline
error (`<p class="error">…</p>`) on a failed sign-in, and it only
appears when `error` is truthy.

---

## Component patterns

### Toast / confirmation

Inline editorial banner, **not a floating modal pill**. Same accent-
left-bar + soft-paprika fill vocabulary as the rest of the system, so
every confirmation in the app speaks the same visual language. The
toast container (`#toast`) lives at the top of `<main>`; the templated
`.toast` wraps itself in the accent-left-bar markup.

The previous floating dark rounded pill is gone. Reaching for it again
is the wrong instinct — the editorial language doesn't have modals.

### Buttons

**Four roles. That's it:**

1. `button.link.accent` — primary actions, paprika, link-style (no
   filled chrome). Used on `Push to display` (recipe).
2. `button.link.danger` — destructive actions, danger red, link-style.
   Used on `Delete this recipe` (recipe) and `Clear` (status display).
3. `.iconbtn` — small square icon-only buttons in the topbar
   (Add / Status / Theme) and the page-nav strip.
4. `.input-action-btn` — the merged paste/submit affordance next to
   single-string text inputs (login access code, add URL). Lives
   inline with the input as a square button. See the "Merged
   paste / submit button" pattern below for the state machine.

There is no filled-paprika CTA.

### Merged paste / submit button (`.input-action`)

Forms that take a single short string (login access code, add-URL
field) get **one button** next to the input, not two. The button
switches state based on whether the field is empty:

- **Empty field** → Paste affordance (clipboard icon, quiet outline).
  Click reads `navigator.clipboard.readText()` into the field. The
  button is `type=button` so it doesn't submit on accidental keyboard
  activation.
- **Field has a value** → Submit affordance (arrow icon, paprika
  outline). Click submits the parent form (native or HTMX).

Markup always ships as `type=submit` so a no-JS visitor (or a password
manager autofilling the field) still gets a working button. The JS
in `input-action.js` downgrades to paste mode only after observing
the field is empty.

The Clipboard API degrades silently when unavailable (insecure context,
denied permission). In that case the button stays as it was (paste-state
icon visible, but clicking does nothing useful) — manual typing still
works and flips it to submit normally.

### h2 title-row meta

Status `<h2>`s have two slots: a `.status-card-h-label` on the left
(icon + uppercase section name) and an optional `.status-card-h-meta`
on the right (right-aligned, muted, normal-case). Use the meta slot
for a single timestamp / qualifier per card. Don't stuff multiple
facts in there.

---

## Mobile adaptations

The design is built mobile-first in practice — the only thing that
changes on small screens is **density**, not language:

- `main` top-padding tightens from 56 → 18 px so the page title lands
  on the first fold.
- `.library-header` becomes a single column (title above meta line).
- Recipe `.recipe-body` collapses to one column; ingredients flow
  above instructions.
- E-ink preview fills the column edge-to-edge, image renders at the
  column width.
- Icon-button labels in the topbar hide; the icons stay.
- All link buttons stay link buttons — no swap to filled buttons just
  because the screen is narrow.

---

## What the design rejects

If a future change looks like one of these, push back on it before
shipping:

- **Card-in-card chrome.** Each section is a hairline-separated row.
  Wrapping a section in a rounded surface defeats the editorial
  flatness.
- **Filled paprika CTAs.** Soft / link-style only. A filled paprika
  button was tried and rejected in round 1 review — it looked too
  loud against the white-space-heavy page.
- **Floating modals or rounded pill toasts.** Confirmations are inline
  accent-bar banners.
- **Multi-color accents.** One accent. Paprika. The danger red is a
  separate semantic, used only for destructive verbs.
- **Pepper icon next to the wordmark.** The typography is the mark.
- **Card backgrounds on the add / status / repertoire rows.** Hairlines
  carry the separation.
- **System-font fallbacks for headings.** Fraunces is the design.
  Inter is the design. The mono is the design.
