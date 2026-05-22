# ePepper — Typography

Two free, open-licensed families plus a metric mono. Loaded via Google Fonts.

## Display — Fraunces

Soft humanist serif with adjustable optical size and softness axes. Curves
are warm and a touch oily, which fits the cookbook-on-a-shelf mood without
veering into "wedding invitation". Used for: page titles (`h1`), recipe
titles, section labels (`h2`), and the brand wordmark.

- Weights used: **500, 600, 700**
- Axes used: optical size (auto, via `font-optical-sizing: auto`), softness
  (`SOFT`) nudged toward 50 for a more rounded feel
- License: SIL Open Font License 1.1

## Body & UI — Nunito

Rounded humanist sans with a tall x-height. Reads gently at body sizes and
holds up clearly on dark backgrounds. Used for: paragraphs, list items, form
inputs, navigation labels, captions.

- Weights used: **400, 500, 600, 700**
- License: SIL Open Font License 1.1

## Mono — JetBrains Mono

Used sparingly: status metrics (battery, temperature, signal), the access
code label, anything that reads as data. Picked because its lowercase has
slight roundness so it doesn't clash with Nunito's softness.

- Weights used: **400, 600**
- License: SIL Open Font License 1.1

## Load URL

The HTML `<head>` of every view links the following stylesheet:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link
  href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,500..700,50&family=Nunito:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap"
  rel="stylesheet">
```

The same import is also present at the top of `styles.css` as a fallback so
the stylesheet works standalone if someone forgets the `<link>`.
