# ePepper — Palette

Garden-inspired warm neutrals + a sage / moss accent family. Every accent has
both a saturated and a tint variant so it works for text, fills, and surfaces.

## Light mode

| Role | Hex | Notes |
| --- | --- | --- |
| `--bg` | `#f6f1e6` | Oat paper — base canvas |
| `--bg-elev` | `#fbf7ee` | Slightly lifted, for the page beneath cards |
| `--surface` | `#ffffff` | Card / form surface |
| `--surface-tint` | `#efe7d4` | Soft tinted surface for callouts |
| `--text` | `#2b2a26` | Bark — primary copy, 13.4:1 on `--bg` |
| `--text-soft` | `#4a4639` | Body softening |
| `--muted` | `#7a7261` | Caption / metadata, 4.6:1 on `--bg` |
| `--border` | `#e3d8c1` | Hairline warm border |
| `--border-strong` | `#cbbe9f` | Hover / focus border |
| `--primary` | `#3f6b46` | Moss-green — primary action |
| `--primary-ink` | `#2c4a31` | Primary text on tinted surfaces, 7.3:1 |
| `--primary-tint` | `#dbe7d7` | Soft pillow under primary glyphs |
| `--accent` | `#a06848` | Terracotta — secondary accent / source labels |
| `--accent-tint` | `#f1e1d4` | Tag / pill background |
| `--danger` | `#a83a2c` | Persimmon — destructive |
| `--danger-tint` | `#f5dcd5` | Banner / hover wash |
| `--warn` | `#b58219` | Honey amber — warnings |
| `--warn-tint` | `#f5e6c4` | Highlight wash |
| `--success` | `#3f6b46` | Same family as primary |
| `--shadow` | `rgba(74, 60, 35, 0.10)` | Soft umber shadow |

Background → text contrast: `#2b2a26` on `#f6f1e6` = **13.4:1** ✅
Muted contrast: `#7a7261` on `#f6f1e6` = **4.6:1** ✅
Primary on bg: `#3f6b46` on `#f6f1e6` = **5.6:1** ✅
Primary button text white on `#3f6b46` = **6.0:1** ✅
Danger: `#a83a2c` on `#f6f1e6` = **5.3:1** ✅

## Dark mode

| Role | Hex | Notes |
| --- | --- | --- |
| `--bg` | `#1c211e` | Forest floor |
| `--bg-elev` | `#222824` | Slight lift |
| `--surface` | `#2a312c` | Cards |
| `--surface-tint` | `#323a35` | Tinted callouts |
| `--text` | `#ece4d2` | Warm cream — primary copy, 13.0:1 on `--bg` |
| `--text-soft` | `#d4cbb6` | Body softening |
| `--muted` | `#9aa097` | Caption, 6.0:1 on `--bg` |
| `--border` | `#3a4239` | Hairline |
| `--border-strong` | `#4f5b4f` | Hover/focus |
| `--primary` | `#a8c89a` | Sage — primary action |
| `--primary-ink` | `#cfe2c2` | Primary text on tint, 9.7:1 |
| `--primary-tint` | `#33402f` | Pillow under primary glyphs |
| `--accent` | `#d99c75` | Soft clay |
| `--accent-tint` | `#3f322a` | Tag background |
| `--danger` | `#e6927e` | Soft persimmon, 5.9:1 |
| `--danger-tint` | `#3d2620` | Banner wash |
| `--warn` | `#e3c171` | Honey |
| `--warn-tint` | `#3a311c` | Wash |
| `--success` | `#a8c89a` | |
| `--shadow` | `rgba(0, 0, 0, 0.35)` | Deeper for dark backgrounds |

Background → text: `#ece4d2` on `#1c211e` = **13.0:1** ✅
Muted: `#9aa097` on `#1c211e` = **6.0:1** ✅
Primary: `#a8c89a` on `#1c211e` = **8.6:1** ✅
Primary button bg `#a8c89a` with `#1c211e` ink = **8.6:1** ✅
Danger: `#e6927e` on `#1c211e` = **5.9:1** ✅
