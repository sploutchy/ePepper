# Typefaces

Three families, all on Google Fonts under the Open Font License.

## Fraunces — display & recipe titles
A contemporary "old-style with attitude" serif by Undercase Type. Variable axes (weight, optical size, SOFT) give it real character at huge sizes while staying readable at h2 sizes. We use the SOFT axis at 50 for slightly rounded terminals — a touch of warmth that keeps the design from feeling clinical.

- Roles: `h1`, recipe titles, brand wordmark, large numerals on instruction steps.
- Weights used: 400 (italic for brand), 600 (h2), 700 (h1/title).
- Optical size: 96 for h1, 36 for h2, 18 for small heads.

## Inter — UI and body
The workhorse. Inter is purpose-built for screens and is unbeatable for small UI text, form controls, and meta. Variable font, hinted, extremely consistent at all sizes. Used at 16–17px for body, 13–14px tracked uppercase for labels.

- Roles: paragraphs, lists, form labels, buttons, navigation, captions.
- Weights used: 400 (body), 500 (UI), 600 (labels), 700 (emphasis).

## JetBrains Mono — code only
Limited to one place: the `<code>API_KEY</code>` mention on the login hint. Carries the "this is technical" signal without polluting the rest of the UI.

- Roles: `<code>`.
- Weights used: 500.

## Loading

The CSS imports the Google Fonts CSS at the top of `styles.css`. The HTML <head> also includes `<link rel="preconnect">` to `fonts.googleapis.com` and `fonts.gstatic.com` for faster first paint. No font self-hosting; no other runtime dependencies.

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght,SOFT@0,9..144,400..700,50;1,9..144,400..700,50&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
```

The same URL is `@import`-ed at the top of `styles.css` so the stylesheet works even if the `<link>` is omitted.
