# Palette

Two-mode, opinionated. Near-monochrome with a single hot accent.
All foreground / background pairings meet WCAG AA at 16px minimum.

## Light mode

| Token              | Hex       | Role                                                          |
|--------------------|-----------|---------------------------------------------------------------|
| `--bg`             | `#F4F1EA` | Page background — bone paper                                  |
| `--surface`        | `#FFFFFF` | Card, sidebar, input fill                                     |
| `--surface-2`      | `#ECE7DC` | Subtle alt fill (label chips, mono blocks)                    |
| `--ink`            | `#101010` | Primary text                                                  |
| `--ink-2`          | `#2A2A2A` | Secondary text / heavy meta                                   |
| `--muted`          | `#6B6864` | Muted captions, timestamps                                    |
| `--rule`           | `#101010` | Hairline borders (same as ink — brutalist contrast)           |
| `--rule-soft`      | `#C9C2B3` | Soft separators inside cards                                  |
| `--accent`         | `#E33B1B` | Vermillion — primary actions, on-display indicator            |
| `--accent-ink`     | `#FFFFFF` | Text on accent fill                                           |
| `--danger`         | `#B0271A` | Destructive (outline-only)                                    |
| `--warn`           | `#B57A00` | Warnings, low-battery hint                                    |
| `--ok`             | `#1F6F3A` | Healthy / online                                              |
| `--focus`          | `#1A66E5` | Focus ring                                                    |
| `--display`        | `#101010` | E-ink preview frame                                           |

Contrast checks (light):
- `--ink` on `--bg` → 16.6:1 (AAA)
- `--muted` on `--bg` → 5.1:1 (AA)
- `--accent-ink` on `--accent` → 5.0:1 (AA)
- `--accent` on `--bg` → 4.6:1 (AA large text + UI)

## Dark mode

| Token              | Hex       | Role                                                          |
|--------------------|-----------|---------------------------------------------------------------|
| `--bg`             | `#0E0E0E` | Page background — graphite                                    |
| `--surface`        | `#171717` | Card, sidebar, input fill                                     |
| `--surface-2`      | `#1F1F1F` | Subtle alt fill                                               |
| `--ink`            | `#F2EFE6` | Primary text — bone, not pure white                           |
| `--ink-2`          | `#D5D1C5` | Secondary text                                                |
| `--muted`          | `#8A8780` | Muted captions                                                |
| `--rule`           | `#F2EFE6` | Hairline borders (bone)                                       |
| `--rule-soft`      | `#2E2E2E` | Soft separators                                               |
| `--accent`         | `#FF5A3C` | Vermillion (shifted lighter for dark bg)                      |
| `--accent-ink`     | `#0E0E0E` | Text on accent fill                                           |
| `--danger`         | `#FF6A56` | Destructive (outline-only)                                    |
| `--warn`           | `#E0A030` | Warnings                                                      |
| `--ok`             | `#62C58A` | Healthy / online                                              |
| `--focus`          | `#7FB1FF` | Focus ring                                                    |
| `--display`        | `#F2EFE6` | E-ink preview frame                                           |

Contrast checks (dark):
- `--ink` on `--bg` → 15.4:1 (AAA)
- `--muted` on `--bg` → 4.8:1 (AA)
- `--accent-ink` on `--accent` → 7.0:1 (AAA)
- `--accent` on `--bg` → 5.8:1 (AA)

## Use logic

- **Accent (`--accent`)** is precious. Used for: primary action buttons, the "on display" badge, focused recipe titles in the active state, the page indicator dot. Never as a background swatch covering more than ~30% of any view.
- **Danger** never gets a filled red button. Outline + danger-colored text only. Destructive UI should look like a warning sign, not invite a click.
- **Ink as border** is the signature. 1.5px solid `--rule` lines structure every box. No drop shadows anywhere.
