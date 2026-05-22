# Typography

Three families, all freely licensed and served from Google Fonts.

| Family          | Role                                                     | Weights used   |
|-----------------|----------------------------------------------------------|----------------|
| Space Grotesk   | Display: H1 (recipe titles, login title, page indices)   | 500, 700       |
| Inter           | Body, navigation, form controls                          | 400, 500, 600  |
| JetBrains Mono  | Labels, metadata, source tags, step numbers, status data | 400, 500, 700  |

## Rationale

**Space Grotesk** as the display face. Geometric forms with slight humanist curves — sharp enough to read as brutalist (rectangular `a`, geometric `g`) but never cold. It tracks tight at large sizes, which is what we want for an H1 that needs to feel like a stencil or a sign.

**Inter** for everything operational. The body face. Inter's near-perfect optical sizing at every weight + true monoline metrics keep recipe steps and form labels readable in any kitchen-light situation. It's the workhorse.

**JetBrains Mono** as the brutalist tell. Used for source tags (`FROM SERIOUSEATS`), step indices (`01 02 03`), library row counters, status numbers (`76%`, `-58 dBm`), file-like metadata. Mono in a recipe app evokes the receipt printer, the kitchen ticket, the workshop part-number — exactly the appliance posture we want.

Numbers everywhere use tabular figures (`font-variant-numeric: tabular-nums`) so counts and pages don't shimmer when they update.

## Google Fonts URL

Single request, two faces (Mono is loaded as a third subset on the same connection):

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
```

This URL is also `@import`-ed at the top of `styles.css`, so HTML pages that link `styles.css` alone will still resolve the fonts. The explicit `<link>` is included in each view's `<head>` for fast first paint.

## Scale

A compact modular scale based on 16px / 1.25:

| Token        | px  | rem   | Used for                          |
|--------------|-----|-------|-----------------------------------|
| `--fs-xs`    | 11  | 0.6875| Mono labels, badges               |
| `--fs-sm`    | 13  | 0.8125| Meta, captions                    |
| `--fs-base`  | 16  | 1     | Body                              |
| `--fs-md`    | 18  | 1.125 | Steps, lead paragraphs            |
| `--fs-lg`    | 22  | 1.375 | H2                                |
| `--fs-xl`    | 32  | 2     | H1 mobile                         |
| `--fs-2xl`   | 48  | 3     | H1 desktop                        |
| `--fs-3xl`   | 64  | 4     | Login title                       |

Line-heights skew long: body at 1.6, steps at 1.55, headlines at 1.05.
