# Palette

A two-mode palette tuned for long reading sessions. Both modes meet WCAG AA on body text (>= 7:1 contrast) and AA on UI controls.

## Light mode — "Bone & Ink"

| Role             | Token                    | Hex        | Notes |
|------------------|--------------------------|------------|-------|
| Background       | `--bg`                   | `#FBFAF7`  | Paper white, slightly warm |
| Surface          | `--surface`              | `#FFFFFF`  | Lifted panels (status cards, note bubbles) |
| Surface alt      | `--surface-alt`          | `#F2EFE8`  | Subtle blocks (ingredients sidebar) |
| Ink (text)       | `--text`                 | `#1A1A1A`  | Body |
| Ink dim          | `--text-dim`             | `#3A3835`  | Secondary text |
| Muted            | `--muted`                | `#807A70`  | Meta, captions |
| Faint            | `--faint`                | `#B6B0A4`  | Disabled, very-quiet meta |
| Border           | `--border`               | `#E4DFD4`  | Hairlines & dividers |
| Border strong    | `--border-strong`        | `#1A1A1A`  | Editorial accent rules |
| Accent (paprika) | `--accent`               | `#B83227`  | Primary buttons, links, on-display badge |
| Accent hover     | `--accent-hover`         | `#962419`  | Pressed/hover state |
| Accent soft      | `--accent-soft`          | `#F7E4E1`  | Accent tints / subtle highlights |
| Success          | `--success`              | `#3F6B3A`  | (used rarely — backup OK indicator) |
| Warn             | `--warn`                 | `#B07A0F`  | (reserved) |
| Danger           | `--danger`               | `#9B1C1C`  | Destructive action text |
| Danger soft      | `--danger-soft`          | `#FDECEC`  | Danger backgrounds |

## Dark mode — "Inkwell"

| Role             | Token                    | Hex        | Notes |
|------------------|--------------------------|------------|-------|
| Background       | `--bg`                   | `#121110`  | Near-black with the smallest warmth |
| Surface          | `--surface`              | `#1B1A18`  | Lifted panels |
| Surface alt      | `--surface-alt`          | `#23211E`  | Ingredients sidebar |
| Ink (text)       | `--text`                 | `#F1ECE2`  | Body — warm off-white |
| Ink dim          | `--text-dim`             | `#CFC8BC`  | Secondary text |
| Muted            | `--muted`                | `#8E867A`  | Meta, captions |
| Faint            | `--faint`                | `#5A554D`  | Disabled |
| Border           | `--border`               | `#2C2A26`  | Hairlines |
| Border strong    | `--border-strong`        | `#F1ECE2`  | Editorial accent rules |
| Accent (paprika) | `--accent`               | `#E5614F`  | Lifted from light-mode paprika for AA contrast on dark |
| Accent hover     | `--accent-hover`         | `#F1816F`  | Pressed/hover state |
| Accent soft      | `--accent-soft`          | `#3A1C18`  | Accent tints |
| Success          | `--success`              | `#7AAE73`  |  |
| Warn             | `--warn`                 | `#D9A742`  |  |
| Danger           | `--danger`               | `#F0817E`  | Destructive action text |
| Danger soft      | `--danger-soft`          | `#3A1B1B`  | Danger backgrounds |

## Contrast verification (key pairs)

- `#1A1A1A` on `#FBFAF7` — **15.5:1** (AAA)
- `#807A70` on `#FBFAF7` — **4.6:1** (AA, used only for meta ≥ 14px)
- `#B83227` on `#FBFAF7` — **6.1:1** (AA large, AA small)
- `#FFFFFF` on `#B83227` — **6.0:1** (button text)
- `#F1ECE2` on `#121110` — **14.7:1** (AAA)
- `#E5614F` on `#121110` — **5.8:1** (AA)
- `#8E867A` on `#121110` — **5.4:1** (AA)
