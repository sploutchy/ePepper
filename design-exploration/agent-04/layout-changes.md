# Layout changes (HTML diff)

The redesign is overwhelmingly CSS-only. The only structural changes are
wrappers added to recipe pages so a two-column "ingredients ↔ instructions"
desktop layout can be expressed with clean grid areas, plus a small wrapper
around the recipe meta line.

## Global (every view)
- Replaced `<link rel="stylesheet" href="/app/static/app.css">` with
  `<link rel="stylesheet" href="../styles.css">`.
- Added Google Fonts preconnect + stylesheet link in `<head>`.
- Replaced `<img src="/app/static/pepper.svg">` with `<img src="../logo.svg">`.
- Local `<script>` blocks (`toggleTheme`, service worker registration, delete
  confirm) are preserved verbatim.

## Login (view-1)
- Wrapped pepper image + wordmark in the existing `<h1>`. No structural change.

## Library views (views 2–5)
- No structural changes. CSS-only.

## Add (view-6)
- No structural changes. CSS-only.

## Status (view-7)
- No structural changes. The `<img>` inside `.display-preview` retains its
  broken `/image?v=...` link; the CSS gives the container a min-height so a
  failed image still renders as a soft frame.

## Recipe detail (views 8–12)
- Added two structural wrappers inside `<article class="recipe">`:
  - `<section class="rcp-ing">` wraps the `<h2>Ingredients</h2>` heading
    plus the `<ul class="ingredients">…</ul>`.
  - `<section class="rcp-ins">` wraps the `<h2>Instructions</h2>` heading
    plus all `<h3>` + `<ol class="steps">` blocks (for sectioned recipes
    like Bibimbap, Galette des Rois — each `h3`/`ol` pair remains inside
    the same section).
- The `<h2>Notes</h2>` heading and `#comments` div remain top-level children
  of `.recipe` so they span the full width below the two columns.
- This wrapping is purely structural — no text, semantics, or links change.
  It allows the recipe to render as **sidebar (ingredients) + main column
  (instructions)** on desktop and stack to a single column on mobile.
