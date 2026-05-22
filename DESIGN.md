# ePepper — design

This file is the design source-of-truth for the web app. It captures the
**concept** and the **rules** every view is meant to follow, so the next
person editing a template knows when they're nudging things in the right
direction and when they're fighting the design.

It started as a chosen direction from a five-direction parallel exploration
(see `claude/epepper-design-exploration-yVw1f` for the full set, including
the four alternatives that lost). The text below has been updated to
reflect the decisions made during round 1–5 review.

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
| **Fraunces** (Google Fonts, OFL) | Display | Recipe titles (`h1`), page titles, the `ePepper` wordmark, the on-display recipe headline on the status page, the LLM cost stat, instruction step numerals |
| **Inter** (Google Fonts, OFL) | UI + body | Paragraphs, lists, form controls, navigation, captions, section labels |
| **JetBrains Mono** (Google Fonts, OFL) | Mono | Only the `API_KEY` chip on the login hint copy — nowhere else |

**Rules**
- Section labels (`INGREDIENTS`, `INSTRUCTIONS`, `NOTES`, `DISPLAY`,
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

### Library — vertical index, not a card stack

The library reads as a **magazine index**: serif titles, tracked
small-caps source labels, hairline rule dividers. No card chrome.
Sorting / filter dropdowns sit above with no extra divider — the
recipe list's own top hairline carries the gap.

The "currently on display" indicator is an **on-air dot**: borderless
pulsing paprika circle plus the tracked small-caps label
`ON DISPLAY`. Not a chip, not an icon, not a badge box.

### Recipe — editorial two-column

Desktop recipe pages are a magazine layout: a narrow ingredients
sidebar pinned on the left, the instructions column flowing on the
right. Instruction steps are numbered with **oversized italic Fraunces
numerals** (`01`, `02`, `03` …). Sub-headings inside instructions
restart the count per section.

Meta line under the title (`from X · saved Y · cooked Z`) wraps
between segments but never inside one — each phrase is an inline-block
no-break span. The hairline below the meta is the same `--border` as
every other separator on the page.

The `Push to display` action is a **quiet link-style button**
(`button.link.accent`), centered. It matches `Delete this recipe` in
the footer, just paprika instead of danger red.

Mobile collapses the two-column to one, the ingredients section
becomes a top block, instructions flow below, the back chip tightens.

### Add — flat list of methods

`Paste a recipe link` and `Snap a recipe` are two **rows separated by
hairlines**, not rounded cards. Each row has a sans-serif h2 with a
paprika section icon, then the input affordance. The URL row gets a
paste-from-clipboard helper next to the input.

Tile h2s use **Inter** (normal case, 17 px, weight 600), not Fraunces
— the magazine serif is reserved for content titles, not form section
labels.

### Status — flat list with the e-ink frame nested

Status sections (`DISPLAY`, `TOMORROW`, `LIBRARY`, `DEVICE`, `LLM`) are
the same hairline-separated rows. Each section's h2 has an optional
meta slot on the right (right-aligned, muted) — used by **Display**
for `Updated 26 min ago` and by **Device** for `Last seen X` plus the
overdue chip.

The **e-ink preview is nested inside the Display section**, between
the title row and the page-nav / Clear controls. There's no caption /
eyebrow above it — the Display section above is its caption. The page
nav and Clear button live below the preview, centered.

Recipe-on-display title uses the same `.display-title` Fraunces
treatment as the Tomorrow card so they read with identical weight.
The LLM card uses the same headline pattern for the monthly cost:
`~0.04 CHF this month` in serif (`inline-meta` keeps `this month`
inline), with the call breakdown as a muted line below.

### Login — single column, generous

The wordmark fills the top at `clamp(48px, 6vw, 72px)`. The form is
input + a small paste helper inline, followed by `Sign in →` as a
quiet `button.link.accent`. **No hint paragraph below the form** — the
field placeholder is enough.

---

## Component patterns

### Toast / confirmation

Inline editorial banner, **not a floating modal pill**. Same accent-
left-bar + soft-paprika fill as the `comments` callouts on the recipe
page, so every confirmation in the app speaks the same visual
language. The toast container (`#toast`) lives at the top of `<main>`;
the templated `.toast` wraps itself in the accent-left-bar markup.

The previous floating dark rounded pill is gone. Reaching for it again
is the wrong instinct — the editorial language doesn't have modals.

### Buttons

**Three roles. That's it:**

1. `button.link.accent` — primary actions, paprika. Used on `Push to
   display` (recipe), `Sign in →` (login), `Fetch →` (add URL).
2. `button.link.danger` — destructive actions, danger red. Used on
   `Delete this recipe` (recipe), `Clear` (status display).
3. `.iconbtn` — small square icon-only buttons in the topbar
   (Add / Status / Theme / Logout) and the page-nav strip.

There is no filled-paprika CTA. The "Add comment" filled-dark button
is the one exception — it's a sub-form action that wants a stronger
affordance than the link-style.

### Paste-from-clipboard helper

Square outline button next to text inputs that benefit from a one-tap
paste (login access code, add-URL field). Uses
`navigator.clipboard.readText()` and degrades silently when the
Clipboard API isn't available (insecure context, denied permission),
so the button can stay in the markup unconditionally.

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
- **Card backgrounds on the add / status / library rows.** Hairlines
  carry the separation.
- **System-font fallbacks for headings.** Fraunces is the design.
  Inter is the design. The mono is the design.
