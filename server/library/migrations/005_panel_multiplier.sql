-- Remember the batch multiplier ("×2") a recipe without a serving count
-- was pushed at, so a restart re-renders the panel at the same scale.
-- NULL = as written. Sits beside 004's `servings`; at most one is set.

ALTER TABLE display_panel ADD COLUMN multiplier REAL;
