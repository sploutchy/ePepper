# ePepper — Console

## Concept
A redesign that treats ePepper like a piece of self-hosted infrastructure rather than a soft, warm cookbook. The recipe library is a **database**, the e-ink display is a **device**, the LLM is a **service**. The interface should feel like a small terminal-adjacent control panel: dense, precise, monospaced where it counts, generous where it counts (recipe reading), and unflinching about contrast.

Think: Linear's command palette meets a Raspberry Pi admin page meets a well-typeset recipe card.

## Mood
- **Precise** — strict 8px grid, monospaced labels, sharp 4px corner radius, no shadows except a single hairline.
- **High contrast** — near-black on near-white in light mode; near-white on a deep neutral in dark mode. Single accent: a saturated lime-pepper green that lights up on dark.
- **Dense but breathable** — chunks of info live behind clear typographic hierarchy and a hairline border system, not behind boxes-within-boxes.

## Intended user
A confident cook who is also the sysadmin of their own kitchen. They want to glance at "what's on the device?", search 25 recipes fast, push one to the display, and read it. They appreciate seeing `BATT 0.76` next to `135 calls / CHF 0.07`.

## Key decisions

**Typography pairing.** Three roles:
- **Display & headings**: *Space Grotesk* — geometric sans with engineered character, modern but warm enough for a recipe title.
- **Body**: *Inter* — neutral, screen-optimized at small sizes, excellent for ingredient lists.
- **Mono/labels**: *JetBrains Mono* — for nav labels, badges, metrics, ID-like data (`COOKED 7x`, `LAST 4d`, `BATT 76%`). Establishes the developer-tool register.

**Color logic.** Two layers per theme: `bg` (page) and `surface` (cards). Single accent (`#7CC23B` light / `#9CE655` dark) reserved for: brand mark, primary CTA, in-focus state, the "on display" badge. Danger is a clear red-orange. Borders carry the visual structure — there are a lot of them — so they are tuned for AA contrast against `surface` (2.4:1+ for non-text decoration, but kept dark enough to feel architectural).

**Layout philosophy.**
- Sticky top bar with a left rail mark, monospaced section label (`/library`, `/recipes/1`), and right-aligned controls.
- Recipe pages use a two-column desktop layout (ingredients sidebar, instructions main). Mobile collapses to one stream.
- Library is a flat dense table (one row per recipe) rather than puffy cards — more recipes visible at a glance.
- Status page is a grid of mono-labeled "stat tiles" plus the device preview.
- Numbers are tabular. Labels are uppercase mono. Recipe prose is generous, serif-adjacent sans, readable from across the kitchen.
