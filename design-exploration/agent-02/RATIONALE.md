# ePepper — "Sunday Kitchen" redesign

## Concept

ePepper is a private recipe library that quietly pushes the next thing you'll
cook to a paper-feeling e-ink display in the kitchen. The current design is
careful and tasteful (cream, forest green, system font) but it reads as a
"tool". This redesign leans into the personal, slightly imperfect nature of
the project — like a recipe box your grandmother kept, but with the warmth
turned up.

The mood is **Sunday Kitchen**: late-morning light through a window, a
saffron-yellow tea towel, a stub of pencil in the margin. Playful, warm,
hand-crafted; never twee.

## Intended user

The single owner of a small home kitchen. They cook in two or three
languages. They are slightly nostalgic about printed cookbooks but live
mostly on their phone. The site has to feel personal, not enterprise.

## Key decisions

**Type pairing.** *Fraunces* (variable, opsz) for display — a quirky modern
serif with optical sizing that lets the recipe titles feel almost
hand-lettered, while ingredient lists stay clean. *Inter* for UI/body — a
quiet workhorse that disappears next to the personality of Fraunces. *JetBrains
Mono* for codes/access keys.

**Color logic.** Warm parchment (`#FBF4E4`) base in light mode and a deep
inky raisin (`#1F1A1D`) in dark. Saffron (`#E8A33D`) is the hero accent —
used for the primary action ("Display"), the brand, and link
underlines. A second mineral teal (`#2C6E6A`) plays support and powers
links to recipe content; a soft persimmon (`#C0533B`) carries warnings and
delete. The trick is *low-saturation surfaces, saturated accents* — the page
itself never shouts.

**Layout philosophy.** A two-column desktop layout for recipe pages
(ingredients in a sidebar with a textured panel background, steps as a
generous reading column) — the way a chef glances at the pantry list and
then keeps eyes on the method. Library is still a single column of cards,
but cards get a tiny accent rule and a stamp-like "on display" badge. The
status page surfaces the device preview at the top with a "Polaroid"-style
mounted frame.

**Polish moves.** Soft 14px radii throughout; a subtle hand-drawn pepper
illustration as the logo; little kitchen affordances (an underline that
looks pencilled, a stamped "currently on display" chip, a numbered list
with circular saffron tokens).
