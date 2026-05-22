# ePepper — Soft & Organic Direction

## Concept

ePepper lives on a kitchen counter, next to a stack of cookbooks and a pot of
basil. The redesign leans into that domestic, tactile world: **rounded edges,
herb-garden palette, a hand-pulled feel without sacrificing legibility**. The
guiding metaphor is a sun-bleached recipe card pinned to a cork board — soft,
slightly imperfect, lived-in.

## Mood

Calm, unhurried, generous with whitespace. Nothing here should feel like an
"app dashboard". The shapes are pillowy (16–28px corner radii everywhere,
including inside forms and buttons), shadows are diffused rather than crisp,
borders are warm and low-contrast. Even the e-ink preview gets a soft frame
so it sits inside the world rather than fighting it.

## Intended user

A home cook with one tablet on the counter, a phone in their pocket, and a
modest library of recipes they actually return to. The interface should
disappear into the recipe and reappear with confidence when needed (push to
display, add a note). Reading must feel as comfortable as a paperback.

## Key decisions

- **Typography pairing.** *Fraunces* for display — a soft humanist serif with
  optical sizing, slightly oily curves, perfect for recipe titles in any of
  the three site languages. *Nunito* for UI and body — high x-height, rounded
  terminals, excellent at small sizes on dark mode. *JetBrains Mono* for the
  occasional metric (`-58 dBm`, `21.4 °C`) so it doesn't get mistaken for prose.
- **Color logic.** A "garden" palette. Light mode is oat paper (`#f6f1e6`)
  with moss-green primary and terracotta danger. Dark mode is forest floor
  (`#1c211e`) lifted by sage accents. Every accent has a paired soft surface
  tint so badges, callouts, and the display preview live on the page instead
  of stamped on top of it.
- **Layout philosophy.** Recipe pages adopt a generous two-column layout on
  desktop (ingredients pinned in a sage sidebar, instructions flowing
  beside), but collapse to a single warm column on mobile. The library uses
  card rows with a soft baseline grid and a left-edge accent stripe that
  brightens on hover — a subtle "tag" that anchors each recipe without
  needing imagery. The status page treats each section as a rounded "tile"
  with a tinted icon medallion, giving it the feel of a kitchen pegboard.
- **Logo.** A stylized chili-leaf hybrid: an organic teardrop pepper with a
  curled leaf on top, drawn with two strokes instead of an outline so it
  reads at favicon scale and re-tints automatically in dark mode.

The result should feel distinctly *not-tech* — closer to a cookbook publisher
than a SaaS — while remaining fully accessible (4.5:1+ in both themes) and
quick on a phone in a busy kitchen.
