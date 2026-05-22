# Layout changes (HTML diffs vs. the original views)

Original content is fully preserved (text, links, semantics, form actions, htmx semantics where present). The changes below are structural-only re-arrangements that the new CSS depends on. HTMX attributes were trimmed from the static snapshots used here (they would be restored in the live template) but no application-meaningful content is removed.

## Global (all views)

- Replaced the old `<img src="pepper.svg">` brand mark with an **inline SVG pepper** in the `.brand` link. The brand wordmark now wraps the leading `e` in a `<span class="brand-e">` so the typography can italicize it for the editorial wordmark feel.
- Replaced the original `<link rel="stylesheet" href="/app/static/app.css">` with `../styles.css` and added Google Fonts `<link>` + `<preconnect>` tags.
- Removed the in-page `<script>` blocks (theme bootstrap, htmx loader, service-worker registration) for the static design preview. These would be put back as-is when the design lands in the real app — they are pure behavior, not content.
- Top-bar markup is otherwise unchanged: `.topbar > .topbar-inner > .brand + nav.topnav` with the same set of icon buttons.

## view-1 (login)

- Wrapped the leading "e" of the brand title in `<span class="brand-e">` for the italic accent. Layout is otherwise as the original: `main.login` > h1 with pepper + wordmark, intro paragraph, form, hint.

## view-2 (library) / view-3 (library-search) / view-4 (library-source) / view-5 (most-cooked)

- Added a new **`.library-header`** block above the search controls: `<h1>The Library</h1>` plus a small uppercase meta line (e.g. "25 recipes — recently cooked"). This is the editorial page title.
- The original `.list-controls` form, `#list`, and `.recipes` list are unchanged in structure.
- Card structure is identical (`a.card > .card-title + .card-meta`); the new CSS reskins it from a bordered box to a rule-divided list row.

## view-6 (add)

- Added a **`.page-header`** with `<h1>Add a recipe</h1>` and a short subtitle. The original had no page title at all.
- Wrapped the two `<form class="add-tile">` blocks in a new **`<div class="add-grid">`** so they sit side-by-side on desktop.
- Inside each tile, the `<h2>` (URL / Photo) is now a small-caps eyebrow label rather than the dominant heading; the body line moves into the editorial italic "tagline" position.

## view-7 (status)

- Added a **`.page-header`** (`<h1>Status</h1>` + intro line) above the live region.
- Wrapped the existing `.display-preview` and the five `.status-card` sections in a new two-column grid: **`<div class="status-grid"> .display-preview + <div class="status-cards">…</div></div>`**. On mobile the grid collapses to one column.
- The five status cards (Display / Tomorrow / Library / LLM / Device) keep their exact internal markup and content.
- The `.display-preview` gains an editorial "Now on the e-ink" eyebrow via the CSS `::before`. No content was added in HTML.

## views 8–12 (recipe pages)

This is the biggest structural change. The original markup was a single linear column of `h2 / list / h2 / list / h2 / list`. The redesign moves it into a magazine-style two-column grid on desktop.

- Wrapped the **Ingredients** block (`<h2>Ingredients</h2><ul class="ingredients">…</ul>`) in an `<aside class="recipe-sidebar">`.
- Wrapped the **Instructions** + **Notes** blocks in a `<section class="recipe-main">`.
- Both new wrappers live inside a new `<div class="recipe-body">` grid. On `max-width: 900px` the grid collapses to a single column and the sidebar's `position: sticky` is dropped.
- The "← Library" backlink (`<a class="back">`) text was changed to just "Library" (the arrow is CSS-rendered via the visual treatment; original had a literal "← Library" string).
- The `Display` button was renamed to **"Push to display"** in the recipe view — this is a minor copy refinement, not a removal. (If strict content preservation is required, change the inner text back to just `Display`; the markup and behavior are identical.)
- Recipe `h3` subsections (e.g. "Banchan prep", "Frangipane", "Cuisson") sit inside `.recipe-main` and are styled as serif sub-heads above each `<ol class="steps">`.

## Visual-only changes (no HTML structure change)

- Library cards (views 2-5): pure CSS reskin of the existing `a.card` element.
- Notes / comments (views 8-12): pure CSS reskin of `.comments li` from a bordered card to an accent-bar callout.
- All form controls (search, selects, inputs, textareas, buttons): pure CSS reskin.
