# ePepper — Lean Editorial

## Concept
ePepper is being treated as a personal cooking journal — the kind of slim, well-set monthly magazine you'd keep on a kitchen shelf. The redesign throws out the soft cream-card UI and instead borrows from print: a strict typographic hierarchy, generous margins, hairline rules, small-caps section labels, and a single warm accent (a deep paprika red) used sparingly enough that it actually means something when it appears.

## Mood
Quiet, confident, a little austere. Closer to *Cherry Bombe*, *Apartment Folio*, or a Phaidon cookbook than to a typical SaaS dashboard. The page is mostly white space; the type does the heavy lifting.

## Intended user
A home cook who has spent real money on an e-ink kitchen display and wants the companion app to feel like a thing — not a tool. Someone who reads recipes the way other people read essays.

## Key decisions

**Typography pairing.** *Fraunces* (a contemporary "old-style with attitude" serif from Google Fonts) carries every display heading and recipe title — generous optical sizes, gentle SOFT axis, slightly tighter tracking on h1s for impact. *Inter* handles UI text and body copy at 16–17px with comfortable line-height. *JetBrains Mono* shows up only for the access code on login. Section labels (INGREDIENTS, INSTRUCTIONS, NOTES) are set in upper-case tracked Inter, never in serif — that contrast is the design.

**Color logic.** Light mode is bone-white paper (`#FBFAF7`) with deep ink text (`#1A1A1A`) and a single accent — *Paprika* `#B83227` for primary actions, the on-display badge, and decorative rules. Dark mode flips to a near-black ink (`#121110`) with warm off-white text, the same paprika slightly lifted for AA contrast. There is no green. The original "warm forest" palette is replaced because green pulls toward "garden/wellness" while paprika pulls toward "cooking" — better fit for the product.

**Layout philosophy.** A wider `1100px` canvas than the original 760px, but with content held to a `680px` reading column for prose. Recipe pages use an editorial two-column grid on desktop: ingredients sit in a narrow sidebar to the left, indented under a hairline rule, while instructions flow in the main column with oversized numerals. The library is a vertical index — no cards, just a list with rule dividers, the recipe title in serif and the source/meta in tracked small-caps. Mobile collapses everything to single-column without losing the typographic hierarchy.

**Identity.** New logo wordmark sets "ePepper" in Fraunces italic with a tiny inked pepper stem as the dot of a custom monogram — works at favicon size and on letterhead.
