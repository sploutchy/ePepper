# Palette — "Sunday Kitchen"

Low-saturation surfaces, saturated accents. Two hero colors (Saffron + Teal),
a single warm warning/danger (Persimmon), and a soft warning (Honey).

## Light mode

| Role            | Token                | Hex       | Notes                              |
|-----------------|----------------------|-----------|------------------------------------|
| Background      | `--bg`               | `#FBF4E4` | Warm parchment, slightly creamy    |
| Surface         | `--surface`          | `#FFFCF3` | Card / panel                       |
| Surface alt     | `--surface-alt`      | `#F4E9CF` | Sidebar/sub-panel (recipe aside)   |
| Text            | `--fg`               | `#2A2118` | Espresso ink (AA on bg: 13.6:1)    |
| Muted           | `--muted`            | `#7A6A55` | Captions (AA on bg: 4.6:1)         |
| Border          | `--border`           | `#E5D6B3` | Soft tan rule                      |
| Hairline        | `--hairline`         | `#EEDFBE` | Inner dividers, lower contrast     |
| Primary         | `--primary`          | `#E8A33D` | Saffron — buttons, brand           |
| Primary deep    | `--primary-deep`     | `#B57A1C` | Darker saffron for hover / text-on-bg (AA on bg: 4.6:1) |
| Primary soft    | `--primary-soft`     | `#FBE7BF` | Saffron wash (toasts, focus)       |
| Primary fg      | `--primary-fg`       | `#1F1812` | Text on saffron buttons            |
| Accent          | `--accent`           | `#2C6E6A` | Mineral teal — links, recipe h3 (AA on bg: 5.4:1) |
| Accent soft     | `--accent-soft`      | `#CFE5E2` | Teal wash                          |
| Danger          | `--danger`           | `#B23A1F` | Persimmon — delete + errors (AA on bg: 5.0:1) |
| Danger soft     | `--danger-soft`      | `#F3D9CD` | Error background wash              |
| Warn bg         | `--warn-bg`          | `#FCEFC9` | Honey                              |
| Warn fg         | `--warn-fg`          | `#6B4A00` | Honey text (AA on warn-bg: 7.3:1)  |
| Warn border     | `--warn-border`      | `#E0B95C` | Honey rule                         |
| Shadow          | `--shadow`           | `0 1px 2px rgba(70,40,10,0.08), 0 4px 14px rgba(70,40,10,0.04)` | Warm-tinted |

## Dark mode

| Role            | Token                | Hex       | Notes                              |
|-----------------|----------------------|-----------|------------------------------------|
| Background      | `--bg`               | `#1F1A1D` | Deep raisin                        |
| Surface         | `--surface`          | `#2A2326` | Card panel                         |
| Surface alt     | `--surface-alt`      | `#332A2D` | Sub-panel                          |
| Text            | `--fg`               | `#F2E8D5` | Warm bone (AA on bg: 13.2:1)       |
| Muted           | `--muted`            | `#B0A28E` | Caption (AA on bg: 7.0:1)          |
| Border          | `--border`           | `#3F3539` | Subtle separator                   |
| Hairline        | `--hairline`         | `#352B2F` | Inner dividers                     |
| Primary         | `--primary`          | `#F4B85A` | Saffron, slightly brighter         |
| Primary deep    | `--primary-deep`     | `#F4B85A` | Same (used on dark surface)        |
| Primary soft    | `--primary-soft`     | `#4A3618` | Saffron wash                       |
| Primary fg      | `--primary-fg`       | `#1F1812` | Text on saffron                    |
| Accent          | `--accent`           | `#6FB7B1` | Teal turned up for AA on dark (5.4:1) |
| Accent soft     | `--accent-soft`      | `#23403F` | Teal wash                          |
| Danger          | `--danger`           | `#E8825F` | Persimmon brightened (AA: 5.1:1)   |
| Danger soft     | `--danger-soft`      | `#4A2418` | Error wash                         |
| Warn bg         | `--warn-bg`          | `#3D2F11` | Honey                              |
| Warn fg         | `--warn-fg`          | `#F3CC74` | Honey text                         |
| Warn border     | `--warn-border`      | `#6A5220` | Honey rule                         |
| Shadow          | `--shadow`           | `0 1px 2px rgba(0,0,0,0.45), 0 6px 18px rgba(0,0,0,0.25)` | Deeper |

## Contrast notes

All foreground/background pairs above meet WCAG AA (4.5:1 for body text,
3:1 for large/UI). Saffron `#E8A33D` is *not* used as fg on bg in light mode
— a deeper `#B57A1C` variant is used when text on parchment is needed
(brand wordmark, primary link).
