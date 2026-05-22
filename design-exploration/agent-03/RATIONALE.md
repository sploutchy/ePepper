# ePepper — Brutalist Minimal

## Concept

A kitchen tool, not a magazine. ePepper is a private appliance for one cook: a self-hosted library that broadcasts to an e-ink screen sitting on a counter, near steam and grease. This redesign treats the website like the appliance it serves — structural, legible from across the room, indifferent to fashion. Recipes are documents, not lifestyle content.

## Mood

Raw, opinionated, high-contrast. Hairline rules instead of shadows. Boxes that read as boxes. Type that earns its weight. The interface has the same posture as a workshop wall: instructions pinned in place, labels in the corner, no decoration that isn't load-bearing.

## Intended user

The person who built the e-ink display in the first place — someone who values function, owns their data, and would rather read than scroll. Also: the same person standing in the kitchen with floured hands, who needs to find step 4 without squinting.

## Key decisions

**Typography.** *Space Grotesk* for display headlines (geometric but warm in the corners) paired with *Inter* for body (operational legibility at every size) and *JT Mono / JetBrains Mono* for metadata, labels, source tags, and step numbers. The mono is the brutalist tell — it makes counts, sources, and indices read like file metadata. UPPERCASE + tracked labels are used as structural markers (`INGREDIENTS`, `STEPS`, `NOTES`).

**Color.** Bone-paper light theme (`#F4F1EA`) and graphite dark theme (`#101010`). One signal color: a single hot vermillion (`#E33B1B`) used sparingly for the primary action, the "on display" indicator, and dangers (which are pushed to outline-only to keep the palette from screaming). Foreground is near-black / near-white. Borders are 1.5px solid — never grey-soft.

**Layout.** A generous 12-column grid on desktop (1180px max). Recipe pages use an asymmetric two-column split: ingredients pinned in a sticky sidebar (the reference column), steps in a wider reading column. The library is a numbered index — each row is "01 / TITLE / SOURCE / META" like a directory listing.

**Brutalism, not brutality.** Sharp edges, no shadows, no gradients — but generous line-height, real whitespace, and a single accent color keep it readable rather than aggressive.

A button looks like a button. A label looks stamped. A recipe looks like something you can cook from.
