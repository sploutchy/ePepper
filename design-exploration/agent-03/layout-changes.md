# Layout changes

All views preserve the original content verbatim. The structural diffs from the
original HTML are listed per view. Anything not mentioned is purely a CSS change.

## Global

- `<head>`: linked Google Fonts (Space Grotesk + Inter + JetBrains Mono) and the
  new `../styles.css`. Removed legacy `app.css` link.
- `<header class="topbar">`: kept the same DOM, kept the SVG icons. The brand
  block now uses the new inline `logo.svg` (`<img src="../logo.svg">`).
- Static script/htmx references stripped (these are static snapshots); the
  `toggleTheme` script kept inline so light/dark toggling still works on the
  static page.

## view-1 (login)

- Wrapped `<input>` and `<button>` in a `<form>` exactly like the original.
- The top corner code `001 / ACCESS` is a CSS `::before` — no DOM change.

## view-2 (library), view-3 (library-search), view-4 (library-source), view-5 (library-most-cooked)

- Added a `<div class="list-header">` block above `.list-controls` with the page
  title (`LIBRARY` / `SEARCH RESULTS` / `BY SOURCE` / `MOST COOKED`) and a
  meta-line on the right ("25 recipes" etc.). This is purely additive markup
  giving the brutalist label-and-context posture.
- Recipe list rows are unchanged. The numeric prefix `01`, `02`, ... is
  generated via CSS counters — no DOM change to `.card`.

## view-6 (add)

- Wrapped the two `<form class="add-tile">` blocks in a `<section class="add-grid">`
  for a two-column desktop layout (URL + Photo side by side). Each tile gains a
  `data-num` attribute (`002 / URL`, `003 / PHOTO`) consumed by the CSS pseudo-
  element label. Added an `<h1>` + `<p>` page header.
- Inner form structure unchanged.

## view-7 (status)

- Wrapped `#status-body` contents in `<div class="status-grid">` with two
  columns. The display preview lives in the left column, the cards in the
  right. Added a page header (`STATUS / E-INK DISPLAY`).
- `display-preview` gains a corner label `E-INK / LIVE` via CSS pseudo-element.
- Card markup is unchanged.

## view-8, view-9, view-10, view-11, view-12 (recipe detail)

- Re-grouped the existing nodes into a brutalist two-column layout without
  losing any content:
  - `<header class="recipe-head">` wraps the title + meta + actions.
  - `<div class="recipe-body">` is the new two-column grid:
    - `<aside class="recipe-aside">` holds Ingredients (sticky on desktop).
    - `<section class="recipe-main">` holds Instructions + Notes + Footer.
- The page meta line is split into a flex row of mono labels (FROM / SAVED /
  COOKED) for the brutalist look. All original text values are preserved.
- The "Notes" section keeps the same `#comments` container, `.comments` list,
  and `<form class="add-comment">` form (DOM identical).

## Logo

The original was `pepper.svg`. Replaced with `logo.svg` — a brutalist pepper
mark using `currentColor` (so it inherits the accent color in the brand block
and the surface color in the login hero).
