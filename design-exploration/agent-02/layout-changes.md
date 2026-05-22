# Layout changes

All views link to `../styles.css` and use `<img src="../logo.svg">` for the
brand mark. Most views are CSS-only — listed here are the structural diffs.

## Global (all views)

- `<link rel="stylesheet" href="../styles.css">` replaces the original
  `/app/static/app.css`.
- Brand image `src` changed from `/app/static/pepper.svg` to `../logo.svg`.
- Theme toggle / service-worker scripts kept inline so behavior matches the
  original snapshot.

## view-1 (login)

- CSS-only. Added an explicit `<p class="muted">…</p>` wrapper around the
  intro text — already in source, no change to text.

## view-2, view-3, view-4, view-5 (library variants)

- CSS-only. Markup of `.list-controls`, `.recipes`, `.card`, `.card-badge`
  preserved. Card styling re-skinned via `::before` strip and new
  typography.

## view-6 (add)

- CSS-only. Same `.add-tile` blocks for URL and Photo.

## view-7 (status)

- Wrapped the existing `<div class="display-preview"><img ...></div>` into
  a `display-preview-frame` div so the polaroid-style frame can render with
  caption and slight rotation:
  ```html
  <div class="display-preview">
    <div class="display-preview-frame">
      <img src="/image?v=038713f0" alt="Current display">
    </div>
  </div>
  ```
  This is purely additive — no original markup removed.

## view-8 to view-12 (recipe detail)

Introduced a two-column reading layout by wrapping the existing structure
into `.recipe-header` (h1 + meta + actions) and `.recipe-body` (aside with
ingredients, main column with instructions + notes):

```html
<article class="recipe">
  <div class="recipe-header">
    <h1>…</h1>
    <p class="muted recipe-meta">…</p>
    <div class="actions">…</div>
  </div>
  <div class="recipe-body">
    <aside class="recipe-aside">
      <h2>Ingredients</h2>
      <ul class="ingredients">…</ul>
    </aside>
    <div class="recipe-main">
      <h2>Instructions</h2>
      …
      <h2>Notes</h2>
      <div id="comments">…</div>
    </div>
  </div>
  <div class="recipe-footer">…</div>
</article>
```

All `<h1>`, `<h2>`, `<h3>`, `<ul.ingredients>`, `<ol.steps>`, `<ul.comments>`,
`<form.add-comment>`, action buttons, delete button, and inline scripts are
preserved verbatim. Only their parent grouping changed.

On screens narrower than 820px the grid collapses to a single column so
mobile remains unchanged in flow.
