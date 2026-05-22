# Fonts — "Sunday Kitchen"

Three open-licensed Google fonts. All served via a single Google Fonts
stylesheet, no JS, no self-hosting required.

## Display & Headings: Fraunces

[Fraunces](https://fonts.google.com/specimen/Fraunces) by Undercase Type
([SIL OFL](https://fonts.google.com/specimen/Fraunces/about)). A modern
display serif with optical-size and SOFT axes. We pull both `opsz` and
`SOFT` so big titles (`opsz: 144`, `SOFT: 50`) feel almost lettered and
playful, while smaller H2 labels (`opsz: 24`) stay sharp.

- Recipe `h1`: 600 weight, `opsz: 144`, `SOFT: 60` — friendly and warm
- Card title (recipe name in library): 600, `opsz: 36`
- Page section labels (`h2`): 600 small caps tracking
- Brand wordmark "ePepper": 700, `opsz: 144`, `SOFT: 80`

Why Fraunces: it carries personality without becoming a costume serif —
slightly chunky terminals, the curves feel hand-baked.

## Body, UI, Form: Inter

[Inter](https://fonts.google.com/specimen/Inter) by Rasmus Andersson
([SIL OFL](https://fonts.google.com/specimen/Inter/about)). Used for body
copy, buttons, captions, navigation. We use the variable axis at 400 / 500
/ 600 / 700 with `font-feature-settings: 'cv11', 'ss01'` for a slightly
warmer feel (single-storey "a" via `ss01` would be over-cute, so we leave
that off — `cv11` keeps the "l" disambiguated).

Why Inter: it's the quiet adult in the room, balances Fraunces, and stays
legible at 14px on a phone.

## Mono: JetBrains Mono

[JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono)
([SIL OFL](https://fonts.google.com/specimen/JetBrains+Mono/about)). Used
for the `API_KEY` hint on the login page and the few `<code>` chips on the
add/error pages.

## Single `<link>` for all three

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght,SOFT@9..144,400..700,30..100&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

(This same URL is embedded once at the top of `styles.css` via `@import`,
so HTML pages that only link the stylesheet still get the fonts.)
