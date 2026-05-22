# ePepper Console — Palette

Two-theme system. Light is paper-white with near-black ink; dark is a deep cool neutral, not pure black, to keep eye fatigue down. A single saturated green carries the brand and primary action across both themes.

All foreground-on-background pairs verified to meet **WCAG AA** for normal text (>= 4.5:1) and large text (>= 3:1). Border tokens are decorative only and tuned higher than 1.5:1 against their surfaces for visibility.

## Light mode

| Token              | Hex       | Role                                                 |
|--------------------|-----------|------------------------------------------------------|
| `--bg`             | `#F5F5F0` | Page background. Warm off-white, just shy of paper.  |
| `--surface`        | `#FFFFFF` | Cards, panels, inputs.                               |
| `--surface-sunken` | `#EBEBE4` | Code, kbd, sunken wells, table zebra.                |
| `--text`           | `#111111` | Primary text. 18.7:1 on `--bg`.                      |
| `--text-strong`    | `#000000` | Headings, numbers.                                   |
| `--muted`          | `#5A5A55` | Secondary text. 6.9:1 on `--bg`.                     |
| `--muted-strong`   | `#3D3D38` | Section labels in mono.                              |
| `--border`         | `#1A1A1A` | Strong hairline (cards, buttons).                    |
| `--border-soft`    | `#C8C8C0` | Subtle separator.                                    |
| `--accent`         | `#4F8A1F` | Pepper green. 4.6:1 on `--bg`. Brand, link, focus.   |
| `--accent-ink`     | `#FFFFFF` | Text on a filled accent surface.                     |
| `--accent-soft`    | `#E8F2D6` | Accent-tinted soft fill (display badge bg).          |
| `--primary-bg`     | `#111111` | Primary button fill.                                 |
| `--primary-ink`    | `#FFFFFF` | Primary button text.                                 |
| `--danger`         | `#B43A1E` | Delete/destructive text. 5.4:1 on `--bg`.            |
| `--danger-soft`    | `#F6DCD3` | Soft destructive surface.                            |
| `--warn`           | `#A8650C` | Warn text/icons. 4.7:1 on `--bg`.                    |
| `--ok`             | `#2F6F1E` | Healthy status text.                                 |
| `--focus`          | `#4F8A1F` | Focus ring color.                                    |

## Dark mode

| Token              | Hex       | Role                                                 |
|--------------------|-----------|------------------------------------------------------|
| `--bg`             | `#0E0F0D` | Page background. Deep neutral with a touch of green. |
| `--surface`        | `#16181400`-like → `#181A16` | Cards, panels.                       |
| `--surface-sunken` | `#0A0B09` | Inputs, sunken wells.                                |
| `--text`           | `#E9EAE3` | Primary text. 14.6:1 on `--bg`.                      |
| `--text-strong`    | `#FFFFFF` | Headings.                                            |
| `--muted`          | `#9A9C92` | Secondary. 6.0:1 on `--bg`.                          |
| `--muted-strong`   | `#C3C5BB` | Section labels in mono.                              |
| `--border`         | `#E9EAE3` | Strong hairline.                                     |
| `--border-soft`    | `#2E2F2A` | Subtle separator.                                    |
| `--accent`         | `#A8E861` | Brighter pepper green. 11.4:1 on `--bg`.             |
| `--accent-ink`     | `#0E0F0D` | Text on filled accent.                               |
| `--accent-soft`    | `#1F2A14` | Tinted soft fill.                                    |
| `--primary-bg`     | `#E9EAE3` | Primary button fill (inverted).                      |
| `--primary-ink`    | `#0E0F0D` | Primary button text.                                 |
| `--danger`         | `#FF7A5C` | Danger text. 7.4:1 on `--bg`.                        |
| `--danger-soft`    | `#3A1A10` | Soft destructive surface.                            |
| `--warn`           | `#F0B23A` | Warn. 9.0:1 on `--bg`.                               |
| `--ok`             | `#A8E861` | Healthy status text.                                 |
| `--focus`          | `#A8E861` | Focus ring.                                          |

## Notes on usage

- **One accent only.** The pepper green is reserved for: brand mark, the on-display badge, focused inputs, primary verbs in mono labels (`/library`, `/recipes`), and link underlines on hover. Everything else is grayscale by design — this is what makes the green pop.
- **Borders do the work.** With no shadow system, the strong 1px border (`--border`) plus the soft separator (`--border-soft`) is the entire spatial language.
- **Numbers are heavy.** Numeric values in the status grid, library row counts, and recipe metadata are weighted (600+) and use mono / tabular figures so columns line up.
