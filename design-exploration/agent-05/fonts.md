# ePepper Console — Typography

Three typefaces, three jobs. All freely licensed (SIL OFL) and hosted on Google Fonts.

## The pairing

### Display & headings — Space Grotesk
A geometric sans with a hint of personality in the `a`, `g`, and ampersand. Used for:
- Recipe titles (`h1`)
- Section headings (`h2`, `h3`)
- The brand wordmark
- Login / Add / Status page titles

Chosen because it reads as confident and modern at large sizes but never gets fussy — it carries a recipe title across a wide column without competing with the body.

### Body — Inter
The standard for screens. Used for:
- Ingredient lists
- Instruction steps
- Notes / comments
- Form labels and help text
- Library row titles

Optical sizing handled by Inter's `wght` variable axis. Body sits at 16px on desktop, 17px on mobile to ease one-handed reading in the kitchen.

### Mono & labels — JetBrains Mono
The developer-tool register. Used for:
- Nav labels (`/library`, `/add`, `/status`)
- Status metrics (`BATT 76%`, `135 CALLS`)
- `<code>` tags (e.g., `API_KEY`)
- Library row metadata (`COOKED 7x · LAST 4d`)
- The "page 1 / 2" indicator on the display preview
- Section eyebrow labels (`DISPLAY`, `LIBRARY`, `LLM`)

JetBrains Mono has good ligatures and a wide character set; it sets a serious tone without feeling stiff. Tabular figures throughout keep numbers aligned.

## Loading

Single Google Fonts link covers all three; weights pruned to what's used.

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap">
```

The `styles.css` includes a matching `@import` at the top, so the stylesheet works standalone if the `<link>` is omitted, but the `<link>` form is preferred for performance.

## Scale

A compressed type scale (1.2 ratio) keeps things dense:

| Token       | Size  | Use                                  |
|-------------|-------|--------------------------------------|
| `--fs-xs`   | 11px  | Micro labels (icon button captions)  |
| `--fs-sm`   | 13px  | Mono labels, badges, meta            |
| `--fs-base` | 15px  | Body                                 |
| `--fs-md`   | 17px  | Ingredient items, instruction steps  |
| `--fs-lg`   | 20px  | h2                                   |
| `--fs-xl`   | 28px  | h1                                   |
| `--fs-2xl`  | 40px  | Hero title (login)                   |

## Licenses

- **Inter** — SIL Open Font License 1.1 (Rasmus Andersson).
- **Space Grotesk** — SIL Open Font License 1.1 (Florian Karsten).
- **JetBrains Mono** — SIL Open Font License 1.1 (JetBrains).

All free for commercial and personal use, redistributable, no API key required from Google Fonts.
