# HTML changes per view

The redesign keeps every piece of content (titles, sources, links, ingredients, instructions, notes, delete actions, hx-* attributes, etc.). Structural changes are the minimum needed to wire up the new visual language. Per-view notes below.

## Global / shared across all views

- Added `<link>` to Google Fonts (Inter, JetBrains Mono, Space Grotesk).
- All views now reference `../styles.css` and `../logo.svg` instead of `/app/static/app.css` and `/app/static/pepper.svg`.
- Stripped the htmx CDN script tag and inline service-worker registration — both are runtime-only and would 404 in the local screenshot harness. The `hx-*` attributes on elements were preserved (they're inert without the script).
- Removed the inline `htmx-config` meta and the inline `.htmx-indicator` style block — handled by the stylesheet.
- The shared topbar gains two new spans (`.brand-divider` + `.brand-route`) showing the current "path" in mono after the brand mark — purely visual; hidden on small mobile via media query.
- The login page wraps its content in `.login` (already existed) but the wrapper now has a `::before` pseudo-element rendering the `/login` route label.

## View-1 (login)
CSS only — markup unchanged structurally.

## View-2 (library) — minor restructure
- The flat `<ul class="recipes">` plus its parent `<div id="list">` are now wrapped in a `<div class="list-shell">` and preceded by a `<div class="list-header">` row of column headings (ID / Title / Source / Last cooked). The header row is hidden via `display: none` on mobile.
- Each `<li> > <a class="card">` was restructured from `card-title` + `card-meta` blocks into 5 grid cells: `card-id`, `card-title`, `card-source`, `card-meta`, `card-chev`. The source moved out of the title cell and into its own column.
- Added a `.page-head` block with the eyebrow + `<h1>Library</h1>` and a `.list-summary` mono line above the list.

## View-3 (search)
Same restructure as view-2 plus reflects the `q=pasta` filter via the search input value and the route label in the topbar.

## View-4 (filtered source)
Same restructure as view-2 plus `source=fooby` selected and route label.

## View-5 (most-cooked)
Same restructure as view-2 plus `sort=most_cooked` selected and route label.

## View-6 (add) — restructured
- The two `<form class="add-tile">` blocks are now wrapped in a `<div class="add-grid">` to display them side-by-side on desktop (collapsing to one column on mobile).
- Added a `.page-head` block above the forms.
- Removed the noisy spinner emoji ("🤖") in the LLM-stage label; kept all the data-attributes that drive the stage flip.

## View-7 (status) — restructured
- The entire status body is wrapped in a `<div class="status-grid">` (two-column on desktop).
- The `.display-preview` and the `Device` card span the full width via `grid-column: 1 / -1` (the latter uses `class="status-card full"`).
- Added a `.page-head` block above the grid.

## View-8 (recipe — Carbonara) — restructured
- The flat sequence of `<h2>` Ingredients + `<h2>` Instructions + `<h2>` Notes was reorganized into a `<div class="recipe-body">` with two children: `<aside class="ingredients-col">` (ingredients only) and `<section class="instructions-col">` (instructions, notes, comments form). On mobile this collapses to a single column.
- Each `<h2>` was given a `data-label` attribute (e.g. `data-label="8 items"`) used by CSS `::after` to print a mono caption to the right of the heading.
- Added a `<span class="eyebrow">Recipe · IT</span>` above the `<h1>` (language tag inferred from content — purely decorative).
- The hard-delete shift-click script is preserved verbatim.

## View-9 (recipe with sections — Bibimbap) — restructured
Same as view-8. The `<h3>` subheadings for "Banchan prep" / "Bowls" sit inside the instructions column and are styled as small mono eyebrows.

## View-10 (recipe FR) — restructured
Same as view-8. Three subsection `<h3>`s (Frangipane / Montage / Cuisson) preserved. `<html lang="fr">` set.

## View-11 (recipe IT) — restructured
Same as view-8. `<html lang="it">` set. The source `Nonna` remains as plain text (no link in source data).

## View-12 (recipe never cooked) — restructured
Same as view-8. "No notes yet." `<li class="muted">` preserved verbatim. Eyebrow shows `Recipe · IT · NEW` reflecting the "never cooked" state.

## Summary
Markup deltas are minor and additive. Every original content node is still present and addressable. The two-column recipe layout, two-column add grid, and status grid are the most material structural changes; everything else is class-naming and small wrapper additions.
